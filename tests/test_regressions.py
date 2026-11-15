"""Regression tests for review findings (A1, M1-M6, B1-B6, H2) and the network guard."""

import socket
import sqlite3

import pytest

from conftest import special, store_item
from steamdeals.db import migrations
from steamdeals.db.database import Database
from steamdeals.db.repositories import MAX_SEND_ATTEMPTS, Repository
from steamdeals.discord_bot.commands import MAX_APPID
from steamdeals.models import DealSnapshot, NewNotification
from steamdeals.notify.dispatcher import ChannelUnavailable, NotificationDispatcher
from steamdeals.steam.store import parse_get_items
from test_commands_embeds import FakeInteraction, FakeSuggest, Clock, SteamCommands
from conftest import FakeStore
from test_dispatcher import Sender, notification

DUMMY = 99999
SID_A = "76561198000000001"
SID_B = "76561198000000002"


async def baseline(env):
    env.specials.items = [special(DUMMY)]
    await env.cycle()
    assert await env.repo.baseline_done()


# ---------------------------------------------------------------- A1
async def test_empty_listing_never_sets_baseline_or_clears_state(env):
    env.specials.items = []
    summary = await env.cycle()
    assert not summary.specials_ok and not await env.repo.baseline_done()
    assert (await env.repo.get_check_states())["specials"]["last_status"] == "error"


async def test_empty_listing_does_not_clear_existing_deals(env):
    await baseline(env)
    env.specials.items = [special(DUMMY), special(5)]
    await env.cycle()
    env.specials.items = []
    for _ in range(3):
        await env.cycle()
    states = await env.repo.get_deal_states()
    assert states[5]["on_sale"] == 1 and states[5]["in_specials"] == 1 and states[5]["missing_cycles"] == 0


async def test_collapsed_listing_is_a_source_failure(env):
    await baseline(env)
    env.specials.items = [special(1000 + n) for n in range(40)]
    await env.cycle()
    env.specials.items = [special(1000)]  # far below what we know: not "everything ended"
    summary = await env.cycle()
    assert not summary.specials_ok
    states = await env.repo.get_deal_states()
    assert all(states[1000 + n]["on_sale"] == 1 for n in range(40))


async def test_game_missing_once_is_not_reannounced_when_it_returns(env):
    await baseline(env)
    env.specials.items = [special(DUMMY), special(7)]
    await env.cycle()
    [n] = await env.pending()
    await env.repo.mark_sent(n["id"], 1)
    env.specials.items = [special(DUMMY)]  # page shift hides it for one cycle
    await env.cycle()
    assert (await env.repo.get_deal_states())[7]["on_sale"] == 1
    env.specials.items = [special(DUMMY), special(7)]
    await env.cycle()
    assert await env.pending() == []
    assert (await env.repo.get_deal_states())[7]["missing_cycles"] == 0


async def test_game_missing_twice_ends_and_comes_back_as_new(env):
    await baseline(env)
    env.specials.items = [special(DUMMY), special(7)]
    await env.cycle()
    [n] = await env.pending()
    await env.repo.mark_sent(n["id"], 1)
    env.specials.items = [special(DUMMY)]
    await env.cycle()
    await env.cycle()
    assert (await env.repo.get_deal_states())[7]["on_sale"] == 0
    env.specials.items = [special(DUMMY), special(7)]
    await env.cycle()
    assert [x["appid"] for x in await env.pending()] == [7]


# ---------------------------------------------------------------- M1
async def test_reversion_a_b_a_reannounces_and_supersedes_intermediate(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100, final=1000)  # A
    await env.cycle()
    [a] = await env.pending()
    await env.repo.mark_sent(a["id"], 1)
    env.store.items[100] = store_item(100, final=500, pct=75)  # B (pending)
    await env.cycle()
    env.store.items[100] = store_item(100, final=1000)  # back to A
    await env.cycle()
    pending = await env.pending()
    assert [p["id"] for p in pending] == [a["id"]] and pending[0]["payload"]["final_cents"] == 1000
    assert await env.repo.count_notifications("superseded") == 1


async def test_enqueue_existing_pending_signature_is_still_ignored(repo):
    assert await repo.enqueue_notification(notification(1))
    assert not await repo.enqueue_notification(notification(1))


# ---------------------------------------------------------------- M2
async def test_pending_of_game_no_longer_on_sale_expires_instead_of_sending(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100)
    await env.cycle()
    env.store.items[100] = store_item(100, final=2000, original=2000, pct=0)  # sale ended
    await env.cycle()
    assert await env.pending() == []
    assert await env.repo.count_notifications("expired") == 1


async def test_dispatcher_revalidates_against_deal_state_before_sending(env, repo):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100)
    await env.cycle()
    async with repo.db.transaction() as c:  # state changed behind the queue's back
        await c.execute("UPDATE deal_state SET on_sale = 0 WHERE appid = 100")
    sender = Sender()
    await NotificationDispatcher(repo, sender).dispatch()
    assert sender.sent == []
    assert await repo.count_notifications("expired") == 1


async def test_transient_off_sale_does_not_lose_the_notification(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100)
    await env.cycle()
    env.store.items[100] = store_item(100, final=None, original=None, pct=0, end=None)  # glitch
    await env.cycle()
    assert await env.repo.count_notifications("expired") == 1
    env.store.items[100] = store_item(100)  # same promotion again
    await env.cycle()
    assert len(await env.pending()) == 1


async def test_migration_v2_preserves_data_and_adds_statuses(tmp_path, monkeypatch):
    path = str(tmp_path / "v1.db")
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:1])
    old = await Database(path).connect()
    await old.conn.execute(
        "INSERT INTO notifications (appid, signature, origins, payload, status, created_at) "
        "VALUES (1, 's', '[]', '{}', 'sent', 't')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await old.conn.execute(
            "INSERT INTO notifications (appid, signature, origins, payload, status, created_at) "
            "VALUES (2, 's', '[]', '{}', 'expired', 't')"
        )
    await old.close()
    monkeypatch.undo()
    new = await Database(path).connect()
    async with new.conn.execute("SELECT appid, status FROM notifications") as cur:
        assert [tuple(r) for r in await cur.fetchall()] == [(1, "sent")]
    await new.conn.execute(
        "INSERT INTO notifications (appid, signature, origins, payload, status, created_at) "
        "VALUES (2, 's', '[]', '{}', 'expired', 't')"
    )
    async with new.conn.execute("PRAGMA table_info(deal_state)") as cur:
        assert "missing_cycles" in [r["name"] for r in await cur.fetchall()]
    await new.close()


# ---------------------------------------------------------------- M3 / M4 / M5
async def test_stale_wishlist_result_is_ignored_after_unlink_or_relink(env):
    repo = env.repo
    await repo.set_steam_link(1, SID_A)
    assert await repo.sync_wishlist_sources(1, [10], SID_A) == (1, 0)
    await repo.remove_steam_link(1)
    assert await repo.sync_wishlist_sources(1, [10, 11], SID_A) is None
    assert await repo.monitored_map() == {}
    await repo.set_steam_link(1, SID_B)
    assert await repo.sync_wishlist_sources(1, [10], SID_A) is None  # fetched for the old id


async def test_import_discards_result_when_link_changed_during_fetch(env):
    await env.repo.set_steam_link(1, SID_A)
    original = env.wishlist.fetch

    async def fetch_then_relink(steam_id):
        result = await original(steam_id)
        await env.repo.set_steam_link(1, SID_B)
        return result

    env.wishlist.set(SID_A, [10])
    env.wishlist.fetch = fetch_then_relink
    result = await env.monitor.import_wishlist(1)
    assert result.status == "error" and result.error == "link_changed"
    assert await env.repo.monitored_map() == {}


async def test_changing_steam_id_drops_old_wishlist_but_keeps_manual(repo):
    await repo.set_steam_link(1, SID_A)
    await repo.sync_wishlist_sources(1, [10, 11])
    await repo.add_manual(1, 12, "x")
    await repo.set_steam_link(1, SID_A)  # same id: nothing dropped
    assert set(await repo.monitored_map()) == {10, 11, 12}
    await repo.set_steam_link(1, SID_B)
    assert set(await repo.monitored_map()) == {12}


async def test_game_that_stops_being_monitored_is_no_longer_on_sale(env):
    await baseline(env)
    repo = env.repo
    await repo.add_manual(1, 100, "G")
    await repo.set_steam_link(2, SID_A)
    await repo.sync_wishlist_sources(2, [101])
    env.store.items[100] = store_item(100)
    env.store.items[101] = store_item(101)
    await env.cycle()
    assert {d["appid"] for d in await repo.top_deals(10)} >= {100, 101}
    await repo.remove_manual(1, 100)
    await repo.remove_steam_link(2)
    assert {d["appid"] for d in await repo.top_deals(10)} == {DUMMY}


async def test_cycle_confirms_end_for_unmonitored_game_absent_from_listing(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100)
    await env.cycle()
    async with env.repo.db.transaction() as c:  # simulate a removal that did not reset state
        await c.execute("DELETE FROM game_sources")
    await env.cycle()
    await env.cycle()
    assert (await env.repo.get_deal_states())[100]["on_sale"] == 0


# ---------------------------------------------------------------- M6 / B3
async def test_poison_message_becomes_failed_after_five_attempts(repo):
    await repo.enqueue_notification(notification(1))
    await repo.enqueue_notification(notification(2))
    sender = Sender()
    sender.fail[1] = RuntimeError("malformed payload")
    d = NotificationDispatcher(repo, sender)
    for _ in range(MAX_SEND_ATTEMPTS):
        await d.dispatch()
    n = await repo.get_notification(1)
    assert n["status"] == "failed" and n["attempts"] == MAX_SEND_ATTEMPTS
    assert "malformed" in n["last_error"]
    sender.fail.clear()
    await d.dispatch()
    assert sender.sent.count(1) == 0  # excluded from selection
    assert await repo.count_notifications("failed") == 1


async def test_channel_level_errors_do_not_consume_attempts(repo):
    await repo.enqueue_notification(notification(1))
    sender = Sender()
    sender.fail[1] = ChannelUnavailable("gone")
    d = NotificationDispatcher(repo, sender)
    for _ in range(MAX_SEND_ATTEMPTS + 3):
        await d.dispatch()
    n = await repo.get_notification(1)
    assert n["status"] == "pending" and n["attempts"] == 0 and "gone" in n["last_error"]


async def test_mark_sent_and_failed_only_touch_pending_rows(repo):
    await repo.enqueue_notification(notification(1))
    await repo.enqueue_notification(notification(1, final=500))  # supersedes the first
    await repo.mark_sent(1, 99)
    await repo.mark_failed(1, "x")
    n = await repo.get_notification(1)
    assert n["status"] == "superseded" and n["attempts"] == 0 and n["discord_message_id"] is None


async def test_status_shows_failed_count(repo, env):
    await repo.enqueue_notification(notification(1))
    for _ in range(MAX_SEND_ATTEMPTS):
        await repo.mark_failed(1, "boom")
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock())
    i = FakeInteraction(1)
    await cmds.steam_status(i)
    assert "Notificações com falha definitiva: 1" in i.text


# ---------------------------------------------------------------- B1
async def test_merge_by_appid_even_when_pending_has_a_different_signature(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100, final=1000)
    await env.cycle()
    await env.repo.add_manual(2, 100, "G")
    # Same state, new user: merge into the pending row of that appid.
    await env.cycle()
    [n] = await env.pending()
    assert n["payload"]["user_ids"] == [1, 2]


# ---------------------------------------------------------------- B4 / B5
async def test_vincular_respects_import_cooldown(repo, env):
    clock = Clock()
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=clock)
    await repo.set_steam_link(1, SID_A)
    env.wishlist.set(SID_A, [1])
    env.wishlist.set(SID_B, [2])
    first = FakeInteraction(1)
    await cmds.wishlist_importar(first)
    assert len(env.wishlist.calls) == 1
    second = FakeInteraction(1)
    await cmds.steam_vincular(second, SID_B)
    assert len(env.wishlist.calls) == 1  # no immediate import during the cooldown
    assert "intervalo mínimo" in second.text
    assert (await repo.get_steam_link(1))["steam_id64"] == SID_B
    second.assert_all_ephemeral()


@pytest.mark.parametrize("value", [str(MAX_APPID + 1), "99999999999999999999"])
async def test_appid_upper_bound_in_commands(repo, env, value):
    store = FakeStore()
    cmds = SteamCommands(repo, env.monitor, store, FakeSuggest(), clock=Clock())
    i = FakeInteraction(1)
    await cmds.wishlist_add(i, value)
    assert "AppID numérico" in i.text
    i = FakeInteraction(1)
    await cmds.wishlist_remover(i, value)
    assert "sugestões" in i.text
    assert await repo.monitored_map() == {}
    store.items[MAX_APPID] = store_item(MAX_APPID)
    i = FakeInteraction(1)
    await cmds.wishlist_add(i, str(MAX_APPID))
    assert "adicionado" in i.text


# ---------------------------------------------------------------- B6
def test_priced_item_without_name_is_kept_and_nameless_unpriced_is_dropped():
    payload = {"response": {"store_items": [
        {"appid": 1, "best_purchase_option": {"final_price_in_cents": "500", "original_price_in_cents": "1000", "discount_pct": 50}},
        {"appid": 2},
    ]}}
    items = parse_get_items(payload)
    assert set(items) == {1} and items[1].name == "" and items[1].on_sale


async def test_nameless_item_uses_stored_name_or_generic_one(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "Stored Name")
    await env.repo.add_manual(1, 101, "x")
    env.store.items[100] = store_item(100, name="")
    env.store.items[101] = store_item(101, name="")
    env.store.items[102] = store_item(102, name="")
    await env.repo.add_manual(1, 102, "")
    await env.cycle()
    names = {n["appid"]: n["payload"]["name"] for n in await env.pending()}
    assert names[101] == "x" and names[100] == "Stored Name" and names[102] == "App 102"
    env.store.items[100] = store_item(100, final=900, pct=55, name="Real Name")
    await env.cycle()
    env.store.items[100] = store_item(100, final=800, pct=60, name="")
    await env.cycle()
    last = [n for n in await env.pending() if n["appid"] == 100][0]
    assert last["payload"]["name"] == "Real Name"  # falls back to the name stored earlier


# ---------------------------------------------------------------- H2
async def test_failed_commit_triggers_rollback(db, monkeypatch):
    conn = db.conn
    calls = []
    real = conn.execute

    async def execute(sql, *a, **k):
        calls.append(sql)
        if sql == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return await real(sql, *a, **k)

    monkeypatch.setattr(conn, "execute", execute)
    with pytest.raises(sqlite3.OperationalError):
        async with db.transaction() as c:
            await c.execute("INSERT INTO users VALUES (1, 't')")
    assert calls[-2:] == ["COMMIT", "ROLLBACK"]
    monkeypatch.undo()
    async with db.read() as c:  # the lock was released and no transaction is left open
        async with c.execute("SELECT COUNT(*) FROM users") as cur:
            assert (await cur.fetchone())[0] == 0
    async with db.transaction() as c:
        await c.execute("INSERT INTO users VALUES (2, 't')")


# ---------------------------------------------------------------- network guard
async def test_real_network_is_blocked_in_tests():
    with pytest.raises(RuntimeError, match="network"):
        socket.getaddrinfo("store.steampowered.com", 443)
    with pytest.raises(RuntimeError, match="network"):
        socket.create_connection(("example.com", 80), timeout=1)
