"""/steam_verificar: permission, immediate reply, cooldown, concurrency, result delivery, shutdown."""

import asyncio
import logging
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from conftest import special, store_item
from steamdeals.config import load_settings
from steamdeals.discord_bot.commands import SteamCommands, register_commands
from steamdeals.discord_bot.manual_check import ManualCheck
from test_client import make_bot
from test_commands_embeds import Clock, FakeInteraction, FakeStore, FakeSuggest

DUMMY = 99999


def build(repo, env, clock=None, cooldown=300):
    clock = clock or Clock()
    manual = ManualCheck(env.monitor, cooldown, clock=clock)
    cmds = SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=clock, manual_check=manual)
    return cmds, manual, clock


async def finish(manual):
    await asyncio.gather(*list(manual._tasks))


def admin(user_id=1):
    return FakeInteraction(user_id, manage_guild=True)


# ----------------------------------------------------------------- permission
async def test_non_admin_is_denied_without_starting_a_cycle(repo, env):
    cmds, manual, _ = build(repo, env)
    i = FakeInteraction(2)
    await cmds.steam_verificar(i)
    assert "Gerenciar Servidor" in i.text
    i.assert_all_ephemeral()
    assert not manual._tasks and env.specials.calls == 0


async def test_user_without_guild_permissions_object_is_denied(repo, env):
    cmds, manual, _ = build(repo, env)
    i = FakeInteraction(2)
    i.user = type("DmUser", (), {"id": 2})()  # a plain User (direct message) has no guild_permissions
    await cmds.steam_verificar(i)
    assert "Gerenciar Servidor" in i.text and env.specials.calls == 0


def test_command_is_registered_guild_only():
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    register_commands(tree, object())
    commands = {c.name: c for c in tree.get_commands()}
    assert commands["steam_verificar"].guild_only and len(commands) == 9


# ---------------------------------------------------------- immediate reply, success
async def test_replies_immediately_and_reports_when_the_cycle_finishes(repo, env):
    env.specials.items = [special(DUMMY)]
    await repo.add_manual(1, 100, "G")
    env.store.items[100] = store_item(100)
    env.specials.gate = asyncio.Event()
    cmds, manual, _ = build(repo, env)
    i = admin()
    await cmds.steam_verificar(i)  # returns although the cycle is blocked
    assert "iniciada" in i.text and i.followup.messages == []
    i.assert_all_ephemeral()
    await env.specials.entered.wait()
    assert env.monitor.running
    env.specials.gate.set()
    await finish(manual)
    [msg] = i.followup.messages
    assert msg["ephemeral"] is True
    text = msg["content"]
    assert "concluída em" in text and "com falhas" not in text
    assert "Ofertas: 1" in text and "Monitorados: 1\n" in text and "1 geradas" in text
    assert "Falhas: nenhuma" in text
    assert manual.last_result.status == "ok" and manual.last_result.duration >= 0


async def test_source_failures_are_listed_in_the_result(repo, env):
    env.specials.error = RuntimeError("x")
    cmds, manual, _ = build(repo, env)
    i = admin()
    await cmds.steam_verificar(i)
    await finish(manual)
    text = i.followup.messages[0]["content"]
    assert "specials: RuntimeError" in text and "concluída com falhas" in text and "Ofertas: falhou" in text


async def test_partial_store_and_wishlist_failures_are_not_hidden(repo, env):
    env.specials.items = [special(DUMMY)]
    await repo.add_manual(1, 100, "A")
    await repo.add_manual(1, 101, "B")
    env.store.items = {100: store_item(100), 101: store_item(101)}
    env.store.failed = {101}
    await repo.set_steam_link(1, "76561198000000001")
    env.wishlist.data["76561198000000001"] = RuntimeError("down")
    cmds, manual, _ = build(repo, env)
    i = admin()
    await cmds.steam_verificar(i)
    await finish(manual)
    text = i.followup.messages[0]["content"]
    assert "concluída com falhas" in text and "Ofertas: 1\n" in text
    assert "Monitorados: 2 (1 sem preço/falha)" in text and "Wishlists com falha: 1" in text
    assert manual.last_result.store_failed == 1 and manual.last_result.wishlists_failed == 1
    status = admin()
    await cmds.steam_status(status)
    assert "concluída com falhas" in status.text


# ------------------------------------------------------------------- cooldown
async def test_cooldown_blocks_then_expires_with_injected_clock(repo, env):
    env.specials.items = [special(DUMMY)]
    cmds, manual, clock = build(repo, env, cooldown=300)
    first = admin()
    await cmds.steam_verificar(first)
    await finish(manual)
    clock.now += 100
    second = admin()
    await cmds.steam_verificar(second)
    assert "Aguarde 200s" in second.text and env.specials.calls == 1
    clock.now += 200
    third = admin()
    await cmds.steam_verificar(third)
    assert "iniciada" in third.text
    await finish(manual)
    assert env.specials.calls == 2


async def test_cooldown_is_not_consumed_when_a_cycle_is_already_running(repo, env):
    env.specials.items = [special(DUMMY)]
    env.specials.gate = asyncio.Event()
    scheduled = asyncio.create_task(env.monitor.run_cycle())
    await env.specials.entered.wait()
    cmds, manual, _ = build(repo, env)
    i = admin()
    await cmds.steam_verificar(i)
    assert "andamento" in i.text and manual.cooldown_left() == 0 and not manual._tasks
    env.specials.gate.set()
    await scheduled
    again = admin()
    await cmds.steam_verificar(again)
    assert "iniciada" in again.text  # not blocked by a cooldown
    await finish(manual)


async def test_scheduled_cycle_starting_right_after_the_request_skips_manual_without_cooldown(repo, env):
    env.specials.items = [special(DUMMY)]
    cmds, manual, _ = build(repo, env)
    i = admin()
    await cmds.steam_verificar(i)  # the background task is created but has not run yet
    await env.monitor._lock.acquire()  # a scheduled cycle takes the lock first
    try:
        await finish(manual)
    finally:
        env.monitor._lock.release()
    assert "agendado" in i.followup.messages[0]["content"]
    assert env.specials.calls == 0 and manual.cooldown_left() == 0 and manual.last_result is None


async def test_zero_cooldown_allows_back_to_back_checks(repo, env):
    env.specials.items = [special(DUMMY)]
    cmds, manual, _ = build(repo, env, cooldown=0)
    for _ in range(2):
        i = admin()
        await cmds.steam_verificar(i)
        assert "iniciada" in i.text
        await finish(manual)


# ---------------------------------------------------------------- concurrency
async def test_two_simultaneous_requests_run_a_single_cycle(repo, env):
    env.specials.items = [special(DUMMY)]
    env.specials.gate = asyncio.Event()
    cmds, manual, _ = build(repo, env)
    a, b = admin(1), admin(2)
    await asyncio.gather(cmds.steam_verificar(a), cmds.steam_verificar(b))
    texts = sorted([a.text, b.text])
    assert sum("iniciada" in t for t in texts) == 1 and sum("andamento" in t for t in texts) == 1
    await env.specials.entered.wait()
    env.specials.gate.set()
    await finish(manual)
    assert env.specials.calls == 1


# -------------------------------------------------------------------- failures
async def test_exception_in_the_cycle_is_logged_and_reported_generically(repo, env, caplog, monkeypatch):
    async def boom():
        raise RuntimeError("secret internals")

    monkeypatch.setattr(env.monitor, "run_cycle", boom)
    cmds, manual, _ = build(repo, env)
    i = admin()
    with caplog.at_level(logging.ERROR):
        await cmds.steam_verificar(i)
        await finish(manual)
    assert "falhou" in i.followup.messages[0]["content"]
    assert "secret internals" not in i.followup.messages[0]["content"]
    assert "Manual check failed" in caplog.text
    assert manual.last_result.status == "failed"


async def test_followup_delivery_failure_is_logged_and_does_not_break(repo, env, caplog):
    env.specials.items = [special(DUMMY)]
    cmds, manual, _ = build(repo, env)
    i = admin()

    async def broken_send(*args, **kwargs):
        raise discord.HTTPException(type("R", (), {"status": 404, "reason": "gone"})(), "Unknown Webhook")

    i.followup.send = broken_send
    with caplog.at_level(logging.WARNING):
        await cmds.steam_verificar(i)
        await finish(manual)
    assert "could not be delivered" in caplog.text
    assert manual.last_result.status == "ok" and manual.last_result.delivered is False
    # the result is still available to admins in /steam_status
    status = admin()
    await cmds.steam_status(status)
    assert "Última verificação manual" in status.text and "concluída" in status.text
    plain = FakeInteraction(5)
    await cmds.steam_status(plain)
    assert "Última verificação manual" not in plain.text


# ------------------------------------------------------------------- shutdown
async def test_shutdown_cancels_and_waits_without_reporting(repo, env):
    env.specials.items = [special(DUMMY)]
    env.specials.gate = asyncio.Event()
    cmds, manual, _ = build(repo, env)
    i = admin()
    await cmds.steam_verificar(i)
    await env.specials.entered.wait()
    task = next(iter(manual._tasks))
    await manual.shutdown()
    assert task.cancelled() and not manual._tasks
    assert i.followup.messages == [] and manual.last_result is None
    assert not env.monitor.running  # the lock was released


async def test_bot_close_waits_for_manual_checks_before_closing_services(monkeypatch):
    bot = make_bot()
    events = []

    class Monitor:
        running = False

        async def run_cycle(self):
            self.running = True
            try:
                await asyncio.sleep(60)
            finally:
                self.running = False
                events.append("manual cycle unwound")

    class Services:
        monitor = Monitor()

        async def close(self):
            events.append("services closed")

    bot.services = Services()
    bot.manual_check = ManualCheck(bot.services.monitor, 0)

    async def deliver(result):
        events.append("delivered")

    assert bot.manual_check.request(1, deliver).status == "started"
    await asyncio.sleep(0.05)
    await bot.close()
    assert events == ["manual cycle unwound", "services closed"]


async def test_manual_check_does_not_touch_the_scheduled_loop(monkeypatch):
    """Real bot loop running: a manual cycle neither reschedules nor adds an iteration."""
    bot = make_bot()  # CHECK_INTERVAL_MINUTES=7
    cycles = []

    class Monitor:
        running = False

        async def run_cycle(self):
            cycles.append(1)
            return SimpleNamespace(specials_ok=True, specials_count=1, monitored=0, store_failed=0,
                                   wishlists_failed=0, enqueued=0, dispatched=0, errors=[])

    class Services:
        monitor = Monitor()

        async def close(self):
            pass

    async def ready():
        return None

    monkeypatch.setattr(bot, "wait_until_ready", ready)
    bot.services = Services()
    bot.manual_check = ManualCheck(bot.services.monitor, 0)
    bot._cycle_loop.start()
    await asyncio.sleep(0.05)
    assert cycles == [1] and bot._cycle_loop.is_running()  # first scheduled iteration done, now sleeping
    scheduled_next = bot._cycle_loop.next_iteration
    delivered = []

    async def deliver(result):
        delivered.append(result)

    assert bot.manual_check.request(1, deliver).status == "started"
    await asyncio.sleep(0.05)
    assert cycles == [1, 1] and delivered[0].status == "ok"  # the manual cycle ran
    assert bot._cycle_loop.next_iteration == scheduled_next  # schedule untouched
    assert bot._cycle_loop.current_loop == 0 and bot._cycle_loop.is_running()  # no extra iteration
    await bot.close()


async def test_a_scheduled_tick_during_a_manual_cycle_is_skipped_by_the_lock(repo, env):
    env.specials.items = [special(DUMMY)]
    env.specials.gate = asyncio.Event()
    cmds, manual, _ = build(repo, env)
    await cmds.steam_verificar(admin())
    await env.specials.entered.wait()
    assert await env.monitor.run_cycle() is None  # what the scheduled loop would get
    env.specials.gate.set()
    await finish(manual)
    assert env.specials.calls == 1


# ------------------------------------------------------- cooldown restoration (None)
async def test_cycle_returning_none_restores_the_previous_cooldown_state(repo, env, monkeypatch):
    env.specials.items = [special(DUMMY)]
    clock = Clock()
    cmds, manual, _ = build(repo, env, clock=clock)
    await cmds.steam_verificar(admin())
    await finish(manual)
    started = manual._last_started
    clock.now += 400  # cooldown (300) expired

    async def none_cycle():
        return None

    monkeypatch.setattr(env.monitor, "run_cycle", none_cycle)
    i = admin()
    await cmds.steam_verificar(i)
    await finish(manual)
    assert manual._last_started == started and manual.cooldown_left() == 0
    assert "agendado" in i.followup.messages[0]["content"]


async def test_scheduled_cycle_taking_the_lock_keeps_the_previous_cooldown_state(repo, env):
    env.specials.items = [special(DUMMY)]
    clock = Clock()
    cmds, manual, _ = build(repo, env, clock=clock)
    await cmds.steam_verificar(admin())
    await finish(manual)
    started = manual._last_started
    clock.now += 400
    await cmds.steam_verificar(admin())
    await env.monitor._lock.acquire()
    try:
        await finish(manual)
    finally:
        env.monitor._lock.release()
    assert manual._last_started == started and manual.cooldown_left() == 0


# ------------------------------------------------- delivery does not block, closing
async def test_delivering_the_result_does_not_block_a_new_request(repo, env):
    env.specials.items = [special(DUMMY)]
    cmds, manual, clock = build(repo, env, cooldown=0)
    i = admin()
    release = asyncio.Event()

    async def slow_send(*args, **kwargs):
        await release.wait()

    i.followup.send = slow_send
    await cmds.steam_verificar(i)
    for _ in range(100):  # wait until the cycle is over and the task is delivering
        await asyncio.sleep(0.01)
        if manual._running_task is None:
            break
    assert not manual.active and manual._tasks  # still delivering, but not "active"
    second = admin()
    await cmds.steam_verificar(second)
    assert "iniciada" in second.text
    release.set()
    await finish(manual)


async def test_request_after_shutdown_is_refused(repo, env):
    cmds, manual, _ = build(repo, env)
    await manual.shutdown()
    assert manual.request(1, lambda r: None).status == "closing"
    i = admin()
    await cmds.steam_verificar(i)
    assert "encerrando" in i.text and env.specials.calls == 0 and not manual._tasks


async def test_request_while_the_bot_is_closing_is_refused(monkeypatch):
    bot = make_bot()
    seen = []

    class Services:
        monitor = type("M", (), {"running": False})()

        async def close(self):
            pass

    bot.services = Services()
    bot.manual_check = ManualCheck(bot.services.monitor, 0)
    original = bot.manual_check.shutdown

    async def shutdown_probe():
        seen.append(bot.manual_check.request(1, lambda r: None).status)  # during close
        await original()

    monkeypatch.setattr(bot.manual_check, "shutdown", shutdown_probe)
    await bot.close()
    assert seen == ["closing"]


async def test_close_refuses_new_manual_requests_before_touching_the_loop(monkeypatch):
    bot = make_bot()
    order = []

    class Services:
        monitor = type("M", (), {"running": False})()

        async def close(self):
            pass

    bot.services = Services()
    bot.manual_check = ManualCheck(bot.services.monitor, 0)
    real_begin = bot.manual_check.begin_close

    def begin():
        order.append("begin_close")
        real_begin()

    def get_task():
        order.append("loop")

    monkeypatch.setattr(bot.manual_check, "begin_close", begin)
    monkeypatch.setattr(bot._cycle_loop, "get_task", get_task)
    await bot.close()
    assert order[0] == "begin_close" and "loop" in order


def test_command_has_manage_guild_default_permission_as_ux_hint():
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    register_commands(tree, object())
    command = {c.name: c for c in tree.get_commands()}["steam_verificar"]
    assert command.default_permissions.manage_guild is True


# ---------------------------------------------------------------------- config
def test_cooldown_setting_default_and_override_and_validation():
    base = {"DISCORD_BOT_TOKEN": "t", "NOTIFY_CHANNEL_ID": "1"}
    assert load_settings(base).manual_check_cooldown_seconds == 300
    assert load_settings({**base, "MANUAL_CHECK_COOLDOWN_SECONDS": "0"}).manual_check_cooldown_seconds == 0
    from steamdeals.config import ConfigError

    with pytest.raises(ConfigError):
        load_settings({**base, "MANUAL_CHECK_COOLDOWN_SECONDS": "-1"})
