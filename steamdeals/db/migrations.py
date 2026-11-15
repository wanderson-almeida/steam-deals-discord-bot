"""Versioned, idempotent SQLite migrations.

Each migration runs inside a single transaction together with its
``schema_version`` row, so a failed migration leaves the database untouched.
Statements use ``IF NOT EXISTS`` so re-running a script is harmless.
"""

from __future__ import annotations

import logging

import aiosqlite

log = logging.getLogger(__name__)

MIGRATIONS: list[tuple[int, str]] = [
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
            await conn.executescript(
                f"BEGIN IMMEDIATE;\n{script}\n"
                f"INSERT INTO schema_version (version) VALUES ({int(number)});\nCOMMIT;"
            )
        except Exception:
            try:
                await conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        version = number
    return version
