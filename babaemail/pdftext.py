"""Shared spatial extraction for leaflet PDFs with an embedded text layer.

Four of the five retailers (Mercator, Spar, Tuš, Hofer) publish real text in
their PDFs. Their layouts differ, but the problem is identical: a page is a grid
of product tiles, and each tile's name, prices and badge must be grouped
together. Reading order is useless — a tile's name often appears after its
price, sometimes beside it, sometimes below.

So: harvest spans with geometry, then group by proximity. Per-retailer rules
live in the adapters; only the mechanics live here.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal

import pymupdf


@dataclass(slots=True)
class Span:
    """One run of text with its size and position."""

    text: str
    size: float
    font: str
    bbox: tuple[float, float, float, float]

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    def size_is(self, target: float, tol: float = 0.6) -> bool:
        return abs(self.size - target) <= tol

    def distance_to(self, other: "Span") -> float:
        return math.hypot(self.cx - other.cx, self.cy - other.cy)

    def gap_to(self, other: "Span") -> float:
        """Edge-to-edge distance; 0 when the boxes overlap.

        Better than centre distance for deciding "is this label attached to
        that price", because a long product name's centre can be far away
        while its box is directly adjacent.
        """
        ax0, ay0, ax1, ay1 = self.bbox
        bx0, by0, bx1, by1 = other.bbox
        dx = max(bx0 - ax1, ax0 - bx1, 0.0)
        dy = max(by0 - ay1, ay0 - by1, 0.0)
        return math.hypot(dx, dy)


def spans(page: pymupdf.Page) -> list[Span]:
    """Every non-empty text span on the page, with geometry.

    ` ` (hair space) is used by several of these leaflets inside price
    strings and must be normalised away before parsing.
    """
    out: list[Span] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for s in line.get("spans", []):
                text = s["text"].replace(" ", " ").replace("\xa0", " ").strip()
                if text:
                    out.append(Span(text=text, size=round(s["size"], 1),
                                    font=s.get("font", ""), bbox=tuple(s["bbox"])))
    return out


def nearest(anchor: Span, candidates: list[Span], max_gap: float) -> Span | None:
    """Closest candidate by edge gap, or None if nothing is within `max_gap`."""
    best, best_gap = None, float("inf")
    for c in candidates:
        g = anchor.gap_to(c)
        if g < best_gap:
            best, best_gap = c, g
    return best if best is not None and best_gap <= max_gap else None


def claim_nearest(anchor: Span, pool: list[Span], max_gap: float) -> Span | None:
    """Like `nearest`, but removes the winner from `pool`.

    Prevents two tiles from both claiming the same product name, which is the
    main way greedy assignment goes wrong on a dense page.
    """
    found = nearest(anchor, pool, max_gap)
    if found is not None:
        pool.remove(found)
    return found


def reconcile(old: Decimal | None, new: Decimal | None,
              badge_pct: Decimal | None,
              slack: Decimal = Decimal("1.2")) -> bool | None:
    """Do the three published price signals agree?

    Returns True/False, or None when there is nothing to check against.

    Compares PERCENTAGES, not reconstructed prices. Retailers truncate the
    badge to a whole percent, so rebuilding the price from it is off by a few
    cents on almost every item - an early version compared prices with a 2c
    tolerance and quarantined 30% of perfectly good products.

    Example: Sir Jost, 14,99 -> 8,49 is a real 43.36% cut and the badge reads
    -43%. Reconstructing from 43% predicts 8,54, five cents adrift, yet nothing
    is wrong.

    The true relationship is `badge == floor(real_pct)`, so a correct item
    satisfies `0 <= real_pct - badge < 1`. `slack` allows a little either way
    for retailers that round rather than truncate. This still catches what
    matters: a misread digit moves the real percentage by tens of points.

    It CANNOT detect a missing item, only a misread one - completeness needs a
    separate, independent count (the badge-count gate in the adapters).
    """
    if old is None or new is None or badge_pct is None or old <= 0:
        return None
    real_pct = (old - new) / old * Decimal(100)
    delta = real_pct - badge_pct
    return Decimal("-0.2") <= delta <= slack


def page_images(doc: pymupdf.Document, index: int, zoom: float = 2.0):
    """Render a page — used by the audit harness to show the human the source."""
    return doc[index].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))

# Marketing furniture that sits at the same size as product names and must
# never be mistaken for one.
LABEL_STOPWORDS = (
    "ZNIŽANO", "ZNIZANO", "VEČ SE", "VEC SE", "SPLAČA", "SPLACA", "OB NAKUPU",
    "VSI KOSI", "SUPER & HIPER", "IZBRANI MARKETI", "PC30", "PC 30", "KOSA ALI",
    "TOČKA", "TOCKA", "NAKUPA", "MERCATORJEVE", "ZNAMKE", "GRATIS", "AKCIJA",
    "NOVA REDNA", "STARA CENA", "REDNA CENA", "SUPER CENA", "ZA KG", "ZA KOS",
    "V MESNICI", "V PEKARNI", "PRIPRAVLJENO V",
    "CENEJE", "TO NI AKCIJA", "KLUB KARTICO", "PLUS KARTICO",
)

_LETTERS = re.compile(r"[A-Za-zČčŠšŽžĆćĐđ]")

# Standalone marketing words. These appear as their own spans at the same size
# as product names, so a substring stoplist is not enough - they are rejected
# on an exact match of the whole span.
EXACT_STOPWORDS = frozenset({
    "SUPER", "CENA", "CENE", "POPUST", "POPUSTI", "PRIPRAVLJENO", "MESNICI",
    "NOVO", "AKCIJA", "GRATIS", "DARILO", "PAKET", "NAJ", "HIT", "TEDEN",
    "TEDNA", "IZBIRA", "KUPON", "BONUS", "EKSKLUZIVNO", "NOVOST", "ZNIŽANJE",
    "PRIHRANEK", "BREZPLAČNO", "KLUB", "KARTICO", "PLUS", "VELIKO",
    "SREDA", "SREDO", "PONEDELJEK", "TOREK", "ČETRTEK", "PETEK", "SOBOTA",
    "NEDELJA", "VIKEND", "MESNICI", "PEKARNI", "TRŽNICI",
})


def is_label_candidate(s: Span, lo: float = 6.5, hi: float = 16.0) -> bool:
    """Could this span be a product name or description?

    Deliberately NOT keyed to one font size: the name size varies by page
    template within a single catalogue (Mercator sets names at 7.8 on one page,
    9.0 on another, 12.0 on a third), so a hardcoded size silently loses whole
    pages of products.
    """
    if not (lo <= s.size <= hi):
        return False
    text = s.text.strip()
    if not (3 < len(text) <= 70):
        return False
    if len(_LETTERS.findall(text)) < 3:
        return False
    upper = text.upper()
    if upper in EXACT_STOPWORDS:
        return False
    if any(w in upper for w in LABEL_STOPWORDS):
        return False
    # a bare price or measurement is not a name
    if re.fullmatch(r"[\d.,/%\s€-]+", text):
        return False
    # letter-spaced decorative headings, e.g. "K J E R  D O Z O R I  O K U S"
    tokens = text.split()
    if len(tokens) >= 4 and sum(1 for t in tokens if len(t) == 1) / len(tokens) > 0.6:
        return False
    return True


def claim_label(anchor: Span, pool: list[Span], max_gap: float) -> Span | None:
    """Pick this tile's product NAME from nearby label spans, and claim it.

    Within a tile the name is typographically larger than its description, so
    among everything in range we take the largest size, breaking ties by
    proximity. Claiming removes it from the pool so a neighbouring tile cannot
    take the same name.
    """
    in_range = [(s, anchor.gap_to(s)) for s in pool]
    in_range = [(s, g) for s, g in in_range if g <= max_gap]
    if not in_range:
        return None
    best = max(in_range, key=lambda sg: (sg[0].size, -sg[1]))[0]
    pool.remove(best)
    return best

@dataclass(slots=True)
class LabelBlock:
    """A product's text block: name lines followed by description lines.

    These leaflets lay a product's text out as one PDF block whose first lines
    are the name and whose later lines are the description, often all at the
    SAME font size. Flattening to spans destroys that grouping and makes
    "za 16 pranj" look like a product name. So we keep the block.
    """

    lines: list[str]
    sizes: list[float]
    bbox: tuple[float, float, float, float]

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def max_size(self) -> float:
        return max(self.sizes) if self.sizes else 0.0

    @property
    def name(self) -> str:
        """Leading lines that read as a product name.

        A description line starts lowercase ("za 16 pranj", "vec vrst, 195 g")
        or is pure measurement, so we accumulate until one of those appears.
        Capped at three lines - anything longer is prose, not a name.
        """
        out: list[str] = []
        for line in self.lines:
            t = line.strip()
            if not t:
                continue
            if out and _looks_like_description(t):
                break
            out.append(t)
            if len(out) >= 3:
                break
        return " ".join(out).strip(" ,;:-")

    @property
    def description(self) -> str:
        name_len = len(self.name.split())
        joined = " ".join(l.strip() for l in self.lines if l.strip())
        return joined[len(self.name):].strip(" ,;:-") if joined.startswith(self.name) else ""

    def gap_to_point(self, x: float, y: float) -> float:
        dx = max(self.bbox[0] - x, x - self.bbox[2], 0.0)
        dy = max(self.bbox[1] - y, y - self.bbox[3], 0.0)
        return math.hypot(dx, dy)


def _looks_like_description(text: str) -> bool:
    first = text[0]
    if first.islower():
        return True
    # "500 g", "2 x 1 l", "30/1 ali 26/1", "1,8l"
    if re.fullmatch(r"[\d][\d.,/x\s]*[a-zA-Z]{0,4}[\d.,/x\s]*", text):
        return True
    return False


def label_blocks(page, lo: float = 5.5, hi: float = 16.0) -> list[LabelBlock]:
    """Text blocks that could be product labels, with their structure intact."""
    out: list[LabelBlock] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        lines: list[str] = []
        sizes: list[float] = []
        for line in block.get("lines", []):
            parts, lsizes = [], []
            for sp in line.get("spans", []):
                t = sp["text"].replace(" ", " ").replace(" ", " ").strip()
                if t:
                    parts.append(t)
                    lsizes.append(round(sp["size"], 1))
            if parts:
                lines.append(" ".join(parts))
                sizes.extend(lsizes)
        if not lines or not sizes:
            continue
        if not (lo <= max(sizes) <= hi):
            continue
        joined = " ".join(lines)
        if len(joined) < 4 or len(_LETTERS.findall(joined)) < 3:
            continue
        upper = joined.upper()
        if any(w in upper for w in LABEL_STOPWORDS):
            continue
        if len(joined) > 160:            # legal disclaimer, not a product
            continue
        out.append(LabelBlock(lines=lines, sizes=sizes, bbox=tuple(block["bbox"])))
    return out


def claim_block(x: float, y: float, pool: list[LabelBlock], max_gap: float) -> LabelBlock | None:
    """Claim the label block nearest to a point, removing it from the pool."""
    scored = [(b, b.gap_to_point(x, y)) for b in pool]
    scored = [(b, g) for b, g in scored if g <= max_gap]
    if not scored:
        return None
    best = min(scored, key=lambda bg: bg[1])[0]
    pool.remove(best)
    return best


def pick_name(labels: list[Span]) -> Span | None:
    """Choose the product NAME from one tile's label spans.

    Ranked by, in order:
      1. not description-like - a description starts lowercase ("za 16 pranj")
         or is pure measurement, and must never beat a real name;
      2. larger font - names are set larger than descriptions where the sizes
         differ at all;
      3. higher on the page - where everything shares one size, the name is the
         first line of the block.
    """
    if not labels:
        return None
    return max(labels, key=lambda s: (not _looks_like_description(s.text),
                                      s.size, -s.bbox[1]))


def partition_by_anchor(labels: list[Span], anchors: list[Span],
                        max_gap: float = 150.0) -> dict[int, list[Span]]:
    """Assign every label to its nearest anchor (a Voronoi split of the page).

    Assigning labels TO tiles, rather than claiming labels PER tile, is what
    makes this stable: claiming is order-sensitive and lets one tile steal a
    neighbour's name, which silently mislabels products. Here every label lands
    in exactly one bucket regardless of iteration order.
    """
    buckets: dict[int, list[Span]] = {i: [] for i in range(len(anchors))}
    if not anchors:
        return buckets
    for label in labels:
        i = min(range(len(anchors)), key=lambda k: anchors[k].gap_to(label))
        if anchors[i].gap_to(label) <= max_gap:
            buckets[i].append(label)
    return buckets


DIGITS_RE = re.compile(r"\d{1,4}")
CENTS_RE = re.compile(r"\d{2}")
PCT_INLINE_RE = re.compile(r"-?\s*(\d{1,2})\s*%")


def price_near(anchor: Span, digit_runs: list[Span],
               max_gap: float = 70.0,
               cents_gap: float = 40.0) -> tuple[Decimal | None, Span | None]:
    """Read a leaflet price laid out as a big euro figure plus raised cents.

    Returns (price, euro_span).

    Scale-relative on purpose. Font sizes for the same element vary from page to
    page within a single catalogue (Spar's euro figure appears at 21.9, 23.0 and
    23.2 in one leaflet), so absolute sizes silently lose whole pages. Instead:

      * the euro part is the LARGEST digit run near the anchor;
      * the cents are a 2-digit run that is smaller than it, sits to its right,
        and is close by.

    That relationship holds across every retailer in this project because they
    all use the same price typography.
    """
    near_runs = [s for s in digit_runs
                 if DIGITS_RE.fullmatch(s.text) and anchor.gap_to(s) <= max_gap]
    if not near_runs:
        return None, None
    euro = max(near_runs, key=lambda s: (s.size, -anchor.gap_to(s)))

    cand = [s for s in digit_runs
            if s is not euro
            and CENTS_RE.fullmatch(s.text)
            and s.size < euro.size * 0.85
            and s.cx > euro.cx
            and euro.gap_to(s) <= cents_gap]
    if not cand:
        return None, euro
    cents = min(cand, key=lambda s: euro.gap_to(s))
    try:
        return Decimal(f"{euro.text}.{cents.text}"), euro
    except Exception:  # noqa: BLE001
        return None, euro


def assemble_name(best: Span | None, bucket: list[Span],
                  size_tol: float = 0.35, x_tol: float = 6.0,
                  line_gap: float = 6.0, max_parts: int = 3) -> str:
    """Re-join a product name that the PDF split across lines.

    Leaflets wrap names ("PISCANCJI" / "FILE"), and taking only the single best
    span leaves grandma reading half a product name. Continuation lines share
    the chosen span's font size and left edge and sit directly beneath it, so
    they can be reattached without guessing.

    Stops at the first line that reads as a description, so the size/pack text
    underneath never gets glued onto the name.
    """
    if best is None:
        return ""
    parts = [best]
    current = best
    for _ in range(max_parts - 1):
        nxt = None
        for cand in bucket:
            if cand in parts:
                continue
            if abs(cand.size - current.size) > size_tol:
                continue
            if abs(cand.bbox[0] - current.bbox[0]) > x_tol:
                continue
            gap = cand.bbox[1] - current.bbox[3]
            if not (-2.0 <= gap <= line_gap):
                continue
            if _looks_like_description(cand.text):
                continue
            if nxt is None or cand.bbox[1] < nxt.bbox[1]:
                nxt = cand
        if nxt is None:
            break
        parts.append(nxt)
        current = nxt
    parts.sort(key=lambda s: (s.bbox[1], s.bbox[0]))
    return " ".join(s.text.strip() for s in parts).strip(" ,;:-")


def percent_digit_ids(page_spans: list[Span]) -> frozenset[int]:
    """Ids of digit runs that are the number part of a percentage.

    A digit run with a "%" immediately to its right is a percentage and can
    NEVER be a price's cents. Without this, Tus's badge digits get paired with
    the euro figure: "SVINJSKA REBRA" at 4,79 with a -50% badge was read as
    4,50, because the badge "50" (25.5pt) and the cents "79" (26.2pt) are
    nearly the same size and both sit to the right of the "4".

    That is a wrong price that passes every other check, so it is worth
    isolating precisely.
    """
    out: set[int] = set()
    marks = [s for s in page_spans if s.text.strip() == "%"]
    for mark in marks:
        # Only the SINGLE nearest digit run to the left belongs to this "%".
        # Marking everything within a radius also swallowed the big euro figure
        # sitting beside the badge, which deleted whole discounted tiles.
        candidates = [s for s in page_spans
                      if DIGITS_RE.fullmatch(s.text)
                      and s.cx < mark.cx
                      and abs(s.cy - mark.cy) <= max(mark.size, s.size)
                      and mark.gap_to(s) <= max(mark.size, s.size) * 1.5]
        if candidates:
            out.add(id(min(candidates, key=lambda s: mark.gap_to(s))))
    # inline forms like "-50%" carry their digits in the same span
    for s in page_spans:
        if PCT_INLINE_RE.fullmatch(s.text.strip()):
            out.add(id(s))
    return frozenset(out)


def price_tiles(page_spans: list[Span], size_ratio: float = 0.8,
                cents_gap: float = 45.0) -> list[tuple[Decimal | None, Span, Span | None]]:
    """Find every big-price tile on a page, anchored on the price itself.

    Needed where most tiles carry no discount badge at all - Tus prints "SUPER
    CENA" on most products and a percentage on only a few - so badges cannot be
    the anchor and the price must be.

    The euro figure is identified relative to the largest digit run on the page
    rather than by absolute size, for the same reason as `price_near`.
    """
    pct_ids = percent_digit_ids(page_spans)
    digit_runs = [s for s in page_spans
                  if DIGITS_RE.fullmatch(s.text) and id(s) not in pct_ids]
    if not digit_runs:
        return []
    biggest = max(s.size for s in digit_runs)
    euros = [s for s in digit_runs if s.size >= biggest * size_ratio]

    out: list[tuple[Decimal | None, Span, Span | None]] = []
    for euro in sorted(euros, key=lambda s: (round(s.bbox[1] / 20), s.bbox[0])):
        cand = [s for s in digit_runs
                if s is not euro
                and CENTS_RE.fullmatch(s.text)
                and s.size < euro.size * 0.85
                and s.cx > euro.cx
                and euro.gap_to(s) <= cents_gap]
        price, cents = None, None
        if cand:
            cents = min(cand, key=lambda s: euro.gap_to(s))
            try:
                price = Decimal(f"{euro.text}.{cents.text}")
            except Exception:  # noqa: BLE001
                price = None
        out.append((price, euro, cents))
    return out


def badge_near(anchor: Span, page_spans: list[Span], max_gap: float = 90.0) -> Decimal | None:
    """Read a discount percentage that the PDF split into separate spans.

    Tus renders "-50%" as three spans - "-", "50" and "%" - at three different
    font sizes, so a single regex over one span never matches. Anchor on the "%"
    and look left for the nearest digits on roughly the same line.
    """
    pcts = [s for s in page_spans if s.text.strip() == "%" or PCT_INLINE_RE.fullmatch(s.text.strip())]
    best: Decimal | None = None
    best_gap = float("inf")
    for p in pcts:
        gap = anchor.gap_to(p)
        if gap > max_gap:
            continue
        m = PCT_INLINE_RE.fullmatch(p.text.strip())
        if m:
            value = Decimal(m.group(1))
        else:
            # bare "%": find the closest digit run to its left on the same line
            left = [s for s in page_spans
                    if DIGITS_RE.fullmatch(s.text)
                    and s.cx < p.cx
                    and abs(s.cy - p.cy) <= max(p.size, s.size)
                    and p.gap_to(s) <= max(p.size, s.size) * 3]
            if not left:
                continue
            digits = max(left, key=lambda s: s.cx)      # nearest to the % sign
            try:
                value = Decimal(digits.text)
            except Exception:  # noqa: BLE001
                continue
        if not (Decimal(1) <= value <= Decimal(95)):
            continue
        if gap < best_gap:
            best, best_gap = value, gap
    return best


def badges_by_anchor(page_spans: list[Span], anchors: list[Span],
                     max_gap: float = 55.0,
                     consumed: frozenset[int] = frozenset()) -> dict[int, Decimal]:
    """Assign each discount percentage on the page to exactly one tile.

    A percentage must belong to a single product. Searching outward from each
    tile instead lets a PAGE-WIDE promotion be attached to every tile near it -
    Tus prints a "SREDA -11%" Wednesday banner, which that way became a phantom
    11% discount on dozens of unrelated products.

    Assigning each percentage to its nearest anchor means one badge can only
    ever belong to one product, and a banner far from any tile is dropped.

    Handles percentages split across spans ("-", "50", "%"), which Tus does.

    `consumed` holds ids of spans already used as a price's euro or cents.
    Without it the CENTS get read as the percentage: Tus sets cents at 26.2pt
    and badge digits at 25.5pt right beside a "%", so 3,21 became "-21%",
    7,50 became "-50%" and 1,80 became "-80%" - 126 fabricated discounts in one
    catalogue, every one of them caught by the reconcile gate.
    """
    scored: list[tuple[Span, Decimal]] = []
    for sp in page_spans:
        text = sp.text.strip()
        m = PCT_INLINE_RE.fullmatch(text)
        if m:
            value = Decimal(m.group(1))
        elif text == "%":
            left = [s for s in page_spans
                    if DIGITS_RE.fullmatch(s.text)
                    and id(s) not in consumed
                    and s.cx < sp.cx
                    and abs(s.cy - sp.cy) <= max(sp.size, s.size)
                    and sp.gap_to(s) <= max(sp.size, s.size) * 0.8]
            if not left:
                continue
            try:
                value = Decimal(max(left, key=lambda s: s.cx).text)
            except Exception:  # noqa: BLE001
                continue
        else:
            continue
        if Decimal(1) <= value <= Decimal(95):
            scored.append((sp, value))

    out: dict[int, Decimal] = {}
    if not anchors:
        return out
    for sp, value in scored:
        i = min(range(len(anchors)), key=lambda k: anchors[k].gap_to(sp))
        if anchors[i].gap_to(sp) <= max_gap and i not in out:
            out[i] = value
    return out
