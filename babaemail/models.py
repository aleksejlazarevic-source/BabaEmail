"""Core data structures.

Money is Decimal everywhere. A float rounding artefact that turns 4.99 into
4.989999 is exactly the class of bug that ends this project, so floats are
never used for prices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

# Slovenian price formatting: comma decimal separator, dot or space as the
# thousands separator.  "1.299,99 €", "4,99€", "€ 4,99", "4.99" (rare, ASCII).
_PRICE_CLEAN = re.compile(r"[^\d,.\-]")
_THOUSANDS_DOT = re.compile(r"\.(?=\d{3}(?:\D|$))")


class PriceParseError(ValueError):
    """Raised when a price string cannot be parsed unambiguously.

    Deliberately fatal rather than returning None: a silently skipped price is
    a silently missing item, which breaks the reliability contract.
    """


def parse_price(raw: str | int | float | Decimal | None) -> Decimal | None:
    """Parse a Slovenian-formatted price into Decimal. None stays None."""
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        # Only reachable if a source hands us a float; go via str to avoid
        # inheriting binary float error.
        return Decimal(str(raw))

    text = raw.strip()
    if not text:
        return None

    text = _PRICE_CLEAN.sub("", text)
    if not text:
        raise PriceParseError(f"no digits in price: {raw!r}")

    # Strip thousands dots ("1.299,99" -> "1299,99") before swapping the comma.
    text = _THOUSANDS_DOT.sub("", text)
    if "," in text:
        if text.count(",") > 1:
            raise PriceParseError(f"ambiguous price: {raw!r}")
        text = text.replace(",", ".")

    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise PriceParseError(f"unparseable price: {raw!r}") from exc
    return value


class SourceStatus(Enum):
    """Outcome of scraping one retailer. Only OK may contribute items."""

    OK = "ok"
    BLOCKED = "blocked"            # 403/429/captcha/empty DOM/canary mismatch
    NO_LEAFLET = "no_leaflet"      # nothing published for today
    PARSE_FAILED = "parse_failed"  # reached it, could not read it
    STALE = "stale"                # leaflet found but its validity has expired


@dataclass(slots=True)
class Deal:
    """One discounted product as published by a retailer.

    Deliberately permissive about prices, because real leaflets are:

    * Some tiles publish no single price at all - Mercator's "buy 2 or more,
      all items -50%" is a real 50% offer with no absolute figure. Forcing a
      price onto it would mean inventing one, so `price_new` is optional and
      `condition` carries the terms.
    * `discount_published` is the retailer's own badge (`-50%`). It is
      authoritative and is preferred over anything we compute.
    * `loyalty_required` marks Tus klub / SPAR plus / Lidl Plus deals. A deal
      she cannot actually get is a wasted trip, which the contract treats as
      seriously as a wrong price.

    `flags` carries quarantine reasons. A flagged Deal is still carried through
    the pipeline and surfaced to Aleks - never dropped (contract rule 4).
    """

    retailer: str
    name: str
    leaflet_id: str
    source_url: str
    valid_from: date
    valid_to: date | None   # None = retailer published no end date. NEVER invent one.

    price_new: Decimal | None = None
    price_old: Decimal | None = None
    discount_published: Decimal | None = None   # the retailer's own badge
    condition: str | None = None                # e.g. "ob nakupu 2 kosov ali vec"
    loyalty_required: str | None = None         # e.g. "Tus klub", "SPAR plus"
    price_30day_low: Decimal | None = None      # PC30 / PC - legally required in SI
    brand: str | None = None
    unit_price: str | None = None               # e.g. "4,98 EUR/kg", as published
    image_url: str | None = None
    category: str | None = None                 # filled by categorize.py
    retailer_category: str | None = None        # retailer's own label; a hint
    source_page: int | None = None
    retailer_item_id: str | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def discount_pct(self) -> Decimal | None:
        """The discount, preferring the retailer's published badge.

        Falls back to computing it from old and new price. Returns None when
        the retailer published neither - we never guess a discount.
        """
        if self.discount_published is not None:
            return self.discount_published
        if self.price_old is None or self.price_new is None or self.price_old <= 0:
            return None
        return (self.price_old - self.price_new) / self.price_old * 100

    @property
    def discount_display(self) -> int | None:
        """Whole-percent discount for display. Truncated, never rounded up, so
        the email can never overstate a saving."""
        pct = self.discount_pct
        return None if pct is None else int(pct)

    @property
    def has_absolute_price(self) -> bool:
        return self.price_new is not None

    @property
    def is_quarantined(self) -> bool:
        return any(f.startswith("quarantine:") for f in self.flags)

    def flag(self, reason: str) -> None:
        if reason not in self.flags:
            self.flags.append(reason)

    def quarantine(self, reason: str) -> None:
        """Hold this item back from the email, but never discard it."""
        self.flag(f"quarantine:{reason}")

    def identity(self) -> tuple[str, str, str]:
        """Stable key for day-over-day diffing and dedupe."""
        return (self.retailer, self.retailer_item_id or self.name.casefold(), self.leaflet_id)


@dataclass(slots=True)
class SourceResult:
    """What one retailer adapter returns for one run.

    Contract rule 6: an empty item list with status OK is not a valid result.
    `contributes` enforces that at the type level rather than by convention.
    """

    retailer: str
    status: SourceStatus
    source_url: str
    fetched_at: datetime
    items: list[Deal] = field(default_factory=list)

    leaflet_id: str | None = None
    leaflet_title: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    pages_declared: int | None = None
    pages_parsed: int | None = None
    error: str | None = None
    raw_snapshot: Path | None = None

    @property
    def contributes(self) -> bool:
        """True only when this result may put items in front of grandma."""
        return self.status is SourceStatus.OK and bool(self.items)

    @property
    def quarantined(self) -> list[Deal]:
        return [d for d in self.items if d.is_quarantined]

    def fail(self, status: SourceStatus, error: str) -> SourceResult:
        """Mark this result failed. Items are kept for debugging but
        `contributes` is now False, so they can never reach the email."""
        self.status = status
        self.error = error
        return self
