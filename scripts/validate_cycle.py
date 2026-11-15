"""Run complete monitoring cycles against the real Steam (no Discord involved).

Usage:
    .venv\\Scripts\\python scripts\\validate_cycle.py [--cycles 2] [--wishlist-limit 150]
                                                   [--steam-id 76561198028121353] [--keep-db]

What it does
    * Builds the same ``Services`` (MonitorService, clients, dispatcher, repository) that the
      bot uses, but with a TEMPORARY database and a FAKE sender that only records the
      notifications. Nothing is ever sent to Discord and no token is needed.
    * Links one test user to a public SteamID64 and caps the imported wishlist to
      ``--wishlist-limit`` AppIDs (default 150). Without the cap a large wishlist would add
      hundreds of GetItems/price lookups per cycle; ~150 AppIDs is two GetItems batches,
      which is enough to exercise the wishlist -> prices -> notification path politely.
    * Instruments the HTTP layer without changing its behaviour (counters around
      ``get_text``, an aiohttp trace for every real request including retries, and recording
      sleep functions) plus per-page / per-source timings and data-integrity checks, and
      prints a report after each cycle.
    * Runs the cycles one after another on the same database: cycle 1 shows the baseline
      behaviour, cycle 2 shows whether anything is announced again without a real change.

Each full cycle takes about 4 minutes (67 pages with a 3 s pause). Be polite: the default
is 2 cycles. The script reads only public endpoints and never touches ``data/``.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from steamdeals.app import Services  # noqa: E402
from steamdeals.config import Settings  # noqa: E402
from steamdeals.steam import specials as specials_module  # noqa: E402
from steamdeals.steam.wishlist import WishlistResult, parse_steam_id  # noqa: E402

DEFAULT_STEAM_ID = "76561198028121353"
TEST_USER_ID = 1
LAG_TICK = 0.05
LAG_REPORT_THRESHOLD = 0.25


class CycleMetrics:
    """Counters reset at the start of every cycle."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls = collections.Counter()  # get_text calls per endpoint (logical requests)
        self.requests = collections.Counter()  # real HTTP requests per endpoint (retries included)
        self.statuses = collections.defaultdict(collections.Counter)  # endpoint -> status -> n
        self.request_seconds = collections.defaultdict(float)
        self.backoff_seconds = 0.0
        self.backoff_sleeps = 0
        self.page_delay_seconds = 0.0
        self.rate_limit_pause_seconds = 0.0
        self.rate_limit_pauses = 0
        self.pages: list[dict] = []
        self.page_failures: list[str] = []
        self.parse_seconds: list[float] = []
        self.source_seconds: dict[str, float] = {}
        self.store_results: list[dict] = []
        self.max_lag = 0.0
        self.lag_events = 0
        self.wishlist_full: int | None = None


def endpoint_name(url: str) -> str:
    """Short, stable endpoint label (host + path without query)."""
    parsed = urlparse(url)
    return f"{parsed.netloc.split('.')[0]}{parsed.path.rstrip('/')}"


class FakeSender:
    """Captures notifications instead of sending them anywhere."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, payload: dict) -> int:
        self.sent.append(payload)
        return 1_000_000 + len(self.sent)


def instrument(services: Services, metrics: CycleMetrics, wishlist_limit: int) -> aiohttp.ClientSession:
    http = services.http

    # --- real requests (retries included) through an aiohttp trace --------------------
    trace = aiohttp.TraceConfig()

    async def on_start(session, ctx, params):
        ctx.t0 = time.monotonic()

    async def on_end(session, ctx, params):
        name = endpoint_name(str(params.url.with_query(None)))
        metrics.requests[name] += 1
        metrics.statuses[name][params.response.status] += 1
        metrics.request_seconds[name] += time.monotonic() - ctx.t0

    async def on_exception(session, ctx, params):
        name = endpoint_name(str(params.url.with_query(None)))
        metrics.requests[name] += 1
        metrics.statuses[name][type(params.exception).__name__] += 1
        metrics.request_seconds[name] += time.monotonic() - ctx.t0

    trace.on_request_start.append(on_start)
    trace.on_request_end.append(on_end)
    trace.on_request_exception.append(on_exception)
    session = aiohttp.ClientSession(headers=http._headers, trace_configs=[trace])
    http._session = session  # closed by ``http.close()`` (the client owns it)

    # --- logical calls ---------------------------------------------------------------
    real_get_text = http.get_text

    async def get_text(url, params=None):
        metrics.calls[endpoint_name(url)] += 1
        return await real_get_text(url, params)

    http.get_text = get_text

    # --- sleeps (backoff inside SteamHttp; page delay / 429 pause inside SpecialsClient) --
    real_http_sleep = http._sleep

    async def http_sleep(seconds):
        metrics.backoff_seconds += seconds
        metrics.backoff_sleeps += 1
        await real_http_sleep(seconds)

    http._sleep = http_sleep

    specials = services.specials
    real_specials_sleep = specials._sleep

    async def specials_sleep(seconds):
        if seconds >= specials._rate_limit_pause:
            metrics.rate_limit_pause_seconds += seconds
            metrics.rate_limit_pauses += 1
        else:
            metrics.page_delay_seconds += seconds
        await real_specials_sleep(seconds)

    specials._sleep = specials_sleep

    # --- per page ---------------------------------------------------------------------
    real_fetch_page = specials.fetch_page

    async def fetch_page(start=0):
        t0 = time.monotonic()
        try:
            page = await real_fetch_page(start)
        except Exception as exc:
            metrics.page_failures.append(f"start={start}: {type(exc).__name__}")
            raise
        metrics.pages.append(
            {
                "start": start,
                "seconds": time.monotonic() - t0,
                "total_count": page.total_count,
                "rows_seen": page.rows_seen,
                "items": len(page.items),
                "skipped": page.skipped,
                "appids": [i.appid for i in page.items],
                "prices": [(i.appid, i.final_cents, i.original_cents, i.discount_pct) for i in page.items],
            }
        )
        return page

    specials.fetch_page = fetch_page

    real_parse = specials_module.parse_results_html

    def timed_parse(html):
        t0 = time.perf_counter()
        try:
            return real_parse(html)
        finally:
            metrics.parse_seconds.append(time.perf_counter() - t0)

    specials_module.parse_results_html = timed_parse

    # --- wishlist cap -----------------------------------------------------------------
    real_wishlist_fetch = services.wishlist.fetch

    async def wishlist_fetch(steam_id):
        result = await real_wishlist_fetch(steam_id)
        metrics.wishlist_full = len(result.appids)
        return WishlistResult(result.appids[:wishlist_limit], result.empty_or_private)

    services.wishlist.fetch = wishlist_fetch

    # --- store results for the integrity checks ----------------------------------------
    real_get_items = services.store.get_items

    async def get_items(appids):
        items, failed = await real_get_items(appids)
        metrics.store_results.append({"requested": len(set(appids)), "items": items, "failed": failed})
        return items, failed

    services.store.get_items = get_items

    # --- per-source durations -----------------------------------------------------------
    monitor = services.monitor

    def timed(obj, attr, label):
        real = getattr(obj, attr)

        async def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                return await real(*args, **kwargs)
            finally:
                metrics.source_seconds[label] = metrics.source_seconds.get(label, 0.0) + time.monotonic() - t0

        setattr(obj, attr, wrapper)

    timed(monitor, "_fetch_specials", "specials")
    timed(monitor, "_import_wishlists", "wishlist")
    timed(monitor, "_detect", "detect (store prices + decisions + db)")
    timed(services.dispatcher, "dispatch", "dispatch")
    return session


async def lag_probe(metrics: CycleMetrics) -> None:
    """Measures how late the event loop wakes up (a proxy for gateway heartbeat risk)."""
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(LAG_TICK)
        lag = time.monotonic() - t0 - LAG_TICK
        metrics.max_lag = max(metrics.max_lag, lag)
        if lag > LAG_REPORT_THRESHOLD:
            metrics.lag_events += 1


def integrity_report(metrics: CycleMetrics) -> dict:
    rows_seen = sum(p["rows_seen"] for p in metrics.pages)
    items = sum(p["items"] for p in metrics.pages)
    skipped = sum(p["skipped"] for p in metrics.pages)
    all_ids = [a for p in metrics.pages for a in p["appids"]]
    unique = set(all_ids)
    dup_ids = [a for a, n in collections.Counter(all_ids).items() if n > 1]
    totals = sorted({p["total_count"] for p in metrics.pages})
    inverted, mismatch, negative = [], [], []
    for page in metrics.pages:
        for appid, final, original, pct in page["prices"]:
            if final < 0:
                negative.append(appid)
            if final > original:
                inverted.append(appid)
            if original > 0:
                expected = round((1 - final / original) * 100)
                if abs(expected - pct) > 1:
                    mismatch.append((appid, final, original, pct))
    store = {"no_price": 0, "not_on_sale": 0, "final_gt_original": 0, "pct_mismatch": 0, "received": 0,
             "requested": 0, "failed": 0}
    for result in metrics.store_results:
        store["requested"] += result["requested"]
        store["failed"] += len(result["failed"])
        for item in result["items"].values():
            store["received"] += 1
            if not item.has_price:
                store["no_price"] += 1
                continue
            if not item.on_sale:
                store["not_on_sale"] += 1
            if item.original_cents is not None and item.final_cents > item.original_cents:
                store["final_gt_original"] += 1
            if item.on_sale and abs(round((1 - item.final_cents / item.original_cents) * 100) - item.discount_pct) > 1:
                store["pct_mismatch"] += 1
    last_total = metrics.pages[-1]["total_count"] if metrics.pages else 0
    return {
        "pages": len(metrics.pages),
        "total_counts_seen": totals,
        "rows_seen": rows_seen,
        "items_parsed": items,
        "unique_appids": len(unique),
        "duplicate_appids_across_pages": len(dup_ids),
        "discarded_rows (bundles/packages/inconsistent)": skipped,
        "coverage_unique_vs_total_count": round(len(unique) / last_total, 4) if last_total else None,
        "short_non_last_pages": [p["start"] for p in metrics.pages[:-1] if p["rows_seen"] < 100],
        "final_gt_original": len(inverted),
        "negative_final": len(negative),
        "discount_vs_price_mismatch": len(mismatch),
        "discount_vs_price_mismatch_sample": mismatch[:5],
        "store": store,
    }


def print_report(number: int, summary, metrics: CycleMetrics, wall: float, sender: FakeSender,
                 new_notifications: list[dict], integrity: dict) -> dict:
    print(f"\n==================== CICLO {number} ====================")
    print(f"duracao total (wall)         : {wall:.1f}s (summary.duration={summary.duration:.1f}s)")
    for label, seconds in metrics.source_seconds.items():
        print(f"  fonte {label:<40}: {seconds:.1f}s")
    print(f"wishlist completa do usuario de teste: {metrics.wishlist_full} AppIDs (importados: ate o limite)")
    print(f"specials_ok={summary.specials_ok} specials_count={summary.specials_count} "
          f"wishlists={summary.wishlists_total} (falhas {summary.wishlists_failed}) "
          f"monitorados={summary.monitored} store_failed={summary.store_failed}")
    print(f"baseline_recorded={summary.baseline_recorded} baseline_completed={summary.baseline_completed} "
          f"enqueued={summary.enqueued} dispatched={summary.dispatched} erros={summary.errors}")
    print("-- requisicoes (logicas = chamadas get_text; reais = HTTP incluindo retries) --")
    for name in sorted(set(metrics.calls) | set(metrics.requests)):
        statuses = dict(metrics.statuses[name])
        print(f"  {name:<45} logicas={metrics.calls[name]:<4} reais={metrics.requests[name]:<4} "
              f"status={statuses} tempo_http={metrics.request_seconds[name]:.1f}s")
    n429 = sum(s.get(429, 0) for s in metrics.statuses.values())
    retries = sum(metrics.requests.values()) - sum(metrics.calls.values())
    print(f"429 recebidos={n429} retries={retries} backoff_http={metrics.backoff_seconds:.1f}s "
          f"({metrics.backoff_sleeps} esperas) pausas_429_de_pagina={metrics.rate_limit_pauses} "
          f"({metrics.rate_limit_pause_seconds:.0f}s) pausa_entre_paginas={metrics.page_delay_seconds:.0f}s")
    if metrics.page_failures:
        print(f"falhas de pagina: {metrics.page_failures}")
    if metrics.pages:
        secs = [p["seconds"] for p in metrics.pages]
        print(f"paginas={len(secs)} tempo/pagina min={min(secs):.2f}s media={sum(secs) / len(secs):.2f}s "
              f"max={max(secs):.2f}s")
    if metrics.parse_seconds:
        print(f"parse HTML (sincrono): total={sum(metrics.parse_seconds):.2f}s "
              f"max/pagina={max(metrics.parse_seconds) * 1000:.0f}ms")
    print(f"event loop: atraso maximo={metrics.max_lag * 1000:.0f}ms eventos>{LAG_REPORT_THRESHOLD * 1000:.0f}ms="
          f"{metrics.lag_events}")
    print("-- cobertura / integridade --")
    for key, value in integrity.items():
        print(f"  {key}: {value}")
    print(f"-- notificacoes novas no ciclo: {len(new_notifications)} (total capturado pelo sender fake: "
          f"{len(sender.sent)}) --")
    for n in new_notifications[:25]:
        print(f"  app {n['appid']} {n['name']!r} origens={n['origins']} {n['reason']}")
    if len(new_notifications) > 25:
        print(f"  ... e mais {len(new_notifications) - 25}")
    return {
        "duration": wall,
        "calls": dict(metrics.calls),
        "requests": dict(metrics.requests),
        "statuses": {k: dict(v) for k, v in metrics.statuses.items()},
        "n429": n429,
        "retries": retries,
        "backoff_seconds": metrics.backoff_seconds,
        "integrity": integrity,
        "enqueued": summary.enqueued,
        "dispatched": summary.dispatched,
        "max_loop_lag": metrics.max_lag,
    }


async def run(args) -> int:
    steam_id = parse_steam_id(args.steam_id)
    tmp = Path(tempfile.mkdtemp(prefix="steamdeals-validate-"))
    db_path = str(tmp / "validate.db")
    settings = Settings(
        discord_token="not-used", notify_channel_id=1, database_path=db_path,
        max_notifications_per_cycle=args.max_per_cycle,
    )
    sender = FakeSender()
    services = await Services.create(settings, sender)
    metrics = CycleMetrics()
    probe: asyncio.Task | None = None
    reports = []
    try:
        instrument(services, metrics, args.wishlist_limit)
        probe = asyncio.create_task(lag_probe(metrics))
        await services.repo.set_steam_link(TEST_USER_ID, steam_id)
        monitored_ids: set[int] = set()
        for number in range(1, args.cycles + 1):
            metrics.reset()
            states_before = await services.repo.get_deal_states()
            sent_before = len(sender.sent)
            async with services.db.read() as c:
                async with c.execute("SELECT id, created_at FROM notifications") as cur:
                    created_before = {r["id"]: r["created_at"] for r in await cur.fetchall()}
            t0 = time.monotonic()
            summary = await services.monitor.run_cycle()
            wall = time.monotonic() - t0
            if summary is None:
                print("ciclo pulado (lock)")
                return 1
            monitored = await services.repo.monitored_map()
            monitored_ids = set(monitored)
            # Which notifications were created or reactivated (not merely sent) in this cycle,
            # and why. A reactivated row keeps its id but gets a new created_at.
            async with services.db.read() as c:
                async with c.execute(
                    "SELECT id, appid, status, payload, created_at FROM notifications ORDER BY id"
                ) as cur:
                    rows = [
                        r for r in await cur.fetchall()
                        if created_before.get(r["id"]) != r["created_at"]
                    ]
            new_notifications = []
            for row in rows:
                payload = json.loads(row["payload"])
                prev = states_before.get(row["appid"])
                if prev is None:
                    reason = "jogo sem estado anterior"
                elif prev["baseline"] and row["appid"] in monitored_ids:
                    reason = "estava no baseline e passou a ser monitorado"
                elif (prev["final_cents"], prev["original_cents"], prev["discount_pct"]) != (
                    payload["final_cents"], payload["original_cents"], payload["discount_pct"]
                ):
                    reason = f"preco mudou {prev['final_cents']}->{payload['final_cents']}"
                elif not prev["on_sale"]:
                    reason = "voltou a ficar em oferta"
                else:
                    reason = "OUTRO (investigar)"
                new_notifications.append({"id": row["id"], "appid": row["appid"], "name": payload["name"],
                                          "origins": payload["origins"], "status": row["status"], "reason": reason})
            integrity = integrity_report(metrics)
            data = print_report(number, summary, metrics, wall, sender, new_notifications, integrity)
            data["new_notifications"] = len(new_notifications)
            data["new_notifications_not_monitored"] = sum(
                1 for n in new_notifications if n["appid"] not in monitored_ids
            )
            reports.append(data)
            pending = await services.repo.count_notifications("pending")
            print(f"fila pendente apos o ciclo: {pending}; monitorados: {len(monitored_ids)}; "
                  f"enviados (fake) neste ciclo: {len(sender.sent) - sent_before}")
            if number == 1:
                outside = [n for n in new_notifications if n["appid"] not in monitored_ids]
                print(f"BASELINE: notificacoes de jogos NAO monitorados no ciclo 1 = {len(outside)} "
                      f"({'OK' if not outside else 'FALHA'})")
        if args.json:
            Path(args.json).write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
        return 0
    finally:
        if probe is not None:
            probe.cancel()
        await services.close()
        await asyncio.sleep(0.25)  # let aiohttp release its connections
        if args.keep_db:
            print(f"banco temporario mantido em {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cycles", type=int, default=2, help="number of consecutive cycles (default 2)")
    parser.add_argument("--wishlist-limit", type=int, default=150,
                        help="max wishlist AppIDs imported for the test user (default 150)")
    parser.add_argument("--steam-id", default=DEFAULT_STEAM_ID, help="public SteamID64 of the test user")
    parser.add_argument("--max-per-cycle", type=int, default=20, help="notifications sent per cycle (default 20)")
    parser.add_argument("--json", help="also write the numeric report to this JSON file")
    parser.add_argument("--keep-db", action="store_true", help="keep the temporary database")
    args = parser.parse_args()
    if args.cycles < 1 or args.wishlist_limit < 1:
        parser.error("--cycles and --wishlist-limit must be >= 1")
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
