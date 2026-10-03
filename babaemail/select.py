"""Preferences apply only to the output of the full verification pass."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from .categorize import LABELS, categorize, normalized
from .models import Deal
from .verify import Verification


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
            if not always:
                if not category.get('show', True):
                    reason = 'category-hidden'
                elif pct is None and deal.retailer != 'lidl':
                    reason = 'no-discount-percent'
                elif pct is not None and pct < Decimal(str(category['floor'] if category.get('floor') is not None else preferences['default_floor'])):
                    reason = 'below-discount-floor'
            if reason:
                result.cuts.append(Cut(deal, reason))
            else:
                rank = (not always, pct is None, -(pct or Decimal(0)),
                        priority.get(deal.category, len(priority)), deal.name.casefold())
                candidates.append((rank, deal))
        candidates.sort(key=lambda item: item[0])
        for index, (_, deal) in enumerate(candidates):
            if index >= store['max_items']:
                result.cuts.append(Cut(deal, 'store-item-limit'))
            elif len(result.items) >= preferences['max_items']:
                result.cuts.append(Cut(deal, 'total-item-limit'))
            else:
                result.items.append(deal)
    return result
