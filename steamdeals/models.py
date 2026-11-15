"""Domain value objects shared by the monitor, repositories and notifier."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

ORIGIN_SPECIALS = "specials"
ORIGIN_WISHLIST = "wishlist"
ORIGIN_MANUAL = "manual"


def price_hash(
    final_cents: int | None,
    original_cents: int | None,
    discount_pct: int | None,
    discount_end: int | None,
) -> str:
    """Hash of the full observed price state (end date included); detects any change."""
    raw = f"{final_cents}|{original_cents}|{discount_pct}|{discount_end}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def promotion_hash(
    final_cents: int | None,
    original_cents: int | None,
    discount_pct: int | None,
    epoch: int = 0,
) -> str:
    """Identity of one promotion: prices, discount and sale period. The end date is excluded."""
    raw = f"{final_cents}|{original_cents}|{discount_pct}"
    if epoch:
        raw += f"|e{epoch}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class DealSnapshot:
    """Current price state of one game as observed in a cycle."""

    appid: int
    name: str
    final_cents: int | None
    original_cents: int | None
    discount_pct: int
    discount_end: int | None
    on_sale: bool
    in_specials: bool = False
    formatted_final: str | None = None
    formatted_original: str | None = None
    image_url: str | None = None
    # Counts how many sale periods the game has been through, so that an
    # identical promotion returning after it ended is announced again.
    epoch: int = 0

    @property
    def state_hash(self) -> str:
        return price_hash(self.final_cents, self.original_cents, self.discount_pct, self.discount_end)

    @property
    def signature(self) -> str:
        """Identifies one promotion; only meaningful when ``on_sale``.

        Built from final price, original price, discount and sale period. The end
        date is deliberately not part of it: extending or adjusting the end of an
        otherwise identical promotion is not a new announcement.
        """
        return promotion_hash(self.final_cents, self.original_cents, self.discount_pct, self.epoch)


@dataclass
class StateUpdate:
    snapshot: DealSnapshot
    baseline: bool = False
    # True when the snapshot's signature should become ``last_signature``.
    set_signature: bool = False


@dataclass
class NewNotification:
    snapshot: DealSnapshot
    origins: set[str] = field(default_factory=set)
    user_ids: set[int] = field(default_factory=set)

    def payload(self) -> dict:
        s = self.snapshot
        return {
            "appid": s.appid,
            "name": s.name,
            "final_cents": s.final_cents,
            "original_cents": s.original_cents,
            "discount_pct": s.discount_pct,
            "discount_end": s.discount_end,
            "formatted_final": s.formatted_final,
            "formatted_original": s.formatted_original,
            "image_url": s.image_url,
            "origins": sorted(self.origins),
            "user_ids": sorted(self.user_ids),
        }


@dataclass
class CycleChanges:
    states: list[StateUpdate] = field(default_factory=list)
    clear_specials: list[int] = field(default_factory=list)
    bump_missing: list[int] = field(default_factory=list)
    notifications: list[NewNotification] = field(default_factory=list)
    merges: list[NewNotification] = field(default_factory=list)
    names: dict[int, str] = field(default_factory=dict)
    mark_baseline: bool = False
