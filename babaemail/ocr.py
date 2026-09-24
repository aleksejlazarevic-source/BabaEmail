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

What works (2026-09, measured against a hand-verified answer key):

  * Isolate the two price inks (Lidl yellow, Lidl red) with colour masks.
  * Find each price GEOMETRICALLY: euro digits, a round dot on their baseline,
    and two top-aligned cents at ~0.75x height. That places the decimal point
    exactly and finds every priced tile on the answer-key pages.
  * Rebuild the glyphs on one baseline at a normal text size (32-64 px;
    Tesseract returns nothing for 250 px digits) and read them with a digit
    whitelist at several sizes and modes. A value is accepted only on a clear
    supermajority (`consensus`); disagreement leaves it None.
  * Small struck-through old prices: a continuous ink channel, 4x upscale, the
    strike line removed (`remove_strike`), glyphs segmented and the decimal
    point placed from the geometry.
  * Every OCR price must then pass the retailer's own arithmetic
    (`reconciles`: Lidl truncates the badge, so the reduction lies in
    [pct, pct+1)). Anything else is quarantined by the caller.
"""

from __future__ import annotations

import math
import os
import pathlib
import re
import shutil
import subprocess
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

# Tesseract is called as a binary rather than through pytesseract: one less
# dependency, and the same code works on Linux (CI, the dev box) and Windows.
TESSERACT_LANG = "slv"
_TESS_CHECKED: list[bool] = []

# The Slovenian model lives in a project-local tessdata dir (the Windows
# installer ships only English and Program Files is not writable).
_LOCAL_TESSDATA = pathlib.Path(__file__).resolve().parent.parent / "tessdata"
if _LOCAL_TESSDATA.is_dir():
    os.environ.setdefault("TESSDATA_PREFIX", str(_LOCAL_TESSDATA))


@lru_cache(maxsize=1)
def _tesseract_cmd() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    for cand in ("/usr/bin/tesseract", "/usr/local/bin/tesseract",
                 "C:/Program Files/Tesseract-OCR/tesseract.exe",
                 "C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"):
        if os.path.exists(cand):
            return cand
    return None


def _run_tesseract(image: np.ndarray, args: list[str]) -> str:
    """Run the tesseract binary on an in-memory image, return stdout."""
    cmd = _tesseract_cmd()
    if cmd is None:
        raise OcrUnavailable("tesseract binary not found")
    if cv2 is None:
        raise OcrUnavailable("opencv not installed")
    ok, png = cv2.imencode(".png", image)
    if not ok:
        raise OcrUnavailable("could not encode image for tesseract")
    # One thread per call: the reads are many and tiny, and Tesseract's
    # OpenMP threads only contend with each other (and with parallel pages).
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    proc = subprocess.run([cmd, "stdin", "stdout", *args], input=png.tobytes(),
                          capture_output=True, timeout=120, env=env)
    if proc.returncode != 0:
        raise OcrUnavailable(f"tesseract failed: {proc.stderr[-300:]!r}")
    return proc.stdout.decode("utf-8", "replace")


# Lidl's price yellow, sampled from the leaflet. Isolating it is what turns
# text-printed-over-photography into clean, segmentable shapes.
YELLOW_LO = dict(r=170, g=150, b=130, spread=70)

# RapidOCR silently returns [] when an image has too little white margin.
# That cost an hour to find; do not reduce it. (Tesseract is not sensitive to
# this, which is one more reason it is preferred.)
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


def read_text(image: np.ndarray) -> list["OcrBox"]:
    """OCR an image, returning boxes.

    Uses RapidOCR, deliberately, for whole leaflet pages.

    The two engines are complementary and neither wins outright:

      * RapidOCR has a neural text DETECTOR and finds the headline prices and
        badges, which are yellow text printed over product photography.
      * Tesseract's binariser cannot see them at all - on Lidl page 4 it
        returns 22 lines, all small print, and misses every price - but it
        reads Slovenian properly once given clean input, where RapidOCR has no
        Slovenian charset.

    So: RapidOCR locates text on the page, and Tesseract reads the isolated,
    colour-masked price strips (`read_price_group`) and cropped names.
    """
    engine = _engine()
    result, _ = engine(image)
    out: list[OcrBox] = []
    for box, text, conf in result or []:
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        try:
            c = float(conf)
        except Exception:  # noqa: BLE001
            c = 0.0
        out.append(OcrBox(text=str(text).strip(), conf=c,
                          bbox=(min(xs), min(ys), max(xs), max(ys))))
    return out


def tesseract_available() -> bool:
    """True when the Tesseract binary AND the Slovenian model are present."""
    if not _TESS_CHECKED:
        ok = False
        cmd = _tesseract_cmd()
        if cmd is not None:
            try:
                out = subprocess.run([cmd, "--list-langs"], capture_output=True,
                                     timeout=30).stdout.decode("utf-8", "replace")
                ok = TESSERACT_LANG in out.split()
            except Exception:  # noqa: BLE001
                ok = False
        _TESS_CHECKED.append(ok)
    return _TESS_CHECKED[0]


def read_digits_tesseract(image: np.ndarray) -> str:
    """Recognise a digits-only strip. Whitelisting the charset stops Tesseract
    proposing letters for glyphs it is unsure about, which is most of the error
    on a price strip."""
    return _run_tesseract(image, ["--psm", "7", "-c",
                                  "tessedit_char_whitelist=0123456789"]).strip()


def read_text_tesseract(image: np.ndarray, lang: str = TESSERACT_LANG,
                        min_conf: float = 30.0) -> list["OcrBox"]:
    """OCR with the Slovenian model, returning LINE-level boxes.

    Lines, not words: Tesseract splits "-27%" into "27" and "%" and "1,79" can
    arrive in pieces, so word-level output matches no price or badge pattern at
    all. Regrouping words back into their source line restores both.

    --psm 3 (automatic page segmentation) massively outperforms --psm 11
    (sparse text) on these leaflets: psm 11 returns character noise
    ('na', 'n', 'ILI') while psm 3 reads "Slovenska rdeča jabolka" correctly.
    """
    tsv = _run_tesseract(image, ["-l", lang, "--psm", "3", "-c", "tessedit_create_tsv=1"])
    rows = [r.split("\t") for r in tsv.splitlines()]
    head, rows = rows[0], [r for r in rows[1:] if len(r) == len(rows[0])]
    data = {k: [r[i] for r in rows] for i, k in enumerate(head)}
    for k in ("block_num", "par_num", "line_num", "left", "top", "width", "height"):
        data[k] = [int(v) for v in data.get(k, [])]

    lines: dict[tuple[int, int, int], dict] = {}
    for i in range(len(data["text"])):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:  # noqa: BLE001
            conf = -1.0
        if conf < min_conf:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x, y = float(data["left"][i]), float(data["top"][i])
        w, h = float(data["width"][i]), float(data["height"][i])
        entry = lines.setdefault(key, {"words": [], "conf": [],
                                       "bbox": [x, y, x + w, y + h]})
        entry["words"].append(text)
        entry["conf"].append(conf)
        bb = entry["bbox"]
        bb[0] = min(bb[0], x); bb[1] = min(bb[1], y)
        bb[2] = max(bb[2], x + w); bb[3] = max(bb[3], y + h)

    out: list[OcrBox] = []
    for entry in lines.values():
        text = " ".join(entry["words"])
        conf = sum(entry["conf"]) / max(1, len(entry["conf"])) / 100.0
        out.append(OcrBox(text=text, conf=conf, bbox=tuple(entry["bbox"])))
    return out


# ---------------------------------------------------------------------------
# Colour isolation
#
# Lidl prints every price in one of two inks: Lidl yellow ~(253,241,1) or
# Lidl red ~(237,28,36). The background varies (red, maroon, navy, light blue,
# salmon, photography, yellow, orange-yellow, light grey, beige), so each ink
# gets one mask that works on all of them.

def glyph_masks(rgb: np.ndarray) -> dict[str, np.ndarray]:
    """Binary masks of the two price inks, keyed "Y" and "R".

    The yellow mask needs G > 205: the orange-yellow tile background
    (252,193,50) is a BACKGROUND for red digits and must not read as yellow.
    The red mask needs R > 180 so the maroon tile background (166,11,17) is
    excluded; red digits never sit on maroon.
    """
    a = rgb.astype(np.int16)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    yellow = (R > 200) & (G > 205) & (B < 130) & (R - B > 110)
    red = (R > 180) & (G < 110) & (B < 120) & (R - G > 110)
    return {"Y": yellow.astype(np.uint8), "R": red.astype(np.uint8)}


def ink_channel(rgb: np.ndarray, ink: str) -> np.ndarray:
    """Continuous 'how much of this ink' channel, for small text.

    yellowness = min(R,G) - B : yellow ~240; red, maroon, navy, light blue,
                                white, grey and teal photography all <= ~0.
    redness    = R - max(G,B)  : red ~200; yellow ~12, orange-yellow ~59,
                                grey/beige ~0-30.
    A hard mask thins small anti-aliased glyphs to broken strokes; this keeps
    the grey levels so they can be upscaled before thresholding.
    """
    a = rgb.astype(np.int16)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    v = np.minimum(R, G) - B if ink == "Y" else R - np.maximum(G, B)
    return np.clip(v, 0, 255).astype(np.uint8)


def lp_blue_mask(rgb: np.ndarray) -> np.ndarray:
    """The Lidl Plus logo blue ~(10,90,159)."""
    a = rgb.astype(np.int16)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    return ((R < 70) & (G > 55) & (G < 130) & (B > 120) & (B < 200)
            & (B - R > 90)).astype(np.uint8)


def yellow_mask(rgb: np.ndarray) -> np.ndarray:
    """Kept for callers of the old API."""
    return glyph_masks(rgb)["Y"]


@dataclass(slots=True)
class Blob:
    x: int
    y: int
    w: int
    h: int
    area: int
    index: int

    @property
    def x1(self) -> int:
        return self.x + self.w

    @property
    def y1(self) -> int:
        return self.y + self.h

    @property
    def fill(self) -> float:
        return self.area / max(1, self.w * self.h)


def components(mask: np.ndarray, min_h: int = 1,
               min_area: int = 1) -> tuple[np.ndarray, list[Blob]]:
    if cv2 is None:
        raise OcrUnavailable("opencv not installed")
    n, labels, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = [Blob(int(st[i][0]), int(st[i][1]), int(st[i][2]), int(st[i][3]),
                int(st[i][4]), i)
           for i in range(1, n) if st[i][3] >= min_h and st[i][4] >= min_area]
    return labels, out


# ---------------------------------------------------------------------------
# Price structure
#
# A Lidl price is: 1-2 large euro digits, a large round dot on their baseline,
# and exactly two cents digits about 0.75x as tall, TOP-aligned with the euros.
# That signature is found geometrically, which also locates the decimal point
# exactly - no "last two digits are the cents" guess is needed.

@dataclass(slots=True)
class PriceStructure:
    ink: str
    euros: list[Blob]
    cents: list[Blob]
    dot: Blob
    height: int                      # euro digit height, px

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (self.euros[0].x, min(b.y for b in self.euros + self.cents),
                self.cents[-1].x1, max(b.y1 for b in self.euros + [self.dot]))


def find_price_structures(blobs: list[Blob], ink: str,
                          min_height: int = 90) -> list[PriceStructure]:
    digits = [b for b in blobs if b.h >= min_height * 0.6
              and 0.15 < b.w / b.h < 1.2 and b.fill < 0.85]
    found: dict[int, PriceStructure] = {}
    for c1 in digits:
        for c2 in digits:
            if c2.x <= c1.x or abs(c2.h - c1.h) > 0.1 * c1.h or abs(c2.y - c1.y) > 0.08 * c1.h:
                continue
            gap = c2.x - c1.x1
            if gap < -0.05 * c1.h or gap > 0.35 * c1.h:
                continue
            euros = [e for e in digits if e.h >= min_height
                     and 0.6 < c1.h / e.h < 0.88 and abs(e.y - c1.y) < 0.1 * e.h
                     and e.x1 <= c1.x + 0.1 * c1.h and c1.x - e.x1 < 0.9 * e.h]
            if not euros:
                continue
            last = max(euros, key=lambda e: e.x)
            he = last.h
            chain = [last]
            while True:
                prev = [e for e in digits if abs(e.h - he) < 0.08 * he
                        and abs(e.y - last.y) < 0.06 * he and e.x < chain[0].x
                        and chain[0].x - e.x1 < 0.3 * he]
                if not prev:
                    break
                chain.insert(0, max(prev, key=lambda e: e.x))
            base = last.y1
            dots = [d for d in blobs if 0.1 * he < d.h < 0.4 * he and 0.6 < d.w / d.h < 1.6
                    and d.fill > 0.55 and abs(d.y1 - base) < 0.1 * he
                    and last.x1 - 0.25 * he < d.x + d.w / 2 < c1.x + c1.w * 0.7]
            if not dots:
                continue
            ps = PriceStructure(ink, chain, [c1, c2], dots[0], he)
            prev_ps = found.get(c1.index)
            if prev_ps is None or len(chain) > len(prev_ps.euros):
                found[c1.index] = ps
    # a third "cents" digit would mean c2 was itself a first cent: keep only
    # structures whose cents are not another structure's euros
    euro_ids = {e.index for ps in found.values() for e in ps.euros}
    return [ps for ps in found.values() if not any(c.index in euro_ids for c in ps.cents)]


# ---------------------------------------------------------------------------
# Recognition
#
# Tesseract reads a clean black-on-white LINE well only when the glyphs are
# a normal text size: 250px-tall digits come back empty. So every strip is
# normalised to a small set of glyph heights and read several ways; callers
# accept a value only when the readings agree.

READ_HEIGHTS = (32, 48, 64)
READ_PSMS = (7, 13)


def glyph_strip(labels: np.ndarray, blobs: list[Blob], height: int = 64,
                gap: int = 12, pad: int = 24) -> np.ndarray:
    """Paste glyphs side by side on one baseline, each scaled to `height`.
    Removes the raised-cents baseline that defeats line OCR."""
    masks = [labels[b.y:b.y1, b.x:b.x1] == b.index for b in sorted(blobs, key=lambda b: b.x)]
    return mask_strip(masks, height, gap, pad)


def mask_strip(masks: list[np.ndarray], height: int = 64, gap: int = 12,
               pad: int = 24) -> np.ndarray:
    """One glyph per mask (already cropped), left to right."""
    parts = []
    for m in masks:
        g = np.where(m, 0, 255).astype(np.uint8)
        s = height / m.shape[0]
        g = cv2.resize(g, (max(1, round(m.shape[1] * s)), height), interpolation=cv2.INTER_AREA)
        # a narrow "1" pasted tight against its neighbours is often dropped;
        # centre every glyph in a cell at least 0.6x as wide as it is tall
        need = int(0.6 * height) - g.shape[1]
        if need > 0:
            g = cv2.copyMakeBorder(g, 0, 0, need // 2, need - need // 2,
                                   cv2.BORDER_CONSTANT, value=255)
        parts.append(g)
        parts.append(np.full((height, gap), 255, np.uint8))
    strip = np.hstack(parts[:-1])
    return cv2.copyMakeBorder(strip, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


def read_strip_votes(strips: list[np.ndarray]) -> list[str]:
    """Digit-whitelisted reads of prepared strips, in both line modes."""
    return [_run_tesseract(st, ["--psm", str(psm), "-c", "tessedit_char_whitelist=0123456789"]).strip()
            for st in strips for psm in READ_PSMS]


def fit_height(ink: np.ndarray, height: int, pad: int = 20) -> np.ndarray | None:
    """ink: bool/0-1 foreground image. Crop to content, scale so the content
    is `height` px tall, return black-on-white with a margin."""
    ys, xs = np.nonzero(ink)
    if len(ys) == 0:
        return None
    crop = np.where(ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1], 0, 255).astype(np.uint8)
    s = height / crop.shape[0]
    crop = cv2.resize(crop, (max(1, round(crop.shape[1] * s)), height),
                      interpolation=cv2.INTER_AREA)
    return cv2.copyMakeBorder(crop, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


def read_votes(ink: np.ndarray, whitelist: str, heights=READ_HEIGHTS,
               psms=READ_PSMS) -> list[str]:
    """Read a foreground image at several sizes and segmentation modes."""
    out = []
    for h in heights:
        img = fit_height(ink, h)
        if img is None:
            return []
        for psm in psms:
            out.append(_run_tesseract(img, ["--psm", str(psm), "-c",
                                            f"tessedit_char_whitelist={whitelist}"]).strip())
    return out


def consensus(votes: list[str], pattern: re.Pattern, min_share: float = 0.75,
              max_dissent: int = 1, min_votes: int = 3) -> str | None:
    """The reading a clear supermajority of votes agree on, or None.

    A vote that does not match `pattern` (empty, a dropped digit, the wrong
    digit count) is a failed read. The winner needs `min_share` of ALL votes
    (failed reads count against it), and no contrary well-formed reading may
    have more than `max_dissent` votes (callers may drop votes from renderings
    that failed before reading, e.g. an unsegmentable old price): two renderings that both see a 2
    where the rest see a 5 mean the digit is genuinely ambiguous.

    Measured on the training pages, a single rendering misreads an old price
    about 4% of the time, sporadically and in different places, so strict
    unanimity over 6-12 votes would reject ~1 in 4 good readings while adding
    no protection against a SYSTEMATIC misread - that is the reconcile
    gate's job, which every OCR price must still pass afterwards."""
    valid = [v for v in votes if pattern.fullmatch(v)]
    if len(valid) < min_votes:
        return None
    ranked = sorted(set(valid), key=valid.count, reverse=True)
    best = ranked[0]
    if valid.count(best) < min_share * len(votes):
        return None
    if any(valid.count(r) > max_dissent for r in ranked[1:]):
        return None
    return best


def read_glyphs(labels: np.ndarray, blobs: list["Blob"], n_digits: int) -> tuple[str | None, list[str]]:
    """Read a run of digit glyphs whose count is known from the geometry."""
    votes = read_strip_votes([glyph_strip(labels, blobs, height=h) for h in READ_HEIGHTS])
    return consensus(votes, re.compile(rf"\d{{{n_digits}}}")), votes


def remove_strike(bw: np.ndarray) -> np.ndarray:
    """Remove a horizontal strike-through line from a 0/1 image of an old
    price. Where a digit stroke crosses the line (ink directly above AND
    below it) the line pixels are kept; everywhere else they are cleared."""
    h, _ = bw.shape
    cols = np.nonzero(bw.any(0))[0]
    if len(cols) == 0:
        return bw
    x0, x1 = cols[0], cols[-1] + 1
    rows = np.nonzero(bw.any(1))[0]
    text_h = rows[-1] - rows[0] + 1
    frac = bw[:, x0:x1].mean(1)
    if frac.max() < 0.75:
        return bw
    # An upscaled 1-2px line can come out as two bands with a lighter middle,
    # so take every near-full-width row close to the strongest one.
    peak = int(np.argmax(frac))
    span = max(4, int(0.25 * text_h))
    hi = [r for r in range(max(0, peak - span), min(h, peak + span + 1)) if frac[r] > 0.7]
    r0, r1 = min(hi), max(hi)
    while r0 - 1 >= 0 and frac[r0 - 1] > 0.45:
        r0 -= 1
    while r1 + 1 < h and frac[r1 + 1] > 0.45:
        r1 += 1
    k = max(3, (r1 - r0 + 1) // 2)
    out = bw.copy()
    for x in range(x0, x1):
        if not (bw[max(0, r0 - k):r0, x].any() and bw[r1 + 1:r1 + 1 + k, x].any()):
            out[r0:r1 + 1, x] = 0
    return out


def digits_to_price(digits: str) -> Decimal | None:
    """"129" -> 1,29 ; "75" -> 0,75. Last two digits are always the cents."""
    digits = re.sub(r"\D", "", digits or "")
    if not (2 <= len(digits) <= 6):
        return None
    euros = digits[:-2] or "0"
    return Decimal(f"{euros}.{digits[-2:]}")


def reconciles(new: Decimal | None, old: Decimal | None,
               pct: Decimal | None) -> bool:
    """True when a published new price, old price and badge agree.

    Lidl TRUNCATES the badge (1.99 from 3.49 is 42.98% and prints -42%), so
    the actual reduction must lie in [pct, pct + 1). Exact Decimal
    arithmetic, no tolerance: the old +-0.06 EUR check was both too loose on
    cheap items and wrong in direction."""
    if new is None or old is None or pct is None:
        return False
    if not (Decimal(0) < new < old) or not (Decimal(0) < pct < Decimal(100)):
        return False
    actual = (old - new) / old * 100
    return pct <= actual < pct + 1


def repair_with_reconcile(digits: str, old: Decimal | None,
                          pct: Decimal | None) -> Decimal | None:
    """Recover a price whose leading digit OCR dropped, when the published old
    price and badge determine it. Returns None unless EXACTLY one prefix
    reconciles - an ambiguous repair is a guess."""
    digits = re.sub(r"\D", "", digits or "")
    if old is None or pct is None or not digits:
        return None
    fits = [c for p in ("", "1", "2", "3", "4", "5", "6", "7", "8", "9")
            if (c := digits_to_price(p + digits)) is not None and reconciles(c, old, pct)]
    return fits[0] if len(fits) == 1 else None
