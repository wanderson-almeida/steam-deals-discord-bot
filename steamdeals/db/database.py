"""SQLite connection management (aiosqlite)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from .migrations import migrate

log = logging.getLogger(__name__)


class Database:
    """A single aiosqlite connection guarded by an asyncio lock.

    All access goes through :meth:`read` / :meth:`transaction` so concurrent
    coroutines (monitor cycle, slash commands) never interleave statements of
    different logical operations.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> "Database":
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        self._conn = conn
        await migrate(conn)
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            yield self.conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            conn = self.conn
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            else:
                try:
                    await conn.execute("COMMIT")
                except BaseException:
                    try:
                        await conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
