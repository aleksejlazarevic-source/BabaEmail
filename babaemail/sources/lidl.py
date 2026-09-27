"""Lidl - image-only leaflet, verified by three independent readers.

Lidl is the only retailer here with no machine-readable text in its leaflet:

  * `endpoints.leaflets.schwarz/v4/flyer` returns the page images and
    `products: []` - the product array is always empty for Slovenia.
  * The leaflet PDF has ZERO characters of text on every page.
  * lidl.si states that where the site and the leaflet disagree, the leaflet
    is authoritative.

So a deal ships only when the printed leaflet confirms it. The pipeline:

  1. flyer metadata (validity, PDF URL, page count)
  2. the PDF, every page rendered at zoom 3
  3. classical OCR on every page (this module + ocr.py), in parallel
     processes: exact new prices, old price / % only when they reconcile
     3b. plain page text for notices such as "Senior dan" (lidl_notices.py)
  4. the local VLM on every page (vlm.py, ollama, cached on disk): names,
     Lidl Plus, tile-level "velja do" dates
  5. lidl.si's own offer API over the leaflet window (lidl_api.py)
  6. lidl_reconcile.reconcile(): the rules that decide what ships and what is
     quarantined (never dropped)

Failure policy (contract rules 3, 5, 6):

  * OCR missing (opencv / tesseract)   -> BLOCKED, never "no offers".
  * VLM unavailable (ollama down, model missing, a page failed) -> BLOCKED,
    Lidl explicitly NOT covered. There is deliberately no silent API+OCR
    fallback: measured on the kw39 truth, API+OCR alone ships items with an
    end date LATER than the tile's own "velja do" (the VLM is the only reader
    of tile dates), and roughly a third fewer deals. `Lidl(use_vlm=False)`
    runs API+OCR for diagnosis only; its result is always a failure.
  * API failure -> classified like any HTTP failure (BLOCKED / PARSE_FAILED).
  * Zero shipped deals -> PARSE_FAILED (items kept for debugging).

Many Lidl offers are "z Lidl Plus" - app-only. She has the app, so they count,
but they are labelled.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import httpx
import numpy as np
import pymupdf

from .. import lidl_api, lidl_crops, lidl_notices, ocr, vlm
from ..lidl_reconcile import LeafletReading, ReconcileResult, from_vlm, reconcile
from ..models import SourceResult, SourceStatus
from ..ocr import Blob, OcrUnavailable, PriceStructure
from .base import UA, Source, classify_http_failure

log = logging.getLogger(__name__)

FLYER_API = "https://endpoints.leaflets.schwarz/v4/flyer"
RENDER_ZOOM = 3.0          # every reader and every truth set was built on zoom-3 renders
OCR_WORKERS = int(os.environ.get("BABAEMAIL_OCR_WORKERS", min(8, os.cpu_count() or 1)))


class OcrPageError(RuntimeError):
    """OCR crashed on one page. The page is unread, so Lidl is not covered."""


class Lidl(Source):
    name = "lidl"
    display_name = "Lidl"

    def __init__(self, *, use_vlm: bool = True, vlm_model: str | None = None,
                 ollama_url: str | None = None, ocr_workers: int | None = None):
        self.use_vlm = use_vlm
        self.vlm_model = vlm_model or vlm.DEFAULT_MODEL
        self.ollama_url = ollama_url or vlm.OLLAMA_URL
        self.ocr_workers = ocr_workers or OCR_WORKERS
        #: the last run's reconcile result and reader counts, for scripts/audit
        self.last_reconcile: ReconcileResult | None = None
        self.last_stats: dict = {}

    def flyer_identifier(self, today: date) -> str:
        iso = today.isocalendar()
        return f"lidlov-katalog-{iso.year}-kw{iso.week:02d}"

    def fetch(self, today: date) -> SourceResult:
        ident = self.flyer_identifier(today)
        res = self.result(SourceStatus.OK, f"{FLYER_API}?flyer_identifier={ident}",
                          leaflet_id=ident)
        self.last_reconcile, self.last_stats = None, {}
        stats = self.last_stats
        t_start = time.monotonic()

        # 1. flyer metadata
        try:
            meta = self._flyer_meta(ident)
        except httpx.HTTPStatusError as exc:
            return res.fail(classify_http_failure(exc.response.status_code, ""),
                            f"flyer api: HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return res.fail(SourceStatus.PARSE_FAILED, f"flyer api: {exc!r}")
        if meta is None:
            return res.fail(SourceStatus.NO_LEAFLET, f"no flyer for {ident}")

        res.valid_from = meta["valid_from"]
        res.valid_to = meta["valid_to"]
        res.leaflet_title = meta.get("title")
        res.pages_declared = meta["pages"] or None

        if not (meta["valid_from"] <= today <= meta["valid_to"]):
            return res.fail(SourceStatus.STALE,
                            f"{ident} valid {meta['valid_from']}..{meta['valid_to']}")

        # 2. the PDF, rendered
        try:
            blob = self._download_pdf(meta["pdf_url"])
        except httpx.HTTPStatusError as exc:
            return res.fail(classify_http_failure(exc.response.status_code, ""),
                            f"pdf: HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return res.fail(SourceStatus.PARSE_FAILED, f"pdf: {exc!r}")
        if not blob.startswith(b"%PDF"):
            return res.fail(SourceStatus.PARSE_FAILED, "response was not a PDF")
        try:
            pngs = self._render_pages(blob)
        except Exception as exc:   # pymupdf raises several unrelated types on a bad file
            return res.fail(SourceStatus.PARSE_FAILED, f"pdf render: {exc!r}")
        if not pngs:
            return res.fail(SourceStatus.PARSE_FAILED, "pdf has no pages")
        if res.pages_declared is None:
            res.pages_declared = len(pngs)
        elif res.pages_declared != len(pngs):
            # The flyer's page list is the independent count; a PDF that
            # disagrees with it is a different or truncated document.
            return res.fail(SourceStatus.PARSE_FAILED,
                            f"flyer declares {res.pages_declared} pages, PDF has {len(pngs)}")
        pages = sorted(pngs)
        stats["pages"] = len(pages)

        # 3. OCR, every page
        t0 = time.monotonic()
        try:
            ocr_readings = self._read_ocr(pngs)
        except OcrUnavailable as exc:
            # Never report "no offers" when the reason is that OCR is missing.
            return res.fail(SourceStatus.BLOCKED, f"ocr unavailable: {exc}")
        except (OcrPageError, BrokenProcessPool) as exc:
            return res.fail(SourceStatus.PARSE_FAILED, f"ocr failed: {exc}")
        stats["ocr_s"] = round(time.monotonic() - t0, 1)
        stats["ocr_readings"] = sum(len(v) for v in ocr_readings.values())
        missing = [p for p in pages if p not in ocr_readings]
        if missing:
            return res.fail(SourceStatus.PARSE_FAILED, f"ocr returned nothing for pages {missing}")

        # 3b. notices (Senior dan, ...): plain page text. Optional: a failure
        # here costs the notices, never the deals, and is recorded in stats.
        t0 = time.monotonic()
        try:
            res.notices = self._read_notices(pngs, (meta["valid_from"], meta["valid_to"]))
            for n in res.notices:
                n.retailer = self.name
        except (ocr.OcrUnavailable, OSError, subprocess.SubprocessError) as exc:
            stats["notices_error"] = repr(exc)
            log.warning("notices not read: %r", exc)
        stats["notices_s"] = round(time.monotonic() - t0, 1)
        stats["notices"] = [f"p{n.page} {n.kind} {'proven' if n.proven else 'held: ' + n.reason}"
                            for n in res.notices]

        # 4. VLM, every page
        vlm_readings: dict[int, list[LeafletReading]] = {}
        if self.use_vlm:
            t0 = time.monotonic()
            try:
                vlm_readings = self._read_vlm(pngs, ocr_readings)
            except vlm.VlmUnavailable as exc:
                return res.fail(SourceStatus.BLOCKED,
                                f"vlm unavailable, Lidl NOT covered: {exc}")
            stats["vlm_s"] = round(time.monotonic() - t0, 1)
            stats["vlm_readings"] = sum(len(v) for v in vlm_readings.values())

        # 5. API candidates over the leaflet window
        t0 = time.monotonic()
        try:
            cands = self._candidates(meta["valid_from"], meta["valid_to"])
        except lidl_api.LidlApiError as exc:
            status = (classify_http_failure(exc.status_code, "") if exc.status_code
                      else SourceStatus.PARSE_FAILED)
            return res.fail(status, f"lidl api: {exc}")
        except httpx.HTTPError as exc:
            return res.fail(SourceStatus.PARSE_FAILED, f"lidl api: {exc!r}")
        stats["api_s"] = round(time.monotonic() - t0, 1)
        stats["api"] = {"offers": len(cands), "listed": len(cands.no_signal),
                        "no_price": len(cands.no_price)}

        # 6. reconcile
        readings = [r for p in pages for r in ocr_readings[p] + vlm_readings.get(p, [])]
        # Optional targeted rereads, collected separately; never launch a
        # second full-leaflet VLM pass. Page hashes exclude stale evidence.
        try:
            crop_evidence = lidl_crops.load_crops(pngs)
            readings = lidl_crops.refine_readings(readings, crop_evidence)
            stats["crop_evidence"] = len(crop_evidence)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return res.fail(SourceStatus.PARSE_FAILED, f"crop evidence: {exc!r}")
        rr = reconcile(list(cands), readings, leaflet_id=ident,
                       leaflet_window=(meta["valid_from"], meta["valid_to"]),
                       listed=list(cands.no_signal), year=meta["valid_from"].year,
                       leaflet_url=meta["pdf_url"], ocr_pages=pages)
        self.last_reconcile = rr
        for d in rr.deals:
            d.retailer = self.name
        res.items = rr.deals
        # a page counts as parsed only when every reader in use has read it
        res.pages_parsed = sum(1 for p in pages if p in ocr_readings
                               and (not self.use_vlm or p in vlm_readings))
        stats["total_s"] = round(time.monotonic() - t_start, 1)
        stats["shipped"] = len(rr.shipped)
        stats["quarantined"] = len(rr.quarantined)
        stats["unmatched_listed"] = len(rr.unmatched_listed)

        if not self.use_vlm:
            return res.fail(SourceStatus.BLOCKED,
                            "VLM disabled: API+OCR diagnostic run, Lidl NOT covered "
                            "(without the VLM, tile 'velja do' dates are not read)")
        if res.pages_parsed != res.pages_declared:
            return res.fail(SourceStatus.PARSE_FAILED,
                            f"parsed {res.pages_parsed} of {res.pages_declared} pages")
        if not rr.shipped:
            # Contract rule 6. Quarantined items are kept for debugging only.
            return res.fail(SourceStatus.PARSE_FAILED,
                            f"{len(pages)} pages, {len(rr.deals)} items, none verified")
        return res

    # -- flyer metadata and PDF ------------------------------------------

    def _flyer_meta(self, ident: str) -> dict | None:
        with self.client({"User-Agent": UA, "Accept": "application/json"}) as client:
            r = client.get(FLYER_API, params={"flyer_identifier": ident})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            payload = r.json()
        flyer = payload.get("flyer") or {}
        if not flyer.get("pdfUrl"):
            return None
        return {
            "pdf_url": flyer["pdfUrl"],
            "title": flyer.get("title"),
            "pages": len(flyer.get("pages") or []),
            "valid_from": date.fromisoformat(flyer.get("offerStartDate") or flyer["startDate"]),
            "valid_to": date.fromisoformat(flyer.get("offerEndDate") or flyer["endDate"]),
        }

    def _download_pdf(self, url: str) -> bytes:
        with self.client({"User-Agent": UA}, timeout=600.0) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.content

    @staticmethod
    def _render_pages(blob: bytes) -> dict[int, bytes]:
        """Every page as a zoom-3 RGB PNG, keyed by 1-based page number.

        PNG bytes, because (a) the VLM cache is keyed on them and these are
        byte-identical to the truth-set renders, and (b) they are 15x smaller
        than raw pixels to ship to OCR worker processes."""
        doc = pymupdf.open(stream=blob, filetype="pdf")
        try:
            m = pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM)
            return {i + 1: doc[i].get_pixmap(matrix=m, alpha=False).tobytes("png")
                    for i in range(doc.page_count)}
        finally:
            doc.close()

    # -- readers -----------------------------------------------------------

    def _read_ocr(self, pngs: dict[int, bytes]) -> dict[int, list[LeafletReading]]:
        """OCR every page in parallel processes (30-60 s a page)."""
        if ocr.cv2 is None:
            raise OcrUnavailable("opencv not installed")
        out: dict[int, list[LeafletReading]] = {}
        with ProcessPoolExecutor(max_workers=min(self.ocr_workers, len(pngs))) as ex:
            futs = {p: ex.submit(_ocr_page, p, png) for p, png in pngs.items()}
            try:
                for p, fut in futs.items():
                    try:
                        out[p] = fut.result()
                    except (OcrUnavailable, BrokenProcessPool):
                        raise
                    except Exception as exc:
                        raise OcrPageError(f"page {p}: {exc!r}") from exc
                    log.info("ocr p%02d: %d readings", p, len(out[p]))
            except BaseException:
                ex.shutdown(wait=False, cancel_futures=True)
                raise
        return out

    def _read_notices(self, pngs: dict[int, bytes],
                      window: tuple[date, date | None]) -> list:
        """Page text for every page (Tesseract, threads: it is a subprocess),
        then lidl_notices.find_notices."""
        with ThreadPoolExecutor(max_workers=min(self.ocr_workers, len(pngs))) as ex:
            texts = dict(zip(pngs, ex.map(lidl_notices.page_text, pngs.values())))
        return lidl_notices.find_notices(texts, window)

    def _read_vlm(self, pngs: dict[int, bytes],
                  ocr_readings: dict[int, list[LeafletReading]]) -> dict[int, list[LeafletReading]]:
        """The VLM on every page, one at a time (cached on disk; ~2 min a
        page on CPU when not cached). Raises VlmUnavailable on any failure.

        A page where the model finds no tiles is accepted only when OCR found
        no priced tile there either (a page without offers). If OCR did find
        prices, the model failed on that page, and that is a failure."""
        out: dict[int, list[LeafletReading]] = {}
        with vlm.ollama_session(self.vlm_model, self.ollama_url):
            for p in sorted(pngs):
                t0 = time.monotonic()
                rs = vlm.read_page(pngs[p], model=self.vlm_model, page=p,
                                   allow_empty=True, url=self.ollama_url)
                if not rs:
                    priced = sum(r.new_price is not None for r in ocr_readings.get(p, []))
                    if priced:
                        raise vlm.VlmUnavailable(
                            f"model found no tiles on page {p}, where OCR read {priced} prices")
                out[p] = [from_vlm(r) for r in rs]
                log.info("vlm p%02d: %d readings in %.0fs", p, len(rs), time.monotonic() - t0)
        return out

    def _candidates(self, window_from: date, window_to: date) -> lidl_api.CandidateList:
        return lidl_api.fetch_candidates(window_from, window_to)


def _ocr_page(page_no: int, png: bytes) -> list[LeafletReading]:
    """Worker: decode one zoom-3 PNG and OCR it (runs in a child process)."""
    import cv2
    bgr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise OcrPageError(f"page {page_no}: could not decode the rendered PNG")
    return read_leaflet_readings(np.ascontiguousarray(bgr[:, :, ::-1]), page_no)


# ---------------------------------------------------------------------------
# Tile reading
#
# Every priced Lidl tile has the same anatomy, whatever its colours:
#
#       [Z Lidl Plus]            (LP logo)
#       [ -42 %      ]  3.49     <- badge box (notch points down), old price
#            V                      struck through, same ink as the price
#       1 . 99                   <- euros, big dot, raised cents
#
# So the reader anchors on the price structure (ocr.find_price_structures),
# then looks for the badge directly above it and the old price above-right of
# its cents. Pairing happens inside the tile, never page-wide by distance.

DISCOUNT_RE = re.compile(r"-(\d{1,2})%")
SPECK = 0.08       # badge "text" shorter than this x price height is not text


@dataclass
class Tile:
    """One priced leaflet tile as OCR read it. Numbers are None unless every
    reading agreed; `raw` keeps the individual readings for audit."""

    page: int
    ink: str                                   # "Y" or "R": price ink
    price_bbox: tuple[int, int, int, int]
    bbox: tuple[int, int, int, int]            # price + badge + old + logo
    price: Decimal | None = None
    old_price: Decimal | None = None
    pct: Decimal | None = None
    lidl_plus: bool = False
    badge: str | None = None                   # "discount" | "flag" | None
    badge_bbox: tuple[int, int, int, int] | None = None
    badge_lines_above: int = 0
    badge_lines_below: int = 0
    lp_logo: bool = False
    lp_text: bool = False
    raw: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def reconciled(self) -> bool:
        return ocr.reconciles(self.price, self.old_price, self.pct)

    def record(self) -> dict:
        """Plain-JSON form, for matching against other sources."""
        return {"page": self.page, "bbox": list(self.bbox), "ink": self.ink,
                "price": _s(self.price), "old_price": _s(self.old_price),
                "pct": _s(self.pct), "lidl_plus": self.lidl_plus,
                "badge": self.badge, "reconciled": self.reconciled,
                "price_bbox": list(self.price_bbox),
                "badge_bbox": list(self.badge_bbox) if self.badge_bbox else None,
                "notes": self.notes, "raw": self.raw}


def _s(v):
    return None if v is None else str(v)


def read_page_tiles(rgb: np.ndarray, page_no: int) -> list[Tile]:
    if ocr.cv2 is None:
        raise OcrUnavailable("opencv not installed")
    masks = ocr.glyph_masks(rgb)
    blue = ocr.lp_blue_mask(rgb)
    tiles = []
    for ink, mask in masks.items():
        labels, blobs = ocr.components(mask, min_h=12, min_area=40)
        for ps in ocr.find_price_structures(blobs, ink):
            tiles.append(_read_tile(rgb, masks, blue, labels, ps, page_no))
    tiles.sort(key=lambda t: (t.price_bbox[1] // 200, t.price_bbox[0]))
    _drop_borrowed_names(tiles)
    _own_text_lines(tiles)
    return tiles


def _anchor(t: Tile) -> tuple[int, int, int, int]:
    boxes = [t.price_bbox] + ([t.badge_bbox] if t.badge_bbox else [])
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _box_gap(a, b) -> float:
    return max(0, a[0] - b[2], b[0] - a[2]) + max(0, a[1] - b[3], b[1] - a[3])


def _drop_borrowed_names(tiles: list[Tile]) -> None:
    """A name block belongs to the tile whose price/badge is nearest to it.
    When a tile's own name was not found, the nearest block can be the
    NEIGHBOUR's name (p40: a sausage tile took "Piščančje hrenovke"); such a
    borrowed name is dropped, which quarantines the tile as no-name."""
    for t in tiles:
        nb = t.raw.get("name_bbox")
        if not nb:
            continue
        own = _box_gap(nb, _anchor(t))
        # names print above or left of their own price, never below or right
        # of it, so only a neighbour the block is not below/right of can claim it
        if any(_box_gap(nb, _anchor(u)) < own and nb[1] < _anchor(u)[3] and nb[0] < _anchor(u)[2]
               for u in tiles if u is not t):
            t.raw["borrowed_name"] = t.raw.pop("name")
            t.raw.pop("brand", None)


def _own_text_lines(tiles: list[Tile]) -> None:
    """``raw["text_lines"]``: every text line Tesseract read around the tile's
    price (``raw["region_lines"]``) that belongs to THIS tile by the same rule
    as the name block (_drop_borrowed_names): no other price is nearer to it
    among the prices it is above/left of. A second, independent reading of what
    the tile prints (name words, "cena za 1 kos: 0.79", ...), for
    lidl_reconcile; never a price reading of its own."""
    for t in tiles:
        own_anchor = _anchor(t)
        keep = []
        for ln in t.raw.get("region_lines", ()):
            box = ln["bbox"]
            own = _box_gap(box, own_anchor)
            if any(_box_gap(box, _anchor(u)) < own and box[1] < _anchor(u)[3]
                   and box[0] < _anchor(u)[2] for u in tiles if u is not t):
                continue
            keep.append(ln["text"])
        t.raw["text_lines"] = keep


def _read_tile(rgb, masks, blue, labels, ps: PriceStructure, page_no: int) -> Tile:
    t = Tile(page=page_no, ink=ps.ink, price_bbox=ps.bbox, bbox=ps.bbox)

    # 1. the price: glyphs rebuilt on one baseline, read several ways; the
    #    digit count is known from the structure (euro glyphs + 2 cents)
    got, votes = ocr.read_glyphs(labels, ps.euros + ps.cents, len(ps.euros) + 2)
    t.raw["price"] = votes
    if got is not None:
        t.price = Decimal(f"{got[:-2]}.{got[-2:]}")

    # 2. the badge box directly above the price
    box = _find_badge(masks, ps)
    if box is not None:
        _read_badge(t, box, ps)

    # 3. the old price, above-right of the cents
    _read_old_price(t, rgb, labels, ps)

    # 4. Lidl Plus logo, beside the badge
    _find_lp_logo(t, masks, blue, ps)

    # 5. the product name: the text block nearest the price
    _read_name(t, rgb, ps)

    x0 = min(b[0] for b in _boxes(t)); y0 = min(b[1] for b in _boxes(t))
    x1 = max(b[2] for b in _boxes(t)); y1 = max(b[3] for b in _boxes(t))
    t.bbox = (x0, y0, x1, y1)
    return t


def _boxes(t: Tile):
    out = [t.price_bbox]
    if t.badge_bbox:
        out.append(t.badge_bbox)
    for k in ("old_bbox", "logo_bbox"):  # noqa
        if t.raw.get(k):
            out.append(tuple(t.raw[k]))
    return out


def _find_badge(masks, ps: PriceStructure):
    """A solid yellow or red box whose bottom (the notch) meets the top of
    the price and which overlaps it horizontally."""
    import cv2
    x0, y0, x1, _ = ps.bbox
    he = ps.height
    H, W = masks["Y"].shape
    X0, X1 = max(0, int(x0 - 0.8 * he)), min(W, int(x1 + 0.4 * he))
    Y0, Yb = max(0, int(y0 - 2.0 * he)), min(H, int(y0 + 0.05 * he))
    best = None
    for col in ("Y", "R"):
        sub = masks[col][Y0:Yb, X0:X1]
        n, lab, st, _ = cv2.connectedComponentsWithStats(sub, 8)
        for i in range(1, n):
            x, y, w, h, a = (int(v) for v in st[i])
            if w < 0.8 * he or h < 0.25 * he or a / (w * h) < 0.45:
                continue
            if w > 4 * he or h > 2 * he:          # a whole panel, not a badge
                continue
            bottom = Y0 + y + h
            if bottom < y0 - 0.45 * he:
                continue
            overlap = min(X0 + x + w, x1) - max(X0 + x, x0)
            if overlap < 0.3 * (x1 - x0):
                continue
            cand = dict(col=col, x=X0 + x, y=Y0 + y, w=w, h=h,
                        comp=(lab[y:y + h, x:x + w] == i).astype(np.uint8))
            if best is None or abs(bottom - y0) < abs(best["y"] + best["h"] - y0):
                best = cand
    return best


def _read_badge(t: Tile, box: dict, ps: PriceStructure) -> None:
    import cv2
    comp = box["comp"]
    filled = comp.copy()
    contours, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cv2.drawContours(filled, contours, -1, 1, -1)
    text = ((filled > 0) & (comp == 0)).astype(np.uint8)
    labels, blobs = ocr.components(text, min_area=15)
    t.badge_bbox = (box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"])
    if not blobs:
        return
    mh = max(b.h for b in blobs)
    # Group the text into lines. The discount "-NN%" is the LOWEST line of
    # full-height glyphs: on panel tiles (Kekec, Nanoska) the product name
    # above it is printed in the same red and nearly as tall.
    lines = _lines([b for b in blobs if b.h >= 0.25 * mh])
    tall = [ln for ln in lines if max(b.h for b in ln) >= 0.8 * mh]
    main = max(tall, key=lambda ln: min(b.y for b in ln))
    mh = max(b.h for b in main)
    ty, by = min(b.y for b in main), max(b.y1 for b in main)
    row = [b for b in blobs if b.y >= ty - 0.1 * mh and b.y1 <= by + 0.1 * mh]
    rx0, rx1 = min(b.x for b in main) - 0.5 * mh, max(b.x1 for b in main) + 0.5 * mh

    def text_lines(sel):
        # a real line of text has several glyphs; photo texture that merged
        # into the box (a pear, a splash) leaves scattered specks
        return [ln for ln in _lines([b for b in sel if b.h >= 0.25 * mh and rx0 <= b.x and b.x1 <= rx1])
                if len(ln) >= 3]
    above_lines = text_lines([b for b in blobs if b.y1 < ty - 0.05 * mh])
    below_lines = text_lines([b for b in blobs if b.y > by + 0.05 * mh])
    t.badge_lines_above = len(above_lines)
    t.badge_lines_below = len(below_lines)
    # what the badge itself prints (for blanking before name OCR): on panel
    # tiles the "box" is the whole panel, and the name is printed in it
    printed = list(main) + (above_lines[-1] if above_lines else [])
    t.raw["badge_text_bbox"] = [box["x"] + min(b.x for b in printed) - 10,
                                box["y"] + min(b.y for b in printed) - 10,
                                box["x"] + max(b.x1 for b in printed) + 10,
                                box["y"] + max(b.y1 for b in printed) + 10]
    kind, digits = _parse_discount_row(row, blobs, mh)
    t.raw["badge_shape"] = kind
    t.raw["badge_main"] = [len(main), round(mh / ps.height, 3)]
    if kind == "flag" and mh < SPECK * ps.height:
        # Nothing printed in the "box": its only "text" is a speck (kw39 p2
        # ribeye: a patch of red background above the price, 2% of the price
        # height; real flag text is >= 11% on every kw39 page, and a real flag
        # whose text is unreadable, p1 "1l = 0.88 EUR", still counts as one).
        t.raw["badge_speck"] = True
        return
    if kind == "discount":
        t.badge = "discount"
        got, votes = ocr.read_glyphs(labels, digits, len(digits))
        t.raw["badge"] = votes
        if got is not None and got[0] != "0":
            t.pct = Decimal(got)
        else:
            t.notes.append("badge-unreadable")
    else:
        t.badge = "flag"               # "za kg", "220 g gratis", "32% gratis"...
        t.raw["flag_text"] = _read_flag_text(labels, [b for b in blobs if b.h >= 0.25 * mh], mh)
    if above_lines:
        above = above_lines[-1]          # the line right above the discount
        a_ink = np.isin(labels, [b.index for b in above])
        txt = " ".join(ocr.read_votes(a_ink, "ZzLlIidDPpu s", heights=(40,), psms=(7,)))
        t.raw["badge_above"] = txt
        t.lp_text = bool(re.search(r"lidl|plus", txt, re.I))


def _read_flag_text(labels, blobs: list[Blob], mh: int) -> str | None:
    """The words printed in a flag badge ("že od", "4+2 gratis", "za kg"):
    its text glyphs, black on white at a fixed line height, Tesseract slv."""
    import cv2
    if not blobs or mh <= 0:
        return None
    ink = np.isin(labels, [b.index for b in blobs])
    ys, xs = np.nonzero(ink)
    crop = np.where(ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1], 0, 255).astype(np.uint8)
    s = 56 / mh
    crop = cv2.resize(crop, (max(1, round(crop.shape[1] * s)), max(1, round(crop.shape[0] * s))),
                      interpolation=cv2.INTER_AREA)
    crop = cv2.copyMakeBorder(crop, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    txt = ocr._run_tesseract(crop, ["-l", ocr.TESSERACT_LANG, "--psm", "6"])
    txt = " ".join(txt.split())
    return txt or None


def _parse_discount_row(row: list[Blob], blobs: list[Blob], mh: int):
    """Decide from shape alone whether a badge reads "-NN%".

    Returns ("discount", [digit blobs]) or ("flag", []). The digits are the
    full-height solid glyphs; a minus must sit left of them and a percent
    sign (1-3 pieces: two small rings and a thin slash) right of them. A flag
    like "32% gratis" has no minus, "za kg" has neither."""
    tall = sorted([b for b in row if b.h >= 0.9 * mh and b.fill >= 0.45], key=lambda b: b.x)
    if not tall:
        return "flag", []
    digits = [tall[0]]
    for b in tall[1:]:
        if b.x - digits[-1].x1 < 0.35 * mh and len(digits) < 3:
            digits.append(b)
        else:
            break
    if len(digits) > 2:
        return "flag", []
    d0, d1 = digits[0], digits[-1]
    top, bot = min(b.y for b in digits), max(b.y1 for b in digits)
    mid = (top + bot) / 2
    minus = [b for b in blobs if b.h < 0.35 * mh and b.w > 1.4 * b.h and b.fill > 0.6
             and abs(b.y + b.h / 2 - mid) < 0.25 * mh and 0 <= d0.x - b.x1 < 0.6 * mh]
    pct_parts = [b for b in row if b.x >= d1.x1 - 2 and b.x - d1.x1 < 0.8 * mh
                 and b.y >= top - 0.1 * mh and b.y1 <= bot + 0.1 * mh]
    if not minus or not pct_parts:
        return "flag", []
    # the percent sign: right of the digits, visibly SHORTER than them
    # (0.74-0.84x measured) and about as wide as a digit
    ph = max(b.y1 for b in pct_parts) - min(b.y for b in pct_parts)
    pw = max(b.x1 for b in pct_parts) - min(b.x for b in pct_parts)
    if not (0.55 * mh <= ph <= 0.9 * mh) or pw < 0.6 * mh:
        return "flag", []
    return "discount", digits


def _lines(blobs: list[Blob]) -> list[list[Blob]]:
    """Cluster glyphs into text lines by vertical overlap, top to bottom."""
    lines: list[dict] = []
    for b in sorted(blobs, key=lambda b: -b.h):
        for ln in lines:
            ov = min(ln["y1"], b.y1) - max(ln["y0"], b.y)
            if ov > 0.5 * min(b.h, ln["y1"] - ln["y0"]):
                ln["b"].append(b)
                break
        else:
            lines.append({"y0": b.y, "y1": b.y1, "b": [b]})
    return [sorted(ln["b"], key=lambda b: b.x) for ln in sorted(lines, key=lambda ln: ln["y0"])]


def _read_old_price(t: Tile, rgb, labels, ps: PriceStructure) -> None:
    """The old price: small digits, same ink as the price, struck through."""
    import cv2
    x0, y0, x1, _ = ps.bbox
    he = ps.height
    H, W = labels.shape
    c1 = ps.cents[0]
    X0, X1 = max(0, int(c1.x - 0.2 * he)), min(W, int(x1 + 1.0 * he))
    Y0, Y1 = max(0, int(y0 - 0.9 * he)), min(H, int(y0 + 0.35 * he))
    chan = ocr.ink_channel(rgb[Y0:Y1, X0:X1], ps.ink)
    bw = (chan > 100).astype(np.uint8)
    own = [b.index for b in ps.euros + ps.cents + [ps.dot]]
    bw[np.isin(labels[Y0:Y1, X0:X1], own)] = 0
    if t.badge_bbox:                         # never read the badge as a price
        bx0, by0, bx1, by1 = t.badge_bbox
        bw[max(0, by0 - Y0):max(0, by1 - Y0), max(0, bx0 - X0):max(0, bx1 - X0)] = 0
    _, blobs = ocr.components(bw, min_area=30)
    blobs = [b for b in blobs if 0.08 * he <= b.h <= 0.3 * he]
    if not blobs:
        return
    rows: list[dict] = []
    for b in sorted(blobs, key=lambda b: b.x):
        cy = b.y + b.h / 2
        for r in rows:
            if abs(r["cy"] - cy) < 0.06 * he and b.x - r["x1"] < 0.25 * he:
                r["b"].append(b)
                r["x1"] = max(r["x1"], b.x1)
                break
        else:
            rows.append({"cy": cy, "x1": b.x1, "b": [b]})
    rows = [r for r in rows if sum(b.w for b in r["b"]) > 0.3 * he]
    if not rows:
        return
    row = max(rows, key=lambda r: sum(b.area for b in r["b"]))
    rx0 = max(0, min(b.x for b in row["b"]) - 4)
    ry0 = max(0, min(b.y for b in row["b"]) - 4)
    rx1 = max(b.x1 for b in row["b"]) + 4
    ry1 = max(b.y1 for b in row["b"]) + 4
    t.raw["old_bbox"] = [X0 + rx0, Y0 + ry0, X0 + rx1, Y0 + ry1]
    g = cv2.resize(chan[ry0:ry1, rx0:rx1], None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    votes, shapes = [], []
    for thr in (95, 125):                    # two binarisations x 3 sizes x 2 modes
        ink = ocr.remove_strike((g > thr).astype(np.uint8))
        seg = _segment_small_price(ink)
        if seg is None:                      # could not segment: no vote
            shapes.append(None)
            continue
        glyphs, n_euro = seg
        shapes.append((len(glyphs), n_euro))
        for v in ocr.read_strip_votes([ocr.mask_strip(glyphs, h) for h in ocr.READ_HEIGHTS]):
            # the decimal point is placed from the geometry, never guessed
            votes.append(f"{int(v[:n_euro])}.{v[n_euro:]}" if len(v) == len(glyphs) else "x")
    t.raw["old"] = votes
    t.raw["old_shape"] = shapes
    got = ocr.consensus(votes, re.compile(r"\d{1,3}\.\d{2}"), min_votes=6)
    if got is not None:
        t.old_price = Decimal(got)


def _segment_small_price(ink: np.ndarray):
    """Split a (de-struck) small price like "3.49" into digit glyphs and find
    its decimal point. Returns ([glyph masks], digits before the point) or
    None unless the shape is exactly <1-3 digits> . <2 digits>."""
    labels, blobs = ocr.components(ink, min_area=40)
    if not blobs:
        return None
    mh = max(b.h for b in blobs)
    base = max(b.y1 for b in blobs if b.h >= 0.6 * mh)
    dots = [b for b in blobs if b.h < 0.35 * mh and 0.5 < b.w / b.h < 2.0
            and abs(b.y1 - base) < 0.15 * mh and b.fill > 0.5]
    parts = sorted([b for b in blobs if b.h >= 0.3 * mh and b not in dots], key=lambda b: b.x)
    if len(dots) != 1 or not parts:
        return None
    dot = dots[0]
    # merge fragments of one glyph (strike removal can split a stroke)
    glyphs: list[list[Blob]] = []
    for b in parts:
        if glyphs:
            g0 = min(p.x for p in glyphs[-1]); g1 = max(p.x1 for p in glyphs[-1])
            ov = min(g1, b.x1) - max(g0, b.x)
            if ov > 0.5 * min(b.w, g1 - g0):
                glyphs[-1].append(b)
                continue
        glyphs.append([b])
    glyphs = [g for g in glyphs if max(p.y1 for p in g) - min(p.y for p in g) >= 0.6 * mh]
    n_euro = sum(1 for g in glyphs if max(p.x1 for p in g) <= dot.x + dot.w / 2)
    if not (1 <= n_euro <= 3) or len(glyphs) - n_euro != 2:
        return None
    masks = []
    for g in glyphs:
        x0 = min(p.x for p in g); y0 = min(p.y for p in g)
        x1 = max(p.x1 for p in g); y1 = max(p.y1 for p in g)
        masks.append(np.isin(labels[y0:y1, x0:x1], [p.index for p in g]))
    return masks, n_euro


def _find_lp_logo(t: Tile, masks, blue, ps: PriceStructure) -> None:
    """The Lidl Plus logo: a blue rounded square holding the yellow Lidl
    circle. Requiring the yellow circle INSIDE the blue square is what keeps
    blue tile backgrounds and packaging from counting."""
    import cv2
    x0, y0, x1, _ = ps.bbox
    he = ps.height
    H, W = blue.shape
    if t.badge_bbox:
        bx0, by0, bx1, by1 = t.badge_bbox
        X0, X1 = bx0, min(W, int(bx1 + 1.6 * he))
        Y0, Y1 = max(0, int(by0 - 0.8 * he)), min(H, int(y0 + 0.3 * he))
    else:
        X0, X1 = x0, min(W, int(x1 + 1.6 * he))
        Y0, Y1 = max(0, int(y0 - 1.8 * he)), min(H, int(y0 + 0.3 * he))
    sub = blue[Y0:Y1, X0:X1]
    n, lab, st, _ = cv2.connectedComponentsWithStats(sub, 8)
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        if not (0.3 * he < h < 1.4 * he and 0.7 < w / h < 1.4 and a > 0.3 * w * h):
            continue
        inner = masks["Y"][Y0 + y:Y0 + y + h, X0 + x:X0 + x + w]
        _, yb = ocr.components(np.ascontiguousarray(inner), min_area=20)
        if any(b.w > 0.35 * w and b.h > 0.35 * h and 0.75 < b.w / b.h < 1.33 for b in yb):
            t.lp_logo = True
            t.raw["logo_bbox"] = [X0 + x, Y0 + y, X0 + x + w, Y0 + y + h]
            break
    t.lidl_plus = t.lp_logo or t.lp_text


# Leaflet furniture that sits in or beside a name block but is not a name.
_NOT_NAME = re.compile(
    r"lidl|plus|za\s*kg|velja|gratis|cena za|drugi kos|več vrst|vec vrst|slovensk|"
    r"hlajen|zamrznjen|pribl|sorta|razred|pakiran|najniž|najniz|izbrana|kakovost|"
    r"popust|blagajn|ponudb|za kos|^izdelek$|%|=|\d+\s*(g|kg|ml|l|kos)\b", re.I)


_NAME_WORD = re.compile(r"[^\W\d_]+(?:[-'’.&][^\W\d_]*)*\.?|&")


def _name_like(text: str) -> bool:
    """A leaflet name line: short, every word purely alphabetic. Small print
    (long lines), unit lines (digits) and photo/packaging text read as
    garbage ("sw:tr 8, SOUR DIP") all fail this."""
    if len(text) > 30 or sum(ch.isalpha() for ch in text) < 3 or _NOT_NAME.search(text):
        return False
    return all(_NAME_WORD.fullmatch(w) for w in text.split())


def _read_name(t: Tile, rgb, ps: PriceStructure) -> None:
    """Tesseract (Slovenian model) on the area left of and above the price.

    Leaflet name blocks are left-aligned stacks: BRAND / name (1-2 lines) /
    pack size and unit price / extras ("več vrst", "velja do ..."). Blocks
    are built from ALL lines, the block nearest the price and badge is
    chosen, and the name is its leading run of name-like lines."""
    import cv2
    x0, y0, x1, y1 = ps.bbox
    he = ps.height
    H, W = rgb.shape[:2]
    X0, X1 = max(0, int(x0 - 4.0 * he)), min(W, int(x1 + 0.2 * he))
    Y0, Y1 = max(0, int(y0 - 3.4 * he)), min(H, y1)
    gray = cv2.cvtColor(np.ascontiguousarray(rgb[Y0:Y1, X0:X1]), cv2.COLOR_RGB2GRAY)
    # blank what is already read (price, badge, old price, logo) so its
    # digits do not bleed into the name lines
    fill = int(np.median(gray))
    blank = [b for b in _boxes(t) if b != t.badge_bbox]
    if t.raw.get("badge_text_bbox"):
        blank.append(tuple(t.raw["badge_text_bbox"]))
    elif t.badge_bbox:
        blank.append(t.badge_bbox)
    for bx0, by0, bx1, by1 in blank:
        gray[max(0, by0 - Y0 - 6):max(0, by1 - Y0 + 6), max(0, bx0 - X0 - 6):max(0, bx1 - X0 + 6)] = fill
    gray = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    lines = []
    for ln in ocr.read_text_tesseract(gray, "slv", min_conf=40):
        text = re.sub(r"^[|(\-—_:;.,'»«>\s]+|[|\-—_:;.,»«\s]+$", "", ln.text)
        if ln.conf < 0.5 or sum(ch.isalnum() for ch in text) < 2:
            continue
        bx0, by0, bx1, by1 = (v * 2 for v in ln.bbox)
        lines.append({"text": text, "ok": _name_like(text) and ln.conf >= 0.8,
                      "x0": X0 + bx0, "y0": Y0 + by0, "x1": X0 + bx1, "y1": Y0 + by1,
                      "h": by1 - by0})
    lines.sort(key=lambda d: d["y0"])
    t.raw["region_lines"] = [{"text": d["text"], "bbox": [d["x0"], d["y0"], d["x1"], d["y1"]]}
                             for d in lines]
    blocks: list[list[dict]] = []
    for ln in lines:
        for blk in blocks:
            last = blk[-1]
            if abs(ln["x0"] - blk[0]["x0"]) < 0.25 * he and 0 <= ln["y0"] - last["y1"] < 1.3 * last["h"]:
                blk.append(ln)
                break
        else:
            blocks.append([ln])
    anchor = [ps.bbox] + ([t.badge_bbox] if t.badge_bbox else [])
    ax0 = min(b[0] for b in anchor); ay0 = min(b[1] for b in anchor)
    ax1 = max(b[2] for b in anchor); ay1 = max(b[3] for b in anchor)
    best, best_d = None, float("inf")
    for blk in blocks:
        run = []
        for ln in blk:
            if not ln["ok"]:
                break
            run.append(ln)
        if not run:
            continue
        bx0 = min(d["x0"] for d in blk); by0 = min(d["y0"] for d in blk)
        bx1 = max(d["x1"] for d in blk); by1 = max(d["y1"] for d in blk)
        d = max(0, bx0 - ax1, ax0 - bx1) + max(0, by0 - ay1, ay0 - by1)
        if d < best_d:
            best, best_d = run, d
    if best is None or best_d > 2.0 * he:
        return
    texts = [d["text"] for d in best]
    brand = []
    while texts and texts[0].upper() == texts[0]:   # BRAND lines are all caps
        brand.append(texts.pop(0))
    if brand:
        t.raw["brand"] = " ".join(brand)
    if not texts:                                    # a brand alone is not a name
        return
    t.raw["name"] = " ".join(texts)
    t.raw["name_bbox"] = [min(d["x0"] for d in best), min(d["y0"] for d in best),
                          max(d["x1"] for d in best), max(d["y1"] for d in best)]


# ---------------------------------------------------------------------------
# Adapter to the multi-reader merge (babaemail/lidl_reconcile.py)

def tile_to_leaflet_reading(t: Tile):
    """One OCR tile as a ``LeafletReading(source="ocr")``.

    * new_price / old_price / pct are the CONSENSUS readings (None when the
      renderings disagreed). They are passed whether or not they reconcile;
      lidl_reconcile applies R1 itself and ignores an unreconciled old/%.
    * A badge with text printed UNDER the percentage ("-80% na drugi kos",
      p1 Milka) makes the price conditional, so old/% are withheld: otherwise
      a second-item price would reconcile as a plain discount.
    * lidl_plus: True when the blue LP logo was found. "Z Lidl Plus" text
      without a logo is None (no opinion), since LP coupon diamonds carry text but no
      logo and the printed price there is the normal one. Otherwise False.
    * name is "BRAND name" when OCR read an all-caps brand line above the
      name (the VLM's convention, which lidl_reconcile.split_leaflet_name
      splits again): the brand is what makes a brand-only match possible.
      It is omitted when the name block was judged to belong to a neighbour.
    """
    from ..lidl_reconcile import LeafletReading

    conditional = t.badge_lines_below > 0
    lp = True if t.lp_logo else (None if t.lp_text else False)
    return LeafletReading(
        source="ocr",
        page=t.page,
        new_price=t.price,
        old_price=None if conditional else t.old_price,
        pct=None if conditional else t.pct,
        lidl_plus=lp,
        name=_reading_name(t),
        bbox=tuple(float(v) for v in t.bbox),
        text=tuple(t.raw.get("text_lines", ())),
        raw=t.record(),
    )


def _reading_name(t: Tile) -> str | None:
    name = t.raw.get("name")
    brand = t.raw.get("brand")
    return f"{brand} {name}" if name and brand else name


def tiles_to_leaflet_readings(tiles: list[Tile]) -> list:
    """Every tile read on a page, including ones whose price is unreadable
    (new_price None), so the merge can count them rather than lose them."""
    return [tile_to_leaflet_reading(t) for t in tiles]


def read_leaflet_readings(rgb: np.ndarray, page_no: int) -> list:
    """Convenience: OCR one rendered page (zoom 3, RGB) straight to readings."""
    return tiles_to_leaflet_readings(read_page_tiles(rgb, page_no))
