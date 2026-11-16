"""Review summaries: parsing, schema v4, cache precedence and capture inside the cycle."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from conftest import FakeHttp, FakeWishlist, load_json, special, store_item
from steamdeals.db import migrations
from steamdeals.db.database import Database
from steamdeals.db.repositories import BROWSE_REVIEW_TTL_SECONDS, Repository
from steamdeals.monitor.service import MonitorService
from steamdeals.steam.specials import (
    SpecialsClient,
    is_partial_tooltip,
    parse_results_html,
    parse_review_tooltip,
)
from steamdeals.steam.store import StoreClient, parse_get_items, parse_reviews

SEARCH = load_json("search_results.json")
DUMMY = 99999


# --------------------------------------------------------------- search tooltip
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Muito positivas<br>98% das 2,227 análises de utilizadores", (2227, 98)),
        ("81% das 71.329 análises", (71329, 81)),
        ("95% das 12\u00a0345 análises", (12345, 95)),
        ("95% das 12\u202f345 análises", (12345, 95)),
        ("100% of the 1 234 567 user reviews", (1234567, 100)),
        ("97% das 474 análises", (474, 97)),
        ("98 % das 5 análises", (5, 98)),
        ("Poucas análises de utilizadores", None),
        ("", None),
        (None, None),
        ("98% das 6,707 análises<br><br>A Classificação Geral é calculada a partir do teu idioma", (6707, 98)),
        ("98% das 6,707 análises<br /><br />extra 50% das 9 coisas", (6707, 98)),
        ("<br><br>sem primeiro paragrafo", None),
        ("Muito positivas<br>98% das 2,227 análises<br>", (2227, 98)),
        ("150% das 100 análises", None),
        ("0% das 0 análises", None),
    ],
)
def test_parse_review_tooltip(text, expected):
    assert parse_review_tooltip(text) == expected


def test_rows_expose_reviews_and_missing_tooltip_means_no_data():
    items, rows, skipped = parse_results_html(SEARCH["results_html"])
    by_id = {i.appid: i for i in items}
    plain = by_id[2406770]  # single-paragraph tooltip
    assert (plain.review_count, plain.percent_positive) == (71329, 81)
    assert not plain.review_partial
    # rows whose tooltip has extra paragraphs keep the first-paragraph figures, flagged as partial
    partial = by_id[227300]
    assert (partial.review_count, partial.percent_positive, partial.review_partial) == (6707, 98, True)
    assert by_id[489830].review_partial and by_id[489830].review_count == 474
    assert is_partial_tooltip("a<br><br>b") and not is_partial_tooltip("a<br>b") and not is_partial_tooltip(None)
    no_tooltip = SEARCH["results_html"].replace("search_review_summary", "other_summary")
    items, rows2, _ = parse_results_html(no_tooltip)
    assert rows2 == rows and all(i.review_count is None and i.percent_positive is None for i in items)


def test_unexpected_tooltip_text_never_breaks_parsing():
    html = SEARCH["results_html"].replace("98% das 6,707 análises", "texto inesperado sem números")
    items, rows, _ = parse_results_html(html)
    assert len(items) == rows == 5 and items[0].review_count is None


# --------------------------------------------------------------------- GetItems
def entry(**reviews):
    return {"appid": 1, "name": "G", "success": 1, "reviews": reviews,
            "best_purchase_option": {"final_price_in_cents": "500", "original_price_in_cents": "1000", "discount_pct": 50}}


def test_get_items_reviews_filtered_unfiltered_and_absent():
    filtered = {"review_count": 500, "percent_positive": 90}
    unfiltered = {"review_count": 800, "percent_positive": 70}
    assert parse_reviews(entry(summary_filtered=filtered, summary_unfiltered=unfiltered)) == (500, 90)
    assert parse_reviews(entry(summary_unfiltered=unfiltered)) == (800, 70)
    assert parse_reviews({"appid": 1}) is None
    assert parse_reviews({"reviews": "x"}) is None
    assert parse_reviews(entry(summary_filtered={"review_count": 0, "percent_positive": 0})) is None
    assert parse_reviews(entry(summary_filtered={"review_count": "12", "percent_positive": "88"})) == (12, 88)
    assert parse_reviews(entry(summary_filtered={"review_count": 10, "percent_positive": 101})) is None


def test_get_items_payload_carries_reviews_into_store_items():
    payload = {"response": {"store_items": [entry(summary_filtered={"review_count": 500, "percent_positive": 90})]}}
    item = parse_get_items(payload)[1]
    assert (item.review_count, item.percent_positive) == (500, 90) and item.on_sale
    assert parse_get_items(load_json("getitems.json"))[227300].review_count is None  # fixture predates reviews


# ----------------------------------------------------------------- schema v4
async def table_info(conn):
    async with conn.execute("PRAGMA table_info(game_reviews)") as cur:
        return {r[1]: r for r in await cur.fetchall()}


async def test_v3_database_is_upgraded_to_v4_and_reopening_is_idempotent(tmp_path, monkeypatch):
    path = str(tmp_path / "v3.db")
    conn = await aiosqlite.connect(path, isolation_level=None)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:3])
    assert await migrations.migrate(conn) == 3
    monkeypatch.undo()
    await conn.close()
    db = await Database(path).connect()
    assert set(await table_info(db.conn)) == {"appid", "review_count", "percent_positive", "source", "updated_at"}
    await db.conn.execute("INSERT INTO game_reviews VALUES (1, 10, 90, 'browse', 1)")
    await db.close()
    db = await Database(path).connect()  # already at v4
    async with db.conn.execute("SELECT COUNT(*) FROM game_reviews") as cur:
        assert (await cur.fetchone())[0] == 1
    async with db.conn.execute("SELECT MAX(version) FROM schema_version") as cur:
        assert (await cur.fetchone())[0] == migrations.LATEST_VERSION >= 4
    await db.close()


async def test_v4_failure_rolls_back_and_keeps_version(tmp_path, monkeypatch):
    path = str(tmp_path / "v3.db")
    conn = await aiosqlite.connect(path, isolation_level=None)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:3])
    await migrations.migrate(conn)
    monkeypatch.undo()
    await conn.close()
    broken = (4, "CREATE TABLE game_reviews (appid INTEGER); INSERT INTO missing_table VALUES (1);")
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:3] + [broken])
    with pytest.raises(Exception):
        await Database(path).connect()
    conn = await aiosqlite.connect(path)
    async with conn.execute("SELECT MAX(version) FROM schema_version") as cur:
        assert (await cur.fetchone())[0] == 3
    async with conn.execute("SELECT name FROM sqlite_master WHERE name = 'game_reviews'") as cur:
        assert await cur.fetchone() is None
    await conn.close()


@pytest.mark.parametrize(
    "row",
    [(1, -1, 50, "browse"), (1, 10, 101, "browse"), (1, 10, -1, "search"), (1, 10, 50, "other")],
)
async def test_check_constraints(db, row):
    with pytest.raises(Exception):
        await db.conn.execute("INSERT INTO game_reviews VALUES (?, ?, ?, ?, 1)", row)


# ------------------------------------------------------ cache upsert / precedence
class MutableClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


async def test_search_updates_search_and_browse_is_not_overwritten_by_search(db):
    clock = MutableClock()
    repo = Repository(db, clock=clock)
    await repo.upsert_reviews([(1, 100, 80, "search"), (2, 200, 90, "browse")])
    await repo.upsert_reviews([(1, 150, 85, "search"), (2, 999, 10, "search")])
    assert (await repo.get_reviews(1))["review_count"] == 150
    kept = await repo.get_reviews(2)
    assert (kept["review_count"], kept["source"]) == (200, "browse")


async def test_browse_overwrites_anything_and_stale_browse_yields_to_search(db):
    clock = MutableClock()
    repo = Repository(db, clock=clock)
    await repo.upsert_reviews([(1, 100, 80, "search"), (2, 200, 90, "browse")])
    await repo.upsert_reviews([(1, 120, 82, "browse")])
    assert (await repo.get_reviews(1))["source"] == "browse"
    clock.now += timedelta(seconds=BROWSE_REVIEW_TTL_SECONDS - 10)
    await repo.upsert_reviews([(2, 1, 1, "search")])
    assert (await repo.get_reviews(2))["source"] == "browse"  # still fresh
    clock.now += timedelta(seconds=20)
    await repo.upsert_reviews([(2, 300, 70, "search")])
    row = await repo.get_reviews(2)
    assert (row["review_count"], row["source"]) == (300, "search")


async def test_empty_upsert_is_a_noop_and_missing_rows_keep_previous_data(repo):
    assert await repo.upsert_reviews([]) == 0
    await repo.upsert_reviews([(1, 10, 90, "search")])
    await repo.upsert_reviews([(2, 20, 80, "search")])  # game 1 has no summary this time: untouched
    assert (await repo.get_reviews(1))["review_count"] == 10 and await repo.reviews_cached() == 2


# ------------------------------------------------------------ cycle integration
def with_reviews(item, count, pct):
    return replace(item, review_count=count, percent_positive=pct)


async def baseline(env):
    env.specials.items = [special(DUMMY)]
    await env.cycle()


async def test_cycle_persists_search_and_browse_reviews(env):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = with_reviews(store_item(100), 5000, 95)
    env.specials.items = [
        special(DUMMY),
        replace(special(200), review_count=700, percent_positive=88),
        special(300),  # no summary
    ]
    await env.cycle()
    assert (await env.repo.get_reviews(100))["source"] == "browse"
    row = await env.repo.get_reviews(200)
    assert (row["review_count"], row["percent_positive"], row["source"]) == (700, 88, "search")
    assert await env.repo.get_reviews(300) is None


async def test_failure_storing_reviews_does_not_break_cycle_or_notifications(env, monkeypatch, caplog):
    await baseline(env)
    await env.repo.add_manual(1, 100, "G")
    env.store.items[100] = with_reviews(store_item(100), 5000, 95)

    async def boom(rows):
        raise RuntimeError("disk full")

    monkeypatch.setattr(env.repo, "upsert_reviews", boom)
    summary = await env.cycle()
    assert summary.enqueued == 1 and len(await env.pending()) == 1
    assert "reviews: RuntimeError" in summary.errors
    assert "Could not store review summaries" in caplog.text


async def test_capturing_reviews_adds_no_requests(repo):
    """One search page + one GetItems batch, exactly as before the reviews were captured."""
    search_http = FakeHttp(lambda url, params: json.dumps(
        {"success": 1, "results_html": SEARCH["results_html"], "total_count": 5}))
    store_http = FakeHttp(lambda url, params: json.dumps({"response": {"store_items": [
        entry(summary_filtered={"review_count": 500, "percent_positive": 90})]}}))
    wishlist = FakeWishlist()
    monitor = MonitorService(repo, SpecialsClient(search_http, page_delay=0), StoreClient(store_http), wishlist)
    await repo.add_manual(1, 1, "G")
    summary = await monitor.run_cycle()
    assert summary.specials_ok
    assert len(search_http.calls) == 1 and len(store_http.calls) == 1 and wishlist.calls == []
    assert (await repo.get_reviews(2406770))["source"] == "search"
    assert (await repo.get_reviews(1))["source"] == "browse"


# ------------------------------------------------------------ ranked repository
async def test_top_ranked_deals_orders_by_score_and_keeps_games_without_reviews(env):
    await baseline(env)
    env.specials.items = [
        special(DUMMY),
        replace(special(1, final=100, original=1000, pct=90), review_count=None),  # 90% off, unknown
        replace(special(2, pct=20, final=1600, original=2000), review_count=500_000, percent_positive=90),
        replace(special(3, pct=50), review_count=200, percent_positive=95),
    ]
    await env.cycle()
    top = await env.repo.top_ranked_deals(10)
    ids = [d["appid"] for d in top]
    assert ids.index(2) < ids.index(3) < ids.index(1)  # reviews outrank raw discount
    assert 1 in ids and top[ids.index(1)]["review_count"] is None
    assert len(await env.repo.top_ranked_deals(2)) == 2
    assert [d["appid"] for d in await env.repo.top_ranked_deals(10)] == ids  # deterministic


# ------------------------------------------------------- partial (extra paragraph) data
async def test_partial_never_overwrites_complete_or_browse_and_is_overwritten_by_them(db):
    repo = Repository(db, clock=MutableClock())
    await repo.upsert_reviews([(1, 100, 80, "search"), (2, 200, 90, "browse"), (3, 50, 70, "search_partial")])
    await repo.upsert_reviews([(1, 1, 1, "search_partial"), (2, 2, 2, "search_partial"), (3, 60, 71, "search_partial")])
    assert (await repo.get_reviews(1))["source"] == "search" and (await repo.get_reviews(1))["review_count"] == 100
    assert (await repo.get_reviews(2))["source"] == "browse"
    assert (await repo.get_reviews(3))["review_count"] == 60  # partial updates partial
    await repo.upsert_reviews([(3, 5000, 95, "search")])  # complete search replaces partial
    assert (await repo.get_reviews(3))["source"] == "search"
    await repo.upsert_reviews([(3, 9000, 96, "browse")])
    assert (await repo.get_reviews(3))["source"] == "browse"
    await repo.upsert_reviews([(1, 700, 99, "browse")])  # browse replaces everything
    assert (await repo.get_reviews(1))["source"] == "browse"


async def test_stale_browse_is_replaced_by_complete_search_but_not_by_partial(db):
    clock = MutableClock()
    repo = Repository(db, clock=clock)
    await repo.upsert_reviews([(1, 100, 80, "browse")])
    clock.now += timedelta(seconds=BROWSE_REVIEW_TTL_SECONDS + 10)
    await repo.upsert_reviews([(1, 1, 1, "search_partial")])
    assert (await repo.get_reviews(1))["source"] == "browse"
    await repo.upsert_reviews([(1, 300, 70, "search")])
    assert (await repo.get_reviews(1))["source"] == "search"


async def test_cycle_stores_partial_rows_with_their_own_source(env):
    await baseline(env)
    env.specials.items = [
        special(DUMMY),
        replace(special(200), review_count=6707, percent_positive=98, review_partial=True),
        replace(special(201), review_count=700, percent_positive=88),
    ]
    await env.cycle()
    assert (await env.repo.get_reviews(200))["source"] == "search_partial"
    assert (await env.repo.get_reviews(201))["source"] == "search"
    ranked = {d["appid"]: d for d in await env.repo.top_ranked_deals(10)}
    assert ranked[200]["review_source"] == "search_partial" and ranked[200]["review_count"] == 6707


async def test_partial_data_still_ranks_a_known_game_above_one_without_data(env):
    await baseline(env)
    env.specials.items = [
        special(DUMMY),
        replace(special(1, pct=20), review_count=6707, percent_positive=98, review_partial=True),
        special(2, pct=90, final=100, original=1000),  # no data at all
    ]
    await env.cycle()
    ids = [d["appid"] for d in await env.repo.top_ranked_deals(10)]
    assert ids.index(1) < ids.index(2)
