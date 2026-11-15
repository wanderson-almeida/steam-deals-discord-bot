"""Steam price data for specific AppIDs (IStoreBrowseService/GetItems)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from .http import SteamError, SteamHttp, SteamRateLimited, SteamSchemaError

log = logging.getLogger(__name__)

GET_ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
# A batch of 500 returns HTTP 400. 200 worked in manual tests and POST is not
# allowed (405); 100 is kept as a conservative, documented batch size.
BATCH_SIZE = 100
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"


def header_image_url(appid: int) -> str:
    return f"{ASSET_BASE}steam/apps/{appid}/header.jpg"


@dataclass(frozen=True)
class StoreItem:
    appid: int
    name: str
    type: int | None = None
    final_cents: int | None = None
    original_cents: int | None = None
    discount_pct: int = 0
    discount_end: int | None = None
    formatted_final: str | None = None
    formatted_original: str | None = None
    image_url: str | None = None
    is_free: bool = False

    @property
    def has_price(self) -> bool:
        return self.final_cents is not None

    @property
    def on_sale(self) -> bool:
        """True only for a coherent discount (original > final >= 0, pct in 1..100)."""
        return (
            self.final_cents is not None
            and self.original_cents is not None
            and 0 < self.discount_pct <= 100
            and 0 <= self.final_cents < self.original_cents
        )


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _image_from(entry: dict, appid: int) -> str:
    assets = entry.get("assets")
    if isinstance(assets, dict):
        fmt, header = assets.get("asset_url_format"), assets.get("header")
        if isinstance(fmt, str) and isinstance(header, str):
            return ASSET_BASE + fmt.replace("${FILENAME}", header)
    return header_image_url(appid)


def parse_store_item(entry: dict) -> StoreItem | None:
    """Convert one ``store_items`` entry. Returns None for unusable entries."""
    appid = _to_int(entry.get("appid", entry.get("id")))
    name = entry.get("name")
    if appid is None or entry.get("success", 1) != 1:
        return None
    option = entry.get("best_purchase_option")
    if not isinstance(name, str) or not name:
        if not isinstance(option, dict):
            return None
        name = ""  # priced item without a name: the caller falls back to the stored name
    final = original = end = formatted_final = formatted_original = None
    discount = 0
    if isinstance(option, dict):
        final = _to_int(option.get("final_price_in_cents"))
        original = _to_int(option.get("original_price_in_cents"))
        discount = _to_int(option.get("discount_pct")) or 0
        formatted_final = option.get("formatted_final_price")
        formatted_original = option.get("formatted_original_price")
        discounts = option.get("active_discounts")
        if isinstance(discounts, list) and discounts and isinstance(discounts[0], dict):
            end = _to_int(discounts[0].get("discount_end_date"))
        if final is not None and original is None:
            original = final  # no discount: Steam omits the original price
    return StoreItem(
        appid=appid,
        name=name,
        type=_to_int(entry.get("type")),
        final_cents=final,
        original_cents=original,
        discount_pct=discount,
        discount_end=end,
        formatted_final=formatted_final if isinstance(formatted_final, str) else None,
        formatted_original=formatted_original if isinstance(formatted_original, str) else None,
        image_url=_image_from(entry, appid),
        is_free=bool(entry.get("is_free")),
    )


def parse_get_items(payload: Any) -> dict[int, StoreItem]:
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        raise SteamSchemaError("GetItems response has no 'response' object")
    entries = payload["response"].get("store_items", [])
    if not isinstance(entries, list):
        raise SteamSchemaError("GetItems 'store_items' is not a list")
    items: dict[int, StoreItem] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item = parse_store_item(entry)
        if item is not None:
            items[item.appid] = item
    return items


def parse_app_details(payload: Any, appid: int) -> StoreItem | None:
    """Parse ``appdetails?filters=price_overview`` (fallback source, no name)."""
    if not isinstance(payload, dict):
        raise SteamSchemaError("appdetails response is not an object")
    entry = payload.get(str(appid))
    if not isinstance(entry, dict) or not entry.get("success"):
        return None
    data = entry.get("data")
    overview = data.get("price_overview") if isinstance(data, dict) else None
    if not isinstance(overview, dict):
        return StoreItem(appid=appid, name="", image_url=header_image_url(appid))
    final = _to_int(overview.get("final"))
    original = _to_int(overview.get("initial"))
    return StoreItem(
        appid=appid,
        name="",
        final_cents=final,
        original_cents=original if original is not None else final,
        discount_pct=_to_int(overview.get("discount_percent")) or 0,
        formatted_final=overview.get("final_formatted"),
        formatted_original=overview.get("initial_formatted"),
        image_url=header_image_url(appid),
    )


def chunked(values: list[int], size: int) -> Iterable[list[int]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


class StoreClient:
    def __init__(
        self,
        http: SteamHttp,
        *,
        country_code: str = "BR",
        language: str = "brazilian",
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._http = http
        self._country_code = country_code
        self._language = language
        self._batch_size = batch_size

    def _input_json(self, appids: list[int]) -> str:
        return json.dumps(
            {
                "ids": [{"appid": a} for a in appids],
                "context": {
                    "language": self._language,
                    "country_code": self._country_code,
                    "steam_realm": 1,
                },
                "data_request": {"include_basic_info": True},
            },
            separators=(",", ":"),
        )

    async def _fetch_batch(self, appids: list[int]) -> dict[int, StoreItem]:
        payload = await self._http.get_json(GET_ITEMS_URL, {"input_json": self._input_json(appids)})
        return parse_get_items(payload)

    async def _fallback(self, appid: int) -> StoreItem | None:
        params = {
            "appids": str(appid),
            "cc": self._country_code.lower(),
            "l": "portuguese",
            "filters": "price_overview",
        }
        payload = await self._http.get_json(APPDETAILS_URL, params)
        return parse_app_details(payload, appid)

    async def get_item(self, appid: int) -> StoreItem | None:
        items, failed = await self.get_items([appid])
        if appid in failed:
            raise SteamError(f"Could not fetch app {appid}")
        return items.get(appid)

    async def get_items(self, appids: Iterable[int]) -> tuple[dict[int, StoreItem], set[int]]:
        """Fetch many apps. Returns ``(items, failed_appids)``.

        AppIDs that Steam does not know are simply absent from both results.
        A failed batch falls back to ``appdetails`` per app (unless Steam is
        rate limiting), and AppIDs that still fail end up in ``failed``.
        """
        unique = sorted({int(a) for a in appids})
        items: dict[int, StoreItem] = {}
        failed: set[int] = set()
        batches = list(chunked(unique, self._batch_size))
        results = await asyncio.gather(*(self._fetch_batch(b) for b in batches), return_exceptions=True)
        for batch, result in zip(batches, results):
            if isinstance(result, dict):
                items.update(result)
                continue
            if not isinstance(result, Exception):
                raise result  # CancelledError and other BaseExceptions
            log.warning("GetItems batch of %d failed (%s); using fallback", len(batch), type(result).__name__)
            if isinstance(result, SteamRateLimited):
                failed.update(batch)
                continue
            fallback = await asyncio.gather(*(self._fallback(a) for a in batch), return_exceptions=True)
            for appid, fb in zip(batch, fallback):
                if isinstance(fb, StoreItem):
                    items[appid] = fb
                elif isinstance(fb, Exception):
                    failed.add(appid)
        return items, failed
