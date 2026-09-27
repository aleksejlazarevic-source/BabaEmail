"""Targeted second reads of Lidl labels, bound to their source page pixels.

Crop boxes are derived from the full-page OCR price/badge/text geometry. OCR
runs before, and without input from, the VLM. Raw outputs are saved for review;
a model's 'confirmed' boolean is never evidence. The ordinary full-page
readings and all reconciliation gates remain necessary. Only crops whose
full-page SHA256 matches are loaded by the live adapter. No answer keys,
product-specific names, or page numbers are used here.
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


def load_crops(pngs: dict[int, bytes], cache_dir=CACHE_DIR, stats: dict | None = None):
    """Load usable evidence, skipping corrupt/stale files independently.

    A stale/different page's crop is never usable, even at the same price.
    ``stats`` lets the live adapter expose bad evidence without turning an
    optional second read into failure of the whole Lidl source.
    """
    out = []
    counts = stats if stats is not None else {}
    counts.setdefault('crop_files_bad', 0)
    counts.setdefault('crop_files_stale', 0)
    digests = {page: hashlib.sha256(png).hexdigest() for page, png in pngs.items()}
    for path in sorted(cache_dir.glob('*.json')):
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise TypeError('crop evidence is not an object')
        except (OSError, UnicodeError, ValueError, TypeError):
            counts['crop_files_bad'] += 1
            continue
        page = data.get('page')
        if page not in digests:
            continue
        if data.get('page_sha256') != digests[page]:
            counts['crop_files_stale'] += 1
            continue
        out.append(data)
    return out


def _crop_box(reading, width: int, height: int):
    """Tight complete label box from OCR geometry, in full-page coordinates."""
    raw = reading.raw
    detail = raw.get('raw') if isinstance(raw.get('raw'), dict) else raw
    price = tuple(raw.get('price_bbox') or reading.bbox or ())
    if len(price) != 4:
        return None
    boxes = [price]
    for key in ('badge_bbox', 'old_bbox', 'logo_bbox'):
        box = raw.get(key) or detail.get(key)
        if box and len(box) == 4:
            boxes.append(tuple(box))
    # A successfully isolated name is safer than all regional text, which can
    # include packaging copy or a neighbouring label. If name isolation failed,
    # use the already-owned text lines recorded by the page OCR reader.
    # Flag panels contain their complete printed label. A page-level name box
    # outside such a panel is often nearby campaign furniture (kw39 p42), not
    # the product; including it can pull another price into the crop.
    flag_panel = raw.get('badge') == 'flag'
    name = None if flag_panel else (raw.get('name_bbox') or detail.get('name_bbox'))
    if name and len(name) == 4:
        boxes.append(tuple(name))
    elif not flag_panel:
        owned = set(raw.get('text_lines') or detail.get('text_lines') or ())
        boxes.extend(tuple(line['bbox']) for line in
                     (raw.get('region_lines') or detail.get('region_lines') or ())
                     if line.get('text') in owned and len(line.get('bbox', ())) == 4)
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    ph = max(1, price[3] - price[1])
    pad = max(24, round(ph * .18))
    box = (max(0, int(x0 - pad)), max(0, int(y0 - pad)),
           min(width, int(x1 + pad)), min(height, int(y1 + pad)))
    return box if box[0] < box[2] and box[1] < box[3] else None


def select_crop_boxes(pngs, readings, preliminary, evidence=(), limit=40):
    """Select crop-settleable held leaflet-only OCR tiles, deterministically.

    Reconciliation is run first, so API matches, duplicates, suspect repeats,
    validity-only holds, and already-shipped tiles are never sent for a crop
    read. This function selects geometry only; all price/name proof remains in
    :func:`_proof` and :func:`refine_readings`.
    """
    from PIL import Image

    held_ids = set()
    for rec in preliminary.records:
        reasons = [f for f in rec.deal.flags if f.startswith('quarantine:')]
        if (rec.rule == 'leaflet-only' and rec.tile is not None and len(reasons) == 1
                and reasons[0].startswith('quarantine:leaflet-only-unverified:')):
            held_ids.update(id(r) for r in rec.tile.readings)

    proven_geometry = set()
    for data in evidence:
        try:
            proof = _proof(data)
        except (KeyError, TypeError, ValueError):
            proof = None
        if proof is not None:
            proven_geometry.add((data.get('page'), proof['price_bbox']))

    vlm_by_key = {}
    for r in readings:
        if r.source == 'vlm' and r.page is not None and r.new_price is not None and r.name:
            vlm_by_key.setdefault((r.page, r.new_price), []).append(r)

    sizes = {}
    selected = []
    seen = set()
    for r in sorted(readings, key=lambda x: (x.page or 0,
                                             tuple((x.raw or {}).get('price_bbox', ())))):
        if (len(selected) >= limit or r.source != 'ocr' or id(r) not in held_ids
                or r.page not in pngs or r.new_price is None or not isinstance(r.raw, dict)):
            continue
        price_box = tuple(r.raw.get('price_bbox', ()))
        if len(price_box) != 4 or (r.page, price_box) in proven_geometry:
            continue
        peers = [v for v in vlm_by_key.get((r.page, r.new_price), ())
                 if isinstance(r.lidl_plus, bool) and v.lidl_plus == r.lidl_plus]
        if not peers:
            continue
        detail = r.raw.get('raw') if isinstance(r.raw.get('raw'), dict) else r.raw
        badge = r.raw.get('badge')
        bare = badge is None and r.old_price is None and r.pct is None
        discount = badge == 'discount'
        from_price = (badge == 'flag'
                      and re.search(r'\bze\s*od\b', fold(detail.get('flag_text') or '')))
        if not (bare or discount or from_price):
            continue
        if r.page not in sizes:
            with Image.open(io.BytesIO(pngs[r.page])) as im:
                sizes[r.page] = im.size
        box = _crop_box(r, *sizes[r.page])
        key = (r.page, box)
        if box is not None and key not in seen:
            selected.append(key)
            seen.add(key)
    return selected


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
    A matching full-page VLM price is mandatory too, and exactly one full-page
    VLM name must share a non-brand product word with the crop name.
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
        # A shared price (and even a shared brand) does not bind the crop to
        # this product.  Require the independent page reader to share a
        # non-brand product word, using the same rule as plain-price tiles.
        # This also prevents a bare brand or an unrelated lone descriptor
        # from becoming the shipped name through the unique-price fallback.
        if len(named) != 1:
            continue
        vi = named
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
        out[vi[0]] = replace(b, name=proof['name'])
        used.update(oi + vi)
    return out
