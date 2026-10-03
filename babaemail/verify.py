"""Final accuracy gate, independent of preferences. Every rejection is retained."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .models import Deal, SourceResult, SourceStatus


@dataclass
class Rejection:
    deal: Deal
    reasons: list[str]


@dataclass
class Verification:
    source: SourceResult
    accepted: list[Deal] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)


def verify_deal(deal: Deal, source: SourceResult, today: date, lookahead_days: int) -> list[str]:
    reasons = []
    if source.status is not SourceStatus.OK:
        reasons.append(f'source-not-ok:{source.status.value}')
    if not source.items:
        reasons.append('source-empty')
    if (source.pages_declared is not None and source.pages_parsed is not None
            and source.pages_declared != source.pages_parsed):
        reasons.append('source-incomplete-pages')
    if deal.retailer != source.retailer:
        reasons.append('retailer-mismatch')
    if deal.is_quarantined:
        reasons.extend(f for f in deal.flags if f.startswith('quarantine:'))
    if deal.price_new is None or deal.price_new <= 0:
        reasons.append('new-price-missing-or-nonpositive')
    if deal.price_old is not None and deal.price_new is not None:
        if deal.price_old <= deal.price_new and deal.condition_kind != 'bonus-pack':
            reasons.append('old-price-not-higher')
    pct = deal.discount_published
    if pct is not None:
        if not pct.is_finite() or pct < 0 or pct > 100:
            reasons.append('published-percent-invalid')
        elif deal.condition_kind == 'from-price':
            reasons.append('from-price-with-percent')
        elif (deal.price_old is not None and deal.price_old > 0
              and deal.price_new is not None):
            actual = (1 - deal.price_new / deal.price_old) * 100
            # Lidl truncates. Other stores may round their printed badge;
            # permit at most one percentage point, using Decimal throughout.
            consistent = (pct <= actual < pct + 1 if source.retailer == 'lidl'
                          else abs(pct - actual) <= Decimal(1))
            if not consistent:
                reasons.append(f'published-percent-inconsistent:{pct} vs {actual}')
    if deal.condition_kind and not (deal.condition or '').strip():
        reasons.append('condition-missing')
    if deal.condition_kind == 'second-item' and deal.price_old is None:
        reasons.append('second-item-first-price-missing')
    if deal.valid_to is not None and deal.valid_to < today:
        reasons.append('expired')
    if deal.valid_to is not None and deal.valid_to < deal.valid_from:
        reasons.append('invalid-validity-window')
    # A printed leaflet cannot establish a deal outside the leaflet's own
    # declared window.  API-only Lidl offers intentionally have their own
    # dates and are therefore exempt from this cross-check.
    if deal.source_kind == 'leaflet':
        outside_window = ((source.valid_from is not None and deal.valid_from < source.valid_from)
                          or (source.valid_to is not None and deal.valid_to is not None
                              and deal.valid_to > source.valid_to))
        if outside_window:
            reasons.append('outside-leaflet-window')
    if deal.valid_from > today + timedelta(days=lookahead_days):
        reasons.append('starts-after-lookahead')
    return reasons


def verify(source: SourceResult, today: date, lookahead_days: int = 0) -> Verification:
    if lookahead_days < 0:
        raise ValueError('lookahead_days must be nonnegative')
    if source.status is SourceStatus.OK and not source.items:
        source.fail(SourceStatus.PARSE_FAILED, 'Zero extracted items')
    if (source.status is SourceStatus.OK and source.pages_declared is not None
            and source.pages_parsed is not None and source.pages_declared != source.pages_parsed):
        source.fail(SourceStatus.PARSE_FAILED, 'Incomplete page coverage')
    result = Verification(source)
    for deal in source.items:
        reasons = verify_deal(deal, source, today, lookahead_days)
        if reasons:
            result.rejected.append(Rejection(deal, reasons))
        else:
            result.accepted.append(deal)
    return result
