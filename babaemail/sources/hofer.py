"""Hofer — the cleanest source in the project.

Unauthenticated JSON API with integer cent prices, so there is no parsing
ambiguity at all: no PDF geometry, no OCR, no float rounding.

Two hard-won details (see FINDINGS.md):
  * Send ONLY `accept-language`. Adding a User-Agent, Origin or Referer
    header makes the API return 403.
  * `limit` must be one of an allowed set. 60 works; 100 and 200 return 400.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import httpx

from ..models import Deal, SourceResult, SourceStatus, parse_price
from .base import Source, classify_http_failure

API = "https://api.hofer.si/v3/product-search"
PAGE_LIMIT = 60                     # must be an allowed value; 100/200 -> HTTP 400
HEADERS = {"accept-language": "sl-SI"}   # adding anything else -> HTTP 403

# TODO: resolve a Ljubljana store id. I032 (1230 Domžale) is the API's default
# and was used for all Phase 0 research.
DEFAULT_SERVICE_POINT = "I032"


class Hofer(Source):
    name = "hofer"
    display_name = "Hofer"

    def __init__(self, service_point: str = DEFAULT_SERVICE_POINT):
        self.service_point = service_point

    def promotion_keys(self, today: date) -> list[str]:
        """Hofer promotions start on Mondays and Thursdays.

        The two currently-running promotions are the most recent Monday and the
        most recent Thursday on or before today.
        """
        keys = []
        for weekday in (0, 3):                      # Monday, Thursday
            delta = (today.weekday() - weekday) % 7
            keys.append((today - timedelta(days=delta)).isoformat())
        return sorted(set(keys))

    def fetch(self, today: date) -> SourceResult:
        res = self.result(SourceStatus.OK, API, valid_from=today)
        all_items: list[Deal] = []
        declared_total = 0

        for key in self.promotion_keys(today):
            try:
                items, total = self._fetch_promotion(key, today)
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:400]
                return res.fail(classify_http_failure(exc.response.status_code, body),
                                f"promotion {key}: HTTP {exc.response.status_code}")
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                return res.fail(SourceStatus.PARSE_FAILED, f"promotion {key}: {exc!r}")
            all_items.extend(items)
            declared_total += total

        res.items = all_items
        # Contract rule 6 / completeness gate: the API tells us how many rows
        # exist, so a short read is detectable rather than silently accepted.
        res.pages_declared = declared_total
        res.pages_parsed = len(all_items)
        res.leaflet_id = "+".join(self.promotion_keys(today))
        if not all_items:
            return res.fail(SourceStatus.NO_LEAFLET, "no products in any active promotion")
        return res

    # -- internals -------------------------------------------------------

    def _fetch_promotion(self, promotion_key: str, today: date) -> tuple[list[Deal], int]:
        deals: list[Deal] = []
        offset, total = 0, None
        with httpx.Client(headers=HEADERS, timeout=60.0, follow_redirects=True) as client:
            while True:
                r = client.get(API, params={
                    "currency": "EUR", "serviceType": "walk-in",
                    "limit": str(PAGE_LIMIT), "offset": str(offset),
                    "promotionKey": promotion_key, "sort": "relevance",
                    "servicePoint": self.service_point})
                r.raise_for_status()
                payload = r.json()
                total = payload.get("meta", {}).get("pagination", {}).get("totalCount", 0)
                rows = payload.get("data") or []
                for row in rows:
                    deal = self._to_deal(row, promotion_key, today)
                    if deal is not None:
                        deals.append(deal)
                offset += PAGE_LIMIT
                if not rows or offset >= (total or 0):
                    break
        return deals, int(total or 0)

    def _to_deal(self, row: dict, promotion_key: str, today: date) -> Deal | None:
        price = row.get("price") or {}
        amount = price.get("amountRelevant", price.get("amount"))
        if amount is None:
            return None
        # integer cents -> exact Decimal, never float
        new_price = (Decimal(int(amount)) / 100).quantize(Decimal("0.01"))

        old_price = None
        try:
            old_price = parse_price(price.get("wasPriceDisplay"))
        except Exception:
            pass

        deal = Deal(
            retailer=self.name,
            name=(row.get("name") or "").strip(),
            price_new=new_price,
            price_old=old_price,
            brand=(row.get("brandName") or None),
            unit_price=price.get("comparisonDisplay") or price.get("perUnitDisplay"),
            image_url=self._image(row),
            retailer_item_id=row.get("sku"),
            leaflet_id=promotion_key,
            source_url=f"https://www.hofer.si/posebna-ponudba/{promotion_key}",
            # Hofer publishes a start date and NO end date. We do not invent one;
            # the email shows "od <date>" rather than a fabricated "velja do".
            valid_from=date.fromisoformat(row.get("onSaleDate") or promotion_key),
            valid_to=None,
        )
        cats = row.get("categories") or []
        if cats:
            deal.retailer_category = (cats[0].get("name") or "").strip() or None
        if row.get("notForSale"):
            deal.flag("notForSale")
        if not deal.name:
            deal.flag("missing-name")
        return deal

    @staticmethod
    def _image(row: dict, width: int = 240) -> str | None:
        """Prefer the front product shot (FR01); fall back to the first asset."""
        assets = row.get("assets") or []
        if not assets:
            return None
        chosen = next((a for a in assets if a.get("assetType") == "FR01"), assets[0])
        url = chosen.get("url") or ""
        if not url:
            return None
        return url.replace("{width}", str(width)).replace("{slug}", row.get("urlSlugText") or "p")
