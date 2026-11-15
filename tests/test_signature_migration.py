"""Upgrade from a database that still holds signatures containing discount_end."""

import hashlib

import pytest

import aiosqlite

from conftest import Env, special, store_item
from steamdeals.db import migrations
from steamdeals.db.database import Database
from steamdeals.db.repositories import Repository


def old_signature(final, original, pct, end, epoch=0):
    raw = f"{final}|{original}|{pct}|{end}"
    if epoch:
        raw += f"|e{epoch}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def make_v2_database(path):
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    real = migrations.MIGRATIONS
    migrations.MIGRATIONS = real[:2]
    try:
        assert await migrations.migrate(conn) == 2
    finally:
        migrations.MIGRATIONS = real
    sql = (
        "INSERT INTO deal_state (appid, name, last_signature, baseline, sale_epoch, state_hash, on_sale, "
        "in_specials, final_cents, original_cents, discount_pct, discount_end, updated_at) "
        "VALUES (?, 'G', ?, 0, ?, 'h', ?, 0, 1000, 2000, 50, ?, 't')"
    )
    # 100: monitored sale with end date; 101: specials-only sale (unknown end, epoch 2); 102: off sale.
    await conn.execute(sql, (100, old_signature(1000, 2000, 50, 1800000000), 0, 1, 1800000000))
    await conn.execute(sql, (101, old_signature(1000, 2000, 50, None, 2), 2, 1, None))
    await conn.execute(sql, (102, old_signature(1000, 2000, 50, 1800000000), 0, 0, None))
    await conn.execute("INSERT INTO users VALUES (1, 't')")
    await conn.execute("INSERT INTO watched_games (appid, name) VALUES (100, 'G')")
    await conn.execute("INSERT INTO game_sources VALUES (1, 100, 'manual', 't')")
    await conn.execute("INSERT INTO settings VALUES ('specials_baseline_done', 't')")
    await conn.close()


async def test_upgrade_does_not_reannounce_games_already_on_sale(tmp_path):
    path = str(tmp_path / "old.db")
    await make_v2_database(path)
    db = await Database(path).connect()
    try:
        env = Env(Repository(db))
        env.specials.items = [special(101)]
        env.store.items[100] = store_item(100, end=1800000000)
        summary = await env.cycle()
        assert summary.enqueued == 0 and await env.pending() == []
        # The end date may also have moved while the bot was down: still silent.
        env.store.items[100] = store_item(100, end=1800099999)
        summary = await env.cycle()
        assert summary.enqueued == 0
    finally:
        await db.close()


async def test_migration_only_touches_games_on_sale(tmp_path):
    path = str(tmp_path / "old.db")
    await make_v2_database(path)
    db = await Database(path).connect()
    try:
        states = await Repository(db).get_deal_states()
        assert states[102]["last_signature"] == old_signature(1000, 2000, 50, 1800000000)
        assert states[100]["last_signature"] != old_signature(1000, 2000, 50, 1800000000)
    finally:
        await db.close()


async def schema_versions(path):
    conn = await aiosqlite.connect(path)
    try:
        async with conn.execute("SELECT version FROM schema_version ORDER BY version") as cur:
            return [r[0] for r in await cur.fetchall()]
    finally:
        await conn.close()


async def test_failure_in_the_middle_of_v3_rolls_back_and_keeps_schema_version(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    await make_v2_database(path)

    async def broken(conn):
        await conn.execute("UPDATE deal_state SET last_signature = 'changed'")
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:2] + [(3, broken)])
    with pytest.raises(RuntimeError):
        await Database(path).connect()
    assert await schema_versions(path) == [1, 2]
    conn = await aiosqlite.connect(path)
    try:
        async with conn.execute("SELECT COUNT(*) FROM deal_state WHERE last_signature = 'changed'") as cur:
            assert (await cur.fetchone())[0] == 0
    finally:
        await conn.close()
    monkeypatch.undo()
    db = await Database(path).connect()  # a later start applies v3 normally
    await db.close()
    assert await schema_versions(path) == [1, 2, 3]


async def test_full_upgrade_from_v1_to_latest(tmp_path, monkeypatch):
    path = str(tmp_path / "v1.db")
    conn = await aiosqlite.connect(path, isolation_level=None)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:1])
    assert await migrations.migrate(conn) == 1
    monkeypatch.undo()
    await conn.execute("INSERT INTO users VALUES (1, 't')")
    await conn.execute(
        "INSERT INTO deal_state (appid, name, last_signature, state_hash, on_sale, final_cents, "
        "original_cents, discount_pct, discount_end, updated_at) VALUES (7, 'G', 'old', 'h', 1, 500, 1000, 50, 5, 't')"
    )
    await conn.close()
    db = await Database(path).connect()
    try:
        assert await migrations.current_version(db.conn) == migrations.LATEST_VERSION == 3
        state = (await Repository(db).get_deal_states())[7]
        assert state["missing_cycles"] == 0 and state["last_signature"] != "old"
    finally:
        await db.close()


async def test_reopening_a_database_already_at_latest_is_idempotent(tmp_path):
    path = str(tmp_path / "old.db")
    await make_v2_database(path)
    first = await Database(path).connect()
    before = await Repository(first).get_deal_states()
    await first.close()
    second = await Database(path).connect()
    try:
        assert await Repository(second).get_deal_states() == before
    finally:
        await second.close()
    assert await schema_versions(path) == [1, 2, 3]
