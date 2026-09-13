"""Tus - dated catalogue PDFs listed on tus.si.

Structurally different from Mercator and Spar in two ways that matter:

  * Most tiles carry "SUPER CENA" and NO percentage, so badges cannot be the
    anchor - the price is. Tile count comes from big-price anchors instead.
  * Where there IS a percentage, the PDF splits "-50%" into three spans ("-",
    "50", "%") at three different font sizes, so it must be reassembled.

Many Tus discounts also read "s Tus klub kartico" - loyalty-card only. That
qualifier is captured, because a deal she cannot actually use is a wasted trip.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import httpx
import pymupdf

from ..models import Deal, SourceResult, SourceStatus, parse_price
from ..pdftext import (Span, assemble_name, badges_by_anchor, drop_repeated,
                       is_label_candidate,
                       nearest, partition_by_anchor, pick_name, price_tiles,
                       reconcile, spans)
from .base import UA, Source, classify_http_failure

INDEX = "https://www.tus.si/katalogi/"
PDF_RE = re.compile(r'https?://[^\s"\']*?/app/uploads/catalogues/[^\s"\']+?\.pdf', re.I)
# 20260904070703_36_akcija_9.9.-15.9.2026_A36_26.pdf
RANGE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.?\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})")
MONEY_RE = re.compile(r"^\d{1,4},\d{2}$")
LOYALTY_RE = re.compile(r"s\s+Tu[sš]\s+klub\s+kartico", re.I)


class Tus(Source):
    name = "tus"
    display_name = "Tuš"

    def fetch(self, today: date) -> SourceResult:
        res = self.result(SourceStatus.OK, INDEX)
        try:
            with self.client({"User-Agent": UA}) as client:
                r = client.get(INDEX)
                r.raise_for_status()
                html = r.text
        except httpx.HTTPStatusError as exc:
            return res.fail(classify_http_failure(exc.response.status_code,
                                                  exc.response.text[:300]),
                            f"index: HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return res.fail(SourceStatus.PARSE_FAILED, f"index: {exc!r}")

        catalogues = self._catalogues(html)
        active = [c for c in catalogues if c["valid_from"] <= today <= c["valid_to"]]
        if not active:
            return res.fail(SourceStatus.NO_LEAFLET,
                            f"none of {len(catalogues)} catalogues cover {today}")

        deals: list[Deal] = []
        declared = 0
        for cat in active:
            try:
                items, tiles = self._parse_pdf(cat)
            except Exception as exc:  # noqa: BLE001
                return res.fail(SourceStatus.PARSE_FAILED, f"{cat['slug']}: {exc!r}")
            deals.extend(items)
            declared += tiles

        res.items = deals
        res.pages_declared = declared
        res.pages_parsed = len(deals)
        res.leaflet_id = "+".join(c["slug"] for c in active)
        res.valid_from = min(c["valid_from"] for c in active)
        res.valid_to = max(c["valid_to"] for c in active)
        if not deals:
            return res.fail(SourceStatus.PARSE_FAILED, "catalogue found but nothing extracted")
        return res

    # -- discovery -------------------------------------------------------

    def _catalogues(self, html: str) -> list[dict]:
        """Validity window comes from the filename, e.g. '9.9.-15.9.2026'."""
        out: list[dict] = []
        for url in dict.fromkeys(PDF_RE.findall(html)):
            fname = url.rsplit("/", 1)[-1]
            m = RANGE_RE.search(fname)
            if not m:
                continue
            d1, m1, d2, m2, yy = (int(x) for x in m.groups())
            try:
                vf = date(yy if m1 <= m2 else yy - 1, m1, d1)
                vt = date(yy, m2, d2)
            except ValueError:
                continue
            out.append({"url": url, "slug": fname.removesuffix(".pdf"),
                        "valid_from": vf, "valid_to": vt})
        return out

    # -- parsing ---------------------------------------------------------

    def _parse_pdf(self, cat: dict) -> tuple[list[Deal], int]:
        with self.client({"User-Agent": UA}, timeout=300.0) as client:
            r = client.get(cat["url"])
            r.raise_for_status()
            blob = r.content
        if not blob.startswith(b"%PDF"):
            raise ValueError("not a PDF")

        doc = pymupdf.open(stream=blob, filetype="pdf")
        deals: list[Deal] = []
        tiles = 0
        for index in range(doc.page_count):
            page_deals, n = self._parse_page(doc[index], index + 1, cat)
            deals.extend(page_deals)
            tiles += n
        return deals, tiles

    def _parse_page(self, page, page_no: int, cat: dict) -> tuple[list[Deal], int]:
        sp = spans(page)
        tiles = price_tiles(sp)
        if not tiles:
            return [], 0

        labels = drop_repeated([s for s in sp if is_label_candidate(s)])
        olds = [s for s in sp if MONEY_RE.match(s.text)]
        loyal = [s for s in sp if LOYALTY_RE.search(s.text)]

        anchors = [euro for _, euro, _ in tiles]
        consumed = frozenset(
            id(s) for _, e, c in tiles for s in (e, c) if s is not None)
        buckets = partition_by_anchor(labels, anchors, max_gap=140)
        pcts = badges_by_anchor(sp, anchors, max_gap=55, consumed=consumed)

        deals: list[Deal] = []
        for i, (price_new, euro, _cents) in enumerate(tiles):
            name_span = pick_name(buckets.get(i, []))
            pct = pcts.get(i)
            # The struck-through old price is printed much smaller than the
            # headline price and sits right beside it. Requiring both keeps a
            # neighbouring tile's price from being claimed as this one's.
            near_olds = [s for s in olds
                         if s.size < euro.size * 0.5 and euro.gap_to(s) <= 45]
            old = nearest(euro, near_olds, max_gap=45)
            card = nearest(euro, loyal, max_gap=70)

            deal = Deal(
                retailer=self.name,
                name=assemble_name(name_span, buckets.get(i, [])) or "(neznan izdelek)",
                leaflet_id=cat["slug"],
                source_url=cat["url"],
                valid_from=cat["valid_from"],
                valid_to=cat["valid_to"],
                price_new=price_new,
                price_old=parse_price(old.text) if old else None,
                discount_published=pct,
                loyalty_required="Tuš klub" if card else None,
                source_page=page_no,
            )
            if name_span is None:
                deal.quarantine("no-name-found")
            if price_new is None:
                deal.quarantine("no-price")
            if reconcile(deal.price_old, deal.price_new, pct) is False:
                deal.quarantine("price-mismatch")
            deals.append(deal)

        return deals, len(tiles)
