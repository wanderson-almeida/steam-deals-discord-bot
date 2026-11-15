"""Shared fixtures and fakes. No test touches the network."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from steamdeals.db.database import Database  # noqa: E402
from steamdeals.db.repositories import Repository  # noqa: E402
from steamdeals.monitor.service import MonitorService  # noqa: E402
from steamdeals.steam.http import SteamError  # noqa: E402
from steamdeals.steam.specials import SpecialItem  # noqa: E402
from steamdeals.steam.store import StoreItem  # noqa: E402
from steamdeals.steam.wishlist import WishlistResult  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "", None}


def _is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    return host in _LOCAL_HOSTS


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Fail loudly if any test tries to reach a non-loopback host."""
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_local(host):
            raise RuntimeError(f"network access blocked in tests (resolve {host!r})")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self, address):
        if isinstance(address, tuple) and not _is_local(address[0]):
            raise RuntimeError(f"network access blocked in tests (connect {address[0]!r})")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json(name: str):
    return json.loads(load_text(name))


# ------------------------------------------------------------------ HTTP fakes
class FakeHttp:
    """Stands in for SteamHttp. ``handler(url, params)`` returns text or raises."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    async def get_text(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        result = self.handler(url, dict(params or {}))
        if isinstance(result, BaseException):
            raise result
        return result

    async def get_json(self, url, params=None):
        return json.loads(await self.get_text(url, params))


# ------------------------------------------------------------ service-level fakes
class FakeSpecials:
    def __init__(self, items=None, error: Exception | None = None):
        self.items = items if items is not None else []
        self.error = error
        self.calls = 0
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def fetch_all(self):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise self.error
        return list(self.items)


class FakeStore:
    def __init__(self, items=None):
        self.items: dict[int, StoreItem] = {i.appid: i for i in (items or [])}
        self.failed: set[int] = set()
        self.error: Exception | None = None
        self.requested: list[list[int]] = []

    async def get_items(self, appids):
        ids = sorted(set(appids))
        self.requested.append(ids)
        if self.error:
            raise self.error
        return {a: self.items[a] for a in ids if a in self.items and a not in self.failed}, set(
            a for a in ids if a in self.failed
        )

    async def get_item(self, appid):
        if self.error:
            raise self.error
        return self.items.get(appid)


class FakeWishlist:
    def __init__(self):
        self.data: dict[str, WishlistResult | Exception] = {}
        self.calls: list[str] = []

    def set(self, steam_id: str, appids, empty_or_private=False):
        self.data[steam_id] = WishlistResult(sorted(appids), empty_or_private)

    async def fetch(self, steam_id):
        self.calls.append(steam_id)
        value = self.data[steam_id]
        if isinstance(value, Exception):
            raise value
        return value


# ------------------------------------------------------------------ builders
def special(appid: int, final: int = 1000, original: int = 2000, pct: int = 50, title: str | None = None):
    return SpecialItem(appid, title or f"Game {appid}", final, original, pct, None)


def store_item(
    appid: int,
    final: int | None = 1000,
    original: int | None = 2000,
    pct: int = 50,
    end: int | None = 1800000000,
    name: str | None = None,
):
    return StoreItem(
        appid=appid,
        name=f"Game {appid}" if name is None else name,
        type=0,
        final_cents=final,
        original_cents=original,
        discount_pct=pct,
        discount_end=end,
    )


# ------------------------------------------------------------------- fixtures
@pytest_asyncio.fixture
async def db(tmp_path):
    database = await Database(str(tmp_path / "test.db")).connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def repo(db):
    return Repository(db)


class Env:
    def __init__(self, repo):
        self.repo = repo
        self.specials = FakeSpecials()
        self.store = FakeStore()
        self.wishlist = FakeWishlist()
        self.monitor = MonitorService(repo, self.specials, self.store, self.wishlist, max_concurrency=2)

    async def cycle(self):
        return await self.monitor.run_cycle()

    async def pending(self):
        return await self.repo.pending_notifications(1000)


@pytest_asyncio.fixture
async def env(repo):
    return Env(repo)


__all__ = ["SteamError"]
