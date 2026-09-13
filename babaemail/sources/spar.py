"""Spar / Interspar - leaflet PDFs from letak.spar.si.

www.spar.si sits behind a Cloudflare managed challenge that no automated
browser gets through. It is not needed: the leaflets live on letak.spar.si,
an entirely unprotected host reachable with plain httpx, and the weekly slug
is derivable from the date. See FINDINGS.md for how this was discovered.

GOTCHA: that server does not implement HEAD - HEAD returns 404 while GET
returns 200. Never probe for a leaflet's existence with HEAD, or the
NO_LEAFLET gate fires every single day.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pymupdf

from ..models import Deal, SourceResult, SourceStatus, parse_price
from ..pdftext import (Span, is_label_candidate, nearest, partition_by_anchor,
                       assemble_name, pick_name, price_near, reconcile, spans)
from .base import UA, Source, classify_http_failure

HOST = "https://letak.spar.si"

# Font sizes for Spar's layout (see FINDINGS.md).
SZ_BADGE, SZ_EURO, SZ_CENT = 23.0, 21.9, 12.7
SZ_NAME, SZ_SMALL = 8.0, 5.0

BADGE_RE = re.compile(r"(\d{1,2})\s*%")
OLD_RE = re.compile(r"redna\s+cena\s+(\d{1,4},\d{2})", re.I)
PC_RE = re.compile(r"\bPC\s+(\d{1,4},\d{2})", re.I)
LOYALTY_RE = re.compile(r"s\s+SPAR\s+plus\s+kartico", re.I)
# "...veljajo od srede 9. 9. do ... najkasneje do torka 15. 9. 2026..."
VALIDITY_RE = re.compile(
    r"veljajo\s+od\s+\w+\s+(\d{1,2})\.\s*(\d{1,2})\..{0,120}?do\s+\w+\s+(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})",
    re.I | re.S)


class Spar(Source):
    name = "spar"
    display_name = "Spar"

    def slug_for(self, today: date) -> tuple[str, date]:
        """Weekly catalogue slug, plus the Wednesday it starts on.

        Spar's week runs Wednesday to Tuesday, so the active catalogue is named
        for the ISO week of the most recent Wednesday on or before today.
        """
        days_since_wed = (today.weekday() - 2) % 7      # Monday=0, Wednesday=2
        start = today - timedelta(days=days_since_wed)
        iso = start.isocalendar()
        return f"katalog-{iso.week:02d}{str(iso.year)[2:]}", start

    def fetch(self, today: date) -> SourceResult:
        slug, start = self.slug_for(today)
        url = f"{HOST}/{slug}/getPdf.ashx"
        res = self.result(SourceStatus.OK, url, leaflet_id=slug)

        try:
            # GET, never HEAD - this server 404s on HEAD.
            with self.client({"User-Agent": UA}, timeout=300.0) as client:
                r = client.get(url)
                r.raise_for_status()
                blob = r.content
        except httpx.HTTPStatusError as exc:
            return res.fail(classify_http_failure(exc.response.status_code,
                                                  exc.response.text[:300]),
                            f"HTTP {exc.response.status_code} for {slug}")
        except httpx.HTTPError as exc:
            return res.fail(SourceStatus.PARSE_FAILED, repr(exc))

        if not blob.startswith(b"%PDF"):
            return res.fail(SourceStatus.PARSE_FAILED, "response was not a PDF")

        try:
            doc = pymupdf.open(stream=blob, filetype="pdf")
        except Exception as exc:  # noqa: BLE001
            return res.fail(SourceStatus.PARSE_FAILED, f"open: {exc!r}")

        valid_from, valid_to = self._validity(doc, start)
        res.valid_from, res.valid_to = valid_from, valid_to

        # Freshness gate using the retailer's OWN published validity statement,
        # not our guess. Catches a stale leaflet still being served.
        if valid_to is not None and not (valid_from <= today <= valid_to):
            return res.fail(SourceStatus.STALE,
                            f"leaflet {slug} valid {valid_from}..{valid_to}, today {today}")

        deals: list[Deal] = []
        badge_total = 0
        for index in range(doc.page_count):
            page_deals, badges = self._parse_page(doc[index], index + 1, slug, url,
                                                  valid_from, valid_to)
            deals.extend(page_deals)
            badge_total += badges

        res.items = deals
        res.pages_declared = badge_total
        res.pages_parsed = len(deals)
        if not deals:
            return res.fail(SourceStatus.PARSE_FAILED,
                            f"{doc.page_count} pages but no products extracted")
        return res

    # -- internals -------------------------------------------------------

    def _validity(self, doc, fallback_start: date) -> tuple[date, date | None]:
        """Read the validity window from the leaflet's own small print."""
        for index in range(min(4, doc.page_count)):
            m = VALIDITY_RE.search(doc[index].get_text())
            if not m:
                continue
            d1, m1, d2, m2, yy = (int(x) for x in m.groups())
            try:
                end = date(yy, m2, d2)
                # the start has no year printed; it is the same year unless the
                # window straddles New Year
                start = date(yy if m1 <= m2 else yy - 1, m1, d1)
                return start, end
            except ValueError:
                continue
        return fallback_start, None

    def _parse_page(self, page, page_no: int, slug: str, url: str,
                    valid_from: date, valid_to: date | None) -> tuple[list[Deal], int]:
        sp = spans(page)
        badges = [s for s in sp if s.size_is(SZ_BADGE) and "%" in s.text and BADGE_RE.search(s.text)]
        if not badges:
            return [], 0

        # Scale-relative, not size-keyed: Spar's euro figure appears at 21.9,
        # 23.0 and 23.2 across one leaflet (see FINDINGS.md).
        digit_runs = [s for s in sp if re.fullmatch(r"\d{1,4}", s.text)]
        smalls = [s for s in sp if s.size_is(SZ_SMALL)]
        labels = [s for s in sp if is_label_candidate(s)]

        prices: list[Decimal | None] = []
        anchors: list[Span] = []
        for badge in badges:
            price_new, euro = price_near(badge, digit_runs, max_gap=70, cents_gap=40)
            prices.append(price_new)
            anchors.append(euro if euro is not None else badge)

        buckets = partition_by_anchor(labels, anchors, max_gap=130)

        deals: list[Deal] = []
        for i, badge in enumerate(badges):
            pct = Decimal(BADGE_RE.search(badge.text).group(1))
            anchor, price_new = anchors[i], prices[i]
            name_span = pick_name(buckets.get(i, []))

            old_price = pc_price = None
            loyalty = None
            for s in smalls:
                if anchor.gap_to(s) > 55:
                    continue
                if (m := OLD_RE.search(s.text)) and old_price is None:
                    old_price = parse_price(m.group(1))
                if (m := PC_RE.search(s.text)) and pc_price is None:
                    pc_price = parse_price(m.group(1))
                if LOYALTY_RE.search(s.text):
                    loyalty = "SPAR plus"

            deal = Deal(
                retailer=self.name,
                name=assemble_name(name_span, buckets.get(i, [])) or "(neznan izdelek)",
                leaflet_id=slug,
                source_url=url,
                valid_from=valid_from,
                valid_to=valid_to,
                price_new=price_new,
                price_old=old_price,
                discount_published=pct,
                price_30day_low=pc_price,
                loyalty_required=loyalty,
                source_page=page_no,
            )
            if name_span is None:
                deal.quarantine("no-name-found")
            if price_new is None:
                deal.quarantine("no-price")
            if reconcile(deal.price_old, deal.price_new, pct) is False:
                deal.quarantine("price-mismatch")
            deals.append(deal)

        return deals, len(badges)
