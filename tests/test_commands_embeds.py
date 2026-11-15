from datetime import datetime, timezone

import discord
import pytest
from discord import app_commands

from conftest import FakeStore, FakeWishlist, store_item
from steamdeals.discord_bot.commands import (
    IMPORT_COOLDOWN_SECONDS,
    MAX_LIST_ITEMS,
    MAX_MANUAL_GAMES,
    SteamCommands,
    register_commands,
)
from steamdeals.models import DealSnapshot, NewNotification
from steamdeals.monitor.service import CycleSummary, MonitorService
from steamdeals.notify.embeds import MAX_MENTIONS, build_embed, build_mentions, origin_label
from steamdeals.steam.http import SteamUnavailable
from steamdeals.steam.suggest import SuggestItem

SID = "76561198028121353"


# ----------------------------------------------------------------- fakes
class FakeResponse:
    def __init__(self):
        self.messages, self.deferred, self._done = [], [], False

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self.messages.append({"content": content, **kwargs})

    async def defer(self, ephemeral=False, **kwargs):
        self._done = True
        self.deferred.append(ephemeral)

    def is_done(self):
        return self._done


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class FakeUser:
    def __init__(self, user_id, manage_guild=False):
        self.id = user_id
        self.guild_permissions = discord.Permissions(manage_guild=manage_guild)


class FakeInteraction:
    def __init__(self, user_id=1, manage_guild=False):
        self.user = FakeUser(user_id, manage_guild)
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    @property
    def sent(self):
        return self.response.messages + self.followup.messages

    @property
    def text(self):
        parts = []
        for m in self.sent:
            if m.get("content"):
                parts.append(m["content"])
            if m.get("embed") is not None:
                e = m["embed"]
                parts.append((e.title or "") + "\n" + (e.description or "") + "\n" + "\n".join(
                    f"{f.name}: {f.value}" for f in e.fields))
        return "\n".join(parts)

    def assert_all_ephemeral(self):
        assert self.sent, "no reply was sent"
        assert all(m.get("ephemeral") is True for m in self.sent)


class FakeSuggest:
    def __init__(self, items=()):
        self.items = list(items)
        self.terms = []

    async def search(self, term):
        self.terms.append(term)
        return self.items


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cmds(repo, env, clock):
    store = FakeStore()
    suggest = FakeSuggest()
    handlers = SteamCommands(repo, env.monitor, store, suggest, clock=clock)
    handlers.fakes = (store, suggest, env)
    return handlers


# ----------------------------------------------------------------- registration
def test_eight_guild_only_commands_registered_with_autocomplete():
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    register_commands(tree, object())  # handlers are only called on invocation
    commands = {c.name: c for c in tree.get_commands()}
    assert set(commands) == {
        "steam_status", "steam_vincular", "steam_desvincular", "wishlist",
        "wishlist_add", "wishlist_remover", "wishlist_importar", "steam_ofertas",
    }
    assert all(c.guild_only for c in commands.values())
    assert commands["wishlist_add"]._params["appid"].autocomplete is not None
    assert commands["wishlist_remover"]._params["appid"].autocomplete is not None
    qty = commands["steam_ofertas"]._params["quantidade"]
    assert (qty.min_value, qty.max_value) == (1, 15) and qty.required is False
    assert [p for p in commands["steam_vincular"]._params] == ["steam_id"]


# ----------------------------------------------------------------- vincular / desvincular
async def test_vincular_valid_id_links_and_imports(cmds, repo):
    _, _, env = cmds.fakes
    env.wishlist.set(SID, [10, 11])
    i = FakeInteraction(5)
    await cmds.steam_vincular(i, f"https://steamcommunity.com/profiles/{SID}")
    assert (await repo.get_steam_link(5))["steam_id64"] == SID
    assert i.response.deferred == [True]
    assert "vinculada" in i.text and "2 jogo(s)" in i.text
    assert set(await repo.monitored_map()) == {10, 11}
    i.assert_all_ephemeral()


async def test_vincular_rejects_invalid_and_vanity(cmds, repo):
    for text, expected in (("123", "inválido"), ("https://steamcommunity.com/id/gaben", "personalizadas")):
        i = FakeInteraction(5)
        await cmds.steam_vincular(i, text)
        assert expected in i.text
        i.assert_all_ephemeral()
    assert await repo.get_steam_link(5) is None


async def test_vincular_private_wishlist_explains(cmds, repo):
    _, _, env = cmds.fakes
    env.wishlist.set(SID, [], empty_or_private=True)
    i = FakeInteraction(5)
    await cmds.steam_vincular(i, SID)
    assert "vazia ou privada" in i.text


async def test_desvincular_removes_link_and_wishlist_but_keeps_manual(cmds, repo):
    await repo.set_steam_link(1, SID)
    await repo.sync_wishlist_sources(1, [10, 11])
    await repo.add_manual(1, 12, "Twelve")
    i = FakeInteraction(1)
    await cmds.steam_desvincular(i)
    assert await repo.get_steam_link(1) is None
    assert set(await repo.monitored_map()) == {12}
    assert "2 jogo(s)" in i.text
    i.assert_all_ephemeral()
    again = FakeInteraction(1)
    await cmds.steam_desvincular(again)
    assert "nenhuma conta" in again.text


# ----------------------------------------------------------------- wishlist listing
async def test_wishlist_empty_message(cmds):
    i = FakeInteraction(1)
    await cmds.wishlist(i)
    assert "não monitora" in i.text
    i.assert_all_ephemeral()


async def test_wishlist_truncates_to_25_with_count_and_shows_db_prices(cmds, repo):
    for n in range(30):
        await repo.add_manual(1, 100 + n, f"Game {n:02d}")
    await repo.add_manual(2, 999, "Someone else")
    snap = DealSnapshot(100, "Game 00", 1674, 6699, 75, 1800000000, True)
    from steamdeals.models import CycleChanges, StateUpdate
    await repo.apply_cycle(CycleChanges(states=[StateUpdate(snap, set_signature=True)]))
    i = FakeInteraction(1)
    await cmds.wishlist(i)
    embed = i.response.messages[0]["embed"]
    lines = embed.description.splitlines()
    assert len(lines) == MAX_LIST_ITEMS + 1
    assert lines[-1] == "... e mais 5 jogo(s)."
    assert "(30)" in embed.title
    assert "R$ 16,74 (-75%)" in lines[0] and "Game 00" in lines[0]  # on-sale first
    assert "Someone else" not in embed.description
    assert "aguardando verificação" in lines[1]
    i.assert_all_ephemeral()


# ----------------------------------------------------------------- wishlist_add / remover
async def test_wishlist_add_numeric_validates_with_store(cmds, repo):
    store, _, _ = cmds.fakes
    store.items[620] = store_item(620, name="Portal 2")
    i = FakeInteraction(1)
    await cmds.wishlist_add(i, "620")
    assert "Portal 2" in i.text and "adicionado" in i.text
    assert (await repo.monitored_map())[620]["manual"] == {1}
    assert i.response.deferred == [True]
    i.assert_all_ephemeral()
    dup = FakeInteraction(1)
    await cmds.wishlist_add(dup, "620")
    assert "já está" in dup.text


async def test_wishlist_add_rejects_bad_input_unknown_app_and_steam_failure(cmds, repo):
    store, _, _ = cmds.fakes
    for bad in ("portal", "-5", "0", ""):
        i = FakeInteraction(1)
        await cmds.wishlist_add(i, bad)
        assert "AppID numérico" in i.text
    i = FakeInteraction(1)
    await cmds.wishlist_add(i, "123456")
    assert "Nenhum jogo encontrado" in i.text
    store.error = SteamUnavailable("down")
    i = FakeInteraction(1)
    await cmds.wishlist_add(i, "620")
    assert "Não foi possível consultar a Steam" in i.text
    assert await repo.monitored_map() == {}


async def test_wishlist_add_enforces_manual_limit(cmds, repo):
    for n in range(MAX_MANUAL_GAMES):
        await repo.add_manual(1, 1000 + n, "x")
    i = FakeInteraction(1)
    await cmds.wishlist_add(i, "620")
    assert "Limite" in i.text


async def test_wishlist_remover_manual_wishlist_and_missing(cmds, repo):
    await repo.add_manual(1, 10, "Ten")
    await repo.sync_wishlist_sources(1, [20])
    i = FakeInteraction(1)
    await cmds.wishlist_remover(i, "10")
    assert "removido" in i.text and 10 not in await repo.monitored_map()
    i = FakeInteraction(1)
    await cmds.wishlist_remover(i, "20")
    assert "wishlist da Steam" in i.text and 20 in await repo.monitored_map()
    i = FakeInteraction(1)
    await cmds.wishlist_remover(i, "30")
    assert "não está" in i.text
    i = FakeInteraction(1)
    await cmds.wishlist_remover(i, "abc")
    i.assert_all_ephemeral()


async def test_users_cannot_remove_each_others_games(cmds, repo):
    await repo.add_manual(1, 10, "Ten")
    i = FakeInteraction(2)
    await cmds.wishlist_remover(i, "10")
    assert 10 in await repo.monitored_map()


# ----------------------------------------------------------------- importar
async def test_importar_requires_link_then_applies_cooldown(cmds, repo, clock):
    _, _, env = cmds.fakes
    i = FakeInteraction(1)
    await cmds.wishlist_importar(i)
    assert "não vinculou" in i.text

    await repo.set_steam_link(1, SID)
    env.wishlist.set(SID, [1, 2, 3])
    i = FakeInteraction(1)
    await cmds.wishlist_importar(i)
    assert "3 jogo(s)" in i.text and i.response.deferred == [True]

    i = FakeInteraction(1)
    await cmds.wishlist_importar(i)
    assert "Aguarde" in i.text and len(env.wishlist.calls) == 1
    other = FakeInteraction(2)
    await repo.set_steam_link(2, SID)
    await cmds.wishlist_importar(other)  # cooldown is per user
    assert len(env.wishlist.calls) == 2

    clock.now += IMPORT_COOLDOWN_SECONDS + 1
    i = FakeInteraction(1)
    await cmds.wishlist_importar(i)
    assert "3 jogo(s)" in i.text and len(env.wishlist.calls) == 3
    i.assert_all_ephemeral()


async def test_importar_handles_steam_errors(cmds, repo):
    _, _, env = cmds.fakes
    await repo.set_steam_link(1, SID)
    env.wishlist.data[SID] = SteamUnavailable("down")
    i = FakeInteraction(1)
    await cmds.wishlist_importar(i)
    assert "Não foi possível" in i.text
    i.assert_all_ephemeral()


# ----------------------------------------------------------------- ofertas
async def test_ofertas_lists_top_discounts_and_clamps_quantity(cmds, repo):
    from steamdeals.models import CycleChanges, StateUpdate
    states = [
        StateUpdate(DealSnapshot(n, f"Game {n}", 1000, 2000, 10 + n, 1800000000, True), set_signature=True)
        for n in range(1, 21)
    ]
    await repo.apply_cycle(CycleChanges(states=states))
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i, 50)
    lines = i.response.messages[0]["embed"].description.splitlines()
    assert len(lines) == 15
    assert "Game 20" in lines[0] and "-30%" in lines[0]  # highest discount first
    i.assert_all_ephemeral()
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i)
    assert len(i.response.messages[0]["embed"].description.splitlines()) == 10
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i, 0)
    assert len(i.response.messages[0]["embed"].description.splitlines()) == 1


async def test_ofertas_empty(cmds):
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i, 5)
    assert "Ainda não há ofertas" in i.text


# ----------------------------------------------------------------- status / permissions
async def test_status_hides_error_details_from_non_admins(cmds, repo):
    _, _, env = cmds.fakes
    await repo.begin_check("specials")
    await repo.finish_check("specials", "error", "SteamUnavailable: secret internal detail")
    env.monitor.last_summary = CycleSummary(datetime.now(timezone.utc), errors=["specials: SteamUnavailable"])
    await repo.add_manual(1, 5, "x")
    await repo.enqueue_notification(NewNotification(DealSnapshot(5, "x", 1, 2, 50, None, True), {"manual"}, {1}))

    user = FakeInteraction(1, manage_guild=False)
    await cmds.steam_status(user)
    assert "falhou" in user.text and "Fila pendente: 1" in user.text and "Jogos monitorados: 1" in user.text
    assert "secret internal detail" not in user.text and "Erros do último ciclo" not in user.text
    user.assert_all_ephemeral()

    admin = FakeInteraction(2, manage_guild=True)
    await cmds.steam_status(admin)
    assert "secret internal detail" in admin.text and "Erros do último ciclo" in admin.text
    admin.assert_all_ephemeral()


async def test_status_without_data_and_next_cycle(cmds, repo):
    cmds.next_cycle = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
    i = FakeInteraction(1)
    await cmds.steam_status(i)
    assert "ainda não verificado" in i.text and "<t:1893456000:R>" in i.text


# ----------------------------------------------------------------- autocomplete
async def test_autocomplete_suggest_limits_and_format(cmds):
    _, suggest, _ = cmds.fakes
    suggest.items = [SuggestItem(n, "N" * 150, "R$ 10,00") for n in range(1, 40)]
    choices = await cmds.autocomplete_suggest(FakeInteraction(), "portal")
    assert len(choices) == 25
    assert all(len(c.name) <= 100 and c.value == str(i) for i, c in enumerate(choices, start=1))
    suggest.items = []
    assert await cmds.autocomplete_suggest(FakeInteraction(), "zzz") == []


async def test_autocomplete_manual_only_own_manual_games(cmds, repo):
    await repo.add_manual(1, 10, "Portal 2")
    await repo.add_manual(1, 11, "Half-Life")
    await repo.add_manual(2, 12, "Portal Knights")
    await repo.sync_wishlist_sources(1, [13])
    choices = await cmds.autocomplete_manual(FakeInteraction(1), "portal")
    assert [(c.name, c.value) for c in choices] == [("Portal 2", "10")]
    all_choices = await cmds.autocomplete_manual(FakeInteraction(1), "")
    assert {c.value for c in all_choices} == {"10", "11"}


# ----------------------------------------------------------------- embeds
def payload(**kw):
    base = {
        "appid": 227300, "name": "Euro Truck Simulator 2", "final_cents": 1674, "original_cents": 6699,
        "discount_pct": 75, "discount_end": 1795539600, "image_url": "https://img/x.jpg",
        "origins": ["specials"], "user_ids": [],
    }
    base.update(kw)
    return base


def test_embed_content():
    e = build_embed(payload())
    fields = {f.name: f.value for f in e.fields}
    assert e.title == "Euro Truck Simulator 2"
    assert e.url == "https://store.steampowered.com/app/227300"
    assert e.image.url == "https://img/x.jpg"
    assert "R$ 66,99" in fields["Preço original"] and "R$ 16,74" in fields["Preço com desconto"]
    assert fields["Desconto"] == "-75%"
    assert fields["Termina"] == "<t:1795539600:R>"
    assert fields["Origem"] == "Ofertas especiais"


def test_embed_without_end_date_has_no_end_field():
    names = [f.name for f in build_embed(payload(discount_end=None)).fields]
    assert "Termina" not in names


def test_origin_labels():
    assert origin_label(["specials"]) == "Ofertas especiais"
    assert origin_label(["wishlist"]) == "Wishlist"
    assert origin_label(["manual"]) == "Wishlist"
    assert origin_label(["specials", "wishlist"]) == "Ambas"
    assert origin_label(["specials", "manual"]) == "Ambas"


def test_mentions_limited_to_ten_users_only():
    users = list(range(1, 16))
    content, allowed = build_mentions(payload(user_ids=users))
    assert content.count("<@") == MAX_MENTIONS and content.endswith("e mais 5")
    assert allowed.everyone is False and allowed.roles is False
    assert [u.id for u in allowed.users] == users[:MAX_MENTIONS]
    assert "@everyone" not in content


def test_no_users_means_no_mentions():
    content, allowed = build_mentions(payload())
    assert content is None
    assert allowed.everyone is False and allowed.users is False and allowed.roles is False
