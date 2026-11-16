"""Review summary in automatic notification embeds (read from the cache at send time)."""

import json

import pytest

from conftest import special, store_item
from steamdeals.discord_bot.offers import build_offer_embed, review_text
from steamdeals.notify.dispatcher import NotificationDispatcher
from steamdeals.notify.embeds import (
    REVIEWS_UNAVAILABLE,
    build_embed,
    format_count_full,
    reviews_field_text,
)
from test_dispatcher import fill, notification

DUMMY = 99999
PAYLOAD = {"appid": 1, "name": "G", "final_cents": 100, "original_cents": 200, "discount_pct": 50,
           "origins": ["specials"], "user_ids": []}


def field(embed, name="Avaliações"):
    return next(f for f in embed.fields if f.name == name)


# ------------------------------------------------------------------ formatting
@pytest.mark.parametrize("n,text", [(0, "0"), (7, "7"), (999, "999"), (1000, "1.000"), (125430, "125.430"),
                                    (9986154, "9.986.154"), (1234567890, "1.234.567.890")])
def test_format_count_full(n, text):
    assert format_count_full(n) == text


@pytest.mark.parametrize(
    "reviews,text",
    [
        ({"review_count": 125430, "percent_positive": 93, "source": "browse"}, "93% positivas · 125.430 avaliações"),
        ({"review_count": 1, "percent_positive": 100, "source": "search"}, "100% positivas · 1 avaliação"),
        ({"review_count": 12, "percent_positive": 0, "source": "search"}, "0% positivas · 12 avaliações"),
        ({"review_count": 9986154, "percent_positive": 85, "source": "browse"}, "85% positivas · 9.986.154 avaliações"),
        ({"review_count": 6707, "percent_positive": 98, "source": "search_partial"},
         "98% positivas · 6.707 avaliações (contagem parcial)"),
        (None, REVIEWS_UNAVAILABLE),
        ({}, REVIEWS_UNAVAILABLE),
        ({"review_count": 0, "percent_positive": 50, "source": "search"}, REVIEWS_UNAVAILABLE),
        ({"review_count": 10, "percent_positive": None, "source": "search"}, REVIEWS_UNAVAILABLE),
    ],
)
def test_reviews_field_text(reviews, text):
    assert reviews_field_text(reviews) == text


def test_embed_field_with_and_without_reviews_and_within_limits():
    with_reviews = build_embed({**PAYLOAD, "reviews": {"review_count": 125430, "percent_positive": 93,
                                                        "source": "browse"}})
    assert field(with_reviews).value == "93% positivas · 125.430 avaliações"
    assert field(with_reviews).inline is False
    without = build_embed(PAYLOAD)
    assert field(without).value == "Avaliações indisponíveis"
    partial = build_embed({**PAYLOAD, "reviews": {"review_count": 6707, "percent_positive": 98,
                                                   "source": "search_partial"}})
    assert field(partial).value.endswith("(contagem parcial)")
    for embed in (with_reviews, without, partial):
        assert len(embed.fields) <= 25 and all(len(f.value) <= 1024 and len(f.name) <= 256 for f in embed.fields)
        assert len(embed) <= 6000


def test_existing_fields_are_unchanged():
    names = [f.name for f in build_embed(PAYLOAD).fields]
    assert names == ["Preço original", "Preço com desconto", "Desconto", "Encontrado em", "Avaliações"]


def test_offers_command_formatting_is_unchanged():
    assert review_text(835_000, 93) == "⭐ 93% positivas · 835 mil avaliações"


# ------------------------------------------------------------------- dispatcher
class Capture:
    def __init__(self):
        self.payloads = []

    async def __call__(self, payload):
        self.payloads.append(payload)
        return 1


async def test_dispatcher_passes_the_cached_reviews_without_persisting_them(repo):
    await repo.upsert_reviews([(1, 125430, 93, "browse")])
    await fill(repo, [1])
    sender = Capture()
    await NotificationDispatcher(repo, sender).dispatch()
    [sent] = sender.payloads
    assert sent["reviews"]["review_count"] == 125430 and sent["reviews"]["source"] == "browse"
    stored = await repo.get_notification(1)
    assert "reviews" not in json.loads(stored["payload"])


async def test_dispatcher_reads_the_freshest_value_at_send_time(repo):
    await fill(repo, [1])
    await repo.upsert_reviews([(1, 10, 50, "search")])
    await repo.upsert_reviews([(1, 20, 60, "search")])  # updated after enqueue, before sending
    sender = Capture()
    await NotificationDispatcher(repo, sender).dispatch()
    assert sender.payloads[0]["reviews"]["review_count"] == 20


async def test_dispatcher_without_cached_reviews_sends_none(repo):
    await fill(repo, [1])
    sender = Capture()
    await NotificationDispatcher(repo, sender).dispatch()
    assert sender.payloads[0]["reviews"] is None
    assert field(build_embed(sender.payloads[0])).value == REVIEWS_UNAVAILABLE


async def test_cache_read_failure_never_blocks_the_notification(repo, monkeypatch, caplog):
    await fill(repo, [1, 2])

    async def boom(appid):
        raise RuntimeError("db locked")

    monkeypatch.setattr(repo, "get_reviews", boom)
    sender = Capture()
    result = await NotificationDispatcher(repo, sender).dispatch()
    assert result.sent == 2 and result.failed == 0
    assert all(p["reviews"] is None for p in sender.payloads)
    assert "Could not read cached reviews" in caplog.text
    assert await repo.count_notifications("sent") == 2


# ------------------------------------------- end to end: both origins, signature intact
async def baseline(env):
    env.specials.items = [special(DUMMY)]
    await env.cycle()


async def test_specials_and_wishlist_origins_both_show_the_field_and_signature_is_unchanged(env, repo):
    from dataclasses import replace

    await baseline(env)
    env.monitor.dispatcher = NotificationDispatcher(repo, sender := Capture(), 20)
    await repo.add_manual(1, 100, "Wished")
    env.store.items[100] = replace(store_item(100), review_count=5000, percent_positive=95)
    env.specials.items = [special(DUMMY), replace(special(200), review_count=700, percent_positive=88)]
    summary = await env.cycle()
    assert summary.enqueued == 2 and summary.dispatched == 2
    by_app = {p["appid"]: p for p in sender.payloads}
    assert field(build_embed(by_app[100])).value == "95% positivas · 5.000 avaliações"
    assert field(build_embed(by_app[200])).value == "88% positivas · 700 avaliações"
    rows = [await repo.get_notification(i) for i in (1, 2)]
    for row in rows:
        assert "reviews" not in json.loads(row["payload"])
    # same prices next cycle: no new notification (signature untouched by the new data)
    again = await env.cycle()
    assert again.enqueued == 0 and again.dispatched == 0


async def test_offers_view_embed_is_unaffected_by_the_notification_field():
    embed = build_offer_embed({"appid": 1, "name": "G", "final_cents": 1, "original_cents": 2,
                               "discount_pct": 50, "review_count": 1234, "percent_positive": 90})
    assert "Avaliações" not in [f.name for f in embed.fields]
    assert embed.footer.text == "⭐ 90% positivas · 1,2 mil avaliações"
