"""Public Steam wishlist import (IWishlistService/GetWishlist)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .http import SteamHttp, SteamSchemaError

WISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"

_STEAM_ID64 = re.compile(r"^7656119\d{10}$")
_PROFILE_URL = re.compile(r"^https?://(?:www\.)?steamcommunity\.com/profiles/(\d+)(?:[/?#].*)?$", re.I)
_VANITY_URL = re.compile(r"^https?://(?:www\.)?steamcommunity\.com/id/", re.I)


class InvalidSteamId(ValueError):
    """The text is not a usable SteamID64. ``kind`` is 'vanity' or 'invalid'."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def parse_steam_id(text: str) -> str:
    """Return a validated SteamID64 from a raw ID or a /profiles/<id> URL."""
    value = (text or "").strip()
    if _VANITY_URL.match(value):
        raise InvalidSteamId("vanity")
    url = _PROFILE_URL.match(value)
    if url:
        value = url.group(1)
    if not _STEAM_ID64.match(value):
        raise InvalidSteamId("invalid")
    return value


@dataclass(frozen=True)
class WishlistResult:
    appids: list[int]
    # True when Steam answered ``{"response":{}}``: the wishlist is either
    # private or empty and the two cases cannot be told apart.
    empty_or_private: bool


def parse_wishlist(payload: Any) -> WishlistResult:
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        raise SteamSchemaError("GetWishlist response has no 'response' object")
    response = payload["response"]
    if "items" not in response:
        return WishlistResult([], True)
    items = response["items"]
    if not isinstance(items, list):
        raise SteamSchemaError("GetWishlist 'items' is not a list")
    appids: list[int] = []
    for item in items:
        appid = item.get("appid") if isinstance(item, dict) else None
        if isinstance(appid, int) and not isinstance(appid, bool) and appid > 0:
            appids.append(appid)
    return WishlistResult(sorted(set(appids)), len(appids) == 0)


class WishlistClient:
    def __init__(self, http: SteamHttp) -> None:
        self._http = http

    async def fetch(self, steam_id: str) -> WishlistResult:
        payload = await self._http.get_json(WISHLIST_URL, {"steamid": steam_id})
        return parse_wishlist(payload)
