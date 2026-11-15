"""Startup/shutdown behaviour of the Discord client, status text and wishlist failure modes."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from conftest import special, store_item
from steamdeals.config import load_settings
from steamdeals.discord_bot.client import SteamDealsBot
from steamdeals.steam.http import SteamRateLimited, SteamSchemaError, SteamUnavailable
from test_client import make_bot
from test_commands_embeds import Clock, FakeInteraction, FakeStore, FakeSuggest, SteamCommands

SID = "76561198028121353"


# ------------------------------------------------------------------ client shutdown
async def test_close_waits_for_the_cancelled_cycle_before_closing_services(monkeypatch):
    bot = make_bot()
    events = []

    class Monitor:
        async def run_cycle(self):
            try:
                await asyncio.sleep(60)
            finally:
                events.append("cycle unwound")

    class Services:
        monitor = Monitor()

        async def close(self):
            events.append("services closed")

    bot.services = Services()

    async def ready():
        return None

    monkeypatch.setattr(bot, "wait_until_ready", ready)
    bot._cycle_loop.start()
    await asyncio.sleep(0.05)  # the cycle is now running
    await bot.close()
    assert events == ["cycle unwound", "services closed"]


async def test_short_interval_logs_a_warning(caplog, monkeypatch):
    bot = SteamDealsBot(
        load_settings({"DISCORD_BOT_TOKEN": "t", "NOTIFY_CHANNEL_ID": "1", "CHECK_INTERVAL_MINUTES": "1"})
    )

    class Services:
        repo = monitor = store = suggest = None

        @classmethod
        async def create(cls, settings, sender):
            return cls()

    monkeypatch.setattr("steamdeals.discord_bot.client.Services", Services)
    monkeypatch.setattr("steamdeals.discord_bot.client.register_commands", lambda tree, handlers: None)
    monkeypatch.setattr(bot._cycle_loop, "start", lambda: None)

    async def sync(**kwargs):
        return []

    monkeypatch.setattr(bot.tree, "sync", sync)
    with caplog.at_level(logging.WARNING):
        await bot.setup_hook()
    assert "back to back" in caplog.text


# --------------------------------------------------------------------- status text
async def test_status_reports_running_cycle_when_next_iteration_is_in_the_past(repo, env):
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock(), next_cycle=lambda: past)
    await env.monitor._lock.acquire()
    try:
        i = FakeInteraction(1)
        await cmds.steam_status(i)
    finally:
        env.monitor._lock.release()
    assert "Próximo ciclo: ciclo em andamento" in i.text


async def test_status_shows_next_cycle_timestamp_when_in_the_future(repo, env):
    future = datetime.now(timezone.utc) + timedelta(minutes=20)
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock(), next_cycle=lambda: future)
    i = FakeInteraction(1)
    await cmds.steam_status(i)
    assert f"<t:{int(future.timestamp())}:R>" in i.text


# ------------------------------------------------------- wishlist: three situations
async def linked_with_games(env, repo):
    await repo.set_steam_link(1, SID)
    env.wishlist.set(SID, [10, 20])
    await env.monitor.import_wishlist(1)
    assert set(await repo.monitored_map()) == {10, 20}


@pytest.mark.parametrize(
    "failure",
    [
        SteamRateLimited("HTTP 429 from Steam"),
        SteamUnavailable("HTTP 503 from Steam", 503),
        SteamUnavailable("TimeoutError: ", None),
        SteamSchemaError("GetWishlist response has no 'response' object"),
    ],
)
async def test_temporary_failure_keeps_sources_and_is_not_reported_as_private(repo, env, failure):
    await linked_with_games(env, repo)
    env.wishlist.data[SID] = failure
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock())
    i = FakeInteraction(1)
    await cmds.wishlist_importar(i)
    assert "privad" not in i.text and "Não foi possível consultar a Steam" in i.text
    assert (await repo.get_steam_link(1))["last_import_status"] == "error"
    assert set(await repo.monitored_map()) == {10, 20}  # nothing was wiped


@pytest.mark.parametrize("failure", [SteamRateLimited("429"), SteamUnavailable("down", 500)])
async def test_cycle_records_error_status_per_user_and_keeps_sources(repo, env, failure):
    await linked_with_games(env, repo)
    await repo.set_steam_link(2, "76561198000000002")
    env.wishlist.set("76561198000000002", [30])
    env.wishlist.data[SID] = failure
    env.specials.items = [special(99999)]
    summary = await env.cycle()
    assert summary.wishlists_failed == 1
    assert (await repo.get_steam_link(1))["last_import_status"] == "error"
    assert (await repo.get_steam_link(2))["last_import_status"] == "ok"
    assert {10, 20, 30} <= set(await repo.monitored_map())


async def test_empty_or_private_is_ambiguous_keeps_sources_and_has_its_own_status(repo, env):
    await linked_with_games(env, repo)
    env.wishlist.set(SID, [], empty_or_private=True)
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock())
    i = FakeInteraction(1)
    await cmds.wishlist_importar(i)
    assert "vazia ou privada" in i.text and "mantidos" in i.text
    assert (await repo.get_steam_link(1))["last_import_status"] == "empty_or_private"
    assert set(await repo.monitored_map()) == {10, 20}


async def test_vincular_on_first_import_failure_links_and_reports_unavailable(repo, env):
    env.wishlist.data[SID] = SteamUnavailable("down", 502)
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock())
    i = FakeInteraction(1)
    await cmds.steam_vincular(i, SID)
    assert f"`{SID}` vinculada" in i.text and "Não foi possível consultar a Steam" in i.text
    assert (await repo.get_steam_link(1))["last_import_status"] == "error"


@pytest.mark.parametrize("text", ["abc", "7656119", "76561198028121353999", "76561188028121353", ""])
async def test_vincular_invalid_id_does_not_link_or_touch_steam(repo, env, text):
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock())
    i = FakeInteraction(1)
    await cmds.steam_vincular(i, text)
    assert "inválido" in i.text
    assert await repo.get_steam_link(1) is None and env.wishlist.calls == []
    i.assert_all_ephemeral()


async def test_recovery_after_error_syncs_normally(repo, env):
    await linked_with_games(env, repo)
    env.wishlist.data[SID] = SteamUnavailable("down", 500)
    await env.monitor.import_wishlist(1)
    env.wishlist.set(SID, [10, 40])
    result = await env.monitor.import_wishlist(1)
    assert result.status == "ok" and (result.added, result.removed) == (1, 1)
    assert (await repo.get_steam_link(1))["last_import_status"] == "ok"


# ------------------------------------------------------------ dispatch errors in status
async def test_channel_failure_is_visible_in_cycle_errors_and_status_for_admins(repo, env):
    from steamdeals.notify.dispatcher import ChannelUnavailable, NotificationDispatcher

    async def send(payload):
        raise ChannelUnavailable("no access")

    env.monitor.dispatcher = NotificationDispatcher(repo, send, 20)
    env.specials.items = [special(99999)]
    await env.cycle()  # baseline
    await repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100)
    summary = await env.cycle()
    assert "dispatch: channel unavailable or rate limited" in summary.errors
    assert await repo.count_notifications("pending") == 1  # stays pending
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock())
    admin = FakeInteraction(1, manage_guild=True)
    await cmds.steam_status(admin)
    assert "Erros do último ciclo" in admin.text and "dispatch" in admin.text
    user = FakeInteraction(2)
    await cmds.steam_status(user)
    assert "Erros do último ciclo" not in user.text


# ------------------------------------------------- wishlist: rejected / nonexistent ids
async def test_steam_http_400_is_reported_as_invalid_id_not_as_a_temporary_error(repo, env):
    await linked_with_games(env, repo)
    env.wishlist.data[SID] = SteamUnavailable("HTTP 400 from Steam", 400)
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock())
    i = FakeInteraction(1)
    await cmds.wishlist_importar(i)
    assert "SteamID64" in i.text and "recusou" in i.text and "alguns minutos" not in i.text
    assert (await repo.get_steam_link(1))["last_import_status"] == "invalid_id"
    assert set(await repo.monitored_map()) == {10, 20}  # still nothing wiped


async def test_cycle_records_invalid_id_status(repo, env):
    await repo.set_steam_link(1, SID)
    env.wishlist.data[SID] = SteamUnavailable("HTTP 400 from Steam", 400)
    env.specials.items = [special(99999)]
    await env.cycle()
    assert (await repo.get_steam_link(1))["last_import_status"] == "invalid_id"


async def test_well_formed_nonexistent_profile_is_the_ambiguous_empty_case(repo, env):
    # Real Steam answers {"response":{}} for a well formed but unknown SteamID64.
    await repo.set_steam_link(1, SID)
    env.wishlist.set(SID, [], empty_or_private=True)
    result = await env.monitor.import_wishlist(1)
    assert result.status == "empty_or_private"
