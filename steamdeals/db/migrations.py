"""Versioned, idempotent SQLite migrations.

Each migration runs inside a single transaction together with its
``schema_version`` row, so a failed migration leaves the database untouched.
Statements use ``IF NOT EXISTS`` so re-running a script is harmless.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Awaitable, Callable

import aiosqlite

log = logging.getLogger(__name__)

# A migration is either an SQL script or an async function that receives the
# connection while the migration transaction is already open.
Migration = str | Callable[[aiosqlite.Connection], Awaitable[None]]


async def _migrate_signatures_without_end_date(conn: aiosqlite.Connection) -> None:
    """Rewrite ``deal_state.last_signature`` of games on sale to the signature that
    excludes ``discount_end`` (final, original, discount, sale epoch).

    Without this, every game on sale would look like a new promotion after the
    upgrade and be announced once more. The hash below is a frozen copy of the
    signature format at the time of this migration; it must not follow later
    changes of the application code. Notification rows keep their old signature
    (they are history; only the comparison against ``deal_state`` matters).
    """
    async with conn.execute(
        "SELECT appid, final_cents, original_cents, discount_pct, sale_epoch FROM deal_state "
        "WHERE on_sale = 1 AND last_signature IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    updates = []
    for appid, final, original, pct, epoch in rows:
        raw = f"{final}|{original}|{pct}"
        if epoch:
            raw += f"|e{epoch}"
        updates.append((hashlib.sha256(raw.encode()).hexdigest()[:16], appid))
    await conn.executemany("UPDATE deal_state SET last_signature = ? WHERE appid = ?", updates)


MIGRATIONS: list[tuple[int, Migration]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS users (
            discord_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS steam_links (
            id INTEGER PRIMARY KEY,
            discord_id INTEGER NOT NULL UNIQUE REFERENCES users(discord_id) ON DELETE CASCADE,
            steam_id64 TEXT NOT NULL,
            linked_at TEXT NOT NULL,
            last_import_at TEXT,
            last_import_status TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_steam_links_steam_id ON steam_links(steam_id64);

        CREATE TABLE IF NOT EXISTS watched_games (
            appid INTEGER PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            last_seen TEXT
        );

        CREATE TABLE IF NOT EXISTS game_sources (
            discord_id INTEGER NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
            appid INTEGER NOT NULL REFERENCES watched_games(appid) ON DELETE CASCADE,
            source TEXT NOT NULL CHECK (source IN ('wishlist', 'manual')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (discord_id, appid, source)
        );
        CREATE INDEX IF NOT EXISTS idx_game_sources_appid ON game_sources(appid);

        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY,
            appid INTEGER NOT NULL,
            final_cents INTEGER,
            original_cents INTEGER,
            discount_pct INTEGER,
            discount_end INTEGER,
            observed_at TEXT NOT NULL,
            hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_price_history_appid ON price_history(appid, id);

        CREATE TABLE IF NOT EXISTS deal_state (
            appid INTEGER PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            last_signature TEXT,
            baseline INTEGER NOT NULL DEFAULT 0,
            sale_epoch INTEGER NOT NULL DEFAULT 0,
            state_hash TEXT,
            on_sale INTEGER NOT NULL DEFAULT 0,
            in_specials INTEGER NOT NULL DEFAULT 0,
            final_cents INTEGER,
            original_cents INTEGER,
            discount_pct INTEGER,
            discount_end INTEGER,
            formatted_final TEXT,
            formatted_original TEXT,
            image_url TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deal_state_sale ON deal_state(on_sale, discount_pct);

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY,
            appid INTEGER NOT NULL,
            signature TEXT NOT NULL,
            origins TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'superseded')),
            created_at TEXT NOT NULL,
            sent_at TEXT,
            discord_message_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            UNIQUE (appid, signature)
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status, id);
        CREATE INDEX IF NOT EXISTS idx_notifications_appid ON notifications(appid, status);

        CREATE TABLE IF NOT EXISTS check_state (
            source TEXT PRIMARY KEY,
            last_started TEXT,
            last_success TEXT,
            last_status TEXT,
            last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """,
    ),
    (
        2,
        """
        DROP TABLE IF EXISTS notifications_v2;
        CREATE TABLE notifications_v2 (
            id INTEGER PRIMARY KEY,
            appid INTEGER NOT NULL,
            signature TEXT NOT NULL,
            origins TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'superseded', 'expired', 'failed')),
            created_at TEXT NOT NULL,
            sent_at TEXT,
            discord_message_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            UNIQUE (appid, signature)
        );
        INSERT INTO notifications_v2 (id, appid, signature, origins, payload, status, created_at,
                                      sent_at, discord_message_id, attempts, last_error)
            SELECT id, appid, signature, origins, payload, status, created_at,
                   sent_at, discord_message_id, attempts, last_error FROM notifications;
        DROP TABLE notifications;
        ALTER TABLE notifications_v2 RENAME TO notifications;
        CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status, id);
        CREATE INDEX IF NOT EXISTS idx_notifications_appid ON notifications(appid, status);

        ALTER TABLE deal_state ADD COLUMN missing_cycles INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (3, _migrate_signatures_without_end_date),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS game_reviews (
            appid INTEGER PRIMARY KEY,
            review_count INTEGER NOT NULL CHECK (review_count >= 0),
            percent_positive INTEGER NOT NULL CHECK (percent_positive BETWEEN 0 AND 100),
            source TEXT NOT NULL CHECK (source IN ('browse', 'search', 'search_partial')),
            updated_at INTEGER NOT NULL
        );
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]


async def current_version(conn: aiosqlite.Connection) -> int:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    async with conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version") as cur:
        row = await cur.fetchone()
    return int(row[0])


async def migrate(conn: aiosqlite.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version.

    The connection must use autocommit mode (``isolation_level=None``).
    """
    version = await current_version(conn)
    for number, script in MIGRATIONS:
        if number <= version:
            continue
        log.info("Applying database migration %d", number)
        try:
            if isinstance(script, str):
                await conn.executescript(
                    f"BEGIN IMMEDIATE;\n{script}\n"
                    f"INSERT INTO schema_version (version) VALUES ({int(number)});\nCOMMIT;"
                )
            else:
                await conn.execute("BEGIN IMMEDIATE")
                await script(conn)
                await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (int(number),))
                await conn.execute("COMMIT")
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        version = number
    return version
