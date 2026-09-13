"""Mercator - dated PDFs with a full text layer.

The extraction anchors on DISCOUNT BADGES, not on prices. That is the single
most important design decision in this file, and it came from a real bug:

An earlier prototype anchored on prices, found 8 products on page 3, and every
one passed the old x (1-pct) = new cross-check. A flawless-looking 8/8. The page
actually had 9 products. It silently dropped "Mehcalec Silan", a genuine 50%
offer with no absolute price ("buy 2 or more, all items -50%").

Every tile carries exactly one badge, so iterating badges makes the count
correct by construction and makes conditional offers impossible to miss.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import httpx
import pymupdf

from ..models import Deal, SourceResult, SourceStatus, parse_price
from ..pdftext import (Span, is_label_candidate, nearest, partition_by_anchor,
                       pick_name, reconcile, spans)
from .base import UA, Source, classify_http_failure

CATALOGUE_INDEX = "https://www.mercator.si/katalogi/"

# Font sizes are strongly diagnostic in Mercator's layout (see FINDINGS.md).
SZ_EURO, SZ_CENT, SZ_BADGE = 27.0, 15.7, 15.0
SZ_OLD, SZ_PC30, SZ_NAME = 7.3, 6.0, 9.0

BADGE_RE = re.compile(r"-?\s*(\d{1,2})\s*%")
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
PDF_HREF_RE = re.compile(r'href="([^"]*?/assets/Katalogi/[^"]+?\.pdf)"', re.I)
MONEY_RE = re.compile(r"(\d{1,4},\d{2})")
CONDITION_HINTS = ("OB NAKUPU", "VSI KOSI", "SPLACA", "SPLAČA", "2 KOSA")


class Mercator(Source):
    name = "mercator"
    display_name = "Mercator"

    def fetch(self, today: date) -> SourceResult:
        res = self.result(SourceStatus.OK, CATALOGUE_INDEX)
        try:
            catalogues = self.discover()
        except httpx.HTTPStatusError as exc:
            return res.fail(
                classify_http_failure(exc.response.status_code, exc.response.text[:300]),
                f"index: HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return res.fail(SourceStatus.PARSE_FAILED, f"index: {exc!r}")

        active = [c for c in catalogues if c["valid_from"] <= today <= c["valid_to"]]
        if not active:
            return res.fail(
                SourceStatus.NO_LEAFLET,
                f"none of {len(catalogues)} catalogues cover {today}")

        all_items: list[Deal] = []
        declared = 0
        for cat in active:
            try:
                items, badges = self._parse_pdf(cat)
            except Exception as exc:  # noqa: BLE001 - report, never crash the run
                return res.fail(SourceStatus.PARSE_FAILED, f"{cat['slug']}: {exc!r}")
            all_items.extend(items)
            declared += badges

        res.items = all_items
        res.pages_declared = declared      # badge count == expected product count
        res.pages_parsed = len(all_items)
        res.leaflet_id = "+".join(c["slug"] for c in active)
        res.valid_from = min(c["valid_from"] for c in active)
        res.valid_to = max(c["valid_to"] for c in active)
        if not all_items:
            return res.fail(SourceStatus.PARSE_FAILED,
                            "catalogue found but no products extracted")
        return res

    # -- discovery -------------------------------------------------------

    def discover(self) -> list[dict]:
        """Find catalogue PDFs and their validity windows from the index page."""
        with self.client({"User-Agent": UA}) as client:
            r = client.get(CATALOGUE_INDEX)
            r.raise_for_status()
            html = r.text

        out: list[dict] = []
        for m in PDF_HREF_RE.finditer(html):
            url = m.group(1)
            if url.startswith("/"):
                url = "https://www.mercator.si" + url
            if any(c["url"] == url for c in out):
                continue
            # Validity dates are rendered near the link as "10.9.2026 - 16.9.2026".
            window = html[max(0, m.start() - 1500): m.start() + 1500]
            dates = DATE_RE.findall(window)
            if len(dates) < 2:
                continue
            try:
                vf = date(int(dates[0][2]), int(dates[0][1]), int(dates[0][0]))
                vt = date(int(dates[1][2]), int(dates[1][1]), int(dates[1][0]))
            except ValueError:
                continue
            if vt < vf:
                continue
            out.append({"url": url,
                        "slug": url.rsplit("/", 1)[-1].removesuffix(".pdf"),
                        "valid_from": vf, "valid_to": vt})
        return out

    # -- parsing ---------------------------------------------------------

    def _parse_pdf(self, cat: dict) -> tuple[list[Deal], int]:
        with self.client({"User-Agent": UA}, timeout=300.0) as client:
            r = client.get(cat["url"])
            r.raise_for_status()
            blob = r.content

        doc = pymupdf.open(stream=blob, filetype="pdf")
        deals: list[Deal] = []
        badge_total = 0
        for index in range(doc.page_count):
            page_deals, badges = self._parse_page(doc[index], index + 1, cat)
            deals.extend(page_deals)
            badge_total += badges
        return deals, badge_total

    def _parse_page(self, page, page_no: int, cat: dict) -> tuple[list[Deal], int]:
        sp = spans(page)
        badges = [s for s in sp
                  if s.size_is(SZ_BADGE) and "%" in s.text and BADGE_RE.search(s.text)]
        if not badges:
            return [], 0

        euros = [s for s in sp if s.size_is(SZ_EURO) and re.fullmatch(r"\d{1,4}", s.text)]
        cents = [s for s in sp if s.size_is(SZ_CENT) and re.fullmatch(r"\d{2}", s.text)]
        olds = [s for s in sp if s.size_is(SZ_OLD) and MONEY_RE.fullmatch(s.text)]
        pc30s = [s for s in sp if s.size_is(SZ_PC30) and "PC30" in s.text.upper()]
        labels = [s for s in sp if is_label_candidate(s)]
        conds = [s for s in sp if any(h in s.text.upper() for h in CONDITION_HINTS)]

        # Each tile's anchor is its big price where it has one, else the badge.
        prices: list[Decimal | None] = []
        anchors: list[Span] = []
        for badge in badges:
            euro = nearest(badge, euros, max_gap=70)
            price_new = None
            if euro is not None:
                cent = nearest(euro, cents, max_gap=40)
                if cent is not None:
                    price_new = Decimal(f"{euro.text}.{cent.text}")
            prices.append(price_new)
            anchors.append(euro if euro is not None else badge)

        buckets = partition_by_anchor(labels, anchors, max_gap=150)

        deals: list[Deal] = []
        for i, badge in enumerate(badges):
            pct = Decimal(BADGE_RE.search(badge.text).group(1))
            anchor, price_new = anchors[i], prices[i]
            name_span = pick_name(buckets.get(i, []))
            old = nearest(anchor, olds, max_gap=60)
            pc30 = nearest(anchor, pc30s, max_gap=60)
            cond = nearest(badge, conds, max_gap=45)

            deal = Deal(
                retailer=self.name,
                name=(name_span.text.strip() if name_span else "") or "(neznan izdelek)",
                leaflet_id=cat["slug"],
                source_url=cat["url"],
                valid_from=cat["valid_from"],
                valid_to=cat["valid_to"],
                price_new=price_new,
                price_old=parse_price(old.text) if old else None,
                discount_published=pct,
                price_30day_low=self._money(pc30),
                condition=cond.text.strip() if (cond and price_new is None) else None,
                source_page=page_no,
            )
            if name_span is None:
                deal.quarantine("no-name-found")
            if price_new is None and deal.condition is None:
                deal.quarantine("no-price-and-no-condition")
            if reconcile(deal.price_old, deal.price_new, pct) is False:
                deal.quarantine("price-mismatch")
            deals.append(deal)

        return deals, len(badges)

    @staticmethod
    def _money(span: Span | None) -> Decimal | None:
        if span is None:
            return None
        m = MONEY_RE.search(span.text)
        if not m:
            return None
        try:
            return parse_price(m.group(1))
        except Exception:  # noqa: BLE001
            return None
