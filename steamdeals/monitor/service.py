"""Monitoring cycle: fetch sources, detect promotions, enqueue notifications.

Detection rules
---------------
* A *signature* is ``hash(final_cents, original_cents, discount_pct, sale_epoch)``
  and exists only for items that are on sale. The end date is not part of it: a
  changed ``discount_end`` with the same prices/discount is stored for display but
  is not announced again. ``sale_epoch`` increases when a sale ends and a new one
  starts, so an identical promotion returning later is announced. The same holds
  when the recorded end date had already passed (more than an hour ago) and the
  same prices now report a later end; an extension before the end is silent. A game is
  announced when its signature differs from the last one recorded for it.
  ``UNIQUE(appid, signature)``
  on the notifications table guarantees one notification per game/state no
  matter how many users or origins (specials, wishlist, manual) contain it.
* When a new signature is enqueued, older still-pending notifications of the
  same game are marked ``superseded`` (kept in the database, never dropped
  silently).
* Origins/users of a still-pending notification are merged on every cycle.

Baseline
--------
On the first cycle in which the ``specials`` source is fetched successfully
(setting ``specials_baseline_done``), every existing special is recorded in
``deal_state`` with ``baseline=1`` and *no notification*, so the bot does not
flood the channel with ~thousands of pre-existing sales. Games that are
monitored (wishlist/manual) are excluded from this suppression and notify
normally. After the baseline, new games and changed signatures notify. If the
specials fetch fails, the baseline is not marked as done and is retried on the
next cycle.

The step that computes decisions is pure Python over data loaded once; all
effects are applied in a single database transaction
(:meth:`Repository.apply_cycle`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from ..db.repositories import Repository
from ..models import (
    CycleChanges,
    DealSnapshot,
    NewNotification,
    ORIGIN_MANUAL,
    ORIGIN_SPECIALS,
    ORIGIN_WISHLIST,
    StateUpdate,
)
from ..steam.http import SteamError, SteamSchemaError, SteamUnavailable
from ..steam.specials import SpecialItem, SpecialsClient
from ..steam.store import StoreClient, StoreItem, header_image_url
from ..steam.wishlist import WishlistClient

log = logging.getLogger(__name__)

# Sanity thresholds for the specials listing (see _sanity_check_specials).
MIN_KNOWN_FOR_COLLAPSE_CHECK = 20
MIN_LISTING_RATIO = 0.5
# Consecutive cycles a game must be absent from the listing before it counts as ended.
MISSING_CYCLES_TO_END = 2
# A recorded end date counts as expired this long after it passed (clock/rounding tolerance).
EXPIRED_END_MARGIN_SECONDS = 3600


# HTTP statuses with which Steam rejects the request itself (for example a SteamID64 that is
# well formed but outside the valid range answers 400). Retrying will not help.
INVALID_ID_HTTP_STATUSES = (400, 404)


def import_failure_status(exc: Exception) -> str:
    """Status stored in ``steam_links.last_import_status`` for a failed wishlist fetch."""
    if isinstance(exc, SteamUnavailable) and exc.status in INVALID_ID_HTTP_STATUSES:
        return "invalid_id"
    return "error"


@dataclass
class ImportResult:
    status: str  # 'ok' | 'empty_or_private' | 'invalid_id' | 'not_linked' | 'error'
    added: int = 0
    removed: int = 0
    total: int = 0
    error: str | None = None


@dataclass
class CycleSummary:
    started_at: datetime
    specials_ok: bool = False
    specials_count: int = 0
    wishlists_total: int = 0
    wishlists_failed: int = 0
    monitored: int = 0
    store_failed: int = 0
    enqueued: int = 0
    baseline_recorded: int = 0
    baseline_completed: bool = False
    errors: list[str] = field(default_factory=list)
    dispatched: int = 0
    duration: float = 0.0


def snapshot_from_special(item: SpecialItem) -> DealSnapshot:
    return DealSnapshot(
        appid=item.appid,
        name=item.title,
        final_cents=item.final_cents,
        original_cents=item.original_cents,
        discount_pct=item.discount_pct,
        discount_end=None,  # the search endpoint does not expose the end date
        on_sale=True,
        in_specials=True,
        image_url=header_image_url(item.appid),
    )


def snapshot_from_store(item: StoreItem, name: str, in_specials: bool) -> DealSnapshot:
    return DealSnapshot(
        appid=item.appid,
        name=name,
        final_cents=item.final_cents,
        original_cents=item.original_cents,
        discount_pct=item.discount_pct if item.on_sale else 0,
        discount_end=item.discount_end if item.on_sale else None,
        on_sale=item.on_sale,
        in_specials=in_specials,
        formatted_final=item.formatted_final,
        formatted_original=item.formatted_original,
        image_url=item.image_url,
    )


class MonitorService:
    def __init__(
        self,
        repo: Repository,
        specials: SpecialsClient,
        store: StoreClient,
        wishlist: WishlistClient,
        *,
        max_concurrency: int = 5,
        dispatcher=None,
        clock=time.time,
    ) -> None:
        self.repo = repo
        self.specials = specials
        self.store = store
        self.wishlist = wishlist
        self.dispatcher = dispatcher
        self._max_concurrency = max_concurrency
        self._clock = clock
        self._lock = asyncio.Lock()
        self.last_summary: CycleSummary | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    # ----------------------------------------------------------------- cycle
    async def run_cycle(self) -> CycleSummary | None:
        """Run one cycle. Returns None when another cycle is already running."""
        if self._lock.locked():
            log.warning("Monitoring cycle skipped: previous cycle is still running")
            return None
        async with self._lock:
            started = time.monotonic()
            summary = CycleSummary(started_at=datetime.now(timezone.utc))
            specials = await self._fetch_specials(summary)
            await self._import_wishlists(summary)
            try:
                await self._detect(specials, summary)
            except Exception as exc:
                log.exception("Detection step failed")
                summary.errors.append(f"detect: {type(exc).__name__}")
            if self.dispatcher is not None:
                try:
                    result = await self.dispatcher.dispatch()
                    summary.dispatched = result.sent
                    if result.aborted:
                        summary.errors.append("dispatch: channel unavailable or rate limited")
                    elif result.failed:
                        summary.errors.append(f"dispatch: {result.failed} not delivered")
                except Exception as exc:
                    log.exception("Dispatch step failed")
                    summary.errors.append(f"dispatch: {type(exc).__name__}")
            summary.duration = time.monotonic() - started
            self.last_summary = summary
            log.info(
                "Cycle done: specials=%s/%d monitored=%d enqueued=%d baseline=%d dispatched=%d errors=%d",
                summary.specials_ok, summary.specials_count, summary.monitored, summary.enqueued,
                summary.baseline_recorded, summary.dispatched, len(summary.errors),
            )
            return summary

    # -------------------------------------------------------------- specials
    async def _fetch_specials(self, summary: CycleSummary) -> list[SpecialItem] | None:
        await self.repo.begin_check("specials")
        try:
            items = await self.specials.fetch_all()
            await self._sanity_check_specials(items)
        except Exception as exc:
            log.warning("Specials source failed: %s: %s", type(exc).__name__, exc)
            await self.repo.finish_check("specials", "error", f"{type(exc).__name__}: {exc}")
            summary.errors.append(f"specials: {type(exc).__name__}")
            return None
        await self.repo.finish_check("specials", "ok")
        summary.specials_ok = True
        summary.specials_count = len(items)
        return items

    async def _sanity_check_specials(self, items: list[SpecialItem]) -> None:
        """An empty or collapsed listing is a source failure, never "everything ended"."""
        if not items:
            raise SteamSchemaError("Specials listing is empty")
        states = await self.repo.get_deal_states()
        known = sum(1 for st in states.values() if st["in_specials"])
        if known >= MIN_KNOWN_FOR_COLLAPSE_CHECK and len(items) < known * MIN_LISTING_RATIO:
            raise SteamSchemaError(
                f"Specials listing collapsed: {len(items)} items vs {known} known"
            )

    # -------------------------------------------------------------- wishlist
    async def _apply_wishlist(
        self, discord_id: int, result, steam_id: str | None = None
    ) -> ImportResult:
        if result.empty_or_private:
            # Private and empty wishlists look identical. Wiping the user's
            # sources on a transient/private answer would be destructive, so
            # existing wishlist sources are kept (use /steam_desvincular to clear).
            await self.repo.set_import_status(discord_id, "empty_or_private")
            return ImportResult("empty_or_private")
        synced = await self.repo.sync_wishlist_sources(discord_id, result.appids, steam_id)
        if synced is None:
            # Unlinked/relinked while the request was in flight: discard the stale result.
            return ImportResult("error", error="link_changed")
        added, removed = synced
        await self.repo.set_import_status(discord_id, "ok")
        return ImportResult("ok", added, removed, len(result.appids))

    async def import_wishlist(self, discord_id: int) -> ImportResult:
        """Import the wishlist linked to one user right now (used by commands)."""
        link = await self.repo.get_steam_link(discord_id)
        if link is None:
            return ImportResult("not_linked")
        try:
            result = await self.wishlist.fetch(link["steam_id64"])
        except Exception as exc:
            log.warning("Wishlist import failed: %s", type(exc).__name__)
            status = import_failure_status(exc)
            await self.repo.set_import_status(discord_id, status)
            return ImportResult(status, error=type(exc).__name__)
        return await self._apply_wishlist(discord_id, result, link["steam_id64"])

    async def _import_wishlists(self, summary: CycleSummary) -> None:
        links = await self.repo.links_by_steam_id()
        if not links:
            return
        await self.repo.begin_check("wishlist")
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def one(steam_id: str, users: list[int]) -> bool:
            try:
                async with semaphore:
                    result = await self.wishlist.fetch(steam_id)
                for user in users:
                    await self._apply_wishlist(user, result, steam_id)
                return True
            except Exception as exc:
                log.warning("Wishlist fetch failed for a linked account: %s", type(exc).__name__)
                for user in users:
                    try:
                        await self.repo.set_import_status(user, import_failure_status(exc))
                    except Exception:
                        log.exception("Could not record import status")
                summary.errors.append(f"wishlist: {type(exc).__name__}")
                return False

        outcomes = await asyncio.gather(*(one(s, u) for s, u in links.items()))
        summary.wishlists_total = len(outcomes)
        summary.wishlists_failed = outcomes.count(False)
        if summary.wishlists_failed == 0:
            await self.repo.finish_check("wishlist", "ok")
        elif summary.wishlists_failed == len(outcomes):
            await self.repo.finish_check("wishlist", "error", f"{summary.wishlists_failed} failed")
        else:
            await self.repo.finish_check(
                "wishlist", "partial", f"{summary.wishlists_failed}/{len(outcomes)} failed"
            )

    # ------------------------------------------------------------- detection
    async def _detect(self, specials: list[SpecialItem] | None, summary: CycleSummary) -> None:
        monitored = await self.repo.monitored_map()
        states = await self.repo.get_deal_states()
        stored_names = await self.repo.watched_names()
        summary.monitored = len(monitored)
        baseline_pending = specials is not None and not await self.repo.baseline_done()
        specials_by_id = {i.appid: i for i in specials} if specials is not None else {}

        store_items: dict[int, StoreItem] = {}
        failed: set[int] = set()
        if monitored:
            await self.repo.begin_check("store")
            try:
                store_items, failed = await self.store.get_items(monitored.keys())
            except Exception as exc:
                log.warning("Store source failed: %s", type(exc).__name__)
                failed = set(monitored)
                summary.errors.append(f"store: {type(exc).__name__}")
            summary.store_failed = len(failed)
            if not failed:
                await self.repo.finish_check("store", "ok")
            elif len(failed) == len(monitored):
                await self.repo.finish_check("store", "error", f"{len(failed)} apps failed")
            else:
                await self.repo.finish_check("store", "partial", f"{len(failed)} apps failed")

        changes = CycleChanges(mark_baseline=baseline_pending)

        def process(snap: DealSnapshot, origins: set[str], users: set[int], is_monitored: bool) -> None:
            prev = states.get(snap.appid)
            epoch = prev["sale_epoch"] if prev else 0
            if prev and snap.on_sale and not prev["on_sale"] and prev["last_signature"]:
                epoch += 1  # a new sale period starts: identical prices must be announced again
            snap = replace(snap, epoch=epoch)
            if not snap.on_sale:
                changes.states.append(StateUpdate(snap))
                return
            same_prices = bool(
                prev
                and prev["on_sale"]
                and prev["final_cents"] == snap.final_cents
                and prev["original_cents"] == snap.original_cents
                and prev["discount_pct"] == snap.discount_pct
            )
            if (
                same_prices
                and snap.discount_end is not None
                and prev["discount_end"] is not None
                and prev["discount_end"] < self._clock() - EXPIRED_END_MARGIN_SECONDS
                and snap.discount_end > prev["discount_end"]
            ):
                # The recorded end had already passed and the same prices now carry a later end:
                # a new sale period (an extension before the end stays silent).
                snap = replace(snap, epoch=snap.epoch + 1)
            if same_prices and snap.discount_end is None:
                # Specials do not expose the end date: keep the one already known
                # (it is only displayed, it does not identify the promotion).
                snap = replace(snap, discount_end=prev["discount_end"])
            signature = snap.signature
            never_announced = bool(prev and prev["baseline"]) and is_monitored
            last = prev["last_signature"] if prev else None
            if last == signature and not never_announced:
                changes.states.append(StateUpdate(snap, baseline=bool(prev["baseline"])))
                changes.merges.append(NewNotification(snap, origins, users))
                return
            if baseline_pending and not is_monitored:
                changes.states.append(StateUpdate(snap, baseline=True, set_signature=True))
                summary.baseline_recorded += 1
                return
            changes.states.append(StateUpdate(snap, set_signature=True))
            changes.notifications.append(NewNotification(snap, origins, users))

        for appid, sources in monitored.items():
            try:
                if appid in failed:
                    continue
                item = store_items.get(appid)
                if item is None:
                    continue
                prev = states.get(appid)
                special = specials_by_id.get(appid)
                in_specials = (
                    special is not None if specials is not None else bool(prev and prev["in_specials"])
                )
                name = item.name or (special.title if special else "") or (prev["name"] if prev else "")
                name = name or stored_names.get(appid, "")
                name = name or f"App {appid}"
                origins: set[str] = set()
                if sources["wishlist"]:
                    origins.add(ORIGIN_WISHLIST)
                if sources["manual"]:
                    origins.add(ORIGIN_MANUAL)
                if in_specials:
                    origins.add(ORIGIN_SPECIALS)
                changes.names[appid] = name
                process(
                    snapshot_from_store(item, name, in_specials),
                    origins,
                    sources["wishlist"] | sources["manual"],
                    True,
                )
            except Exception:
                log.exception("Skipping monitored app %s after unexpected error", appid)
                summary.errors.append(f"app {appid}")

        for appid, special in specials_by_id.items():
            if appid in monitored:
                continue
            try:
                process(snapshot_from_special(special), {ORIGIN_SPECIALS}, set(), False)
            except Exception:
                log.exception("Skipping special %s after unexpected error", appid)
                summary.errors.append(f"app {appid}")

        if specials is not None:
            for appid, st in states.items():
                if not st["on_sale"] or appid in specials_by_id or appid in monitored:
                    continue
                # Absent from the listing and nobody monitors it. A single absence can be
                # a pagination shift, so the end must be confirmed on consecutive cycles.
                if st["missing_cycles"] + 1 >= MISSING_CYCLES_TO_END:
                    changes.clear_specials.append(appid)
                else:
                    changes.bump_missing.append(appid)

        summary.enqueued = await self.repo.apply_cycle(changes)
        summary.baseline_completed = baseline_pending
