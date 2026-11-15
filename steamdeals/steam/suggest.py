"""Store search suggestions used by slash command autocomplete."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable

from .http import SteamHttp

log = logging.getLogger(__name__)

SUGGEST_URL = "https://store.steampowered.com/search/suggest"


@dataclass(frozen=True)
class SuggestItem:
    appid: int
    name: str
    price: str | None = None


class _SuggestParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[SuggestItem] = []
        self._cur: dict | None = None
        self._capture: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs_list):
        attrs = {k: (v or "") for k, v in attrs_list}
        classes = set(attrs.get("class", "").split())
        if tag == "a" and "match" in classes:
            self._cur = {"appid": attrs.get("data-ds-appid", ""), "name": None, "price": None}
        elif self._cur is not None and tag == "div":
            if "match_name" in classes:
                self._capture, self._buf = "name", []
            elif "match_price" in classes:
                self._capture, self._buf = "price", []

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._capture and tag == "div" and self._cur is not None:
            self._cur[self._capture] = "".join(self._buf).strip()
            self._capture = None
        elif tag == "a" and self._cur is not None:
            cur, self._cur = self._cur, None
            if cur["appid"].isdigit() and cur["name"]:
                self.items.append(SuggestItem(int(cur["appid"]), cur["name"], cur["price"] or None))


def parse_suggestions(html: str) -> list[SuggestItem]:
    parser = _SuggestParser()
    parser.feed(html)
    parser.close()
    return parser.items


class SuggestClient:
    """Autocomplete backend with a small in-memory TTL cache.

    The cache doubles as debounce: repeated keystrokes for the same term (and
    several users typing the same term) do not hit Steam again within the TTL.
    """

    def __init__(
        self,
        http: SteamHttp,
        *,
        country: str = "br",
        language: str = "portuguese",
        ttl: float = 60.0,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._country = country
        self._language = language
        self._ttl = ttl
        self._max_entries = max_entries
        self._clock = clock
        self._cache: dict[str, tuple[float, list[SuggestItem]]] = {}

    async def search(self, term: str) -> list[SuggestItem]:
        term = (term or "").strip().lower()
        if len(term) < 2:
            return []
        now = self._clock()
        cached = self._cache.get(term)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        try:
            html = await self._http.get_text(
                SUGGEST_URL,
                {"term": term, "f": "games", "cc": self._country, "l": self._language},
            )
            items = parse_suggestions(html)
        except Exception as exc:  # autocomplete must never raise
            log.warning("Suggest lookup failed: %s", type(exc).__name__)
            return []
        if len(self._cache) >= self._max_entries:
            self._cache.pop(next(iter(self._cache)))
        self._cache[term] = (now, items)
        return items
