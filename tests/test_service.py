import asyncio

import pytest

from conftest import special, store_item
from steamdeals.monitor.service import MonitorService
from steamdeals.notify.embeds import build_mentions
from steamdeals.steam.http import SteamUnavailable
from steamdeals.steam.store import StoreItem

DUMMY = 99999  # keeps the specials listing non-empty (an empty listing is a failure)
SID_A = "76561198000000001"
SID_B = "76561198000000002"


async def baseline(env):
    """Complete the baseline with an empty specials listing."""
    env.specials.items = [special(DUMMY)]
    await env.cycle()
    assert await env.repo.baseline_done()


# ------------------------------------------------------------ wishlist import
async def test_import_adds_updates_and_preserves_manual(env):
    repo = env.repo
    await repo.set_steam_link(1, SID_A)
    await repo.add_manual(1, 3, "Three")
    await repo.add_manual(1, 9, "Nine")
    env.wishlist.set(SID_A, [1, 2, 3])
    result = await env.monitor.import_wishlist(1)
    assert (result.status, result.added, result.removed, result.total) == ("ok", 3, 0, 3)

    env.wishlist.set(SID_A, [2, 3, 4])  # 1 left the wishlist, 4 entered
    result = await env.monitor.import_wishlist(1)
    assert (result.added, result.removed) == (1, 1)
    mapped = await repo.monitored_map()
    assert set(mapped) == {2, 3, 4, 9}
    assert mapped[3] == {"wishlist": {1}, "manual": {1}}
    assert mapped[9] == {"wishlist": set(), "manual": {1}}
    assert 1 not in mapped
    assert (await repo.get_steam_link(1))["last_import_status"] == "ok"


async def test_import_private_or_empty_keeps_existing_sources(env):
    await env.repo.set_steam_link(1, SID_A)
    env.wishlist.set(SID_A, [1, 2])
    await env.monitor.import_wishlist(1)
    env.wishlist.set(SID_A, [], empty_or_private=True)
    result = await env.monitor.import_wishlist(1)
    assert result.status == "empty_or_private"
    assert set(await env.repo.monitored_map()) == {1, 2}
    assert (await env.repo.get_steam_link(1))["last_import_status"] == "empty_or_private"


async def test_import_without_link_and_with_error(env):
    assert (await env.monitor.import_wishlist(1)).status == "not_linked"
    await env.repo.set_steam_link(1, SID_A)
    env.wishlist.data[SID_A] = SteamUnavailable("down")
    result = await env.monitor.import_wishlist(1)
    assert result.status == "error"
    assert (await env.repo.get_steam_link(1))["last_import_status"] == "error"


async def test_cycle_fetches_one_wishlist_per_distinct_steam_id(env):
    for user, sid in ((1, SID_A), (2, SID_A), (3, SID_B)):
        await env.repo.set_steam_link(user, sid)
    env.wishlist.set(SID_A, [10])
    env.wishlist.set(SID_B, [20])
    await env.cycle()
    assert sorted(env.wishlist.calls) == [SID_A, SID_B]
    mapped = await env.repo.monitored_map()
    assert mapped[10]["wishlist"] == {1, 2} and mapped[20]["wishlist"] == {3}


async def test_one_failing_wishlist_does_not_block_the_others(env):
    await env.repo.set_steam_link(1, SID_A)
    await env.repo.set_steam_link(2, SID_B)
    env.wishlist.data[SID_A] = SteamUnavailable("down")
    env.wishlist.set(SID_B, [20])
    summary = await env.cycle()
    assert summary.wishlists_failed == 1
    assert 20 in await env.repo.monitored_map()
    assert (await env.repo.get_check_states())["wishlist"]["last_status"] == "partial"


# ---------------------------------------------------------------- detection
async def test_new_promotion_for_monitored_game_is_enqueued(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "Game 100")
    env.store.items[100] = store_item(100)
    summary = await env.cycle()
    assert summary.enqueued == 1
    [n] = await env.pending()
    assert n["appid"] == 100 and n["payload"]["origins"] == ["manual"]
    assert n["payload"]["user_ids"] == [1]
    assert n["payload"]["discount_end"] == 1800000000


async def test_same_signature_does_not_repeat(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100)
    await env.cycle()
    await env.cycle()
    await env.cycle()
    assert len(await env.pending()) == 1
    assert await env.repo.count_notifications("superseded") == 0


async def test_price_change_creates_new_notification_and_supersedes_pending(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100, final=1000)
    await env.cycle()
    env.store.items[100] = store_item(100, final=500, pct=75)
    await env.cycle()
    pending = await env.pending()
    assert len(pending) == 1 and pending[0]["payload"]["final_cents"] == 500
    assert await env.repo.count_notifications("superseded") == 1


async def test_price_change_after_sent_notifies_again_and_keeps_sent(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100, final=1000)
    await env.cycle()
    [first] = await env.pending()
    await env.repo.mark_sent(first["id"], 55)
    env.store.items[100] = store_item(100, final=900, pct=55)
    await env.cycle()
    [second] = await env.pending()
    assert second["id"] != first["id"]
    assert (await env.repo.get_notification(first["id"]))["status"] == "sent"
    assert await env.repo.count_notifications("superseded") == 0


async def test_end_date_change_is_a_new_promotion(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100, end=1800000000)
    await env.cycle()
    env.store.items[100] = store_item(100, end=1800099999)
    await env.cycle()
    assert len(await env.pending()) == 1
    assert await env.repo.count_notifications("superseded") == 1


async def test_dedup_across_users_and_origins_single_notification_mentions_all(env):
    await baseline(env)
    await env.repo.set_steam_link(1, SID_A)
    await env.repo.set_steam_link(2, SID_B)
    env.wishlist.set(SID_A, [100])
    env.wishlist.set(SID_B, [100])
    await env.repo.add_manual(3, 100, "Game 100")
    env.specials.items = [special(100)]
    env.store.items[100] = store_item(100)
    await env.cycle()
    [n] = await env.pending()
    assert n["payload"]["origins"] == ["manual", "specials", "wishlist"]
    assert n["payload"]["user_ids"] == [1, 2, 3]
    content, allowed = build_mentions(n["payload"])
    assert content == "<@1> <@2> <@3>"
    assert env.store.requested[-1] == [100]  # queried once even with 3 users


async def test_pending_origins_and_users_are_merged_until_sent(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100)
    await env.cycle()
    await env.repo.add_manual(2, 100, "G")
    env.specials.items = [special(100)]
    await env.cycle()
    [n] = await env.pending()
    assert n["payload"]["user_ids"] == [1, 2]
    assert set(n["payload"]["origins"]) == {"manual", "specials"}
    assert json_origins(n) == n["payload"]["origins"]


def json_origins(n):
    import json
    return json.loads(n["origins"])


async def test_invalid_or_missing_prices_are_handled(env):
    await baseline(env)
    for appid in (1, 2, 3, 4):
        await env.repo.add_manual(1, appid, f"G{appid}")
    env.store.items = {
        1: store_item(1, final=None, original=None, pct=0, end=None),  # no price (free/unreleased)
        2: store_item(2, final=900, original=500, pct=20),  # final > original
        3: store_item(3, final=500, original=500, pct=0),  # no discount
        4: store_item(4, final=500, original=1000, pct=0),  # discount 0 but prices differ
    }
    summary = await env.cycle()
    assert summary.enqueued == 0 and await env.pending() == []
    states = await env.repo.get_deal_states()
    assert states[1]["final_cents"] is None and states[1]["on_sale"] == 0
    assert all(states[a]["on_sale"] == 0 for a in (1, 2, 3, 4))


async def test_free_to_keep_promotion_is_a_valid_deal(env):
    await baseline(env)
    await env.repo.add_manual(1, 5, "Free")
    env.store.items[5] = store_item(5, final=0, original=3000, pct=100)
    await env.cycle()
    assert len(await env.pending()) == 1


async def test_store_failures_are_isolated_per_game(env):
    await baseline(env)
    await env.repo.add_manual(1, 1, "A")
    await env.repo.add_manual(1, 2, "B")
    env.store.items = {1: store_item(1), 2: store_item(2)}
    env.store.failed = {2}
    summary = await env.cycle()
    assert summary.store_failed == 1
    assert [n["appid"] for n in await env.pending()] == [1]
    assert 2 not in await env.repo.get_deal_states()
    assert (await env.repo.get_check_states())["store"]["last_status"] == "partial"
    env.store.failed = set()
    await env.cycle()
    assert sorted(n["appid"] for n in await env.pending()) == [1, 2]


async def test_total_store_failure_keeps_cycle_alive(env):
    await baseline(env)
    await env.repo.add_manual(1, 1, "A")
    env.store.error = SteamUnavailable("down")
    env.specials.items = [special(50)]
    summary = await env.cycle()
    assert summary is not None and "store: SteamUnavailable" in summary.errors
    assert (await env.pending())[0]["appid"] == 50  # specials still processed


async def test_price_history_recorded_only_when_it_changes(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100, final=1000)
    for _ in range(3):
        await env.cycle()
    assert len(await env.repo.price_history(100)) == 1
    env.store.items[100] = store_item(100, final=800, pct=60)
    await env.cycle()
    assert [h["final_cents"] for h in await env.repo.price_history(100)] == [1000, 800]


# ---------------------------------------------------------------- baseline
async def test_baseline_records_specials_without_notifying_but_notifies_monitored(env):
    await env.repo.add_manual(1, 2, "Game 2")
    env.specials.items = [special(1), special(2), special(3)]
    env.store.items[2] = store_item(2)
    summary = await env.cycle()
    assert summary.baseline_completed and summary.baseline_recorded == 2
    assert [n["appid"] for n in await env.pending()] == [2]  # monitored is not suppressed
    states = await env.repo.get_deal_states()
    assert set(states) == {1, 2, 3}
    assert states[1]["baseline"] == 1 and states[3]["baseline"] == 1 and states[2]["baseline"] == 0
    assert await env.repo.baseline_done()


async def test_after_baseline_new_and_changed_specials_notify(env):
    env.specials.items = [special(1), special(2)]
    await env.cycle()
    assert await env.pending() == []
    await env.cycle()
    assert await env.pending() == []  # unchanged: still nothing

    env.specials.items = [special(1), special(2, final=500, pct=75), special(3)]
    await env.cycle()
    assert sorted(n["appid"] for n in await env.pending()) == [2, 3]


async def test_baseline_not_completed_when_specials_fails(env):
    await env.repo.add_manual(1, 2, "Game 2")
    env.store.items[2] = store_item(2)
    env.specials.error = SteamUnavailable("down")
    summary = await env.cycle()
    assert not summary.specials_ok and not summary.baseline_completed
    assert not await env.repo.baseline_done()
    assert (await env.repo.get_check_states())["specials"]["last_status"] == "error"
    assert [n["appid"] for n in await env.pending()] == [2]  # other sources still work

    env.specials.error = None
    env.specials.items = [special(1), special(3)]
    await env.cycle()
    assert await env.repo.baseline_done()
    assert [n["appid"] for n in await env.pending()] == [2]  # 1 and 3 baselined silently


async def test_baseline_only_runs_once(env):
    env.specials.items = [special(1)]
    await env.cycle()
    env.specials.items = [special(1), special(9)]
    await env.cycle()
    assert [n["appid"] for n in await env.pending()] == [9]


async def test_monitored_game_baselined_earlier_is_announced_when_user_starts_monitoring(env):
    env.specials.items = [special(7)]
    await env.cycle()
    assert await env.pending() == []
    await env.repo.add_manual(1, 7, "Game 7")
    env.store.items[7] = store_item(7, final=1000, original=2000, pct=50)
    await env.cycle()
    assert [n["appid"] for n in await env.pending()] == [7]
    await env.cycle()
    assert len(await env.pending()) == 1


async def test_specials_end_dates_do_not_cause_duplicate_when_monitoring_changes(env):
    await baseline(env)
    env.specials.items = [special(7)]
    await env.cycle()
    [n] = await env.pending()
    await env.repo.mark_sent(n["id"], 1)
    # The user starts monitoring: we learn the end date but it is the same promotion.
    await env.repo.add_manual(1, 7, "Game 7")
    env.store.items[7] = store_item(7, final=1000, original=2000, pct=50, end=1800000000)
    await env.cycle()
    assert await env.pending() == []
    # The user stops monitoring: specials-only data (no end date) must not re-announce either.
    await env.repo.remove_manual(1, 7)
    await env.cycle()
    assert await env.pending() == []


async def test_specials_that_ended_leave_the_deal_list_and_return_as_new_promotion(env):
    await baseline(env)
    env.specials.items = [special(DUMMY), special(8)]
    await env.cycle()
    [n] = await env.pending()
    await env.repo.mark_sent(n["id"], 1)
    assert len(await env.repo.top_deals(10)) == 2

    env.specials.items = [special(DUMMY)]
    await env.cycle()
    assert len(await env.repo.top_deals(10)) == 2  # one absence is not enough to end it
    await env.cycle()
    assert [d["appid"] for d in await env.repo.top_deals(10)] == [DUMMY]

    env.specials.items = [special(DUMMY), special(8)]  # same price returns later
    await env.cycle()
    assert len(await env.pending()) == 1


# ---------------------------------------------------------------- lock
async def test_concurrent_cycles_are_not_allowed(env):
    env.specials.gate = asyncio.Event()
    first = asyncio.create_task(env.cycle())
    await env.specials.entered.wait()
    assert env.monitor.running
    assert await env.cycle() is None  # second trigger is skipped
    assert env.specials.calls == 1
    env.specials.gate.set()
    summary = await first
    assert summary is not None and not env.monitor.running
    assert await env.cycle() is not None  # lock released


async def test_lock_released_after_unexpected_error(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(env.monitor, "_detect", boom)
    summary = await env.cycle()
    assert any(e.startswith("detect") for e in summary.errors)
    assert not env.monitor.running


async def test_dispatcher_runs_inside_cycle(env, repo):
    class D:
        called = 0

        async def dispatch(self):
            D.called += 1
            from steamdeals.notify.dispatcher import DispatchResult
            return DispatchResult(sent=3)

    monitor = MonitorService(repo, env.specials, env.store, env.wishlist, dispatcher=D())
    summary = await monitor.run_cycle()
    assert D.called == 1 and summary.dispatched == 3


def test_store_item_defaults():
    assert not StoreItem(appid=1, name="x").on_sale
