"""Manual monitoring cycle triggered by an administrator (``/steam_verificar``).

The cycle itself is the regular :meth:`MonitorService.run_cycle` (same baseline, dedupe,
queue and dispatch path as the scheduled one); this module only decides whether a manual run
may start, runs it in a background task and hands the outcome to a delivery callback.

Concurrency: :meth:`ManualCheck.request` has no ``await``, so on the single event loop two
requests can never both pass the checks. The monitor's own lock remains the guarantee against
overlapping cycles, including a scheduled cycle that starts at the same moment: in that case
the manual run notices it (``skipped``) and does not consume the cooldown.

The cooldown is global for the bot (one cycle serves every server), measured from the moment
a manual cycle actually started, and is not consumed when no cycle was started.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class ManualCheckResult:
    user_id: int
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"  # 'ok' | 'failed' | 'skipped'
    duration: float = 0.0
    specials_ok: bool = False
    specials_count: int = 0
    monitored: int = 0
    store_failed: int = 0
    wishlists_failed: int = 0
    enqueued: int = 0
    dispatched: int = 0
    errors: list[str] = field(default_factory=list)
    delivered: bool = True


@dataclass(frozen=True)
class RequestOutcome:
    status: str  # 'started' | 'running' | 'cooldown' | 'closing'
    remaining: int = 0


Deliver = Callable[[ManualCheckResult], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ManualCheck:
    def __init__(
        self,
        monitor,
        cooldown_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.monitor = monitor
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._now = now
        self._last_started: float | None = None
        self._tasks: set[asyncio.Task] = set()  # strong references until done
        # The task that is currently running the cycle (cleared before the result is delivered).
        self._running_task: asyncio.Task | None = None
        self._closing = False
        self.last_result: ManualCheckResult | None = None

    @property
    def active(self) -> bool:
        return self._running_task is not None or self.monitor.running

    def begin_close(self) -> None:
        """Refuse new requests from now on (called first when the bot shuts down)."""
        self._closing = True

    def cooldown_left(self) -> int:
        if self._last_started is None:
            return 0
        elapsed = self._clock() - self._last_started
        left = self.cooldown_seconds - elapsed
        return max(0, math.ceil(left))

    def request(self, user_id: int, deliver: Deliver) -> RequestOutcome:
        """Start a background cycle if allowed. Synchronous on purpose (see module docstring)."""
        if self._closing:
            return RequestOutcome("closing")
        if self.active:
            return RequestOutcome("running")
        left = self.cooldown_left()
        if left:
            return RequestOutcome("cooldown", left)
        task = asyncio.get_running_loop().create_task(self._run(user_id, deliver))
        self._tasks.add(task)
        self._running_task = task
        task.add_done_callback(self._on_done)
        return RequestOutcome("started")

    def _on_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if self._running_task is task:
            self._running_task = None

    async def _run(self, user_id: int, deliver: Deliver) -> None:
        result = ManualCheckResult(user_id=user_id, started_at=self._now())
        started = time.monotonic()
        try:
            if self.monitor.running:  # a scheduled cycle won the race: nothing was started
                result.status = "skipped"
            else:
                previous = self._last_started
                self._last_started = self._clock()
                summary = await self.monitor.run_cycle()
                if summary is None:
                    self._last_started = previous
                    result.status = "skipped"
                else:
                    result.status = "ok"
                    result.specials_ok = summary.specials_ok
                    result.specials_count = summary.specials_count
                    result.monitored = summary.monitored
                    result.store_failed = summary.store_failed
                    result.wishlists_failed = summary.wishlists_failed
                    result.enqueued = summary.enqueued
                    result.dispatched = summary.dispatched
                    result.errors = list(summary.errors)
        except Exception:
            log.exception("Manual check failed")
            result.status = "failed"
        if self._running_task is asyncio.current_task():
            self._running_task = None  # the cycle is over; delivering the result does not block new requests
        result.duration = time.monotonic() - started
        result.finished_at = self._now()
        if result.status != "skipped":
            self.last_result = result
        try:
            await deliver(result)
        except Exception as exc:
            result.delivered = False
            log.warning("Manual check result could not be delivered to the interaction: %s", type(exc).__name__)
        log.info(
            "Manual check by user %s finished: %s in %.0fs (enqueued=%d dispatched=%d errors=%d)",
            user_id, result.status, result.duration, result.enqueued, result.dispatched, len(result.errors),
        )

    async def shutdown(self) -> None:
        """Cancel running manual checks and wait for them to unwind."""
        self.begin_close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
