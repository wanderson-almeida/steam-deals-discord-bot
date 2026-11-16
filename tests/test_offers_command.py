"""/steam_ofertas: ranked embeds, pagination view, Discord limits."""

import discord
import pytest

from steamdeals.discord_bot.commands import SteamCommands
from steamdeals.discord_bot.offers import (
    MAX_EMBED_CHARS_PER_MESSAGE,
    MAX_GAMES,
    NO_REVIEWS_TEXT,
    PAGE_SIZE,
    REVIEWS_NOT_LOADED_NOTICE,
    OffersView,
    build_offer_embed,
    build_pages,
    compact_count,
    page_content,
    review_text,
)
from steamdeals.models import CycleChanges, DealSnapshot, StateUpdate
from test_commands_embeds import Clock, FakeInteraction, FakeStore, FakeSuggest


# ------------------------------------------------------------------ fakes
class ViewResponse:
    def __init__(self, fail=None):
        self.edits, self.sent, self.fail = [], [], fail

    async def edit_message(self, **kwargs):
        if self.fail:
            raise self.fail
        self.edits.append(kwargs)

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class ClickInteraction:
    def __init__(self, user_id, fail=None):
        self.user = type("U", (), {"id": user_id})()
        self.response = ViewResponse(fail)
        self.edited = []

    async def edit_original_response(self, **kwargs):
        self.edited.append(kwargs)


class OriginalMessage:
    def __init__(self, fail=None):
        self.edited, self.fail = [], fail

    async def edit_original_response(self, **kwargs):
        if self.fail:
            raise self.fail
        self.edited.append(kwargs)


def http_error(status):
    return discord.HTTPException(type("R", (), {"status": status, "reason": "x"})(), "boom")


def deal(n, **over):
    base = {"appid": n, "name": f"Game {n}", "final_cents": 1000, "original_cents": 2000, "discount_pct": 50,
            "discount_end": None, "image_url": None, "review_count": 1000 * n, "percent_positive": 90}
    return {**base, **over}


async def seed(repo, count, reviews=True):
    states = [
        StateUpdate(DealSnapshot(n, f"Game {n}", 1000, 2000, 10 + (n % 80), 1800000000 if n % 2 else None, True),
                    set_signature=True)
        for n in range(1, count + 1)
    ]
    await repo.apply_cycle(CycleChanges(states=states))
    if reviews:
        await repo.upsert_reviews([(n, 100 * n, 80 + n % 20, "search") for n in range(1, count + 1)])


@pytest.fixture
def cmds(repo, env):
    return SteamCommands(repo, env.monitor, FakeStore(), FakeSuggest(), clock=Clock())


# --------------------------------------------------------------- formatting
@pytest.mark.parametrize(
    "n,text",
    [(0, "0"), (835, "835"), (1000, "1 mil"), (2227, "2,2 mil"), (9999, "10 mil"), (10_000, "10 mil"),
     (835_000, "835 mil"), (835_400, "835 mil"), (999_600, "1 mi"), (1_000_000, "1 mi"), (1_234_567, "1,2 mi"),
     (12_000_000, "12 mi")],
)
def test_compact_count(n, text):
    assert compact_count(n) == text


def test_review_text():
    assert review_text(835_000, 93) == "⭐ 93% positivas · 835 mil avaliações"
    assert review_text(1, 100) == "⭐ 100% positivas · 1 avaliação"
    assert review_text(None, None) == NO_REVIEWS_TEXT == "Sem avaliações suficientes"
    assert review_text(0, 50) == NO_REVIEWS_TEXT


# ------------------------------------------------------------------- embed
def test_embed_has_prices_discount_end_image_and_reviews():
    embed = build_offer_embed(deal(1, discount_end=1800000000, image_url="https://img/x.jpg"))
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Preço original"] == "~~R$ 20,00~~" and fields["Preço com desconto"] == "**R$ 10,00**"
    assert fields["Desconto"] == "-50%" and fields["Termina"] == "<t:1800000000:R>"
    assert embed.image.url == "https://img/x.jpg" and embed.url.endswith("/app/1")
    assert embed.footer.text == "⭐ 90% positivas · 1 mil avaliações"


def test_embed_without_end_image_or_reviews_renders():
    embed = build_offer_embed(deal(7, review_count=None, percent_positive=None))
    assert "Termina" not in [f.name for f in embed.fields]
    assert embed.image.url.endswith("/steam/apps/7/header.jpg")
    assert embed.footer.text == NO_REVIEWS_TEXT


# ----------------------------------------------------------------- limits
def test_discord_limits_with_worst_case_data():
    deals = [deal(n, name="N" * 400, discount_end=1800000000, final_cents=10**9, original_cents=10**9 + 1,
                  review_count=10**9, percent_positive=100, image_url="https://x/" + "a" * 500)
             for n in range(1, 80)]
    pages = build_pages(deals)
    assert len(pages) == 10 and all(len(p) <= PAGE_SIZE <= 10 for p in pages)
    assert sum(len(p) for p in pages) == MAX_GAMES
    for page in pages:
        assert sum(len(e) for e in page) <= MAX_EMBED_CHARS_PER_MESSAGE
        for e in page:
            assert len(e.title) <= 256 and len(e.fields) <= 25
            assert all(len(f.value) <= 1024 and len(f.name) <= 256 for f in e.fields)
            assert len(e.footer.text) <= 2048
    assert len(page_content(9, 10, True)) <= 2000


# ----------------------------------------------------------------- command
async def test_empty_state_keeps_the_old_message(cmds):
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i)
    assert "Ainda não há ofertas" in i.text
    i.assert_all_ephemeral()


async def test_first_page_has_five_embeds_ephemeral_and_page_indicator(cmds, repo):
    await seed(repo, 60)
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i)
    [message] = i.response.messages
    assert message["ephemeral"] is True and len(message["embeds"]) == 5
    assert message["content"] == page_content(0, 10)
    assert "Página 1/10" in message["content"] and "ordenado por relevância" in message["content"]
    assert isinstance(message["view"], OffersView)
    assert REVIEWS_NOT_LOADED_NOTICE not in message["content"]
    ranked = await repo.top_ranked_deals(50)
    assert [e.title for e in message["embeds"]] == [d["name"] for d in ranked[:5]]  # best relevance first


async def test_at_most_fifty_games_ten_pages(cmds, repo):
    await seed(repo, 200)
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i)
    view = i.response.messages[0]["view"]
    assert len(view.pages) == 10 and sum(len(p) for p in view.pages) == 50


async def test_few_games_single_page_disables_both_buttons(cmds, repo):
    await seed(repo, 3)
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i)
    view = i.response.messages[0]["view"]
    assert len(view.pages) == 1 and view.previous.disabled and view.next.disabled


async def test_notice_only_when_no_review_is_cached_and_ranking_falls_back_to_discount(cmds, repo):
    await seed(repo, 8, reviews=False)
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i)
    message = i.response.messages[0]
    assert REVIEWS_NOT_LOADED_NOTICE in message["content"]
    discounts = [int(e.fields[2].value.strip("-%")) for e in message["embeds"]]
    assert discounts == sorted(discounts, reverse=True)
    assert all(e.footer.text == NO_REVIEWS_TEXT for e in message["embeds"])


async def test_games_without_reviews_render_when_others_have_them(cmds, repo):
    await seed(repo, 6)
    await repo.apply_cycle(CycleChanges(states=[
        StateUpdate(DealSnapshot(999, "No Reviews", 100, 1000, 90, None, True), set_signature=True)]))
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i)
    message = i.response.messages[0]
    assert REVIEWS_NOT_LOADED_NOTICE not in message["content"]
    footers = [e.footer.text for p in message["view"].pages for e in p]
    assert NO_REVIEWS_TEXT in footers


# -------------------------------------------------------------------- view
async def make_view(cmds, repo, count=60, author=1):
    await seed(repo, count)
    i = FakeInteraction(author)
    await cmds.steam_ofertas(i)
    return i.response.messages[0]["view"], i


async def test_buttons_navigate_and_disable_at_the_edges(cmds, repo):
    view, _ = await make_view(cmds, repo)
    assert view.previous.disabled and not view.next.disabled
    click = ClickInteraction(1)
    await view.next.callback(click)
    [edit] = click.response.edits
    assert edit["content"] == page_content(1, 10) and len(edit["embeds"]) == 5 and edit["view"] is view
    assert not view.previous.disabled
    for _ in range(8):
        await view.next.callback(ClickInteraction(1))
    assert view.page == 9 and view.next.disabled and not view.previous.disabled
    await view.next.callback(ClickInteraction(1))  # extra click cannot go past the end
    assert view.page == 9
    await view.previous.callback(ClickInteraction(1))
    assert view.page == 8


async def test_only_the_author_may_use_the_buttons(cmds, repo):
    view, _ = await make_view(cmds, repo, author=1)
    stranger = ClickInteraction(2)
    assert await view.interaction_check(stranger) is False
    [denied] = stranger.response.sent
    assert denied["ephemeral"] is True and "Apenas quem executou" in denied["content"]
    assert await view.interaction_check(ClickInteraction(1)) is True


async def test_timeout_disables_buttons_by_editing_the_message(cmds, repo):
    view, _ = await make_view(cmds, repo)
    original = OriginalMessage()
    view.interaction = original
    await view.on_timeout()
    assert all(child.disabled for child in view.children)
    assert original.edited == [{"view": view}]
    assert view.timeout == 180


async def test_timeout_and_clicks_survive_discord_errors(cmds, repo):
    view, _ = await make_view(cmds, repo)
    view.interaction = OriginalMessage(fail=http_error(404))
    await view.on_timeout()  # message deleted: no exception
    click = ClickInteraction(1, fail=http_error(404))
    await view.next.callback(click)  # edit fails: logged, no exception
    view.interaction = None
    await view.on_timeout()


async def test_view_interaction_is_attached_for_the_timeout_edit(cmds, repo):
    view, i = await make_view(cmds, repo)
    assert view.interaction is i


# ------------------------------------------------------------- markdown in titles
@pytest.mark.parametrize("name", ["@everyone", "<@123>", "**x**", "_y_", "~~z~~", "`code`", "||spoiler||"])
def test_game_names_are_markdown_escaped_in_titles(name):
    embed = build_offer_embed(deal(1, name=name))
    assert embed.title == discord.utils.escape_markdown(name)
    for marker in ("**", "~~", "||", "`"):
        if marker in name:
            assert marker not in embed.title.replace("\\" + marker[0], "")  # every marker is escaped


def test_escaped_title_is_still_cut_at_256():
    embed = build_offer_embed(deal(1, name="*" * 400))
    assert len(embed.title) == 256


async def test_message_content_has_no_user_supplied_text(cmds, repo):
    await seed(repo, 3)
    await repo.apply_cycle(CycleChanges(states=[
        StateUpdate(DealSnapshot(500, "@everyone <@123> **x**", 100, 1000, 90, None, True), set_signature=True)]))
    i = FakeInteraction(1)
    await cmds.steam_ofertas(i)
    content = i.response.messages[0]["content"]
    assert "@everyone" not in content and "<@123>" not in content


# -------------------------------------------------- timeout uses the newest token
async def test_timeout_edits_through_the_latest_component_interaction(cmds, repo):
    view, _ = await make_view(cmds, repo)
    original = OriginalMessage()
    view.interaction = original

    class Click(ClickInteraction):
        def __init__(self):
            super().__init__(1)

    click = Click()
    await view.next.callback(click)
    await view.on_timeout()
    assert click.edited == [{"view": view}] and original.edited == []
    assert all(child.disabled for child in view.children)


async def test_timeout_falls_back_to_the_original_interaction_when_the_latest_fails(cmds, repo):
    view, _ = await make_view(cmds, repo)
    original = OriginalMessage()
    view.interaction = original

    class Broken(ClickInteraction):
        async def edit_original_response(self, **kwargs):
            raise http_error(401)

    await view.next.callback(Broken(1))
    await view.on_timeout()
    assert original.edited == [{"view": view}]
    view.interaction = OriginalMessage(fail=http_error(404))
    await view.on_timeout()  # both fail: logged only


# ----------------------------------------------------- real discord.py view structure
async def test_real_view_structure_and_callbacks_inside_a_running_loop(cmds, repo):
    view, _ = await make_view(cmds, repo)
    assert isinstance(view, discord.ui.View)
    assert [c.label for c in view.children] == ["Anterior", "Próxima"]
    assert all(isinstance(c, discord.ui.Button) for c in view.children)
    ids = [c.custom_id for c in view.children]
    assert all(isinstance(i, str) and 0 < len(i) <= 100 for i in ids) and len(set(ids)) == 2
    assert [c.disabled for c in view.children] == [True, False]
    assert view.timeout == 180 and not view.is_persistent()
    await view.next.callback(ClickInteraction(1))
    assert [c.disabled for c in view.children] == [False, False]
    await view.previous.callback(ClickInteraction(1))
    assert [c.disabled for c in view.children] == [True, False] and view.page == 0
