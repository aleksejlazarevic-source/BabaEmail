"""Conservative Slovenian keyword categories; unknown products remain visible."""
from __future__ import annotations

import re
import unicodedata

from .models import Deal

LABELS = {
    'meso': 'Meso', 'ribe': 'Ribe', 'mlecno': 'Mlečni izdelki in jajca',
    'sadje_zelenjava': 'Sadje in zelenjava', 'kruh': 'Kruh in pekovski izdelki',
    'shramba': 'Shramba', 'sladko': 'Sladko in prigrizki', 'pijace': 'Pijače',
    'zamrznjeno': 'Zamrznjeno', 'gospodinjstvo': 'Gospodinjstvo', 'ostalo': 'Ostalo',
    'oblacila': 'Oblačila', 'orodje': 'Orodje', 'elektronika': 'Elektronika',
    'vrt': 'Vrt', 'igrace': 'Igrače',
}


def normalized(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', text.casefold())
                   if unicodedata.category(c) != 'Mn')


# First match wins. Non-food brands and household terms precede food stems.
RULES = [
    ('orodje', r'parkside|vrtal|brusil|vijac|orodj|kladiv'),
    ('oblacila', r'esmara|lupilu|crivit|livergy|majic|hlac|nogavic|oblac|jakn|copat|cevl'),
    ('elektronika', r'silvercrest|slusalk|televiz|polnil|elektronik|baterij'),
    ('vrt', r'vrtna|vrtni|vrtno|sadik|gnojil|zemlja za|loncnica'),
    ('igrace', r'playtive|igrac|puzzle|sestavljanka'),
    ('gospodinjstvo', r'livarno|w5\b|cien\b|silan|persil|ariel|cistil|detergent|praln|mehcal|pomival|toalet|papirnat|robck|vrec.*smet|gobica|milo|sampon|zobn|plenice'),
    ('zamrznjeno', r'zamrzn|sladoled|pomfrit'),
    ('ribe', r'\brib|losos|postrv|sarenk|tun[ai]|sardin|oslic|skus|kozic|lignj'),
    ('meso', r'meso|mesni|pisc|svinj|govej|goved|telec|pur[aij]|klobas|salam|sunk|prsut|cevap|pleskavic|slanina|perut|zrezk|pancet|chicken|nugget'),
    ('mlecno', r'mlek|mlec|milbona|pilos|jogurt|skut|\bsir|\bsmetan|\bmaslo|\bjajc|kefir|margarin'),
    ('sadje_zelenjava', r'jabolk|hrusk|banan|mango|grozd|bresk|sliv|pomaranc|limon|mandarin|avokad|kivi|borovnic|malin|jagod|fige|pesa|krompir|paradiz|kumar|solat|koren|cebul|cesen|paprik|buck|zelje|brokoli|cveta|gob[ae]'),
    ('kruh', r'kruh|zemlj|pekov|roglj|baget|burek|borek|pecivo|struck'),
    ('sladko', r'cokolad|cokol|piskot|keks|bonbon|cips|cokolino|napolit|alesto|oreh|mandlj|lesnik|prigriz'),
    ('pijace', r'pivo|\bvin[oa]|sok|pijac|\bvod[ae]|radensk|nektar|kav[aen]|\bcaj|fruc|janzevec'),
    ('shramba', r'testen|spaget|makaron|\briz|moka|olje|kis\b|konzerv|omak|namaz|med\b|marmelad|lec[ae]|fizol|cicerik|zacimb|sol\b|kosmic|jesprenj|sladkor'),
]
HINTS = {
    'f+v': 'sadje_zelenjava', 'fruit': 'sadje_zelenjava', 'vegetable': 'sadje_zelenjava',
    'non-food': 'ostalo', 'non food': 'ostalo', 'meat': 'meso', 'fish': 'ribe',
    'dairy': 'mlecno', 'bakery': 'kruh', 'beverage': 'pijace', 'household': 'gospodinjstvo',
}


def categorize(deal: Deal) -> str:
    text = normalized(f'{deal.brand or ""} {deal.name}')
    for key, pattern in RULES:
        if re.search(pattern, text):
            return key
    hint = normalized(deal.retailer_category or '')
    if hint in LABELS:
        return hint
    for term, key in HINTS.items():
        if term in hint:
            return key
    for key, pattern in RULES:
        if re.search(pattern, hint):
            return key
    return 'ostalo'
