"""Targeted second reads of Lidl labels, bound to their source page pixels.

An operator supplies a tight crop containing ONE complete name/price/badge
area. OCR runs before, and without input from, the VLM. Raw outputs are saved
for review; a model's 'confirmed' boolean is never evidence. The ordinary
full-page readings and all reconciliation gates remain necessary.

This is a cache-consuming checkpoint workflow, not automatic tile selection.
Only crops whose full-page SHA256 matches are loaded by the live adapter.
No answer keys, product-specific names, or page numbers are used here.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from . import vlm
from .lidl_reconcile import (LeafletReading, _share_product_word, fold,
                             ocr_reconciled, split_leaflet_name)

CACHE_DIR = Path(__file__).resolve().parents[1] / '.cache' / 'lidl-crops'
VERSION = 1
PROMPT = """Read ONE cropped Slovenian Lidl product label. Copy the PRINTED
label name (brand + Slovenian product name, not words on packaging).
Read only the largest new price. Old price means a visibly crossed-out
price, NEVER a '1 kg =' or '1 l =' unit price. Do not infer any numbers.
Copy the entire badge text, including 'že od', gratis or other conditions.
Return JSON: name, new_price, old_price (null if absent), discount_pct
(positive integer or null), badge_text (null if absent), lidl_plus
(true if visible, false if absent, null if uncertain), single_piece_text
(copy the exact 'cena za 1 kos:' line or null), purchase_quantity_text
(copy any 'velja za nakup točno ... kosov' line or null).
Use decimal price strings. Never compute a unit price or quantity.
"""


def _is_plus(mask):
    """A printed cross has two solid centre strokes and FOUR empty corners.

    This is shape evidence for '+', never a repair of OCR's '4' using maths.
    """
    h, w = mask.shape
    if min(h, w) < 12 or not 0.75 <= w / h <= 1.3:
        return False
    qh, qw = h // 4, w // 4
    corners = (mask[:qh, :qw], mask[:qh, -qw:], mask[-qh:, :qw], mask[-qh:, -qw:])
    return (mask[h//2, :].mean() > .9 and mask[:, w//2].mean() > .9
            and all(c.mean() < .08 for c in corners))


def read_multibuy_badge(rgb):
    """OCR the digits either side of one geometrically proven '+' in a badge."""
    from . import ocr
    labels, blobs = ocr.components(ocr.glyph_masks(rgb)['R'], min_area=20)
    results = set()
    for plus in blobs:
        if not _is_plus(labels[plus.y:plus.y1, plus.x:plus.x1] == plus.index):
            continue
        cy = plus.y + plus.h / 2
        digits = sorted((b for b in blobs if 1.2 * plus.h <= b.h <= 1.8 * plus.h
                         and abs(b.y + b.h / 2 - cy) < .2 * plus.h), key=lambda b: b.x)
        left = [b for b in digits if b.x1 < plus.x]
        right = [b for b in digits if b.x > plus.x1]
        if not (1 <= len(left) <= 3 and 1 <= len(right) <= 3):
            continue
        seq = left + [plus] + right
        if any(b.x - a.x1 > .6 * plus.h for a, b in zip(seq, seq[1:])):
            continue
        n, _ = ocr.read_glyphs(labels, left, len(left))
        m, _ = ocr.read_glyphs(labels, right, len(right))
        if n and m and int(n) > 0 and int(m) > 0:
            results.add(f'{int(n)}+{int(m)}')
    return next(iter(results)) if len(results) == 1 else None


def words(text):
    return re.findall(r'[a-z0-9]+', fold(text or ''))


def contains_name(text, name):
    """All words, in order, including the brand; no fuzzy/generic-word match."""
    a, b = words(text), words(name)
    return bool(b) and any(a[i:i + len(b)] == b for i in range(len(a) - len(b) + 1))


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() and n > 0 else None
    except InvalidOperation:
        return None


def read_crop(page_png: bytes, page: int, box: tuple[int, int, int, int], *,
              model=vlm.DEFAULT_MODEL, url=vlm.OLLAMA_URL):
    """Read one operator-selected crop. No page-level VLM inference is run."""
    import cv2
    import numpy as np
    from PIL import Image
    from . import ocr
    from .sources.lidl import read_page_tiles

    im = Image.open(io.BytesIO(page_png)).convert('RGB')
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= im.width and 0 <= y0 < y1 <= im.height):
        raise ValueError('crop outside page')
    cropped = im.crop(box)
    buf = io.BytesIO()
    cropped.save(buf, format='PNG')
    rgb = np.array(cropped)
    tiles = read_page_tiles(rgb, page)
    if len(tiles) != 1:
        raise ValueError(f'crop must contain exactly one OCR price structure, found {len(tiles)}')
    # The same OCR engine at several renderings is ONE reader, not votes
    # independent of each other. The VLM is the second reader.
    texts = [' '.join(line.text for line in ocr.read_text_tesseract(rgb, 'slv'))]
    for tile in tiles:
        texts.append(' '.join(ln['text'] for ln in tile.raw.get('region_lines', ())))
    # Read the actual badge separately: a bold '+' is often OCR'd as '4'.
    # Its shape, not the offer arithmetic, determines the sign.
    badge_count = None
    if tiles[0].badge_bbox:
        bx0, by0, bx1, by1 = tiles[0].badge_bbox
        badge_rgb = rgb[by0:by1, bx0:bx1]
        badge_count = read_multibuy_badge(badge_rgb)
        for mask in ocr.glyph_masks(badge_rgb).values():
            bw = np.where(mask, 0, 255).astype('uint8')
            texts.append(ocr._run_tesseract(bw, ['-l', 'slv', '--psm', '11']))
    for mask in ocr.glyph_masks(rgb).values():
        bw = np.where(mask, 0, 255).astype('uint8')
        bw = cv2.resize(bw, None, fx=0.5, fy=0.5)
        for psm in (6, 11):
            texts.append(ocr._run_tesseract(bw, ['-l', 'slv', '--psm', str(psm)]))
    payload = dict(model=model, prompt=PROMPT,
                   images=[base64.b64encode(buf.getvalue()).decode()],
                   stream=False, format='json', options=dict(temperature=0, num_ctx=8192))
    response = httpx.post(url.rstrip('/') + '/api/generate', json=payload, timeout=300)
    response.raise_for_status()
    envelope = response.json()
    if envelope.get('done_reason') not in (None, 'stop') or envelope.get('error'):
        raise vlm.VlmUnavailable('crop VLM failed or truncated its output')
    # Parse before saving; preserve the original response too for audit.
    json.loads(envelope['response'])
    return dict(version=VERSION, page=page, page_sha256=hashlib.sha256(page_png).hexdigest(),
                crop_box=list(box), crop_sha256=hashlib.sha256(buf.getvalue()).hexdigest(),
                model=model, prompt=PROMPT, ocr_tiles=[t.record() for t in tiles],
                ocr_texts=texts, ocr_badge_count=badge_count, vlm=envelope)


def save_crop(evidence, cache_dir=CACHE_DIR):
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / (evidence['page_sha256'] + '-' + evidence['crop_sha256'] + '.json')
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    return path


def load_crops(pngs: dict[int, bytes], cache_dir=CACHE_DIR):
    """A stale/different page's crop is never usable, even at the same price."""
    out = []
    for page, png in pngs.items():
        digest = hashlib.sha256(png).hexdigest()
        for path in sorted(cache_dir.glob(digest + '-*.json')):
            data = json.loads(path.read_text())
            if data.get('page_sha256') == digest and data.get('page') == page:
                out.append(data)
    return out


def _proof(data):
    """Validate independent reads; return no proof on missing/ambiguous data."""
    if data.get('version') != VERSION or len(data.get('ocr_tiles', ())) != 1:
        return None
    tile = data['ocr_tiles'][0]
    try:
        model = json.loads(data['vlm']['response'])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(model, dict) or not isinstance(model.get('name'), str):
        return None
    price = _decimal(tile.get('price'))
    if price is None or price != _decimal(model.get('new_price')):
        return None
    if price != price.quantize(Decimal('.01')):
        return None
    name = model['name']
    texts = data.get('ocr_texts', ())
    if not any(contains_name(text, name) for text in texts):
        return None
    # A price qualifier may only be introduced when BOTH readers copied it.
    from_price = (words(model.get('badge_text')) == ['ze', 'od']
                  and any(re.search(r'\bze\s*od\b', fold(t)) for t in texts)
                  and not any(re.search(r'gratis|drugi\s+kos|nakup|kupon|%', fold(t)) for t in texts)
                  and tile.get('badge') == 'flag'
                  and tile.get('old_price') is None and tile.get('pct') is None)
    old, pct = _decimal(tile.get('old_price')), _decimal(tile.get('pct'))
    r = LeafletReading('ocr', data['page'], price, old, pct)
    old_proven = (ocr_reconciled(r) and old == _decimal(model.get('old_price'))
                  and pct == _decimal(model.get('discount_pct'))
                  and tile.get('badge') == 'discount'
                  and re.fullmatch(r'-\s*' + str(int(pct)) + r'\s*%',
                                   model.get('badge_text') or '') is not None)
    multi_buy = _multi_buy(data, model, price)
    x0, y0, _, _ = data['crop_box']
    a, b, c, d = tile['price_bbox']
    return dict(name=name, price=price, price_bbox=(a+x0, b+y0, c+x0, d+y0),
                old_pct=(old, pct) if old_proven else None, from_price=from_price,
                multi_buy=multi_buy,
                lidl_plus=model.get('lidl_plus'))


_SINGLE = re.compile(r'cena\s+za\s+1\s+kos\s*:\s*(\d+[.,]\d{2})(?!\d)', re.I)
_MULTI = re.compile(r'\s*(\d+)\s*\+\s*(\d+)\s+gratis\s*', re.I)
_QUANTITY = re.compile(r'velja\s+za\s+nakup\s+tocno\s+(\d+)\s+kosov', re.I)


def _multi_buy(data, model, price):
    """Two readers prove badge + single-piece line; big price is paid*single.

    A per-litre or per-piece number fails the equality. Never fix a leaflet
    misprint by calculating a new price (kw39 Freshona: .76 != 2*.57).
    """
    tile = data['ocr_tiles'][0]
    if (tile.get('badge') != 'flag' or tile.get('old_price') is not None
            or tile.get('pct') is not None or model.get('old_price') is not None
            or model.get('discount_pct') is not None):
        return None
    badge = _MULTI.fullmatch(model.get('badge_text') or '')
    if not badge:
        return None
    n, m = map(int, badge.groups())
    texts = data.get('ocr_texts', ())
    if (not n or not m or data.get('ocr_badge_count') != f'{n}+{m}'
            or not any(re.search(r'\bgratis\b', fold(t)) for t in texts)):
        return None
    seen = {D.replace(',', '.') for t in texts for D in _SINGLE.findall(t)}
    vm = _SINGLE.fullmatch((model.get('single_piece_text') or '').strip())
    if len(seen) != 1 or vm is None:
        return None
    single = _decimal(next(iter(seen)))
    if single is None or single != _decimal(vm[1].replace(',', '.')) or n * single != price:
        return None
    # A printed exact-quantity restriction cannot contradict the badge or
    # disappear between readers. The N+M badge itself proves the bundle size.
    oq = {int(q) for t in texts for q in _QUANTITY.findall(fold(t))}
    vq = {int(q) for q in _QUANTITY.findall(fold(model.get('purchase_quantity_text')))}
    if oq != vq or (oq and oq != {n + m}):
        return None
    return dict(paid=n, free=m, single_price=str(single))


def refine_readings(readings, evidence):
    """R0d: replace only names/independently proven fields, never a new price.

    The full-page OCR price geometry must equal the crop's price geometry.
    A matching full-page VLM price is mandatory too. With price collisions,
    exactly one VLM name must match, or one OCR structure plus unanimous
    variant metadata permits a new scoped reading without deleting originals.
    Crops cannot overrule existing Lidl Plus votes, dates, or API flags.
    """
    out = list(readings)
    # Disagreeing crop rereads are not settled by file order. Keep the tile
    # unchanged until the conflict is reviewed. Identical proofs collapse.
    grouped = {}
    for data in evidence:
        proof = _proof(data)
        if proof is not None:
            grouped.setdefault((data['page'], proof['price_bbox']), []).append((data, proof))
    used = set()
    for group in grouped.values():
        data, proof = group[0]
        if any(p != proof for _, p in group[1:]):
            continue
        page, price = data['page'], proof['price']
        oi = [i for i, r in enumerate(out) if r.source == 'ocr' and r.page == page
              and r.new_price == price and isinstance(r.raw, dict)
              and tuple(r.raw.get('price_bbox', ())) == proof['price_bbox']]
        vi = [i for i, r in enumerate(out) if r.source == 'vlm' and r.page == page
              and r.new_price == price]
        named = [i for i in vi if _share_product_word(out[i].name, proof['name'])]
        add_scoped = False
        if len(named) == 1:
            vi = named
        else:
            if sum(r.source == 'ocr' and r.page == page and r.new_price == price for r in out) != 1:
                continue
            if len(vi) > 1:
                # A page VLM may list pictured variants as separate products.
                # Keep those readings quarantinable; ADD the spatially scoped
                # crop name, only when all metadata and the brand agree.
                keys = {(r.lidl_plus, r.valid_to_text, r.unit,
                         tuple(words(split_leaflet_name(r.name)[0]))) for r in (out[i] for i in vi)}
                brand = words(split_leaflet_name(proof['name'])[0])
                if len(keys) != 1 or not brand or next(iter(keys))[3] != tuple(brand):
                    continue
                add_scoped = True
        if len(oi) != 1 or not vi or used.intersection(oi + vi):
            continue
        a, b = out[oi[0]], out[vi[0]]
        brand = words(split_leaflet_name(b.name)[0])
        if brand and words(proof['name'])[:len(brand)] != brand:
            continue
        x0, y0, x1, y1 = data['crop_box']
        # Do not accept a crop that cut off the original detected badge.
        if a.bbox is None or not (x0 <= a.bbox[0] and y0 <= a.bbox[1]
                                   and x1 >= a.bbox[2] and y1 >= a.bbox[3]):
            continue
        lp = proof['lidl_plus']
        if isinstance(lp, bool) and any(r.lidl_plus is not None and r.lidl_plus != lp for r in (a, b)):
            continue
        raw = dict(a.raw)
        raw['crop_confirmation'] = dict(page_sha256=data['page_sha256'],
                                         crop_sha256=data['crop_sha256'],
                                         name=proof['name'], from_price=proof['from_price'],
                                         multi_buy=proof['multi_buy'])
        old, pct = a.old_price, a.pct
        if proof['old_pct'] is not None and a.raw.get('badge') == 'discount':
            co, cp = proof['old_pct']
            # A conflicting earlier OCR value is uncertainty, not a licence
            # to choose whichever crop reconciles.
            if (old is None or old == co) and (pct is None or pct == cp):
                old, pct = co, cp
        out[oi[0]] = replace(a, name=proof['name'], old_price=old, pct=pct, raw=raw)
        if add_scoped:
            out.append(replace(b, name=proof['name'], bbox=proof['price_bbox'],
                               raw={'crop_sha256': data['crop_sha256']}))
        else:
            out[vi[0]] = replace(b, name=proof['name'])
        used.update(oi + vi)
    return out
