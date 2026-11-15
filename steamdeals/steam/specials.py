"""Steam store "specials" (discounted items) via the search results endpoint.

The endpoint is undocumented and returns an HTML fragment inside JSON, so the
parser is deliberately defensive: rows that cannot be parsed are dropped and
logged, and a page that contains no rows although ``total_count`` says there
should be some raises :class:`SteamSchemaError`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from html.parser import HTMLParser

from .http import SteamHttp, SteamRateLimited, SteamSchemaError
from .money import parse_price_text

log = logging.getLogger(__name__)

SEARCH_URL = "https://store.steampowered.com/search/results/"
PAGE_SIZE = 100
# Pause between pages. Validated live: ~1.6 s/page triggered HTTP 429 after ~26 pages;
# 3 s/page completed all 67 pages (~4 min) without a single 429.
PAGE_DELAY_SECONDS = 3.0
# If a page is still rate limited after the HTTP client's own retries, wait this
# long (the live limiter has no Retry-After header) and retry the page.
RATE_LIMIT_PAUSE_SECONDS = 60.0
RATE_LIMIT_PAGE_RETRIES = 2
# A full pass that parses fewer than this fraction of total_count is treated as a failure.
MIN_PARSED_RATIO = 0.9
# category1=998 restricts the search to "Games" (base games), excluding DLC,
# software, soundtracks, etc. Validated against the live endpoint.
GAMES_CATEGORY = "998"


@dataclass(frozen=True)
class SpecialItem:
    appid: int
    title: str
    final_cents: int
    original_cents: int
    discount_pct: int
    image_url: str | None = None


@dataclass(frozen=True)
class SpecialsPage:
    items: list[SpecialItem]
    total_count: int
    rows_seen: int
    skipped: int


class _RowParser(HTMLParser):
    """Collects ``a.search_result_row`` rows from the results fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._capture: str | None = None
        self._buffer: list[str] = []

    @staticmethod
    def _classes(attrs: dict) -> set[str]:
        return set((attrs.get("class") or "").split())

    def handle_starttag(self, tag, attrs_list):
        attrs = {k: (v or "") for k, v in attrs_list}
        classes = self._classes(attrs)
        if tag == "a" and "search_result_row" in classes:
            self._row = {"attrs": attrs, "title": None, "original": None, "block": None, "image": None}
            return
        row = self._row
        if row is None:
            return
        if tag == "span" and "title" in classes and row["title"] is None:
            self._start("title")
        elif tag == "div" and "discount_original_price" in classes and row["original"] is None:
            self._start("original")
        elif tag == "div" and "discount_block" in classes and row["block"] is None:
            row["block"] = attrs
        elif tag == "img" and row["image"] is None and attrs.get("src"):
            row["image"] = attrs["src"]

    def _start(self, name: str) -> None:
        self._capture = name
        self._buffer = []

    def handle_data(self, data):
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if self._capture and tag in ("span", "div") and self._row is not None:
            self._row[self._capture] = "".join(self._buffer).strip()
            self._capture = None
            self._buffer = []
        elif tag == "a" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._capture = None


def _to_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except ValueError:
        return None


def parse_results_html(html: str) -> tuple[list[SpecialItem], int, int]:
    """Parse a results fragment. Returns ``(items, rows_seen, skipped)``."""
    parser = _RowParser()
    parser.feed(html)
    parser.close()
    items: list[SpecialItem] = []
    skipped = 0
    for row in parser.rows:
        attrs = row["attrs"]
        appid_raw = attrs.get("data-ds-appid", "")
        if "data-ds-bundleid" in attrs or "data-ds-packageid" in attrs or not appid_raw.isdigit():
            skipped += 1  # bundles/packages (or rows without a single appid)
            continue
        appid = int(appid_raw)
        block = row["block"] or {}
        final = _to_int(block.get("data-price-final"))
        discount = _to_int(block.get("data-discount"))
        original = parse_price_text(row["original"])
        title = (row["title"] or "").strip()
        if (
            not title
            or final is None
            or original is None
            or discount is None
            or not 0 < discount <= 100
            or final < 0
            or final >= original
        ):
            log.info(
                "Discarding inconsistent specials row appid=%s final=%s original=%s discount=%s",
                appid, final, original, discount,
            )
            skipped += 1
            continue
        items.append(SpecialItem(appid, title, final, original, discount, row["image"]))
    return items, len(parser.rows), skipped


class SpecialsClient:
    def __init__(
        self,
        http: SteamHttp,
        *,
        country: str = "br",
        language: str = "portuguese",
        include_non_games: bool = False,
        page_size: int = PAGE_SIZE,
        page_delay: float = PAGE_DELAY_SECONDS,
        rate_limit_pause: float = RATE_LIMIT_PAUSE_SECONDS,
        rate_limit_page_retries: int = RATE_LIMIT_PAGE_RETRIES,
        sleep=asyncio.sleep,
    ) -> None:
        self._http = http
        self._country = country
        self._language = language
        self._include_non_games = include_non_games
        self._page_size = page_size
        self._page_delay = page_delay
        self._rate_limit_pause = rate_limit_pause
        self._rate_limit_retries = rate_limit_page_retries
        self._sleep = sleep

    def _params(self, start: int) -> dict[str, str]:
        params = {
            "query": "",
            "start": str(start),
            "count": str(self._page_size),
            "specials": "1",
            "cc": self._country,
            "l": self._language,
            "infinite": "1",
            "json": "1",
            # Deterministic order. With the default (relevance) order pagination is
            # unstable: validated live, 210 duplicate rows and ~210 apps missing per
            # full pass; with Name_ASC there were 0 duplicates and nothing missing.
            "sort_by": "Name_ASC",
        }
        if not self._include_non_games:
            params["category1"] = GAMES_CATEGORY
        return params

    async def fetch_page(self, start: int = 0) -> SpecialsPage:
        payload = await self._http.get_json(SEARCH_URL, self._params(start))
        if not isinstance(payload, dict) or "results_html" not in payload or "total_count" not in payload:
            raise SteamSchemaError("Specials response is missing results_html/total_count")
        html = payload["results_html"]
        try:
            total = int(payload["total_count"])
        except (TypeError, ValueError) as exc:
            raise SteamSchemaError("Specials total_count is not an integer") from exc
        if not isinstance(html, str):
            raise SteamSchemaError("Specials results_html is not a string")
        items, rows_seen, skipped = parse_results_html(html)
        if rows_seen == 0 and total > start:
            raise SteamSchemaError(
                f"Specials page start={start} had no result rows but total_count={total}; "
                "the HTML layout may have changed"
            )
        return SpecialsPage(items, total, rows_seen, skipped)

    async def _fetch_page_throttled(self, start: int) -> SpecialsPage:
        for attempt in range(self._rate_limit_retries + 1):
            try:
                return await self.fetch_page(start)
            except SteamRateLimited:
                if attempt >= self._rate_limit_retries:
                    raise
                log.warning(
                    "Specials page start=%d rate limited; pausing %.0fs (retry %d)",
                    start, self._rate_limit_pause, attempt + 1,
                )
                await self._sleep(self._rate_limit_pause)
        raise AssertionError("unreachable")  # pragma: no cover

    async def fetch_all(self) -> list[SpecialItem]:
        """Fetch every page. Any failure aborts the whole fetch (no partial data)."""
        result: dict[int, SpecialItem] = {}
        start = 0
        total = 1
        while start < total:
            if start:
                await self._sleep(self._page_delay)
            page = await self._fetch_page_throttled(start)
            total = page.total_count
            for item in page.items:
                result[item.appid] = item
            start += self._page_size
        if not result:
            raise SteamSchemaError("Specials listing is empty")
        if len(result) < total * MIN_PARSED_RATIO:
            raise SteamSchemaError(
                f"Only {len(result)} specials parsed out of total_count={total}; "
                "the listing is incomplete or the layout changed"
            )
        log.info("Fetched %d specials (total_count=%d)", len(result), total)
        return list(result.values())
