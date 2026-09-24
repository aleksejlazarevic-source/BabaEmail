"""Leaflet notices: announcements with no product and no price.

The kw39 leaflet (p57) carried "Senior dan": pensioners get 10 % off one
whole purchase on 30. 9. 2026, in every Lidl. Nothing in the tile readers or
the offer API sees such a box, so this module reads each page's plain text
(Tesseract, Slovenian, ~2 s a page) and recognises the known kinds.

Same rule as for prices: a notice is SHOWN only when the print proves it,
otherwise it is kept and held back (``Notice.proven`` False, with the reason).

Senior day is proven when, in one passage of the fine print:
  * pensioners are named ("upokojen..."),
  * exactly one full date is given ("na dan 30. 9. 2026") and it lies inside
    the leaflet's own validity,
  * the percentage is stated at least TWICE with the same value (the rules
    text repeats it: "prejme 10 % popust ... 10 % popust je mogoče uveljaviti").
    Two separate print locations read alike are two readings, not one.

Coupon diamonds ("-30% z Lidl Plus" on Knorr, "-15% pri nakupu najmanj 3
izbranih kosov" on Vitasia) are white on colour and plain OCR does not read
them; the Knorr coupon prices come from the offer API as ordinary Lidl Plus
deals instead.
"""

from __future__ import annotations

import re
import subprocess
from datetime import date

from . import ocr
from .models import Notice

SENIOR = "senior-day"

_PENSIONER = re.compile(r"upokojen", re.I)
_PCT = re.compile(r"(?<!\d)(\d{1,2})\s*%\s*popust", re.I)
_ON_DAY = re.compile(r"na\s+dan\s+(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(20\d\d)", re.I)
_PASSAGE = 1200          # characters of fine print read around the "upokojen" mention


def page_text(png: bytes) -> str:
    """Plain text of one rendered page (sparse-text mode finds the small print
    boxes that automatic segmentation merges into photos)."""
    cmd = ocr._tesseract_cmd()
    if cmd is None:
        raise ocr.OcrUnavailable("tesseract binary not found")
    proc = subprocess.run([cmd, "stdin", "stdout", "-l", ocr.TESSERACT_LANG, "--psm", "11"],
                          input=png, capture_output=True, check=True, timeout=120)
    return proc.stdout.decode("utf-8", "replace")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def senior_day(page: int, text: str, window: tuple[date, date | None]) -> Notice | None:
    """The senior-day notice on this page, proven or not; None if there is none."""
    flat = _flat(text)
    m = _PENSIONER.search(flat)
    if m is None:
        return None
    passage = flat[max(0, m.start() - 200): m.start() + _PASSAGE]
    pcts = [int(x) for x in _PCT.findall(passage)]
    days = set()
    for d, mo, y in _ON_DAY.findall(passage):
        try:
            days.add(date(int(y), int(mo), int(d)))
        except ValueError:
            pass
    n = Notice("Lidl", SENIOR, page, None, None, False, "", evidence=passage)
    if len(days) != 1:
        n.reason = f"expected one date, read {sorted(days) or 'none'}"
        return n
    n.day = days.pop()
    lo, hi = window
    if n.day < lo or (hi is not None and n.day > hi):
        n.reason = f"date {n.day} outside the leaflet {lo}..{hi}"
        return n
    if not pcts:
        n.reason = "no percentage read"
        return n
    if len(set(pcts)) != 1:
        n.reason = f"percentages disagree: {pcts}"
        return n
    if len(pcts) < 2:
        n.reason = f"percentage {pcts[0]} read only once"
        return n
    n.pct = pcts[0]
    n.proven = True
    n.reason = f"{len(pcts)}x {n.pct} %, one date"
    n.text = (f"Senior dan: {n.day.day}. {n.day.month}. {n.day.year} imajo upokojenci "
              f"{n.pct} % popusta na cel nakup v vseh Lidlovih trgovinah. Na blagajni "
              f"pokažite upokojensko kartico ali pokojninski odrezek.")
    return n


def find_notices(texts: dict[int, str], window: tuple[date, date | None]) -> list[Notice]:
    """Every notice on every page, proven or held back. Never drops one."""
    out = []
    for page in sorted(texts):
        n = senior_day(page, texts[page], window)
        if n is not None:
            out.append(n)
    return out
