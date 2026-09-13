"""Local OCR for image-only leaflets. Free, offline, no API.

Only Lidl needs this: its leaflet API returns `products: []` and its PDF has
zero text on all 58 pages. Everything else in the project parses real text.

Reading Lidl's prices took a specific method, because the obvious approaches
all fail (see FINDINGS.md):

  * Page-level OCR drops decimal points and whole digits - 1,29 reads as "29",
    2,49 as "249", 0,99 as "660" - because Lidl sets the euros large with the
    cents raised as superscript, and a line detector cannot handle two
    baselines.
  * Cropping tighter does not help; the price is yellow text printed straight
    over product photography, so the background is noise.
  * Recognising each digit separately does not work either: the model is
    trained on text lines and returns nothing for a single character.

What works: isolate the yellow, segment the digits geometrically, then paste
them back onto ONE baseline and recognise that strip as a line.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

import numpy as np

try:  # optional so the rest of the project imports without OCR installed
    import cv2
    from rapidocr_onnxruntime import RapidOCR
except Exception:  # noqa: BLE001  pragma: no cover
    cv2 = None
    RapidOCR = None


# Lidl's price yellow, sampled from the leaflet.
YELLOW_LO = dict(r=170, g=150, b=130, spread=70)

# RapidOCR silently returns [] when an image has too little white margin.
# This cost an hour to find; do not reduce it.
OCR_PAD = 120


@dataclass(slots=True)
class OcrBox:
    text: str
    conf: float
    bbox: tuple[float, float, float, float]   # x0, y0, x1, y1

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    def gap_to(self, other: "OcrBox") -> float:
        dx = max(other.bbox[0] - self.bbox[2], self.bbox[0] - other.bbox[2], 0.0)
        dy = max(other.bbox[1] - self.bbox[3], self.bbox[1] - other.bbox[3], 0.0)
        return math.hypot(dx, dy)


class OcrUnavailable(RuntimeError):
    """Raised when OCR dependencies are missing, so the caller can report
    BLOCKED rather than silently producing an empty leaflet."""


@lru_cache(maxsize=1)
def _engine():
    if RapidOCR is None or cv2 is None:
        raise OcrUnavailable("rapidocr-onnxruntime / opencv not installed")
    return RapidOCR()


def read_text(image: np.ndarray) -> list[OcrBox]:
    """OCR an image, returning boxes. Good for names, unreliable for prices."""
    engine = _engine()
    result, _ = engine(image)
    out: list[OcrBox] = []
    for box, text, conf in result or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        try:
            c = float(conf)
        except Exception:  # noqa: BLE001
            c = 0.0
        out.append(OcrBox(text=str(text).strip(), conf=c,
                          bbox=(min(xs), min(ys), max(xs), max(ys))))
    return out


def yellow_mask(rgb: np.ndarray) -> np.ndarray:
    """Binary mask of Lidl's price yellow. Turns text-over-photo into clean
    shapes, which is what makes the digits recoverable at all."""
    a = rgb.astype(np.int16)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    m = ((R > YELLOW_LO["r"]) & (G > YELLOW_LO["g"]) & (B < YELLOW_LO["b"])
         & ((R + G) / 2 - B > YELLOW_LO["spread"]))
    return m.astype(np.uint8)


def digit_clusters(mask: np.ndarray, min_area: int = 200,
                   min_height: int = 25) -> list[dict]:
    """Connected components of the mask that are plausible glyphs."""
    if cv2 is None:
        raise OcrUnavailable("opencv not installed")
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        if area < min_area or h < min_height:
            continue
        out.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h),
                    "area": int(area), "index": i})
    return out


def group_into_prices(blobs: list[dict], page_height: int | None = None,
                      x_gap_ratio: float = 1.0,
                      y_overlap: float = 0.3) -> list[list[dict]]:
    """Group glyph blobs into prices by adjacency.

    Union-find over pairwise adjacency rather than a chain through x-order: a
    leaflet page contains large yellow shapes (banners, badge plates) that sit
    between the digits in x-order and sever a chain, leaving every digit in its
    own group. Pairwise merging is immune to that.

    Blobs far too large to be glyphs are dropped first - one banner on Lidl's
    page 4 is 3.3 million pixels and 2286px tall.
    """
    if not blobs:
        return []

    def is_glyph(b: dict) -> bool:
        if page_height and b["h"] > page_height * 0.12:
            return False
        if b["w"] > 0 and b["h"] / b["w"] > 12:      # a rule or thin divider
            return False
        if b["w"] > 0 and b["w"] / max(1, b["h"]) > 6:
            return False
        # a solid block is a plate, not a digit
        return b["area"] <= b["w"] * b["h"] * 0.92

    glyphs = [b for b in blobs if is_glyph(b)]
    if not glyphs:
        return []

    parent = list(range(len(glyphs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(glyphs):
        for j in range(i + 1, len(glyphs)):
            b = glyphs[j]
            left, right = (a, b) if a["x"] <= b["x"] else (b, a)
            gap = right["x"] - (left["x"] + left["w"])
            if gap > min(a["h"], b["h"]) * x_gap_ratio:
                continue
            top = max(a["y"], b["y"])
            bottom = min(a["y"] + a["h"], b["y"] + b["h"])
            tallest = max(a["h"], b["h"])
            if tallest and (bottom - top) / tallest >= y_overlap:
                union(i, j)

    buckets: dict[int, list[dict]] = {}
    for i, b in enumerate(glyphs):
        buckets.setdefault(find(i), []).append(b)
    return [sorted(g, key=lambda b: b["x"]) for g in buckets.values()]


def read_price_group(mask: np.ndarray, labels: np.ndarray,
                     group: list[dict], target_height: int = 120) -> Decimal | None:
    """Recognise one price by rebuilding its digits on a single baseline.

    This is the core trick. The digits are normalised to a common height and
    pasted side by side, which removes the raised-superscript baseline that
    defeats the text detector, then the strip is read as one line. The decimal
    point is not reconstructed - the last two digits are the cents, which is
    how every leaflet in this project prints prices.
    """
    if cv2 is None:
        raise OcrUnavailable("opencv not installed")
    parts = []
    for b in sorted(group, key=lambda g: g["x"]):
        comp = (labels[b["y"]:b["y"] + b["h"], b["x"]:b["x"] + b["w"]] == b["index"])
        glyph = np.where(comp, 0, 255).astype(np.uint8)
        scale = target_height / max(1, b["h"])
        glyph = cv2.resize(glyph, (max(1, int(b["w"] * scale)), target_height),
                           interpolation=cv2.INTER_AREA)
        parts.append(glyph)
        parts.append(np.full((target_height, 18), 255, np.uint8))
    if not parts:
        return None
    strip = np.hstack(parts)
    strip = cv2.copyMakeBorder(strip, OCR_PAD, OCR_PAD, OCR_PAD, OCR_PAD,
                               cv2.BORDER_CONSTANT, value=255)
    boxes = read_text(cv2.cvtColor(strip, cv2.COLOR_GRAY2RGB))
    digits = "".join(re.sub(r"\D", "", b.text) for b in boxes)
    return digits_to_price(digits)


def digits_to_price(digits: str) -> Decimal | None:
    """"129" -> 1,29 ; "75" -> 0,75. Last two digits are always the cents."""
    digits = re.sub(r"\D", "", digits or "")
    if not (2 <= len(digits) <= 6):
        return None
    euros = digits[:-2] or "0"
    return Decimal(f"{euros}.{digits[-2:]}")


def repair_with_reconcile(digits: str, old: Decimal | None,
                          pct: Decimal | None,
                          tolerance: Decimal = Decimal("0.06")) -> Decimal | None:
    """Recover a price whose leading digit OCR dropped.

    Constrained repair, not guessing: the old price and the badge percentage are
    both published, so together they predict the new price. If the OCR digits
    with a prefix restored land within a few cents of that prediction, the
    prefix is determined by the retailer's own numbers rather than invented.

    Used only when plain reading disagrees with the prediction, and returns
    None when nothing fits - in which case the item is quarantined.
    """
    if old is None or pct is None or old <= 0:
        return None
    predicted = old * (Decimal(100) - pct) / Decimal(100)
    digits = re.sub(r"\D", "", digits or "")
    if not digits:
        return None
    for prefix in ("", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
        candidate = digits_to_price(prefix + digits)
        if candidate is not None and abs(candidate - predicted) <= tolerance:
            return candidate
    return None
