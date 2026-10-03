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
    'vrt': 'Vrt', 'igrace': 'Igrače', 'neprehrana': 'Neživilsko',
}


def normalized(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', text.casefold())
                   if unicodedata.category(c) != 'Mn')


# First match wins.  Match complete words or deliberate word stems, never an
# arbitrary substring ("ribani" is grated, not fish).  The short exclusions
# keep ambiguous food words out of an otherwise broad stem family.
RULES = [
    ('orodje', (), ('parkside', 'vrtal', 'brusil', 'vijac', 'orodj', 'kladiv')),
    ('oblacila', (), ('esmara', 'lupilu', 'crivit', 'livergy', 'majic', 'hlac', 'nogavic', 'oblac', 'jakn', 'copat', 'cevl')),
    ('elektronika', (), ('silvercrest', 'slusalk', 'televiz', 'polnil', 'elektronik', 'baterij', 'sesal')),
    ('vrt', (), ('vrtna', 'vrtni', 'vrtno', 'sadik', 'gnojil', 'loncnica')),
    ('igrace', (), ('playtive', 'igrac', 'puzzle', 'sestavljanka')),
    ('neprehrana', ('zaustavljalnik', 'deznik', 'pse', 'psa', 'psi', 'pesek', 'igra', 'obraz', 'krema za obraz'), ('svec', 'vlosek', 'kovcek', 'kapacitet', 'cvrtnik', 'elektron', 'mikrov', 'higiensk', 'kozmetik', 'briv', 'susilec', 'susilnik', 'mack', 'zival', 'knjig', 'druzabn')),
    ('gospodinjstvo', ('w5', 'cien', 'silan', 'persil', 'ariel', 'bref', 'faks', 'wc', 'jar', 'dash'), ('livarno', 'cistil', 'detergent', 'praln', 'mehcal', 'pomival', 'toalet', 'papirnat', 'papir', 'robck', 'vrec', 'gobica', 'milo', 'sampon', 'zobn', 'plenice', 'obesank', 'razmasceval', 'prhan')),
    ('zamrznjeno', (), ('zamrzn', 'sladoled', 'pomfrit', 'spomladan', 'calzone')),
    ('kruh', (), ('kruh', 'zemlj', 'pekov', 'roglj', 'baget', 'burek', 'borek', 'pecivo', 'struck', 'hlebec')),
    ('ribe', ('riba', 'ribe', 'ribji', 'ribje', 'ocean', 'sea'), ('losos', 'postrv', 'sarenk', 'tun', 'sardin', 'oslic', 'skus', 'kozic', 'skamp', 'lignj')),
    ('meso', (), ('meso', 'mesni', 'pisc', 'svinj', 'govej', 'goved', 'telec', 'pur', 'jagnj', 'klobas', 'salam', 'sunk', 'prsut', 'cevap', 'pleskavic', 'slanina', 'perut', 'zrezk', 'pancet', 'chicken', 'nugget', 'mortadel', 'bresaol', 'narezek', 'zasek', 'ocvirk', 'vratin', 'hrenovk', 'pecenic')),
    ('mlecno', (), ('mlek', 'mlec', 'milbona', 'pilos', 'jogurt', 'skut', 'sir', 'smetan', 'maslo', 'jajc', 'kefir', 'margarin', 'mozzarell', 'kajmak', 'gorgonzol')),
    ('sadje_zelenjava', (), ('jabolk', 'hrusk', 'banan', 'mango', 'grozd', 'bresk', 'sliv', 'pomaranc', 'limon', 'mandarin', 'avokad', 'kivi', 'borovnic', 'malin', 'jagod', 'fige', 'pesa', 'krompir', 'paradiz', 'kumar', 'solat', 'koren', 'cebul', 'cesen', 'paprik', 'buck', 'zelje', 'brokoli', 'cveta', 'gob')),
    ('sladko', (), ('cokolad', 'cokol', 'piskot', 'keks', 'bonbon', 'cips', 'cokolino', 'napolit', 'alesto', 'oreh', 'mandlj', 'lesnik', 'prigriz', 'biskvit', 'kinder', 'bueno', 'pingui', 'cards', 'lada', 'sladic', 'desert', 'tiramisu', 'cannoli', 'vaflj', 'kolack', 'rolad', 'gumi', 'flips', 'smoki')),
    ('pijace', ('hop', 'cao', 'fruc'), ('pivo', 'vin', 'sok', 'pijac', 'vod', 'radensk', 'nektar', 'kav', 'caj', 'janzevec', 'bellarom', 'espresso', 'kakav', 'cockta', 'cola', 'cider', 'redbull', 'kombucha', 'energi')),
    ('shramba', ('riz', 'kis', 'med', 'sol'), ('testen', 'spaget', 'makaron', 'njok', 'testo', 'moka', 'olje', 'konzerv', 'omak', 'namaz', 'marmelad', 'lec', 'fizol', 'cicerik', 'zacimb', 'kosmic', 'jesprenj', 'sladkor', 'vegeta', 'kvas', 'kapr', 'oliv', 'panko', 'drobtin', 'juh', 'ragu', 'majonez', 'rezanc', 'pesto', 'lazanj', 'salsa', 'paradiz')),
]
HINTS = {
    'f+v': 'sadje_zelenjava', 'fruit': 'sadje_zelenjava', 'vegetable': 'sadje_zelenjava',
    'non-food': 'ostalo', 'non food': 'ostalo', 'meat': 'meso', 'fish': 'ribe',
    'dairy': 'mlecno', 'bakery': 'kruh', 'beverage': 'pijace', 'household': 'gospodinjstvo',
}


def categorize(deal: Deal) -> str:
    text = normalized(f'{deal.brand or ""} {deal.name}')
    words = tuple(re.findall(r'\w+', text))
    for key, exact, stems in RULES:
        if any(word in exact for word in words) or any(word.startswith(stem) for word in words for stem in stems):
            return key
    hint = normalized(deal.retailer_category or '')
    if hint in LABELS:
        return hint
    for term, key in HINTS.items():
        if term in hint:
            return key
    hint_words = tuple(re.findall(r'\w+', hint))
    for key, exact, stems in RULES:
        if any(word in exact for word in hint_words) or any(word.startswith(stem) for word in hint_words for stem in stems):
            return key
    return 'ostalo'
