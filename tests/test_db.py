import sqlite3

import aiosqlite
import pytest

from conftest import store_item
from steamdeals.db import migrations
from steamdeals.db.database import Database
from steamdeals.db.repositories import Repository
from steamdeals.models import CycleChanges, DealSnapshot, NewNotification, StateUpdate

TABLES = {
    "users", "steam_links", "watched_games", "game_sources", "price_history",
    "deal_state", "notifications", "check_state", "settings", "schema_version",
}


def snap(appid=1, final=1000, original=2000, pct=50, end=1800000000, on_sale=True, name="G"):
    return DealSnapshot(appid, name, final, original, pct, end, on_sale)


async def names(conn):
    async with conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
        return {r[0] for r in await cur.fetchall()}


async def test_schema_created_and_version_recorded(db):
    assert TABLES <= await names(db.conn)
    async with db.conn.execute("SELECT version FROM schema_version") as cur:
        assert [r[0] for r in await cur.fetchall()] == list(range(1, migrations.LATEST_VERSION + 1))


async def test_pragmas(db):
    async with db.conn.execute("PRAGMA foreign_keys") as cur:
        assert (await cur.fetchone())[0] == 1
    async with db.conn.execute("PRAGMA journal_mode") as cur:
        assert (await cur.fetchone())[0].lower() == "wal"
    async with db.conn.execute("PRAGMA busy_timeout") as cur:
        assert (await cur.fetchone())[0] == 5000


async def test_migrations_are_idempotent(tmp_path):
    path = str(tmp_path / "m.db")
    d1 = await Database(path).connect()
    await d1.close()
    d2 = await Database(path).connect()  # re-run on an existing database
    assert await migrations.migrate(d2.conn) == migrations.LATEST_VERSION
    async with d2.conn.execute("SELECT COUNT(*) FROM schema_version") as cur:
        assert (await cur.fetchone())[0] == migrations.LATEST_VERSION
    await d2.close()


async def test_migration_scripts_can_be_rerun_without_error(db):
    # IF NOT EXISTS everywhere: executing the script again must not fail.
    await db.conn.executescript(migrations.MIGRATIONS[0][1])


async def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    bad = migrations.MIGRATIONS + [
        (migrations.LATEST_VERSION + 1, "CREATE TABLE half_done (x); INSERT INTO missing_table VALUES (1);")
    ]
    monkeypatch.setattr(migrations, "MIGRATIONS", bad)
    with pytest.raises(sqlite3.OperationalError):
        await Database(str(tmp_path / "bad.db")).connect()
    conn = await aiosqlite.connect(str(tmp_path / "bad.db"), isolation_level=None)
    assert "half_done" not in await names(conn)
    assert await migrations.current_version(conn) == migrations.LATEST_VERSION
    await conn.close()


async def test_incremental_migration_applies_only_new_version(tmp_path, monkeypatch):
    path = str(tmp_path / "inc.db")
    await (await Database(path).connect()).close()
    extra = migrations.MIGRATIONS + [(migrations.LATEST_VERSION + 1, "CREATE TABLE IF NOT EXISTS extra (x);")]
    monkeypatch.setattr(migrations, "MIGRATIONS", extra)
    d = await Database(path).connect()
    assert "extra" in await names(d.conn)
    assert await migrations.current_version(d.conn) == migrations.LATEST_VERSION + 1
    await d.close()


async def test_foreign_keys_and_checks_are_enforced(db):
    c = db.conn
    with pytest.raises(sqlite3.IntegrityError):  # unknown user
        await c.execute("INSERT INTO steam_links (discord_id, steam_id64, linked_at) VALUES (1, 'x', 't')")
    await c.execute("INSERT INTO users VALUES (1, 't')")
    await c.execute("INSERT INTO watched_games (appid, name) VALUES (10, 'g')")
    await c.execute("INSERT INTO game_sources VALUES (1, 10, 'manual', 't')")
    with pytest.raises(sqlite3.IntegrityError):  # bad source
        await c.execute("INSERT INTO game_sources VALUES (1, 10, 'other', 't')")
    with pytest.raises(sqlite3.IntegrityError):  # duplicate PK triple
        await c.execute("INSERT INTO game_sources VALUES (1, 10, 'manual', 't')")
    with pytest.raises(sqlite3.IntegrityError):  # unknown game
        await c.execute("INSERT INTO game_sources VALUES (1, 99, 'manual', 't')")
    await c.execute("INSERT INTO steam_links (discord_id, steam_id64, linked_at) VALUES (1, 'x', 't')")
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE discord_id
        await c.execute("INSERT INTO steam_links (discord_id, steam_id64, linked_at) VALUES (1, 'y', 't')")
    await c.execute("DELETE FROM users WHERE discord_id = 1")  # cascades
    async with c.execute("SELECT (SELECT COUNT(*) FROM game_sources) + (SELECT COUNT(*) FROM steam_links)") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_notifications_unique_and_status_check(db):
    c = db.conn
    sql = "INSERT INTO notifications (appid, signature, origins, payload, status, created_at) VALUES (?,?,?,?,?,?)"
    await c.execute(sql, (1, "s", "[]", "{}", "pending", "t"))
    with pytest.raises(sqlite3.IntegrityError):
        await c.execute(sql, (1, "s", "[]", "{}", "pending", "t"))
    with pytest.raises(sqlite3.IntegrityError):
        await c.execute(sql, (1, "s2", "[]", "{}", "bogus", "t"))
    for status in ("sent", "superseded", "expired", "failed"):
        await c.execute(sql, (2, status, "[]", "{}", status, "t"))


async def test_check_state_and_settings(repo):
    await repo.begin_check("specials")
    await repo.finish_check("specials", "ok")
    state = (await repo.get_check_states())["specials"]
    assert state["last_status"] == "ok" and state["last_success"]
    success = state["last_success"]
    await repo.finish_check("specials", "error", "boom")
    state = (await repo.get_check_states())["specials"]
    assert state["last_status"] == "error" and state["last_error"] == "boom"
    assert state["last_success"] == success  # an error does not move last_success
    assert await repo.get_setting("k") is None
    await repo.set_setting("k", "1")
    await repo.set_setting("k", "2")
    assert await repo.get_setting("k") == "2"


async def test_manual_and_link_operations(repo):
    assert await repo.add_manual(1, 10, "Ten") is True
    assert await repo.add_manual(1, 10, "Ten") is False
    assert await repo.manual_count(1) == 1
    await repo.set_steam_link(1, "76561198028121353")
    assert (await repo.get_steam_link(1))["steam_id64"] == "76561198028121353"
    await repo.sync_wishlist_sources(1, [10, 11])
    assert await repo.user_sources(1, 10) == {"manual", "wishlist"}
    had, removed = await repo.remove_steam_link(1)
    assert had and removed == 2
    assert await repo.user_sources(1, 10) == {"manual"}
    assert await repo.remove_steam_link(1) == (False, 0)
    assert await repo.remove_manual(1, 10) and not await repo.remove_manual(1, 10)


async def test_apply_cycle_prunes_unreferenced_watched_games(repo):
    await repo.sync_wishlist_sources(1, [5])
    await repo.sync_wishlist_sources(1, [])
    await repo.apply_cycle(CycleChanges())
    async with repo.db.read() as c:
        async with c.execute("SELECT COUNT(*) FROM watched_games") as cur:
            assert (await cur.fetchone())[0] == 0


async def test_price_history_only_on_change(repo):
    for final in (1000, 1000, 800, 800):
        await repo.apply_cycle(CycleChanges(states=[StateUpdate(snap(final=final), set_signature=True)]))
    history = await repo.price_history(1)
    assert [h["final_cents"] for h in history] == [1000, 800]


async def test_restart_keeps_pending_and_state(tmp_path):
    path = str(tmp_path / "restart.db")
    d1 = await Database(path).connect()
    r1 = Repository(d1)
    s = snap(appid=7)
    await r1.apply_cycle(
        CycleChanges(
            states=[StateUpdate(s, set_signature=True)],
            notifications=[NewNotification(s, {"specials"}, {5})],
            mark_baseline=True,
        )
    )
    await d1.close()

    d2 = await Database(path).connect()
    r2 = Repository(d2)
    pending = await r2.pending_notifications(10)
    assert len(pending) == 1 and pending[0]["payload"]["user_ids"] == [5]
    assert (await r2.get_deal_states())[7]["last_signature"] == s.signature
    assert await r2.baseline_done()
    await d2.close()


async def test_transaction_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        async with db.transaction() as c:
            await c.execute("INSERT INTO users VALUES (1, 't')")
            raise RuntimeError("fail")
    async with db.read() as c:
        async with c.execute("SELECT COUNT(*) FROM users") as cur:
            assert (await cur.fetchone())[0] == 0


def test_store_item_helper_sanity():
    assert store_item(1).on_sale
