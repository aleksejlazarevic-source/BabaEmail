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


CONDITION_KINDS = ("second-item", "bonus-pack", "from-price", "multi-buy")
SOURCE_KINDS = ("leaflet", "website_only")

CENT = Decimal("0.01")
_MONEY_FIELDS = frozenset({"price_new", "price_old", "price_30day_low"})


def money(value: Decimal | int | None, what: str = "price") -> Decimal | None:
    """A price as Decimal with exactly two places: 3 -> Decimal("3.00").

    Never rounds: a value with fractional cents (4.989) is a parsing bug and
    raises instead of turning into a price nobody printed. Floats are refused
    for the same reason (module docstring). None stays None.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"{what} must be Decimal, got {type(value).__name__}: {value!r}")
    d = Decimal(value)
    if not d.is_finite():
        raise ValueError(f"{what} is not a finite number: {value!r}")
    q = d.quantize(CENT)
    if q != d:
        raise ValueError(f"{what} has fractional cents: {value!r}")
    return q


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

    Prices (`price_new`, `price_old`, `price_30day_low`) are normalised by
    `money()` on every assignment, at creation and after: always Decimal with
    two places ("3.00", never "3"), never a float, never fractional cents.

    `price_unit` is "kg" when the prices are per kilogram (Lidl "za kg", loose
    produce), so the email can never show 5.99 as the price of one piece.
    None means the price is for the item or pack as sold.

    `source_kind` is a structured section marker. It defaults to `leaflet`;
    Lidl API offers absent from the printed leaflet use `website_only` so an
    email writer never has to infer provenance by matching a display flag.

    `condition_kind` says how to read a conditional price (`condition` has the
    printed terms):
      "second-item"  price_new is the price of the SECOND piece, price_old the
                     first piece's full price ("-80% na drugi kos": 1.99, then
                     0.39). discount_pct is the saving over the two pieces
                     (40%, not the badge's 80%), so a filter never overstates it.
      "bonus-pack"   price_new is the price of the pack as sold; `condition`
                     says what is extra in it ("10+4 gratis"). No discount.
      "from-price"   price_new is a LOWER BOUND ("že od"), not a uniform
                     price across variants. Always display the qualifier.
                     No percentage saving can be inferred from a lower bound.
      "multi-buy"    price_new is the price of the whole P+F bundle, NEVER one
                     piece. `condition` carries P+F, any exact purchase count,
                     and the printed per-piece price needed for safe display.
    None means price_new is simply what the item or pack costs.

    `flags` carries quarantine reasons. A flagged Deal is still carried through
    the pipeline and surfaced to Aleks - never dropped (contract rule 4).
    """

    retailer: str
    name: str
    leaflet_id: str
    source_url: str
    valid_from: date
    valid_to: date | None   # None = retailer published no end date. NEVER invent one.

    source_kind: str = "leaflet"               # email section, not a display flag
    price_new: Decimal | None = None
    price_old: Decimal | None = None
    price_unit: str | None = None               # "kg" = prices are per kilogram
    discount_published: Decimal | None = None   # the retailer's own badge
    condition: str | None = None                # e.g. "ob nakupu 2 kosov ali vec"
    condition_kind: str | None = None           # see CONDITION_KINDS
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

    def __setattr__(self, name: str, value) -> None:
        if name in _MONEY_FIELDS:
            value = money(value, name)
        elif name == "price_unit" and value not in (None, "kg"):
            raise ValueError(f"price_unit must be None or 'kg', got {value!r}")
        elif name == "condition_kind" and value not in (None, *CONDITION_KINDS):
            raise ValueError(f"condition_kind must be None or one of {CONDITION_KINDS}, "
                             f"got {value!r}")
        elif name == "source_kind" and value not in SOURCE_KINDS:
            raise ValueError(f"source_kind must be one of {SOURCE_KINDS}, got {value!r}")
        object.__setattr__(self, name, value)

    @property
    def discount_pct(self) -> Decimal | None:
        """The discount, preferring the retailer's published badge.

        Falls back to computing it from old and new price. Returns None when
        the retailer published neither - we never guess a discount.
        """
        if self.condition_kind in ("from-price", "multi-buy"):
            return None
        if self.condition_kind == "second-item":
            if self.price_old is None or self.price_new is None or self.price_old <= 0:
                return None
            return (self.price_old - self.price_new) / (2 * self.price_old) * 100
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
class Notice:
    """A leaflet announcement with no product and no price ("Senior dan":
    pensioners get 10 % off on 30. 9.). Shown only when ``proven``; otherwise
    kept, with ``reason``, like a quarantined Deal (contract rule 4).
    ``text`` is the Slovenian sentence the email shows, built only from
    proven values; ``evidence`` is the printed text it was read from."""

    retailer: str
    kind: str                       # e.g. "senior-day"
    page: int
    day: date | None
    pct: int | None
    proven: bool
    reason: str
    text: str | None = None
    evidence: str = ""


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
    notices: list[Notice] = field(default_factory=list)   # never gate `contributes`

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
