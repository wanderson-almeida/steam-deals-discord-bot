import math

import pytest

from steamdeals import ranking
from steamdeals.ranking import rank_deals, recognition, quality, score, score_components


def test_constants_match_the_documented_formula():
    assert (ranking.W_RECOGNITION, ranking.W_QUALITY, ranking.W_DISCOUNT) == (0.60, 0.25, 0.15)
    assert math.isclose(ranking.W_RECOGNITION + ranking.W_QUALITY + ranking.W_DISCOUNT, 1.0)
    assert ranking.PRIOR_POSITIVE == 0.70 and ranking.PRIOR_WEIGHT == 100


def test_components_are_explained_and_add_up():
    c = score_components(50, 90, 10_000)
    assert math.isclose(c.recognition, math.log10(10_001) / 6)
    adjusted = (0.9 * 10_000 + 70) / 10_100
    assert math.isclose(c.quality, (adjusted - 0.5) / 0.5)
    assert c.discount == 0.5
    assert math.isclose(c.score, 100 * (0.60 * c.recognition + 0.25 * c.quality + 0.15 * c.discount))


@pytest.mark.parametrize("n,expected", [(0, 0.0), (9, 1 / 6), (999_999, 1.0), (10**7, 1.0), (None, 0.0)])
def test_recognition(n, expected):
    assert math.isclose(recognition(n), expected, rel_tol=1e-6)


def test_no_reviews_gives_zero_recognition_and_quality():
    c = score_components(80)
    assert c.recognition == 0 and c.quality == 0 and math.isclose(c.score, 100 * 0.15 * 0.8)
    assert quality(100, 0) == 0 and quality(None, 50) == 0


def test_more_reviews_score_higher_all_else_equal():
    values = [score(50, 90, n) for n in (0, 10, 100, 1_000, 100_000, 1_000_000)]
    assert values == sorted(values) and len(set(values)) == len(values)


def test_quality_is_monotonic_in_percent_and_clamped():
    qs = [quality(p, 5_000) for p in range(0, 101, 10)]
    assert qs == sorted(qs) and all(0 <= q <= 1 for q in qs)
    assert quality(0, 5_000) == 0  # below the 50% line: clamped at 0
    assert quality(100, 10**9) == pytest.approx(1.0, abs=1e-6)


def test_small_samples_are_pulled_towards_the_prior():
    assert quality(100, 3) < quality(90, 5_000)
    assert quality(100, 3) < quality(100, 3_000)  # same percent, more evidence wins
    assert 0 < quality(100, 3) < 0.5


def test_no_reviews_ranks_below_a_known_game_with_a_much_smaller_discount():
    unknown = score(90)
    known = score(10, 80, 1_000)
    assert unknown < known


def test_discount_is_not_the_main_criterion():
    assert score(95) < score(20, 90, 500_000)


def test_zero_reviews_with_a_percent_is_treated_as_no_data():
    assert score(50, 100, 0) == score(50)


def test_rank_is_deterministic_with_tiebreakers():
    base = {"discount_pct": 50, "percent_positive": 90, "review_count": 1_000}
    deals = [
        {"appid": 3, "final_cents": 500, **base},
        {"appid": 1, "final_cents": 500, **base},
        {"appid": 2, "final_cents": 400, **base},  # cheaper wins the tie
    ]
    assert [d["appid"] for d in rank_deals(deals)] == [2, 1, 3]
    assert [d["appid"] for d in rank_deals(list(reversed(deals)))] == [2, 1, 3]


@pytest.fixture
def equal_scores(monkeypatch):
    """Force every deal to the same score so only the tiebreakers decide."""
    fixed = ranking.ScoreComponents(0.5, 0.5, 0.5, 50.0)
    monkeypatch.setattr(ranking, "score_components", lambda *args, **kwargs: fixed)


def test_equal_scores_are_ordered_by_discount_then_price_then_appid(equal_scores):
    deals = [
        {"appid": 9, "final_cents": 100, "discount_pct": 40},
        {"appid": 8, "final_cents": 300, "discount_pct": 60},  # larger discount first
        {"appid": 7, "final_cents": 200, "discount_pct": 60},  # same discount: cheaper first
        {"appid": 5, "final_cents": 200, "discount_pct": 60},  # same price: lower appid first
        {"appid": 6, "final_cents": None, "discount_pct": 60},  # unknown price last among equals
    ]
    assert [d["appid"] for d in rank_deals(deals)] == [5, 7, 8, 6, 9]
    assert [d["appid"] for d in rank_deals(list(reversed(deals)))] == [5, 7, 8, 6, 9]
    keys = [ranking.sort_key(d) for d in rank_deals(deals)]
    assert keys == sorted(keys)


def test_sort_key_compares_discount_before_price_before_appid(equal_scores):
    a = {"appid": 1, "final_cents": 10, "discount_pct": 40}
    b = {"appid": 2, "final_cents": 999, "discount_pct": 41}
    assert ranking.sort_key(b) < ranking.sort_key(a)  # discount beats a cheaper price
    c = {"appid": 3, "final_cents": 10, "discount_pct": 40}
    assert ranking.sort_key(a) < ranking.sort_key(c)  # then price equal -> lower appid


def test_rank_returns_copies_with_components_and_respects_limit():
    deals = [{"appid": i, "final_cents": 100, "discount_pct": i} for i in range(1, 11)]
    top = rank_deals(deals, 3)
    assert [d["appid"] for d in top] == [10, 9, 8]
    assert top[0]["score"].score == score(10) and "score" not in deals[0]


def test_missing_prices_do_not_break_sorting():
    deals = [{"appid": 1, "final_cents": None, "discount_pct": 50}, {"appid": 2, "final_cents": 10, "discount_pct": 50}]
    assert [d["appid"] for d in rank_deals(deals)] == [2, 1]
