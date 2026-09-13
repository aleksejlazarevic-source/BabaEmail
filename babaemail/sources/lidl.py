"""Lidl - image-only leaflet, read with free local OCR.

Lidl is the only retailer here with no machine-readable text anywhere:

  * `endpoints.leaflets.schwarz/v4/flyer` returns 58 page images and
    `products: []` - the product array is always empty for Slovenia.
  * The leaflet PDF has ZERO characters of text on all 58 pages.
  * lidl.si shows no prices, and states that where the site and the leaflet
    disagree, the leaflet is authoritative.

So the leaflet is both the only source and the authoritative one, and OCR is
unavoidable. The method lives in ocr.py; this module locates the leaflet,
pairs prices with names and badges, and refuses to emit anything it cannot
reconcile.

Many Lidl offers are "z Lidl Plus" - app-only. She has the app, so they count,
but they are labelled.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import httpx
import numpy as np
import pymupdf

from ..models import Deal, SourceResult, SourceStatus
from ..ocr import (OcrBox, OcrUnavailable, digit_clusters, digits_to_price,
                   group_into_prices, read_price_group, read_text,
                   repair_with_reconcile, yellow_mask)
from .base import UA, Source, classify_http_failure

FLYER_API = "https://endpoints.leaflets.schwarz/v4/flyer"
RENDER_ZOOM = 3.0

# OCR frequently loses the "%" glyph entirely ("-27%" reads as "-27"), so the
# leading minus is what identifies a badge. Requiring the "%" silently dropped
# every badge, and discount_display then COMPUTED a percentage from old/new -
# which made a badge that was never read look like it had been.
BADGE_RE = re.compile(r"-\s*(\d{1,2})\s*%?$")
MONEY_RE = re.compile(r"(\d{1,3})[.,](\d{2})")
LIDL_PLUS_RE = re.compile(r"lidl\s*plus", re.I)


class Lidl(Source):
    name = "lidl"
    display_name = "Lidl"

    def __init__(self, max_pages: int | None = None):
        # OCR costs a few seconds per page; capping is useful in tests.
        self.max_pages = max_pages

    def flyer_identifier(self, today: date) -> str:
        iso = today.isocalendar()
        return f"lidlov-katalog-{iso.year}-kw{iso.week:02d}"

    def fetch(self, today: date) -> SourceResult:
        ident = self.flyer_identifier(today)
        res = self.result(SourceStatus.OK, f"{FLYER_API}?flyer_identifier={ident}",
                          leaflet_id=ident)
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

        if not (meta["valid_from"] <= today <= meta["valid_to"]):
            return res.fail(SourceStatus.STALE,
                            f"{ident} valid {meta['valid_from']}..{meta['valid_to']}")

        try:
            with self.client({"User-Agent": UA}, timeout=600.0) as client:
                r = client.get(meta["pdf_url"])
                r.raise_for_status()
                blob = r.content
        except httpx.HTTPError as exc:
            return res.fail(SourceStatus.PARSE_FAILED, f"pdf: {exc!r}")
        if not blob.startswith(b"%PDF"):
            return res.fail(SourceStatus.PARSE_FAILED, "response was not a PDF")

        doc = pymupdf.open(stream=blob, filetype="pdf")
        pages = doc.page_count if self.max_pages is None else min(self.max_pages, doc.page_count)

        deals: list[Deal] = []
        tiles = 0
        try:
            for index in range(pages):
                page_deals, n = self._parse_page(doc, index, meta, res.source_url)
                deals.extend(page_deals)
                tiles += n
        except OcrUnavailable as exc:
            # Never report "no offers" when the reason is that OCR is missing.
            return res.fail(SourceStatus.BLOCKED, f"ocr unavailable: {exc}")

        res.items = deals
        res.pages_declared = tiles
        res.pages_parsed = len(deals)
        if not deals:
            return res.fail(SourceStatus.PARSE_FAILED,
                            f"{pages} pages but nothing extracted")
        return res

    # -- flyer metadata --------------------------------------------------

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

    # -- page parsing ----------------------------------------------------

    def _parse_page(self, doc, index: int, meta: dict,
                    source_url: str) -> tuple[list[Deal], int]:
        import cv2  # local import; guarded by OcrUnavailable upstream

        pix = doc[index].get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM))
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            rgb = rgb[:, :, :3]
        rgb = np.ascontiguousarray(rgb)

        mask = yellow_mask(rgb)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        blobs = digit_clusters(mask)
        if not blobs:
            return [], 0

        # Prices are the largest yellow glyph groups on the page.
        groups = group_into_prices(blobs, page_height=rgb.shape[0])
        groups = [g for g in groups if len(g) >= 2]
        if not groups:
            return [], 0
        tallest = max(max(b["h"] for b in g) for g in groups)
        groups = [g for g in groups if max(b["h"] for b in g) >= tallest * 0.45]

        text_boxes = read_text(rgb)
        badges = [(b, Decimal(BADGE_RE.search(b.text).group(1)))
                  for b in text_boxes if BADGE_RE.search(b.text or "")]
        olds = [(b, Decimal(f"{m.group(1)}.{m.group(2)}"))
                for b in text_boxes if (m := MONEY_RE.search(b.text or ""))]
        plus = [b for b in text_boxes if LIDL_PLUS_RE.search(b.text or "")]
        names = [b for b in text_boxes
                 if len(b.text) > 3 and not BADGE_RE.search(b.text)
                 and not MONEY_RE.fullmatch(b.text.strip())
                 and sum(ch.isalpha() for ch in b.text) >= 3]

        deals: list[Deal] = []
        for group in groups:
            box = _group_box(group)
            price = read_price_group(mask, labels, group)

            pct = _nearest_value(box, badges, max_gap=420)
            old = _nearest_value(box, olds, max_gap=260)
            name_box = _nearest_box(box, names, max_gap=520)
            has_plus = _nearest_box(box, plus, max_gap=380) is not None

            # If the reading disagrees with what old x (1-pct) predicts, try a
            # constrained repair before giving up - OCR most often loses the
            # LEADING digit (1,29 -> "29").
            if price is not None and old is not None and pct is not None:
                predicted = old * (Decimal(100) - pct) / Decimal(100)
                if abs(price - predicted) > Decimal("0.06"):
                    repaired = repair_with_reconcile(
                        re.sub(r"\D", "", str(price).replace(".", "")), old, pct)
                    price = repaired if repaired is not None else price

            deal = Deal(
                retailer=self.name,
                name=(name_box.text.strip() if name_box else "") or "(neznan izdelek)",
                leaflet_id=meta.get("title") or "lidl",
                source_url=source_url,
                valid_from=meta["valid_from"],
                valid_to=meta["valid_to"],
                price_new=price,
                price_old=old,
                discount_published=pct,
                loyalty_required="Lidl Plus" if has_plus else None,
                source_page=index + 1,
            )
            deal.flag("ocr")
            if price is None:
                deal.quarantine("ocr-price-unreadable")
            if name_box is None:
                deal.quarantine("no-name-found")
            # OCR output is trusted ONLY when the retailer's own numbers agree.
            if price is not None and old is not None and pct is not None:
                predicted = old * (Decimal(100) - pct) / Decimal(100)
                if abs(price - predicted) > Decimal("0.06"):
                    deal.quarantine("ocr-unreconciled")
            elif price is not None and (old is None or pct is None):
                # Nothing to check against. Lidl is the only source where this
                # is possible, and an unverifiable price is not shippable.
                deal.quarantine("ocr-unverifiable")
            deals.append(deal)

        return deals, len(groups)


def _group_box(group: list[dict]) -> OcrBox:
    x0 = min(b["x"] for b in group)
    y0 = min(b["y"] for b in group)
    x1 = max(b["x"] + b["w"] for b in group)
    y1 = max(b["y"] + b["h"] for b in group)
    return OcrBox(text="", conf=1.0, bbox=(x0, y0, x1, y1))


def _nearest_box(anchor: OcrBox, boxes: list[OcrBox], max_gap: float) -> OcrBox | None:
    best, best_gap = None, float("inf")
    for b in boxes:
        g = anchor.gap_to(b)
        if g < best_gap:
            best, best_gap = b, g
    return best if best is not None and best_gap <= max_gap else None


def _nearest_value(anchor: OcrBox, pairs: list[tuple[OcrBox, Decimal]],
                   max_gap: float) -> Decimal | None:
    best, best_gap = None, float("inf")
    for b, value in pairs:
        g = anchor.gap_to(b)
        if g < best_gap:
            best, best_gap = value, g
    return best if best is not None and best_gap <= max_gap else None
