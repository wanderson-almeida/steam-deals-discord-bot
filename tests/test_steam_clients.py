import json

import pytest

from conftest import FakeHttp, load_json, load_text
from steamdeals.steam.http import SteamRateLimited, SteamSchemaError, SteamUnavailable
from steamdeals.steam.money import format_brl, parse_price_text
from steamdeals.steam.specials import SpecialsClient, parse_results_html
from steamdeals.steam.store import StoreClient, parse_get_items
from steamdeals.steam.suggest import SuggestClient, parse_suggestions
from steamdeals.steam.wishlist import (
    InvalidSteamId,
    WishlistClient,
    parse_steam_id,
    parse_wishlist,
)

SEARCH = load_json("search_results.json")


# ---------------------------------------------------------------- money
@pytest.mark.parametrize(
    "text,cents",
    [("R$66,99", 6699), ("R$ 1.234,56", 123456), ("R$ 33,99", 3399), ("R$100", 10000),
     ("$19.99", 1999), ("Free", None), ("", None), (None, None)],
)
def test_parse_price_text(text, cents):
    assert parse_price_text(text) == cents


def test_format_brl():
    assert format_brl(1674) == "R$ 16,74"
    assert format_brl(123456) == "R$ 1.234,56"
    assert format_brl(0) == "R$ 0,00"
    assert format_brl(None) == "—"


# ---------------------------------------------------------------- specials
def test_parse_specials_fixture():
    items, rows, skipped = parse_results_html(SEARCH["results_html"])
    assert rows == 5 and skipped == 0
    assert [i.appid for i in items] == [227300, 2406770, 489830, 1547000, 270880]
    first = items[0]
    assert first.title == "Euro Truck Simulator 2"
    assert (first.final_cents, first.original_cents, first.discount_pct) == (1674, 6699, 75)
    assert "227300" in first.image_url


def test_specials_skips_bundles_and_inconsistent_rows():
    html = SEARCH["results_html"]
    bundle = html.replace('data-ds-appid="227300"', 'data-ds-bundleid="227300"', 1)
    items, rows, skipped = parse_results_html(bundle)
    assert rows == 5 and skipped == 1 and 227300 not in [i.appid for i in items]

    package = html.replace('data-ds-appid="227300"', 'data-ds-appid="227300" data-ds-packageid="5"', 1)
    assert 227300 not in [i.appid for i in parse_results_html(package)[0]]

    no_discount = html.replace('data-discount="75"', 'data-discount="0"', 1)
    items, _, skipped = parse_results_html(no_discount)
    assert skipped == 1 and len(items) == 4

    final_above_original = html.replace('data-price-final="1674"', 'data-price-final="9999"')
    items, _, skipped = parse_results_html(final_above_original)
    assert 227300 not in [i.appid for i in items]


def specials_http(total, pages=None):
    def handler(url, params):
        start = int(params["start"])
        html = SEARCH["results_html"]
        if pages is not None:
            html = pages(start)
        return json.dumps({"success": 1, "results_html": html, "total_count": total, "start": start})

    return FakeHttp(handler)


def shifted_html(offset):
    html = SEARCH["results_html"]
    for appid in (227300, 2406770, 489830, 1547000, 270880):
        html = html.replace(str(appid), str(appid + offset))
    return html


async def test_fetch_page_params_games_only():
    http = specials_http(5)
    page = await SpecialsClient(http, country="br", language="portuguese").fetch_page(0)
    assert page.total_count == 5 and len(page.items) == 5
    params = http.calls[0][1]
    assert params["category1"] == "998"
    assert params["specials"] == "1" and params["cc"] == "br" and params["l"] == "portuguese"
    assert params["json"] == "1" and params["infinite"] == "1" and params["count"] == "100"
    assert params["sort_by"] == "Name_ASC"  # stable pagination


async def test_fetch_page_include_non_games_has_no_category_filter():
    http = specials_http(5)
    await SpecialsClient(http, include_non_games=True).fetch_page(0)
    assert "category1" not in http.calls[0][1]


async def test_fetch_all_paginates_until_total_count_and_dedupes():
    http = specials_http(10, lambda start: SEARCH["results_html"] if start == 0 else shifted_html(1_000_000))
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    items = await SpecialsClient(http, page_size=5, page_delay=0.7, sleep=fake_sleep).fetch_all()
    assert len(items) == 10
    assert [c[1]["start"] for c in http.calls] == ["0", "5"]
    assert sleeps == [0.7]  # throttled between pages, not before the first


async def test_html_layout_change_raises_schema_error():
    http = specials_http(10, lambda start: "<div>nothing here</div>")
    with pytest.raises(SteamSchemaError):
        await SpecialsClient(http).fetch_page(0)
    renamed = SEARCH["results_html"].replace("search_result_row", "result_row_v2")
    with pytest.raises(SteamSchemaError):
        await SpecialsClient(specials_http(10, lambda s: renamed)).fetch_all()


async def test_empty_listing_is_a_failure_not_an_empty_result():
    http = specials_http(0, lambda start: "")
    with pytest.raises(SteamSchemaError):
        await SpecialsClient(http).fetch_all()


async def test_listing_far_below_total_count_is_a_failure():
    http = specials_http(100)  # 5 distinct items repeated while total_count says 100
    with pytest.raises(SteamSchemaError):
        await SpecialsClient(http, page_size=5, page_delay=0).fetch_all()


async def test_listing_at_ninety_percent_is_accepted():
    http = specials_http(5)
    assert len(await SpecialsClient(http, page_delay=0).fetch_all()) == 5


async def test_missing_keys_is_schema_error():
    http = FakeHttp(lambda u, p: json.dumps({"success": 1}))
    with pytest.raises(SteamSchemaError):
        await SpecialsClient(http).fetch_page(0)


async def test_failure_aborts_without_partial_result():
    state = {"n": 0}

    def handler(url, params):
        state["n"] += 1
        if state["n"] == 2:
            return SteamUnavailable("down")
        return json.dumps({"results_html": SEARCH["results_html"], "total_count": 10})

    with pytest.raises(SteamUnavailable):
        await SpecialsClient(FakeHttp(handler), page_size=5, page_delay=0).fetch_all()


async def test_rate_limited_page_is_retried_after_a_pause_and_gives_up_eventually():
    state = {"n": 0}

    def flaky(url, params):
        state["n"] += 1
        if state["n"] <= 2:
            return SteamRateLimited("429")
        return json.dumps({"results_html": SEARCH["results_html"], "total_count": 5})

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    client = SpecialsClient(FakeHttp(flaky), rate_limit_pause=45, sleep=fake_sleep)
    assert len(await client.fetch_all()) == 5
    assert sleeps == [45, 45]

    always = SpecialsClient(FakeHttp(lambda u, p: SteamRateLimited("429")), sleep=fake_sleep,
                            rate_limit_pause=1, rate_limit_page_retries=1)
    with pytest.raises(SteamRateLimited):
        await always.fetch_all()


# ---------------------------------------------------------------- GetItems
def test_parse_get_items_fixture():
    items = parse_get_items(load_json("getitems.json"))
    assert set(items) == {227300, 2406770, 440, 1091500}
    eu = items[227300]
    assert (eu.final_cents, eu.original_cents, eu.discount_pct) == (1674, 6699, 75)
    assert eu.discount_end == 1795539600 and eu.on_sale
    assert eu.formatted_final == "R$16,74" and eu.formatted_original == "R$66,99"
    assert eu.image_url.endswith("/steam/apps/227300/header.jpg")
    assert eu.type == 0


def test_parse_get_items_without_price_and_without_discount():
    items = parse_get_items(load_json("getitems.json"))
    free = items[440]
    assert free.final_cents is None and not free.has_price and not free.on_sale and free.is_free
    full = items[1091500]
    assert full.final_cents == 19990 and full.original_cents == 19990
    assert full.discount_pct == 0 and not full.on_sale


def test_get_items_inconsistent_prices_are_not_on_sale():
    payload = {"response": {"store_items": [{
        "appid": 1, "name": "X", "success": 1, "type": 0,
        "best_purchase_option": {"final_price_in_cents": "900", "original_price_in_cents": "500", "discount_pct": 20},
    }, {"appid": 2, "name": "Y", "best_purchase_option": {
        "final_price_in_cents": "abc", "original_price_in_cents": "500", "discount_pct": 20}},
        {"appid": 3, "success": 2}, {"appid": 4}]}}
    items = parse_get_items(payload)
    assert set(items) == {1, 2}
    assert not items[1].on_sale and not items[2].on_sale and not items[2].has_price


def test_get_items_schema_errors():
    for bad in ({}, [], {"response": []}, {"response": {"store_items": "x"}}):
        with pytest.raises(SteamSchemaError):
            parse_get_items(bad)
    assert parse_get_items({"response": {}}) == {}


def test_assets_are_used_when_present():
    payload = {"response": {"store_items": [{
        "appid": 5, "name": "A", "assets": {"asset_url_format": "steam/apps/5/${FILENAME}?t=1", "header": "h.jpg"}}]}}
    assert parse_get_items(payload)[5].image_url.endswith("steam/apps/5/h.jpg?t=1")


def getitems_http(known=None, fail_batches=(), appdetails=None):
    fixture = load_json("getitems.json")

    def handler(url, params):
        if "GetItems" in url:
            ids = [i["appid"] for i in json.loads(params["input_json"])["ids"]]
            handler.batches.append(ids)
            if len(handler.batches) in fail_batches:
                return SteamUnavailable("boom")
            if known is not None:
                return json.dumps({"response": {"store_items": [e for e in fixture["response"]["store_items"] if e["appid"] in ids]}})
            return json.dumps({"response": {"store_items": []}})
        handler.fallbacks.append(params["appids"])
        return appdetails(params) if appdetails else SteamUnavailable("down")

    handler.batches, handler.fallbacks = [], []
    return FakeHttp(handler), handler


async def test_store_request_shape_and_batching():
    http, h = getitems_http()
    client = StoreClient(http)
    await client.get_items(range(1, 251))
    assert [len(b) for b in h.batches] == [100, 100, 50]
    url, params = http.calls[0]
    payload = json.loads(params["input_json"])
    assert payload["context"] == {"language": "brazilian", "country_code": "BR", "steam_realm": 1}
    assert payload["data_request"] == {"include_basic_info": True}


async def test_store_get_items_returns_known_and_ignores_unknown():
    http, h = getitems_http(known=True)
    items, failed = await StoreClient(http).get_items([227300, 440, 999999999])
    assert set(items) == {227300, 440} and failed == set()


async def test_store_fallback_to_appdetails_for_failed_batch():
    def appdetails(params):
        appid = params["appids"]
        assert params["filters"] == "price_overview" and params["cc"] == "br"
        return json.dumps({appid: {"success": True, "data": {"price_overview": {
            "initial": 6699, "final": 1674, "discount_percent": 75,
            "initial_formatted": "R$ 66,99", "final_formatted": "R$ 16,74"}}}})

    http, h = getitems_http(fail_batches=(1,), appdetails=appdetails)
    items, failed = await StoreClient(http).get_items([10, 20])
    assert h.fallbacks and set(h.fallbacks) == {"10", "20"}
    assert items[10].on_sale and items[10].final_cents == 1674 and items[10].name == ""
    assert failed == set()


async def test_store_fallback_free_game_has_no_price():
    http, h = getitems_http(
        fail_batches=(1,), appdetails=lambda p: json.dumps({p["appids"]: {"success": True, "data": []}})
    )
    items, _ = await StoreClient(http).get_items([7])
    assert not items[7].has_price


async def test_store_fallback_failure_marks_failed_but_other_batches_survive():
    http, h = getitems_http(known=True, fail_batches=(1,))
    ids = [227300] + list(range(1000, 1100))  # 101 ids -> 2 batches (first fails)
    items, failed = await StoreClient(http).get_items(ids)
    assert 227300 in items
    assert failed == set(range(1000, 1100))


async def test_store_rate_limit_skips_fallback():
    http, h = getitems_http()
    original = http.handler

    def handler(url, params):
        if "GetItems" in url:
            return SteamRateLimited("429", 5)
        return original(url, params)

    http.handler = handler
    items, failed = await StoreClient(http).get_items([1, 2, 3])
    assert items == {} and failed == {1, 2, 3} and h.fallbacks == []


async def test_get_item_single():
    http, _ = getitems_http(known=True)
    assert (await StoreClient(http).get_item(227300)).name == "Euro Truck Simulator 2"
    assert await StoreClient(http).get_item(123456789) is None


# ---------------------------------------------------------------- wishlist
def test_parse_steam_id_variants():
    sid = "76561198028121353"
    assert parse_steam_id(sid) == sid
    assert parse_steam_id(f"  {sid} ") == sid
    assert parse_steam_id(f"https://steamcommunity.com/profiles/{sid}") == sid
    assert parse_steam_id(f"https://steamcommunity.com/profiles/{sid}/") == sid
    assert parse_steam_id(f"http://www.steamcommunity.com/profiles/{sid}/wishlist?x=1") == sid


@pytest.mark.parametrize("bad", ["", "123", "7656119802812135", "765611980281213533", "abc",
                                 "66561198028121353", "https://steamcommunity.com/profiles/12"])
def test_parse_steam_id_invalid(bad):
    with pytest.raises(InvalidSteamId) as exc:
        parse_steam_id(bad)
    assert exc.value.kind == "invalid"


def test_parse_steam_id_vanity_is_rejected_with_specific_kind():
    with pytest.raises(InvalidSteamId) as exc:
        parse_steam_id("https://steamcommunity.com/id/gaben")
    assert exc.value.kind == "vanity"


def test_parse_wishlist_public_and_private():
    public = parse_wishlist(load_json("wishlist.json"))
    assert len(public.appids) == 6 and not public.empty_or_private
    assert public.appids == sorted(public.appids)
    private = parse_wishlist(load_json("wishlist_private.json"))
    assert private.appids == [] and private.empty_or_private
    assert parse_wishlist({"response": {"items": []}}).empty_or_private


def test_parse_wishlist_schema_error():
    for bad in ([], {}, {"response": []}, {"response": {"items": "x"}}):
        with pytest.raises(SteamSchemaError):
            parse_wishlist(bad)


async def test_wishlist_client_fetch():
    http = FakeHttp(lambda u, p: load_text("wishlist.json"))
    result = await WishlistClient(http).fetch("76561198028121353")
    assert http.calls[0][1] == {"steamid": "76561198028121353"}
    assert 660 in result.appids


# ---------------------------------------------------------------- suggest
def test_parse_suggestions_fixture():
    items = parse_suggestions(load_text("suggest.html"))
    assert [i.appid for i in items] == [620, 400, 374040]
    assert items[0].name == "Portal 2" and items[0].price == "R$ 33,99"


async def test_suggest_cache_and_short_terms():
    now = {"t": 0.0}
    http = FakeHttp(lambda u, p: load_text("suggest.html"))
    client = SuggestClient(http, ttl=60, clock=lambda: now["t"])
    assert await client.search("p") == []
    assert http.calls == []
    first = await client.search("Portal")
    assert await client.search("portal") == first
    assert len(http.calls) == 1
    assert http.calls[0][1]["f"] == "games" and http.calls[0][1]["cc"] == "br"
    now["t"] = 61
    await client.search("portal")
    assert len(http.calls) == 2


async def test_suggest_failure_returns_empty_list():
    client = SuggestClient(FakeHttp(lambda u, p: SteamUnavailable("down")))
    assert await client.search("portal") == []
