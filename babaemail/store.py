"""Read the weekly Lidl cache; record history only after a successful send."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .models import Deal, Notice, SourceResult, SourceStatus, parse_price


@dataclass
class LidlCache:
    source: SourceResult
    path: Path | None
    held_records: list[str]


def failure(retailer: str, reason: str, status: SourceStatus = SourceStatus.PARSE_FAILED) -> SourceResult:
    return SourceResult(retailer, status, '', datetime.now(), error=reason)


def _day(value) -> date | None:
    return date.fromisoformat(value) if value else None


def _deal(raw: dict) -> Deal:
    data = {f.name: raw[f.name] for f in fields(Deal) if f.name in raw}
    flags = data.get('flags', [])
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        raise ValueError('flags must be a list of strings')
    for key in ('price_new', 'price_old', 'price_30day_low'):
        if key in data:
            data[key] = parse_price(data[key])
    if data.get('discount_published') is not None:
        data['discount_published'] = Decimal(str(data['discount_published']))
    data['valid_from'] = _day(data.get('valid_from'))
    if data['valid_from'] is None:
        raise ValueError('Missing valid_from')
    data['valid_to'] = _day(data.get('valid_to'))
    return Deal(**data)


def _partner(raw: dict, source: SourceResult) -> Deal:
    """Accept only structured dates and money, never interpret marketing prose."""
    validity = raw.get('validity') or {}
    if isinstance(validity, str):
        # An ISO end date is safe; an arbitrary Slovenian sentence is not.
        validity = {'valid_to': validity}
    start = raw.get('valid_from') or validity.get('valid_from') or validity.get('from')
    end = raw.get('valid_to') or validity.get('valid_to') or validity.get('to')
    if not start or not end:
        raise ValueError('Partner offer needs explicit valid_from and valid_to')
    partner = raw['partner'].strip()
    what = raw['what'].strip()
    if not partner or not what:
        raise ValueError('Partner offer needs partner and what')
    price = raw.get('price_new', raw.get('price'))
    if isinstance(price, str) and not re.fullmatch(r'\s*\d+(?:[.,]\d{1,2})?\s*(?:€|EUR)?\s*', price):
        raise ValueError('Partner price must be absolute money, without marketing qualifiers')
    conditions = raw.get('conditions') or raw.get('condition')
    if conditions is not None and not isinstance(conditions, str):
        raise ValueError('Partner conditions must be published text')
    deal = _deal({
        'retailer': 'lidl', 'name': what, 'brand': partner,
        'leaflet_id': source.leaflet_id or '', 'source_url': source.source_url,
        'valid_from': start, 'valid_to': end,
        'price_new': price,
        'price_old': raw.get('price_old'), 'discount_published': raw.get('discount_pct'),
        'condition': conditions,
        'condition_kind': raw.get('condition_kind'), 'loyalty_required': 'Lidl Plus',
        'flags': ['email:partner-offer', *raw.get('flags', [])],
    })
    if raw.get('quarantined') or raw.get('proven') is False:
        deal.quarantine('partner-unproven')
    return deal


def load_lidl(today: date, directory: Path = Path('out'), path: Path | None = None) -> LidlCache:
    held = []
    if path is None:
        candidates = sorted(directory.glob('lidl_*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for candidate in candidates:
            try:
                doc = json.loads(candidate.read_text('utf-8'), parse_float=Decimal)
                start, end = _day(doc.get('valid_from')), _day(doc.get('valid_to'))
                if start and end and start <= today <= end:
                    path = candidate
                    break
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                held.append(f'Unreadable cache {candidate}: {exc}')
        if path is None:
            return LidlCache(failure('lidl', 'No cached Lidl leaflet covers this date', SourceStatus.NO_LEAFLET), None, held)
    source = failure('lidl', 'Cache not read')
    try:
        doc = json.loads(path.read_text('utf-8'), parse_float=Decimal)
        if doc.get('retailer') != 'lidl':
            raise ValueError('Not a Lidl cache')
        source = SourceResult(
            retailer='lidl', status=SourceStatus(doc['status']),
            source_url=doc.get('source_url', ''), fetched_at=datetime.fromtimestamp(path.stat().st_mtime),
            leaflet_id=doc.get('leaflet_id'), leaflet_title=doc.get('leaflet_title'),
            valid_from=_day(doc.get('valid_from')), valid_to=_day(doc.get('valid_to')),
            pages_declared=doc.get('pages_declared'), pages_parsed=doc.get('pages_parsed'), error=doc.get('error'),
        )
        if not source.valid_from or not source.valid_to or not source.valid_from <= today <= source.valid_to:
            source.fail(SourceStatus.STALE, 'Cached Lidl window does not cover this date')
        if doc.get('contributes') is False and source.status is SourceStatus.OK:
            source.fail(SourceStatus.PARSE_FAILED, 'Cache explicitly marked non-contributing')
        if 'items' in doc:
            records = [(raw, False) for raw in doc['items']]
        else:
            records = ([(raw, False) for raw in doc.get('shipped', [])]
                       + [(raw, True) for raw in doc.get('quarantined', [])])
        for index, (raw, held_back) in enumerate(records):
            try:
                deal = _deal(raw)
                if held_back and not deal.is_quarantined:
                    deal.quarantine('cache-held-record')
                source.items.append(deal)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                held.append(f'Item {index} {raw!r}: {exc}')
                source.fail(SourceStatus.PARSE_FAILED, 'Malformed cached item; coverage is incomplete')
        # Partner offers cannot make an otherwise empty leaflet valid.
        if not source.items and source.status is SourceStatus.OK:
            source.fail(SourceStatus.PARSE_FAILED, 'Zero cached leaflet items')
        for raw in doc.get('notices', []):
            try:
                data = {f.name: raw[f.name] for f in fields(Notice) if f.name in raw}
                data['day'] = _day(data.get('day'))
                if data.get('proven') is not True:
                    data['proven'] = False
                source.notices.append(Notice(**data))
            except (ValueError, TypeError, KeyError) as exc:
                held.append(f'Notice {raw!r}: {exc}')
        for raw in doc.get('partner_offers') or []:
            try:
                source.items.append(_partner(raw, source))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                held.append(f'Partner {raw!r}: {exc}')
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        source.fail(SourceStatus.PARSE_FAILED, f'Cannot load Lidl cache {path}: {exc}')
    return LidlCache(source, path, held)


def write_history(directory: Path, today: date, items: list[Deal]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    sent_at = datetime.now().astimezone()
    path = directory / f'email-{today}-{sent_at.strftime("%Y%m%dT%H%M%S%f")}.json'
    doc = {'date': today.isoformat(), 'sent_at': sent_at.isoformat(),
           'items': [{'identity': list(d.identity()), 'price_new': str(d.price_new),
                      'condition': d.condition, 'loyalty_required': d.loyalty_required} for d in items]}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    return path
