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
    "CENEJE", "TO NI AKCIJA", "KLUB KARTICO", "PLUS KARTICO",
)

_LETTERS = re.compile(r"[A-Za-zČčŠšŽžĆćĐđ]")


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
