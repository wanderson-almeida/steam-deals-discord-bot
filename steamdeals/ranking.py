"""Relevance ranking of discounted games (presentation only).

``score = 100 * (W_RECOGNITION * recognition + W_QUALITY * quality + W_DISCOUNT * discount)``

* **recognition** = ``min(1, log10(n + 1) / RECOGNITION_LOG_SCALE)`` where ``n`` is the review
  count: how well known the game is. 1,000,000 reviews saturate it.
* **quality** = Bayesian-adjusted positive ratio, so a small sample is pulled towards a prior
  instead of being trusted: ``adj = (p * n + PRIOR_POSITIVE * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)``
  with ``p = percent_positive / 100``, then ``clamp((adj - 0.5) / 0.5, 0, 1)``. With no reviews
  (``n = 0``) quality is 0.
* **discount** = ``percent / 100``.

There is deliberately no minimum number of reviews and no minimum positive percentage: a game
without reviews is eligible, it just scores lower (recognition 0, quality 0). Ties are broken by
larger discount, then lower final price, then lower appid, so the order is deterministic.

Everything here is a pure function; the weights are module constants.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

W_RECOGNITION = 0.60
W_QUALITY = 0.25
W_DISCOUNT = 0.15
# log10(n + 1) / RECOGNITION_LOG_SCALE reaches 1 at 10**6 reviews.
RECOGNITION_LOG_SCALE = 6.0
# Prior positive ratio and its weight (as a number of virtual reviews) for the quality estimate.
PRIOR_POSITIVE = 0.70
PRIOR_WEIGHT = 100


@dataclass(frozen=True)
class ScoreComponents:
    recognition: float
    quality: float
    discount: float
    score: float


def recognition(review_count: int | None) -> float:
    n = max(0, review_count or 0)
    return min(1.0, math.log10(n + 1) / RECOGNITION_LOG_SCALE)


def quality(percent_positive: int | None, review_count: int | None) -> float:
    n = max(0, review_count or 0)
    if n == 0 or percent_positive is None:
        return 0.0
    p = min(100, max(0, percent_positive)) / 100
    adjusted = (p * n + PRIOR_POSITIVE * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)
    return min(1.0, max(0.0, (adjusted - 0.5) / 0.5))


def discount_ratio(discount_pct: int | None) -> float:
    return min(1.0, max(0.0, (discount_pct or 0) / 100))


def score_components(
    discount_pct: int | None, percent_positive: int | None = None, review_count: int | None = None
) -> ScoreComponents:
    """The score and each of its (0..1) components, to explain a position."""
    rec = recognition(review_count)
    qual = quality(percent_positive, review_count)
    disc = discount_ratio(discount_pct)
    total = 100 * (W_RECOGNITION * rec + W_QUALITY * qual + W_DISCOUNT * disc)
    return ScoreComponents(rec, qual, disc, total)


def score(discount_pct: int | None, percent_positive: int | None = None, review_count: int | None = None) -> float:
    return score_components(discount_pct, percent_positive, review_count).score


def sort_key(deal: dict) -> tuple:
    """Ascending sort key: best first (score desc, discount desc, final price asc, appid asc)."""
    comp = score_components(deal.get("discount_pct"), deal.get("percent_positive"), deal.get("review_count"))
    final = deal.get("final_cents")
    return (-round(comp.score, 9), -(deal.get("discount_pct") or 0), final if final is not None else 2**62,
            deal["appid"])


def rank_deals(deals: list[dict], limit: int | None = None) -> list[dict]:
    """Deals (dicts with discount_pct, final_cents, appid and optional review fields) best first.

    Each returned dict is a copy with a ``score`` key holding the :class:`ScoreComponents`."""
    ranked = []
    for deal in deals:
        item = dict(deal)
        item["score"] = score_components(
            deal.get("discount_pct"), deal.get("percent_positive"), deal.get("review_count")
        )
        ranked.append(item)
    ranked.sort(key=sort_key)
    return ranked if limit is None else ranked[:limit]
