"""/wishlist: price ordering, pagination view, permissions, timeout and Discord limits."""

import discord
import pytest

from steamdeals.discord_bot.wishlist_view import (
    MAX_DESCRIPTION,
    MAX_EMBED_CHARS,
    PAGE_SIZE,
    WishlistView,
    build_pages,
    game_line,
    price_text,
    sort_games,
)
from steamdeals.models import CycleChanges, DealSnapshot, StateUpdate
from steamdeals.discord_bot.commands import SteamCommands
from test_commands_embeds import Clock, FakeInteraction, FakeStore, FakeSuggest
from test_offers_command import ClickInteraction, OriginalMessage, http_error


@pytest.fixture
def cmds(repo, env):
    return SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock())


def game(n, final=1000, name=None, **over):
    base = {"appid": n, "name": name or f"Game {n}", "final_cents": final,
            "original_cents": None if final is None else final, "discount_pct": 0, "on_sale": False,
            "updated_at": None if final is None else "2026-01-01T00:00:00", "sources": {"wishlist"}}
    return {**base, **over}


def names(games):
    return [g["name"] for g in sort_games(games)]


# ------------------------------------------------------------------ ordering
def test_sorted_by_ascending_price():
    games = [game(1, 3000, "C"), game(2, 500, "A"), game(3, 1500, "B")]
    assert names(games) == ["A", "B", "C"]


def test_free_first_and_unknown_price_last():
    games = [game(1, None, "Unknown"), game(2, 1000, "Paid"), game(3, 0, "Free"), game(4, None, "Aaa unknown")]
    assert names(games) == ["Free", "Paid", "Aaa unknown", "Unknown"]


def test_ties_break_alphabetically_ignoring_case_then_appid():
    games = [game(3, 1000, "banana"), game(2, 1000, "Apple"), game(5, 1000, "cherry"),
             game(9, 1000, "same"), game(4, 1000, "same")]
    assert names(games) == ["Apple", "banana", "cherry", "same", "same"]
    assert [g["appid"] for g in sort_games(games)][-2:] == [4, 9]


def test_sorting_uses_cents_not_text():
    assert names([game(1, 10000, "big"), game(2, 999, "small")]) == ["small", "big"]


def test_sort_does_not_mutate_input():
    games = [game(1, 2000), game(2, 1000)]
    sort_games(games)
    assert [g["appid"] for g in games] == [1, 2]


# ------------------------------------------------------------------ line text
def test_price_texts():
    sale = game(1, 1674, original_cents=6699, discount_pct=75, on_sale=True)
    assert price_text(sale) == "~~R$ 66,99~~ **R$ 16,74** (-75%)"
    assert price_text(game(1, 4990)) == "**R$ 49,90** · sem promoção"
    assert price_text(game(1, 0)) == "**R$ 0,00** · gratuito"
    assert "gratuito" in price_text(game(1, 0, original_cents=3000, discount_pct=100, on_sale=True))
    assert price_text(game(1, None)) == "aguardando verificação"
    assert price_text(game(1, None, updated_at="2026-01-01T00:00:00")) == "preço indisponível"


def test_line_has_number_link_and_manual_tag():
    line = game_line(7, game(570, 1000, "Dota", sources={"manual"}))
    assert line.startswith("` 7.`") and "[Dota](https://store.steampowered.com/app/570)" in line
    assert line.endswith("· manual")
    assert "manual" not in game_line(1, game(570, 1000, sources={"wishlist"}))
    assert "manual" not in game_line(1, game(570, 1000, sources={"wishlist", "manual"}))


@pytest.mark.parametrize("name", ["[x](http://evil)", "**b**", "@everyone", "<@1>", "||s||"])
def test_names_cannot_inject_markdown_or_links(name):
    line = game_line(1, game(1, 1000, name))
    link_text = line.split("](https://store.steampowered.com/app/1)")[0].split(" [", 1)[1]
    assert "[" not in link_text and "]" not in link_text  # the name cannot close the link early
    assert line.count("](") == 1


# ---------------------------------------------------------------- pagination
@pytest.mark.parametrize("total,pages", [(1, 1), (10, 1), (11, 2), (25, 3), (100, 10), (101, 11)])
def test_page_counts_include_every_game(total, pages):
    built = build_pages([game(n, n * 10) for n in range(1, total + 1)])
    assert len(built) == pages
    text = "\n".join(e.description for page in built for e in page)
    assert text.count("store.steampowered.com/app/") == total
    assert all(len(page) == 1 for page in built)
    assert built[-1][0].footer.text == f"Página {pages}/{pages}"


def test_numbering_is_global_and_header_has_total_and_sort():
    built = build_pages([game(n, n * 10) for n in range(1, 26)])
    second = built[1][0]
    assert second.title == "Minha Wishlist — Steam"
    assert "**25** jogo(s)" in second.description and "menor preço" in second.description
    assert "`11.`" in second.description and "`20.`" in second.description


def test_pagination_keeps_global_sort_across_pages():
    games = [game(n, (50 - n) * 10, f"G{n:02d}") for n in range(1, 31)]
    built = build_pages(games)
    flat = [ln for p in built for ln in p[0].description.splitlines() if ln.startswith("`")]
    prices = [int(ln.split("R$ ")[1].split("**")[0].replace(",", "").replace(" ", "")) for ln in flat]
    assert prices == sorted(prices)


def test_embed_limits_with_worst_case_names():
    games = [game(n, 1000 + n, "𝕏" * 400, original_cents=999999999, discount_pct=99, on_sale=True,
                  sources={"manual"}) for n in range(1, 21)]
    for page in build_pages(games):
        embed = page[0]
        assert len(embed.description) <= MAX_DESCRIPTION
        assert len(embed) <= MAX_EMBED_CHARS  # discord.py counts title+description+footer
        assert len(embed.title) <= 256 and len(embed.footer.text) <= 2048
    assert PAGE_SIZE == 10


def test_empty_list_yields_a_single_page():
    assert len(build_pages([])) == 1


# ---------------------------------------------------------------- command
async def seed(repo, count, user=1):
    for n in range(count):
        await repo.add_manual(user, 1000 + n, f"Game {n:02d}")
    states = [StateUpdate(DealSnapshot(1000 + n, f"Game {n:02d}", 100 + 10 * (count - n), 5000, 10, None, True),
                          set_signature=True) for n in range(0, count, 2)]
    await repo.apply_cycle(CycleChanges(states=states))


async def run(cmds, user=1):
    i = FakeInteraction(user)
    await cmds.wishlist(i)
    return i.response.messages[0]["view"], i


async def test_command_sends_sorted_first_page_with_view_and_no_content(cmds, repo):
    await seed(repo, 23)
    view, i = await run(cmds)
    message = i.response.messages[0]
    assert isinstance(view, WishlistView) and message["content"] is None and message["ephemeral"] is True
    assert view.interaction is i and view.timeout == 180
    assert view.previous.disabled and not view.next.disabled and len(view.pages) == 3


async def test_single_page_disables_both_buttons(cmds, repo):
    await seed(repo, 4)
    view, _ = await run(cmds)
    assert len(view.pages) == 1 and view.previous.disabled and view.next.disabled


async def test_unpriced_games_stay_listed_at_the_end(cmds, repo):
    await seed(repo, 6)
    view, _ = await run(cmds)
    lines = [ln for ln in view.pages[0][0].description.splitlines() if ln.startswith("`")]
    assert len(lines) == 6
    assert "aguardando verificação" in lines[-1] and "R$" in lines[0]


async def test_only_own_games_are_listed(cmds, repo):
    await seed(repo, 3, user=1)
    await repo.add_manual(2, 9999, "Other user")
    view, _ = await run(cmds)
    assert "Other user" not in view.pages[0][0].description


async def test_navigation_first_middle_last_edits_message_without_new_queries(cmds, repo):
    await seed(repo, 25)
    view, _ = await run(cmds)
    calls = []
    original = repo.user_games

    async def spy(*a, **k):
        calls.append(1)
        return await original(*a, **k)

    repo.user_games = spy
    click = ClickInteraction(1)
    await view.next.callback(click)
    assert view.page == 1 and not view.previous.disabled and not view.next.disabled
    assert click.response.edits[0]["embeds"] == view.pages[1]
    await view.next.callback(ClickInteraction(1))
    assert view.page == 2 and view.next.disabled
    await view.next.callback(ClickInteraction(1))
    assert view.page == 2
    await view.previous.callback(ClickInteraction(1))
    await view.previous.callback(ClickInteraction(1))
    await view.previous.callback(ClickInteraction(1))
    assert view.page == 0 and view.previous.disabled
    assert calls == []


async def test_snapshot_is_not_affected_by_later_changes(cmds, repo):
    await seed(repo, 12)
    view, _ = await run(cmds)
    before = [e.description for p in view.pages for e in p]
    await repo.add_manual(1, 777777, "Added later")
    await view.next.callback(ClickInteraction(1))
    assert [e.description for p in view.pages for e in p] == before


async def test_only_the_author_may_navigate(cmds, repo):
    await seed(repo, 12)
    view, _ = await run(cmds, user=1)
    stranger = ClickInteraction(2)
    assert await view.interaction_check(stranger) is False
    assert stranger.response.sent[0]["ephemeral"] is True
    assert await view.interaction_check(ClickInteraction(1)) is True


async def test_timeout_disables_buttons_and_survives_expired_interactions(cmds, repo):
    await seed(repo, 12)
    view, _ = await run(cmds)
    original = OriginalMessage()
    view.interaction = original
    await view.on_timeout()
    assert all(c.disabled for c in view.children) and original.edited == [{"view": view}]
    view.interaction = OriginalMessage(fail=http_error(404))
    await view.on_timeout()  # expired/deleted message: logged only
    await view.next.callback(ClickInteraction(1, fail=http_error(404)))  # edit fails: no exception
    view.interaction = None
    await view.on_timeout()


async def test_empty_wishlist_message_has_no_view(cmds):
    i = FakeInteraction(1)
    await cmds.wishlist(i)
    assert "view" not in i.response.messages[0] and "não monitora" in i.text
