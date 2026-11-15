"""Coverage / absence heuristics against incomplete or odd specials listings.

Thresholds under test: ``MIN_PARSED_RATIO`` (90% of total_count), ``MIN_LISTING_RATIO``
(50% of the known listing, with at least ``MIN_KNOWN_FOR_COLLAPSE_CHECK`` known items)
and ``MISSING_CYCLES_TO_END`` (2 consecutive cycles of absence).
"""

import json

import pytest

from conftest import FakeHttp, special
from steamdeals.monitor.service import (
    MIN_KNOWN_FOR_COLLAPSE_CHECK,
    MIN_LISTING_RATIO,
    MISSING_CYCLES_TO_END,
)
from steamdeals.steam.http import SteamRateLimited, SteamSchemaError, SteamUnavailable
from steamdeals.steam.specials import MIN_PARSED_RATIO, SpecialsClient

PAGE = 10
DUMMY = 99999


def row(appid):
    return (
        f'<a class="search_result_row" data-ds-appid="{appid}"><span class="title">Game {appid}</span>'
        '<div class="discount_block" data-price-final="1000" data-discount="50">'
        '<div class="discount_original_price">R$ 20,00</div></div></a>'
    )


def html(appids):
    return "".join(row(a) for a in appids)


class Listing:
    """Fake search endpoint over appids 1..n with per-page overrides."""

    def __init__(self, n=100, total=None, overrides=None, total_for=None):
        self.n = n
        self.total = n if total is None else total
        self.overrides = overrides or {}  # start -> html | Exception | callable(appids)
        self.total_for = total_for or {}  # start -> total_count reported by that page
        self.starts = []

    def __call__(self, url, params):
        start = int(params["start"])
        self.starts.append(start)
        ids = list(range(start + 1, min(start + PAGE, self.n) + 1))
        override = self.overrides.get(start)
        if isinstance(override, BaseException):
            return override
        page_html = html(ids) if override is None else (override(ids) if callable(override) else override)
        total = self.total_for.get(start, self.total)
        return json.dumps({"success": 1, "results_html": page_html, "total_count": total, "start": start})


async def no_sleep(_):
    pass


def client(handler):
    return SpecialsClient(
        FakeHttp(handler), page_size=PAGE, page_delay=0, rate_limit_pause=0, sleep=no_sleep
    )


# ----------------------------------------------------------- fetch_all (pagination)
async def test_complete_listing_is_accepted():
    items = await client(Listing(100)).fetch_all()
    assert len(items) == 100


async def test_last_page_shorter_than_page_size_is_accepted():
    listing = Listing(95)
    assert len(await client(listing).fetch_all()) == 95
    assert listing.starts[-1] == 90


async def test_middle_page_without_rows_fails_the_whole_fetch():
    with pytest.raises(SteamSchemaError):
        await client(Listing(100, overrides={50: "<div>sorry</div>"})).fetch_all()


@pytest.mark.parametrize("error", [SteamRateLimited("429"), SteamUnavailable("down", 503)])
async def test_persistently_failing_middle_page_fails_without_partial_data(error):
    listing = Listing(100, overrides={50: error})
    with pytest.raises(type(error)):
        await client(listing).fetch_all()
    assert 60 not in listing.starts  # gave up, did not skip the page and carry on


@pytest.mark.parametrize(
    "missing,accepted",
    [(0, True), (5, True), (10, True), (11, False), (30, False)],
)
async def test_ninety_percent_coverage_boundary(missing, accepted):
    """``missing`` rows of 100 never reach us (pages returning fewer rows than total_count promises)."""
    overrides = {}
    left = missing
    for start in range(0, 100, PAGE):
        if left <= 0:
            break
        drop = min(left, PAGE - 1)  # keep at least one row so the page is not "empty"
        overrides[start] = lambda ids, d=drop: html(ids[: len(ids) - d])
        left -= drop
    assert MIN_PARSED_RATIO == 0.9
    if accepted:
        assert len(await client(Listing(100, overrides=overrides)).fetch_all()) == 100 - missing
    else:
        with pytest.raises(SteamSchemaError):
            await client(Listing(100, overrides=overrides)).fetch_all()


async def test_total_count_growing_between_pages_is_followed():
    listing = Listing(100, total=100, total_for={start: 105 for start in range(30, 100, PAGE)})
    items = await client(listing).fetch_all()
    assert len(items) == 100  # 100/105 = 95%: accepted


async def test_total_count_growing_a_lot_is_a_failure():
    listing = Listing(100, total=100, total_for={start: 150 for start in range(30, 160, PAGE)})
    with pytest.raises(SteamSchemaError):
        await client(listing).fetch_all()


async def test_total_count_shrinking_mid_fetch_ends_pagination_early():
    listing = Listing(100, total=100, total_for={start: 70 for start in range(50, 100, PAGE)})
    items = await client(listing).fetch_all()
    assert len(items) == 70 and listing.starts[-1] == 60  # honours the latest total_count


async def test_items_shifting_forward_are_deduplicated_without_loss():
    """A new item appears before the cursor: the last item of a page repeats on the next one."""

    def shifted(url, params):
        start = int(params["start"])
        # Page 0 was served before the insertion; later pages are shifted by one (total 101).
        first = 1 if start == 0 else start
        ids = [i for i in range(first, first + PAGE) if i <= 100]
        return json.dumps({"results_html": html(ids), "total_count": 100 if start == 0 else 101})

    assert len(await client(shifted).fetch_all()) == 100  # overlaps collapse into one entry each


async def test_items_shifting_backward_lose_one_item_per_boundary_but_stay_above_threshold():
    """An item before the cursor disappears: one item per page boundary is skipped.

    This is the pagination-shift case the ``missing_cycles`` rule exists for."""

    def shifted(url, params):
        start = int(params["start"])
        ids = [i for i in range(start + 2, start + PAGE + 2) if i <= 100]  # skips one id per page
        return json.dumps({"results_html": html(ids), "total_count": 100})

    items = await client(shifted).fetch_all()
    assert 90 <= len(items) < 100


# ------------------------------------------------------- service-level heuristics
async def baseline(env):
    env.specials.items = [special(DUMMY)]
    await env.cycle()


def items(n, base=1000):
    return [special(base + i) for i in range(n)]


@pytest.mark.parametrize(
    "known,listing,fails",
    [
        (40, 19, True),   # < 50% of known
        (40, 20, False),  # exactly 50% is accepted
        (20, 9, True),    # smallest known size that arms the check
        (20, 10, False),
        (19, 1, False),   # below MIN_KNOWN: the check is not armed
    ],
)
async def test_collapse_threshold(env, known, listing, fails):
    assert MIN_KNOWN_FOR_COLLAPSE_CHECK == 20 and MIN_LISTING_RATIO == 0.5
    await baseline(env)
    env.specials.items = items(known - 1)  # plus the baseline DUMMY, still counted as known
    summary = await env.cycle()
    assert summary.specials_ok
    env.specials.items = items(listing)
    summary = await env.cycle()
    assert summary.specials_ok is (not fails)


async def test_partial_listing_for_one_cycle_does_not_clear_or_reannounce(env):
    await baseline(env)
    full = [special(DUMMY)] + items(50)
    env.specials.items = full
    await env.cycle()
    for n in await env.pending():
        await env.repo.mark_sent(n["id"], 1)
    env.specials.items = full[:-5]  # ~10% lost to a bad page, still above the 50% guard
    await env.cycle()
    env.specials.items = full
    summary = await env.cycle()
    assert summary.enqueued == 0 and await env.pending() == []


async def test_partial_listing_for_missing_cycles_ends_the_games(env):
    """Documents the residual risk: a page lost for MISSING_CYCLES_TO_END cycles in a row
    marks its games as ended, and they are announced again when they reappear."""
    assert MISSING_CYCLES_TO_END == 2
    await baseline(env)
    full = [special(DUMMY)] + items(50)
    env.specials.items = full
    await env.cycle()
    for n in await env.pending():
        await env.repo.mark_sent(n["id"], 1)
    env.specials.items = full[:-5]
    await env.cycle()
    await env.cycle()
    states = await env.repo.get_deal_states()
    assert all(states[s.appid]["on_sale"] == 0 for s in full[-5:])
    env.specials.items = full
    summary = await env.cycle()
    assert summary.enqueued == 5
