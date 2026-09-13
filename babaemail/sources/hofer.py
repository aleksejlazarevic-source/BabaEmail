"""Hofer - leaflet PDFs via Publitas.

IMPORTANT: Hofer's product API (api.hofer.si) returns exact prices but NEVER an
old price - 0 of 822 products carry `wasPriceDisplay`. Their own leaflet banner
says "TO NI AKCIJA!" ("this is not a sale"): Hofer's model is permanently low
prices, not markdowns. So the API cannot tell us what is discounted, and the
leaflet is the source of truth. The API remains useful for enrichment (product
photos, per-unit prices) and that is what `api_enrichment` is for.

Three-step chain, all plain httpx - no Cloudflare, no browser:
  1. hofer.si/aktualni-letaki-in-brosure  -> weekly leaflet slug
  2. letaki.hofer.si/<slug>/              -> Publitas viewer, contains one .pdf
  3. view.publitas.com/.../<uuid>.pdf     -> the leaflet

The slug carries a marketing suffix that changes, so it must be discovered from
step 1 rather than constructed.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import httpx
import pymupdf

from ..models import Deal, SourceResult, SourceStatus
from ..pdftext import (Span, assemble_name, decimal_price_tiles,
                       drop_repeated, is_label_candidate, nearest, parse_money,
                       partition_by_anchor, pick_name, reconcile, spans)
from .base import UA, Source, classify_http_failure

INDEX = "https://www.hofer.si/aktualni-letaki-in-brosure"
LEAFLET_HOST = "letaki.hofer.si"

# hofer.si REJECTS requests that carry a User-Agent (HTTP 403) and accepts
# those that do not - the same inversion as api.hofer.si. letaki.hofer.si and
# Publitas behave normally and want an ordinary UA. Do not "fix" this by adding
# browser headers; that is what breaks it.
HOFER_SI_HEADERS = {"accept-language": "sl-SI"}

SLUG_RE = re.compile(r"https?://letaki\.hofer\.si/([A-Za-z0-9_\-]+)")
WEEK_SLUG_RE = re.compile(r"letak_kw(\d{1,2})_(\d{4})", re.I)
PDF_RE = re.compile(r"https?://[^\s\"'<>\\)]+?\.pdf", re.I)

BADGE_RE = re.compile(r"-\s*(\d{1,2})\s*%")
OLD_LABEL_RE = re.compile(r"STARA\s+CENA", re.I)
MONEY_RE = re.compile(r"^\d{1,4},\d{2}$")
UNIT_RE = re.compile(r"^\d{1,4},\d{2}\s*/\s*\w+$")


class Hofer(Source):
    name = "hofer"
    display_name = "Hofer"

    def fetch(self, today: date) -> SourceResult:
        res = self.result(SourceStatus.OK, INDEX)
        try:
            slug = self._current_slug(today)
        except httpx.HTTPStatusError as exc:
            return res.fail(classify_http_failure(exc.response.status_code,
                                                  exc.response.text[:300]),
                            f"index: HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return res.fail(SourceStatus.PARSE_FAILED, f"index: {exc!r}")
        if slug is None:
            return res.fail(SourceStatus.NO_LEAFLET, "no weekly leaflet slug found")

        viewer = f"https://{LEAFLET_HOST}/{slug}/"
        res.source_url = viewer
        res.leaflet_id = slug

        try:
            pdf_url = self._pdf_url(viewer)
            if pdf_url is None:
                return res.fail(SourceStatus.PARSE_FAILED, f"no PDF link in {viewer}")
            with self.client({"User-Agent": UA}, timeout=300.0) as client:
                r = client.get(pdf_url)
                r.raise_for_status()
                blob = r.content
        except httpx.HTTPStatusError as exc:
            return res.fail(classify_http_failure(exc.response.status_code, ""),
                            f"pdf: HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return res.fail(SourceStatus.PARSE_FAILED, f"pdf: {exc!r}")

        if not blob.startswith(b"%PDF"):
            return res.fail(SourceStatus.PARSE_FAILED, "response was not a PDF")

        valid_from = self._week_start(slug) or today
        doc = pymupdf.open(stream=blob, filetype="pdf")
        deals: list[Deal] = []
        tiles = 0
        for index in range(doc.page_count):
            page_deals, n = self._parse_page(doc[index], index + 1, slug, viewer, valid_from)
            deals.extend(page_deals)
            tiles += n

        res.items = deals
        res.pages_declared = tiles
        res.pages_parsed = len(deals)
        res.valid_from = valid_from
        # Hofer prints no end date on these; we never invent one.
        res.valid_to = None
        if not deals:
            return res.fail(SourceStatus.PARSE_FAILED,
                            f"{doc.page_count} pages but nothing extracted")
        return res

    # -- discovery -------------------------------------------------------

    def _current_slug(self, today: date) -> str | None:
        with self.client(HOFER_SI_HEADERS) as client:
            r = client.get(INDEX)
            r.raise_for_status()
            html = r.text

        slugs = list(dict.fromkeys(SLUG_RE.findall(html)))
        week = today.isocalendar().week
        year = today.isocalendar().year
        weekly = []
        for slug in slugs:
            m = WEEK_SLUG_RE.search(slug)
            if m:
                weekly.append((int(m.group(1)), int(m.group(2)), slug))
        if not weekly:
            return None
        # the leaflet for this week, else the most recent one that has started
        exact = [s for w, y, s in weekly if w == week and y == year]
        if exact:
            return exact[0]
        started = [(w, y, s) for w, y, s in weekly if (y, w) <= (year, week)]
        if started:
            return max(started)[2]
        return min(weekly)[2]

    def _pdf_url(self, viewer: str) -> str | None:
        with self.client({"User-Agent": UA}, timeout=120.0) as client:
            r = client.get(viewer)
            r.raise_for_status()
            for url in PDF_RE.findall(r.text):
                if "publitas" in url or url.endswith(".pdf"):
                    return url.split("?")[0]
        return None

    @staticmethod
    def _week_start(slug: str) -> date | None:
        m = WEEK_SLUG_RE.search(slug)
        if not m:
            return None
        try:
            return date.fromisocalendar(int(m.group(2)), int(m.group(1)), 1)
        except ValueError:
            return None

    # -- parsing ---------------------------------------------------------

    def _parse_page(self, page, page_no: int, slug: str, viewer: str,
                    valid_from: date) -> tuple[list[Deal], int]:
        sp = spans(page)
        tiles = decimal_price_tiles(sp)
        if not tiles:
            return [], 0

        labels = drop_repeated([s for s in sp if is_label_candidate(s)])
        badges = [s for s in sp if BADGE_RE.fullmatch(s.text.strip())]
        smalls = [s for s in sp if MONEY_RE.match(s.text.strip()) and s.size < 20]
        units = [s for s in sp if UNIT_RE.match(s.text.strip().replace(" ", ""))]
        old_labels = [s for s in sp if OLD_LABEL_RE.search(s.text)]

        anchors = [span for _, span in tiles]
        buckets = partition_by_anchor(labels, anchors, max_gap=130)

        deals: list[Deal] = []
        for i, (price_new, anchor) in enumerate(tiles):
            name_span = pick_name(buckets.get(i, []))
            badge = nearest(anchor, badges, max_gap=70)
            pct = Decimal(BADGE_RE.fullmatch(badge.text.strip()).group(1)) if badge else None

            # The old price sits next to a "STARA CENA:" label; find the label
            # first so a nearby unit price is never mistaken for it.
            old_price = None
            label = nearest(anchor, old_labels, max_gap=70)
            if label is not None:
                cand = [s for s in smalls if label.gap_to(s) <= 40]
                if cand:
                    old_price = parse_money(min(cand, key=lambda s: label.gap_to(s)).text)

            unit = nearest(anchor, units, max_gap=60)

            deal = Deal(
                retailer=self.name,
                name=assemble_name(name_span, buckets.get(i, [])) or "(neznan izdelek)",
                leaflet_id=slug,
                source_url=viewer,
                valid_from=valid_from,
                valid_to=None,
                price_new=price_new,
                price_old=old_price,
                discount_published=pct,
                unit_price=unit.text.strip() if unit else None,
                source_page=page_no,
            )
            if name_span is None:
                deal.quarantine("no-name-found")
            if reconcile(deal.price_old, deal.price_new, pct) is False:
                deal.quarantine("price-mismatch")
            deals.append(deal)

        return deals, len(tiles)
