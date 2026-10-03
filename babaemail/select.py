"""Preferences apply only to the output of the full verification pass."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from .categorize import LABELS, categorize, normalized
from .models import Deal
from .verify import Verification


_NAME_STOP_WORDS = frozenset({'akcija', 'izdelek', 'lidl', 'plus', 'sveze', 'svezi', 'slovensko',
                              'slovenski', 'nepakirano', 'pakirano', 'kosov', 'kosi', 'kos'})


def _name_stems(deal: Deal) -> set[str]:
    """Product-word stems used only to suppress an identical Lidl web repeat."""
    words = normalized(f'{deal.brand or ""} {deal.name}').split()
    return {word[:5] for word in words if len(word) >= 5 and word not in _NAME_STOP_WORDS}


def _same_product_price(left: Deal, right: Deal) -> bool:
    return (left.price_new == right.price_new
            and bool(_name_stems(left) & _name_stems(right)))


@dataclass
class Cut:
    deal: Deal
    reason: str


@dataclass
class Selection:
    items: list[Deal] = field(default_factory=list)
    cuts: list[Cut] = field(default_factory=list)


def load_preferences(path: Path) -> dict:
    preferences = yaml.safe_load(path.read_text('utf-8'))
    if not isinstance(preferences, dict):
        raise ValueError('preferences.yaml must contain a mapping')
    keys = [s['key'] for s in preferences['stores']]
    if len(keys) != len(set(keys)) or set(keys) - {'lidl', 'hofer', 'mercator', 'spar', 'tus'}:
        raise ValueError('Unknown or duplicate store in preferences')
    for value in [preferences['max_items'], preferences['lookahead_days'],
                  *(s['max_items'] for s in preferences['stores'])]:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('Item limits and lookahead_days must be nonnegative integers')
    for store in preferences['stores']:
        if 'website_only_max' in store and (store['key'] != 'lidl' or isinstance(store['website_only_max'], bool)
                                            or not isinstance(store['website_only_max'], int)
                                            or store['website_only_max'] < 0):
            raise ValueError('website_only_max is a nonnegative Lidl-only integer')
    for value in [preferences['default_floor'],
                  *(c['floor'] if c.get('floor') is not None else preferences['default_floor'] for c in preferences['categories'].values())]:
        if not Decimal(str(value)).is_finite() or not 0 <= Decimal(str(value)) <= 100:
            raise ValueError('Discount floors must be between 0 and 100')
    return preferences


def select(verified: list[Verification], preferences: dict) -> Selection:
    result = Selection()
    stores = {v.source.retailer: v for v in verified}
    configured = {s['key'] for s in preferences['stores']}
    for verification in verified:
        if verification.source.retailer not in configured:
            result.cuts.extend(Cut(d, 'store-disabled') for d in verification.accepted)
    priority = {key: i for i, key in enumerate(preferences['categories'])}
    always_words = [normalized(w) for w in preferences['always_show'] if w.strip()]
    for store in preferences['stores']:
        verification = stores.get(store['key'])
        if verification is None:
            continue
        candidates = []
        for deal in verification.accepted:
            deal.category = categorize(deal)
            category = preferences['categories'].get(deal.category, {'label': LABELS.get(deal.category, 'Ostalo'), 'show': True})
            always = any(w in normalized(f'{deal.brand or ""} {deal.name}') for w in always_words)
            pct = deal.discount_pct if deal.condition_kind != 'bonus-pack' else None
            reason = None
            if store.get('checked', True) is False:
                # Contract rule 3: a store is fully covered or explicitly not covered.
                reason = 'store-not-yet-checked'
            elif not always:
                if not category.get('show', True):
                    reason = 'category-hidden'
                elif pct is None and deal.retailer != 'lidl':
                    reason = 'no-discount-percent'
                elif pct is not None and pct < Decimal(str(category['floor'] if category.get('floor') is not None else preferences['default_floor'])):
                    reason = 'below-discount-floor'
            if reason:
                result.cuts.append(Cut(deal, reason))
            else:
                # A leaflet deal takes precedence over an API-only repeat.
                rank = (deal.source_kind == 'website_only', not always, pct is None, -(pct or Decimal(0)),
                        priority.get(deal.category, len(priority)), deal.name.casefold())
                candidates.append((rank, deal))
        candidates.sort(key=lambda item: item[0])
        leaflet_items = []
        website_count = 0
        website_max = store.get('website_only_max', 6) if store['key'] == 'lidl' else 0
        for _, deal in candidates:
            if deal.source_kind == 'website_only' and any(_same_product_price(deal, item) for item in leaflet_items):
                result.cuts.append(Cut(deal, 'website-only-duplicate-leaflet'))
            elif deal.source_kind == 'website_only' and website_count >= website_max:
                result.cuts.append(Cut(deal, 'website-only-limit'))
            elif deal.source_kind != 'website_only' and len(leaflet_items) >= store['max_items']:
                result.cuts.append(Cut(deal, 'store-item-limit'))
            elif len(result.items) >= preferences['max_items']:
                result.cuts.append(Cut(deal, 'total-item-limit'))
            else:
                result.items.append(deal)
                if deal.source_kind == 'website_only':
                    website_count += 1
                else:
                    leaflet_items.append(deal)
    return result
