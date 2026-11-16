"""Persistent notification queue dispatcher.

Delivery semantics (at-least-once)
----------------------------------
A notification row is marked ``sent`` only *after* the sender returned
successfully (the Discord message exists). Therefore a failure leaves the row
``pending`` and it is retried in a later cycle, and notifications are never
lost. The trade-off: if the process crashes (or the database write fails)
between a successful ``send()`` and the ``UPDATE ... status='sent'``, the same
notification is sent again after restart. A duplicate is possible; an unrecorded loss is not.

Not every enqueued notification ends up on Discord, but none disappears silently:
``superseded`` (replaced by a newer state of the same game), ``expired`` (the game
stopped being on sale before delivery) and ``failed`` (5 permanent delivery errors)
stay in the database with that status.

At most ``max_per_cycle`` notifications are sent per call, oldest first; the
rest stay ``pending`` for later cycles. Notifications replaced by a newer
promotion of the same game are marked ``superseded`` when the newer one is
enqueued (see ``Repository.apply_cycle``), never silently dropped.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..db.repositories import Repository

log = logging.getLogger(__name__)

# ``send`` receives the notification payload and returns the Discord message id.
Sender = Callable[[dict], Awaitable[str | int | None]]


class ChannelUnavailable(Exception):
    """The configured notification channel cannot be used (missing/no access)."""


@dataclass
class DispatchResult:
    sent: int = 0
    failed: int = 0
    remaining: int = 0
    aborted: bool = False


def _is_fatal(exc: Exception) -> bool:
    """Errors that will hit every message too: stop the batch instead of hammering."""
    if isinstance(exc, ChannelUnavailable):
        return True
    return getattr(exc, "status", None) in (403, 404, 429)


class NotificationDispatcher:
    def __init__(self, repo: Repository, send: Sender, max_per_cycle: int = 20) -> None:
        self._repo = repo
        self._send = send
        self._max = max_per_cycle
        self._lock = asyncio.Lock()

    async def _with_reviews(self, payload: dict) -> dict:
        """Copy of the payload with the freshest cached review summary (never persisted).

        A failure to read the cache must not stop the notification: it goes out without reviews."""
        try:
            reviews = await self._repo.get_reviews(payload["appid"])
        except Exception:
            log.exception("Could not read cached reviews for app %s", payload.get("appid"))
            reviews = None
        return {**payload, "reviews": reviews}

    async def dispatch(self) -> DispatchResult:
        async with self._lock:
            result = DispatchResult()
            # Revalidate against the current state: games that are no longer on sale
            # are marked 'expired' (kept in the database) instead of being announced.
            await self._repo.expire_stale_pending()
            batch = await self._repo.pending_notifications(self._max)
            for row in batch:
                try:
                    message_id = await self._send(await self._with_reviews(row["payload"]))
                except Exception as exc:
                    result.failed += 1
                    log.warning(
                        "Notification %s for app %s not delivered: %s",
                        row["id"], row["appid"], type(exc).__name__,
                    )
                    fatal = _is_fatal(exc)
                    # Channel-level problems do not count towards the 'failed' threshold.
                    await self._repo.mark_failed(row["id"], f"{type(exc).__name__}: {exc}", permanent=not fatal)
                    if fatal:
                        result.aborted = True
                        break
                    continue
                try:
                    await self._repo.mark_sent(row["id"], message_id)
                except Exception:
                    # Message is already on Discord; it will be re-sent after
                    # the row is retried (at-least-once).
                    log.exception("Notification %s sent but could not be marked as sent", row["id"])
                    result.failed += 1
                    continue
                result.sent += 1
            result.remaining = await self._repo.count_notifications("pending")
            return result
