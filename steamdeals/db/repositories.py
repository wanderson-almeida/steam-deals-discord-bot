"""Data access layer. Every public method is one atomic operation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable, Iterable

from .. import ranking
from ..models import CycleChanges, DealSnapshot, NewNotification
from .database import Database

BASELINE_SETTING = "specials_baseline_done"
MAX_SEND_ATTEMPTS = 5
# A 'browse' (GetItems) review summary is only replaced by a 'search' one after this long.
BROWSE_REVIEW_TTL_SECONDS = 7 * 24 * 3600


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Repository:
    def __init__(self, db: Database, clock: Callable[[], datetime] = utc_now) -> None:
        self.db = db
        self._clock = clock

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ users
    async def ensure_user(self, discord_id: int) -> None:
        async with self.db.transaction() as c:
            await self._ensure_user(c, discord_id)

    async def _ensure_user(self, c, discord_id: int) -> None:
        await c.execute(
            "INSERT OR IGNORE INTO users (discord_id, created_at) VALUES (?, ?)",
            (discord_id, self._now()),
        )

    async def set_steam_link(self, discord_id: int, steam_id64: str) -> None:
        async with self.db.transaction() as c:
            await self._ensure_user(c, discord_id)
            async with c.execute(
                "SELECT steam_id64 FROM steam_links WHERE discord_id = ?", (discord_id,)
            ) as cur:
                old = await cur.fetchone()
            if old is not None and old["steam_id64"] != steam_id64:
                # A different account: wishlist games of the old account must go.
                await self._drop_wishlist_sources(c, discord_id)
            await c.execute(
                "INSERT INTO steam_links (discord_id, steam_id64, linked_at) VALUES (?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET steam_id64 = excluded.steam_id64, "
                "linked_at = excluded.linked_at, last_import_at = NULL, last_import_status = NULL",
                (discord_id, steam_id64, self._now()),
            )

    async def get_steam_link(self, discord_id: int) -> dict | None:
        async with self.db.read() as c:
            async with c.execute("SELECT * FROM steam_links WHERE discord_id = ?", (discord_id,)) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def remove_steam_link(self, discord_id: int) -> tuple[bool, int]:
        """Remove the link and the user's 'wishlist' sources (manual ones stay)."""
        async with self.db.transaction() as c:
            cur = await c.execute("DELETE FROM steam_links WHERE discord_id = ?", (discord_id,))
            had_link = cur.rowcount > 0
            removed = await self._drop_wishlist_sources(c, discord_id)
            return had_link, removed

    async def _drop_wishlist_sources(self, c, discord_id: int) -> int:
        async with c.execute(
            "SELECT appid FROM game_sources WHERE discord_id = ? AND source = 'wishlist'", (discord_id,)
        ) as cur:
            appids = [r["appid"] for r in await cur.fetchall()]
        await c.execute(
            "DELETE FROM game_sources WHERE discord_id = ? AND source = 'wishlist'", (discord_id,)
        )
        await self._reset_unmonitored(c, appids)
        return len(appids)

    async def _reset_unmonitored(self, c, appids) -> None:
        """Games nobody monitors and the specials listing does not carry are not on sale anymore."""
        await c.executemany(
            "UPDATE deal_state SET on_sale = 0, missing_cycles = 0 WHERE appid = ? AND in_specials = 0 "
            "AND on_sale = 1 AND appid NOT IN (SELECT appid FROM game_sources)",
            [(a,) for a in appids],
        )

    async def links_by_steam_id(self) -> dict[str, list[int]]:
        async with self.db.read() as c:
            async with c.execute("SELECT steam_id64, discord_id FROM steam_links ORDER BY id") as cur:
                rows = await cur.fetchall()
        result: dict[str, list[int]] = {}
        for row in rows:
            result.setdefault(row["steam_id64"], []).append(row["discord_id"])
        return result

    async def set_import_status(self, discord_id: int, status: str) -> None:
        async with self.db.transaction() as c:
            await c.execute(
                "UPDATE steam_links SET last_import_at = ?, last_import_status = ? WHERE discord_id = ?",
                (self._now(), status, discord_id),
            )

    # --------------------------------------------------------------- sources
    async def sync_wishlist_sources(
        self, discord_id: int, appids: Iterable[int], expected_steam_id: str | None = None
    ) -> tuple[int, int] | None:
        """Make the user's 'wishlist' sources equal ``appids``. Returns (added, removed).

        When ``expected_steam_id`` is given, the user's link must still exist with that
        SteamID64 in this same transaction; otherwise nothing is written and None is
        returned (the user unlinked or relinked while the wishlist was being fetched).
        """
        wanted = set(appids)
        async with self.db.transaction() as c:
            if expected_steam_id is not None:
                async with c.execute(
                    "SELECT steam_id64 FROM steam_links WHERE discord_id = ?", (discord_id,)
                ) as cur:
                    link = await cur.fetchone()
                if link is None or link["steam_id64"] != expected_steam_id:
                    return None
            await self._ensure_user(c, discord_id)
            async with c.execute(
                "SELECT appid FROM game_sources WHERE discord_id = ? AND source = 'wishlist'",
                (discord_id,),
            ) as cur:
                existing = {r["appid"] for r in await cur.fetchall()}
            to_add = sorted(wanted - existing)
            to_remove = sorted(existing - wanted)
            now = self._now()
            await c.executemany(
                "INSERT OR IGNORE INTO watched_games (appid, name, last_seen) VALUES (?, '', ?)",
                [(a, now) for a in to_add],
            )
            await c.executemany(
                "INSERT OR IGNORE INTO game_sources (discord_id, appid, source, created_at) "
                "VALUES (?, ?, 'wishlist', ?)",
                [(discord_id, a, now) for a in to_add],
            )
            await c.executemany(
                "DELETE FROM game_sources WHERE discord_id = ? AND appid = ? AND source = 'wishlist'",
                [(discord_id, a) for a in to_remove],
            )
            await self._reset_unmonitored(c, to_remove)
            return len(to_add), len(to_remove)

    async def add_manual(self, discord_id: int, appid: int, name: str) -> bool:
        """Returns False when the game was already in the user's manual list."""
        async with self.db.transaction() as c:
            await self._ensure_user(c, discord_id)
            now = self._now()
            await c.execute(
                "INSERT INTO watched_games (appid, name, last_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(appid) DO UPDATE SET name = CASE WHEN excluded.name <> '' "
                "THEN excluded.name ELSE watched_games.name END",
                (appid, name, now),
            )
            cur = await c.execute(
                "INSERT OR IGNORE INTO game_sources (discord_id, appid, source, created_at) "
                "VALUES (?, ?, 'manual', ?)",
                (discord_id, appid, now),
            )
            return cur.rowcount > 0

    async def remove_manual(self, discord_id: int, appid: int) -> bool:
        async with self.db.transaction() as c:
            cur = await c.execute(
                "DELETE FROM game_sources WHERE discord_id = ? AND appid = ? AND source = 'manual'",
                (discord_id, appid),
            )
            removed = cur.rowcount > 0
            if removed:
                await self._reset_unmonitored(c, [appid])
            return removed

    async def manual_count(self, discord_id: int) -> int:
        async with self.db.read() as c:
            async with c.execute(
                "SELECT COUNT(*) FROM game_sources WHERE discord_id = ? AND source = 'manual'",
                (discord_id,),
            ) as cur:
                return (await cur.fetchone())[0]

    async def user_sources(self, discord_id: int, appid: int) -> set[str]:
        async with self.db.read() as c:
            async with c.execute(
                "SELECT source FROM game_sources WHERE discord_id = ? AND appid = ?",
                (discord_id, appid),
            ) as cur:
                return {r["source"] for r in await cur.fetchall()}

    async def user_games(self, discord_id: int) -> list[dict]:
        """The user's monitored games with the latest known price from the DB."""
        sql = """
            SELECT g.appid AS appid,
                   COALESCE(NULLIF(d.name, ''), g.name) AS name,
                   group_concat(DISTINCT s.source) AS sources,
                   d.on_sale AS on_sale, d.final_cents AS final_cents,
                   d.original_cents AS original_cents, d.discount_pct AS discount_pct,
                   d.formatted_final AS formatted_final, d.updated_at AS updated_at
            FROM game_sources s
            JOIN watched_games g ON g.appid = s.appid
            LEFT JOIN deal_state d ON d.appid = g.appid
            WHERE s.discord_id = ?
            GROUP BY g.appid
            ORDER BY COALESCE(d.on_sale, 0) DESC, LOWER(COALESCE(NULLIF(d.name, ''), g.name)), g.appid
        """
        async with self.db.read() as c:
            async with c.execute(sql, (discord_id,)) as cur:
                rows = await cur.fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["sources"] = set((item["sources"] or "").split(","))
            result.append(item)
        return result

    async def monitored_map(self) -> dict[int, dict[str, set[int]]]:
        """appid -> {'wishlist': {user ids}, 'manual': {user ids}} (union of all users)."""
        async with self.db.read() as c:
            async with c.execute("SELECT appid, discord_id, source FROM game_sources") as cur:
                rows = await cur.fetchall()
        result: dict[int, dict[str, set[int]]] = {}
        for r in rows:
            entry = result.setdefault(r["appid"], {"wishlist": set(), "manual": set()})
            entry[r["source"]].add(r["discord_id"])
        return result

    async def watched_names(self) -> dict[int, str]:
        async with self.db.read() as c:
            async with c.execute("SELECT appid, name FROM watched_games WHERE name <> ''") as cur:
                return {r["appid"]: r["name"] for r in await cur.fetchall()}

    async def counts(self) -> dict[str, int]:
        queries = {
            "monitored": "SELECT COUNT(DISTINCT appid) FROM game_sources",
            "users": "SELECT COUNT(*) FROM users",
            "links": "SELECT COUNT(*) FROM steam_links",
            "on_sale": "SELECT COUNT(*) FROM deal_state WHERE on_sale = 1",
        }
        out = {}
        async with self.db.read() as c:
            for key, sql in queries.items():
                async with c.execute(sql) as cur:
                    out[key] = (await cur.fetchone())[0]
        return out

    # ----------------------------------------------------------------- deals
    async def get_deal_states(self) -> dict[int, dict]:
        async with self.db.read() as c:
            async with c.execute("SELECT * FROM deal_state") as cur:
                rows = await cur.fetchall()
        return {r["appid"]: dict(r) for r in rows}

    async def top_deals(self, limit: int) -> list[dict]:
        async with self.db.read() as c:
            async with c.execute(
                "SELECT * FROM deal_state WHERE on_sale = 1 AND name <> '' "
                "ORDER BY discount_pct DESC, final_cents ASC, appid ASC LIMIT ?",
                (limit,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # --------------------------------------------------------------- reviews
    async def upsert_reviews(self, rows: Iterable[tuple[int, int, int, str]]) -> int:
        """Store ``(appid, review_count, percent_positive, source)`` rows in one short transaction.

        Sources, best first: 'browse' (GetItems), 'search' (complete tooltip) and 'search_partial'
        (tooltip with extra paragraphs: an under-estimate). 'browse' always replaces; 'search'
        replaces 'search' or 'search_partial', and a 'browse' row only when older than
        ``BROWSE_REVIEW_TTL_SECONDS``; 'search_partial' only replaces 'search_partial'.
        Returns the number of rows offered (not necessarily changed)."""
        rows = list(rows)
        if not rows:
            return 0
        now = int(self._clock().timestamp())
        cutoff = now - BROWSE_REVIEW_TTL_SECONDS
        async with self.db.transaction() as c:
            await c.executemany(
                "INSERT INTO game_reviews (appid, review_count, percent_positive, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(appid) DO UPDATE SET "
                "review_count = excluded.review_count, percent_positive = excluded.percent_positive, "
                "source = excluded.source, updated_at = excluded.updated_at "
                "WHERE excluded.source = 'browse' "
                "OR (excluded.source = 'search' AND (game_reviews.source IN ('search', 'search_partial') "
                "OR game_reviews.updated_at < ?)) "
                "OR (excluded.source = 'search_partial' AND game_reviews.source = 'search_partial')",
                [(a, n, p, src, now, cutoff) for a, n, p, src in rows],
            )
        return len(rows)

    async def get_reviews(self, appid: int) -> dict | None:
        async with self.db.read() as c:
            async with c.execute("SELECT * FROM game_reviews WHERE appid = ?", (appid,)) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def reviews_cached(self) -> int:
        async with self.db.read() as c:
            async with c.execute("SELECT COUNT(*) FROM game_reviews") as cur:
                return (await cur.fetchone())[0]

    async def top_ranked_deals(self, limit: int = 50) -> list[dict]:
        """Games on sale ranked by :mod:`steamdeals.ranking` (reviews, quality, discount).

        Ranking happens in Python over the on-sale rows (a few thousand); games without a
        cached review summary stay eligible. Deterministic: ties follow ``ranking.sort_key``."""
        sql = (
            "SELECT d.appid, d.name, d.final_cents, d.original_cents, d.discount_pct, d.discount_end, "
            "d.image_url, r.review_count, r.percent_positive, r.source AS review_source "
            "FROM deal_state d LEFT JOIN game_reviews r ON r.appid = d.appid "
            "WHERE d.on_sale = 1 AND d.name <> ''"
        )
        async with self.db.read() as c:
            async with c.execute(sql) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        return ranking.rank_deals(rows, limit)

    async def price_history(self, appid: int) -> list[dict]:
        async with self.db.read() as c:
            async with c.execute(
                "SELECT * FROM price_history WHERE appid = ? ORDER BY id", (appid,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def apply_cycle(self, changes: CycleChanges) -> int:
        """Persist everything a monitoring cycle decided, atomically.

        Returns the number of notifications newly enqueued. Because the state
        update, the enqueue and the baseline flag share one transaction, a
        crash can never record a signature as seen without its notification.
        """
        now = self._now()
        async with self.db.transaction() as c:
            async with c.execute(
                "SELECT appid, state_hash, in_specials, on_sale, name, last_signature, baseline, sale_epoch, missing_cycles FROM deal_state"
            ) as cur:
                existing = {r["appid"]: r for r in await cur.fetchall()}

            history_rows, upserts = [], []
            for upd in changes.states:
                s = upd.snapshot
                prev = existing.get(s.appid)
                signature = s.signature if upd.set_signature else (prev["last_signature"] if prev else None)
                if (
                    prev is not None
                    and prev["state_hash"] == s.state_hash
                    and prev["in_specials"] == int(s.in_specials)
                    and prev["on_sale"] == int(s.on_sale)
                    and prev["last_signature"] == signature
                    and prev["baseline"] == int(upd.baseline)
                    and prev["sale_epoch"] == s.epoch
                    and prev["missing_cycles"] == 0
                    and (prev["name"] == s.name or not s.name)
                ):
                    continue
                if prev is None or prev["state_hash"] != s.state_hash:
                    history_rows.append(
                        (s.appid, s.final_cents, s.original_cents, s.discount_pct, s.discount_end, now, s.state_hash)
                    )
                upserts.append(
                    (
                        s.appid, s.name, signature, int(upd.baseline), s.epoch, 0, s.state_hash, int(s.on_sale),
                        int(s.in_specials), s.final_cents, s.original_cents, s.discount_pct,
                        s.discount_end, s.formatted_final, s.formatted_original, s.image_url, now,
                    )
                )
            await c.executemany(
                "INSERT INTO price_history (appid, final_cents, original_cents, discount_pct, "
                "discount_end, observed_at, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                history_rows,
            )
            await c.executemany(
                """
                INSERT INTO deal_state (appid, name, last_signature, baseline, sale_epoch, missing_cycles, state_hash, on_sale,
                    in_specials, final_cents, original_cents, discount_pct, discount_end,
                    formatted_final, formatted_original, image_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(appid) DO UPDATE SET
                    name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE deal_state.name END,
                    last_signature = excluded.last_signature, baseline = excluded.baseline,
                    sale_epoch = excluded.sale_epoch, missing_cycles = 0,
                    state_hash = excluded.state_hash, on_sale = excluded.on_sale,
                    in_specials = excluded.in_specials, final_cents = excluded.final_cents,
                    original_cents = excluded.original_cents, discount_pct = excluded.discount_pct,
                    discount_end = excluded.discount_end, formatted_final = excluded.formatted_final,
                    formatted_original = excluded.formatted_original, image_url = excluded.image_url,
                    updated_at = excluded.updated_at
                """,
                upserts,
            )
            # Specials that disappeared from the listing are no longer on sale.
            await c.executemany(
                "UPDATE deal_state SET missing_cycles = missing_cycles + 1 WHERE appid = ? AND on_sale = 1",
                [(a,) for a in changes.bump_missing],
            )
            await c.executemany(
                "UPDATE deal_state SET on_sale = 0, in_specials = 0, missing_cycles = 0, updated_at = ? "
                "WHERE appid = ?",
                [(now, a) for a in changes.clear_specials],
            )

            enqueued = 0
            for item in changes.notifications:
                enqueued += await self._enqueue(c, item, now)

            if changes.merges:
                async with c.execute(
                    "SELECT id, appid, signature, origins, payload, status FROM notifications "
                    "WHERE status IN ('pending', 'expired')"
                ) as cur:
                    rows = await cur.fetchall()
                pending = {r["appid"]: r for r in rows if r["status"] == "pending"}
                expired = {(r["appid"], r["signature"]): r for r in rows if r["status"] == "expired"}
                for item in changes.merges:
                    snap = item.snapshot
                    revived = expired.get((snap.appid, snap.signature))
                    if revived is not None and snap.appid not in pending and snap.on_sale:
                        # The promotion came back after a transient off-sale observation.
                        await c.execute(
                            "UPDATE notifications SET status = 'pending', last_error = NULL WHERE id = ?",
                            (revived["id"],),
                        )
                        pending[snap.appid] = revived
                    row = pending.get(snap.appid)
                    if row is not None:
                        await self._merge(c, row, item)

            # Pending notifications of games that are no longer on sale are not sent.
            await c.execute(
                "UPDATE notifications SET status = 'expired' WHERE status = 'pending' AND appid IN "
                "(SELECT appid FROM deal_state WHERE on_sale = 0)"
            )

            await c.executemany(
                "UPDATE watched_games SET name = ?, last_seen = ? WHERE appid = ?",
                [(name, now, appid) for appid, name in changes.names.items() if name],
            )
            await c.execute(
                "DELETE FROM watched_games WHERE appid NOT IN (SELECT appid FROM game_sources)"
            )
            if changes.mark_baseline:
                await self._set_setting(c, BASELINE_SETTING, now)
            return enqueued

    async def _enqueue(self, c, item: NewNotification, now: str) -> int:
        snap = item.snapshot
        payload = item.payload()
        cur = await c.execute(
            "INSERT OR IGNORE INTO notifications (appid, signature, origins, payload, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (snap.appid, snap.signature, json.dumps(payload["origins"]), json.dumps(payload), now),
        )
        if cur.rowcount == 0:
            async with c.execute(
                "SELECT id, status FROM notifications WHERE appid = ? AND signature = ?",
                (snap.appid, snap.signature),
            ) as existing_cur:
                existing = await existing_cur.fetchone()
            if existing is None or existing["status"] == "pending":
                return 0
            # Reversion (A -> B -> A) or a retry of an expired/failed one: the state is
            # current again, so the same row is reactivated and announced again.
            await c.execute(
                "UPDATE notifications SET status = 'pending', attempts = 0, last_error = NULL, "
                "sent_at = NULL, discord_message_id = NULL, origins = ?, payload = ?, created_at = ? "
                "WHERE id = ?",
                (json.dumps(payload["origins"]), json.dumps(payload), now, existing["id"]),
            )
            notification_id = existing["id"]
        else:
            notification_id = cur.lastrowid
        await c.execute(
            "UPDATE notifications SET status = 'superseded' "
            "WHERE appid = ? AND status = 'pending' AND id <> ?",
            (snap.appid, notification_id),
        )
        return 1

    async def _merge(self, c, row, item: NewNotification) -> None:
        payload = json.loads(row["payload"])
        origins = set(payload.get("origins", [])) | item.origins
        users = set(payload.get("user_ids", [])) | item.user_ids
        snap = item.snapshot
        # The end date is display-only. Keep the pending message current when the same
        # promotion (same prices and discount) reports a different, known end date.
        new_end = (
            snap.discount_end
            if snap.discount_end is not None
            and payload.get("final_cents") == snap.final_cents
            and payload.get("original_cents") == snap.original_cents
            and payload.get("discount_pct") == snap.discount_pct
            else payload.get("discount_end")
        )
        if (
            origins == set(payload.get("origins", []))
            and users == set(payload.get("user_ids", []))
            and new_end == payload.get("discount_end")
        ):
            return
        payload["discount_end"] = new_end
        payload["origins"] = sorted(origins)
        payload["user_ids"] = sorted(users)
        await c.execute(
            "UPDATE notifications SET origins = ?, payload = ? WHERE id = ?",
            (json.dumps(payload["origins"]), json.dumps(payload), row["id"]),
        )

    # --------------------------------------------------------- notifications
    async def enqueue_notification(self, item: NewNotification) -> bool:
        async with self.db.transaction() as c:
            return bool(await self._enqueue(c, item, self._now()))

    async def pending_notifications(self, limit: int) -> list[dict]:
        async with self.db.read() as c:
            async with c.execute(
                "SELECT * FROM notifications WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    async def count_notifications(self, status: str = "pending") -> int:
        async with self.db.read() as c:
            async with c.execute("SELECT COUNT(*) FROM notifications WHERE status = ?", (status,)) as cur:
                return (await cur.fetchone())[0]

    async def get_notification(self, notification_id: int) -> dict | None:
        async with self.db.read() as c:
            async with c.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def mark_sent(self, notification_id: int, message_id: str | int | None) -> None:
        async with self.db.transaction() as c:
            await c.execute(
                "UPDATE notifications SET status = 'sent', sent_at = ?, discord_message_id = ?, "
                "attempts = attempts + 1, last_error = NULL WHERE id = ? AND status = 'pending'",
                (self._now(), None if message_id is None else str(message_id), notification_id),
            )

    async def mark_failed(self, notification_id: int, error: str, permanent: bool = True) -> None:
        """Record a failed delivery attempt.

        ``permanent=True`` counts the attempt; after MAX_SEND_ATTEMPTS the row becomes
        'failed' (kept in the database, no longer selected for sending). Environment-level
        errors (channel missing/forbidden, 429) pass ``permanent=False``: only the error
        text is recorded and the row stays pending without consuming attempts.
        """
        async with self.db.transaction() as c:
            if permanent:
                await c.execute(
                    "UPDATE notifications SET attempts = attempts + 1, last_error = ?, "
                    "status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END "
                    "WHERE id = ? AND status = 'pending'",
                    (error[:500], MAX_SEND_ATTEMPTS, notification_id),
                )
            else:
                await c.execute(
                    "UPDATE notifications SET last_error = ? WHERE id = ? AND status = 'pending'",
                    (error[:500], notification_id),
                )

    async def expire_stale_pending(self) -> int:
        """Mark pending notifications whose game is no longer on sale as 'expired'."""
        async with self.db.transaction() as c:
            cur = await c.execute(
                "UPDATE notifications SET status = 'expired' WHERE status = 'pending' AND appid IN "
                "(SELECT appid FROM deal_state WHERE on_sale = 0)"
            )
            return cur.rowcount

    # ------------------------------------------------------- checks/settings
    async def begin_check(self, source: str) -> None:
        async with self.db.transaction() as c:
            await c.execute(
                "INSERT INTO check_state (source, last_started, last_status) VALUES (?, ?, 'running') "
                "ON CONFLICT(source) DO UPDATE SET last_started = excluded.last_started, last_status = 'running'",
                (source, self._now()),
            )

    async def finish_check(self, source: str, status: str, error: str | None = None) -> None:
        """``status`` is 'ok', 'partial' or 'error'; last_success moves on ok/partial."""
        now = self._now()
        async with self.db.transaction() as c:
            await c.execute(
                "INSERT INTO check_state (source, last_started, last_success, last_status, last_error) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET "
                "last_success = CASE WHEN excluded.last_status = 'error' THEN check_state.last_success "
                "ELSE excluded.last_success END, last_status = excluded.last_status, "
                "last_error = excluded.last_error",
                (source, now, now if status != "error" else None, status, (error or None) and error[:500]),
            )

    async def get_check_states(self) -> dict[str, dict]:
        async with self.db.read() as c:
            async with c.execute("SELECT * FROM check_state") as cur:
                return {r["source"]: dict(r) for r in await cur.fetchall()}

    async def get_setting(self, key: str) -> str | None:
        async with self.db.read() as c:
            async with c.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self.db.transaction() as c:
            await self._set_setting(c, key, value)

    async def _set_setting(self, c, key: str, value: str) -> None:
        await c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def baseline_done(self) -> bool:
        return await self.get_setting(BASELINE_SETTING) is not None
