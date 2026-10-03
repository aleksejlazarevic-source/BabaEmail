"""Slovenian, scroll-only email presentation. No network or images."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .categorize import LABELS
from .models import Deal, SourceResult, SourceStatus, money
from .select import Selection
from .verify import verify_deal

STORE_LABELS = {'lidl': 'Lidl', 'hofer': 'Hofer', 'mercator': 'Mercator', 'spar': 'Spar', 'tus': 'Tuš'}
WEEKDAYS = ('ponedeljek', 'torek', 'sredo', 'četrtek', 'petek', 'soboto', 'nedeljo')
SHORT_DAYS = ('pon.', 'tor.', 'sre.', 'čet.', 'pet.', 'sob.', 'ned.')
ROOT = Path(__file__).resolve().parents[1]


def euros(value: Decimal, unit: str | None = None) -> str:
    value = money(value)
    return f'{value:.2f}'.replace('.', ',') + ' €' + ('/kg' if unit == 'kg' else '')


def short_date(day: date) -> str:
    return f'{SHORT_DAYS[day.weekday()]} {day.day}. {day.month}.'


def deal_view(deal: Deal, today: date) -> dict:
    name = deal.name
    if deal.brand and deal.brand.casefold() not in name.casefold():
        name = f'{deal.brand} {name}'
    new = euros(deal.price_new, deal.price_unit)
    old = euros(deal.price_old, deal.price_unit) if deal.price_old is not None else None
    pct = deal.discount_display
    condition = deal.condition or ''
    if deal.condition_kind == 'second-item':
        # The raw condition carries the SECOND-piece badge; the separate %
        # below is discount_display, the honest saving over two pieces.
        price = f'drugi kos {condition}: {new} (prvi {old})'
        # Avoid the redundant "drugi kos -80% na drugi kos" wording.
        import re
        badge = re.search(r'[-−]?\s*(\d+)\s*%', condition)
        if badge:
            price = f'drugi kos −{badge.group(1)} %: {new} (prvi {old})'
        old = None
    elif deal.condition_kind == 'from-price':
        price, old, pct = f'že od {new}', None, None
    else:
        price = new
        if deal.condition_kind == 'bonus-pack':
            old, pct = None, None
    validity = []
    if deal.valid_from > today:
        validity.append('od ' + short_date(deal.valid_from))
    if deal.valid_to:
        validity.append('do ' + short_date(deal.valid_to))
    return {'name': name, 'price': price, 'old': old, 'pct': pct,
            'condition': condition, 'condition_kind': deal.condition_kind,
            'unit': deal.unit_price or '', 'loyalty': deal.loyalty_required or '',
            'validity': ' · '.join(validity)}


def render(selection: Selection, sources: list[SourceResult], preferences: dict, today: date) -> tuple[str, str]:
    by_store = {s.retailer: s for s in sources}
    # Defence in depth: an arbitrary Selection cannot bypass the gate.
    for deal in selection.items:
        source = by_store[deal.retailer]
        reasons = verify_deal(deal, source, today, preferences['lookahead_days'])
        if reasons:
            raise ValueError(f'Unsafe selected item {deal.name}: {reasons}')
    stores = []
    for config in preferences['stores']:
        key = config['key']
        items = [d for d in selection.items if d.retailer == key]
        if not items:
            continue
        groups = []
        for category in dict.fromkeys([*preferences['categories'], *LABELS]):
            deals = [deal_view(d, today) for d in items if (d.category or 'ostalo') == category
                     and d.source_kind != 'website_only' and 'email:partner-offer' not in d.flags]
            if deals:
                groups.append({'label': preferences['categories'].get(category, {}).get('label', LABELS.get(category, 'Ostalo')),
                               'deals': deals})
        website = [deal_view(d, today) for d in items if d.source_kind == 'website_only'
                   and 'email:partner-offer' not in d.flags]
        partners = [deal_view(d, today) for d in items if 'email:partner-offer' in d.flags]
        stores.append({'label': STORE_LABELS[key], 'key': key, 'groups': groups,
                       'website': website, 'partners': partners,
                       'lidl_plus': key == 'lidl' and any(d.loyalty_required for d in items)})
    failures = [f'Danes ni bilo mogoče preveriti akcij v trgovini {STORE_LABELS.get(s.retailer, s.retailer)}.'
                for s in sources if s.status is not SourceStatus.OK or not s.items]
    notices = [n.text for s in sources if s.contributes for n in s.notices
               if n.proven and n.text and (n.day is None or today <= n.day <= today + timedelta(days=preferences['lookahead_days']))]
    context = {'title': f'Akcije za {WEEKDAYS[today.weekday()]}, {today.day}. {today.month}.',
               'count': len(selection.items), 'store_count': len(stores),
               'failures': failures, 'notices': notices, 'stores': stores}
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'), undefined=StrictUndefined,
                      autoescape=select_autoescape(['html', 'xml', 'html.j2']),
                      trim_blocks=True, lstrip_blocks=True)
    html = env.get_template('email.html.j2').render(**context)
    plain = env.get_template('email.txt.j2').render(**context)
    if len(html.encode('utf-8')) >= 90_000:
        raise ValueError('Email HTML exceeds the 90 KB limit')
    return html, plain
