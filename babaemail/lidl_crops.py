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
from .lidl_reconcile import (LeafletReading, _ocr_text_proves_name,
                             _share_product_word, _stems, _unverified_why,
                             build_tiles, fold, ocr_reconciled, pack_size,
                             split_leaflet_name)

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


def _cached_logo_complete(data, page_image):
    """Validate legacy detector bounds against pixels beyond the saved box.

    Old caches segmented a restricted window and could save a clipped logo.
    A one-pixel border suffices to disprove truncation: a complete connected
    blue component has exactly the saved bounds and cannot reach that border.
    This rechecks geometry only; it never rereads a name, price or LP vote.
    """
    import numpy as np
    from . import ocr

    tile = data['ocr_tiles'][0]
    logo = tile['raw']['logo_bbox']
    if (not isinstance(logo, (list, tuple)) or len(logo) != 4
            or not all(type(v) is int for v in logo)):
        return False
    cx, cy, _, _ = data['crop_box']
    x0, y0, x1, y1 = (int(v) for v in
                      (logo[0] + cx, logo[1] + cy, logo[2] + cx, logo[3] + cy))
    if not (0 < x0 < x1 < page_image.width and 0 < y0 < y1 < page_image.height):
        return False
    rgb = np.array(page_image.crop((x0 - 1, y0 - 1, x1 + 1, y1 + 1)))
    _, blobs = ocr.components(ocr.lp_blue_mask(rgb))
    return any((b.x, b.y, b.x1, b.y1) == (1, 1, x1 - x0 + 1, y1 - y0 + 1)
               for b in blobs)


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
    page_images = {}
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
        tiles = data.get('ocr_tiles')
        if (isinstance(tiles, list) and len(tiles) == 1
                and isinstance(tiles[0], dict)
                and isinstance(tiles[0].get('raw'), dict)
                and tiles[0]['raw'].get('logo_bbox') is not None):
            try:
                from PIL import Image
                if page not in page_images:
                    page_images[page] = Image.open(io.BytesIO(pngs[page])).convert('RGB')
                data['logo_geometry_complete'] = _cached_logo_complete(data, page_images[page])
            except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
                # Keep the evidence and explicitly refuse positive ownership;
                # skipping it could leave a page False/False free to ship.
                data['logo_geometry_complete'] = False
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
    owned = set(raw.get('text_lines') or detail.get('text_lines') or ())
    # A multi-buy's per-piece line is part of the price semantics and is often
    # printed just above the coloured flag panel.  Include all tile-owned text
    # when that line is present; ``read_crop`` still rejects a box containing
    # more than one price structure.
    multi_buy_panel = flag_panel and any(
        re.search(r'cena\s+za\s*1\s+kos', fold(line)) for line in owned)
    name = None if flag_panel else (raw.get('name_bbox') or detail.get('name_bbox'))
    if name and len(name) == 4:
        boxes.append(tuple(name))
    elif not flag_panel or multi_buy_panel:
        boxes.extend(tuple(line['bbox']) for line in
                     (raw.get('region_lines') or detail.get('region_lines') or ())
                     if line.get('text') in owned and len(line.get('bbox', ())) == 4)
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    ph = max(1, price[3] - price[1])
    # A name-only hold commonly has no usable OCR name geometry at all. Lidl's
    # non-food layouts put the label immediately left of, and slightly above,
    # the large price. Give the reread enough of that label to transcribe it.
    # ``read_crop`` still rejects the box if this expansion catches a second
    # price, so the wider search cannot merge two offers.
    if not flag_panel and not reading.name:
        x0 = min(x0, price[0] - 4 * ph)
        y0 = min(y0, price[1] - 2 * ph)
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

    ocr_price_counts = {}
    for r in readings:
        if r.source == 'ocr' and r.page is not None and r.new_price is not None:
            key = (r.page, r.new_price)
            ocr_price_counts[key] = ocr_price_counts.get(key, 0) + 1

    held_ids = set()
    for rec in preliminary.records:
        reasons = [f for f in rec.deal.flags if f.startswith('quarantine:')]
        if (rec.rule == 'leaflet-only' and rec.tile is not None and len(reasons) == 1
                and reasons[0].startswith('quarantine:leaflet-only-unverified:')):
            why = _unverified_why(rec.tile)
            name_only = any(s.startswith('name has one reader') for s in why)
            multi_buy = any(s.startswith('multi-buy missing two-reader') for s in why)
            lp_disputed = why == ['Lidl Plus not agreed']
            lone_ocr = (rec.tile.sources == ('ocr',)
                        and ocr_price_counts.get((rec.tile.page, rec.tile.new_price)) == 1)
            if name_only or multi_buy or lp_disputed or lone_ocr:
                held_ids.update(id(r) for r in rec.tile.readings)

    # Only evidence that refinement actually accepted is settled. A readable
    # crop can still fail an LP/name/geometry gate; it must remain eligible for
    # a better, newly derived crop instead of being suppressed by its cache.
    proven_geometry = {
        (r.page, tuple(r.raw.get('price_bbox', ())))
        for r in readings if r.source == 'ocr' and isinstance(r.raw, dict)
        and isinstance(r.raw.get('crop_confirmation'), dict)
    }

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
                 if ((not r.name or _share_product_word(r.name, v.name))
                     and (v.lidl_plus == r.lidl_plus
                          or {v.lidl_plus, r.lidl_plus} == {False, True}))]
        if not peers:
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
    """Validate a crop read; name independence is checked in refinement.

    OCR and the crop VLM must agree on the price.  The crop name is only a
    candidate here: :func:`refine_readings` separately requires agreement
    from a full-page VLM, tile-owned OCR text, or a second crop.  Keeping those
    stages separate lets bilingual full-page names be repaired without ever
    treating broad/borrowed crop OCR text as name proof.
    """
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
    name = model['name'].strip()
    brand, product = split_leaflet_name(name)
    stems = _stems(product if brand else name)
    if not stems:
        return None
    meaningful = [w for w in words(product if brand else name)
                  if not w.isdigit() and w not in {'maks', 'max', 'kg', 'g', 'ml', 'l',
                                                   'kos', 'kosa', 'kompl', 'komplet'}]
    if not meaningful:
        return None
    texts = data.get('ocr_texts', ())
    # A price qualifier may only be introduced when BOTH readers copied it.
    badge_words = words(model.get('badge_text'))
    from_price = (badge_words[:2] == ['ze', 'od']
                  and (len(badge_words) == 2
                       or re.fullmatch(r'ze\s*od\s+\d+[.,]\d{2}',
                                       fold(model.get('badge_text') or '')) is not None)
                  and any(re.search(r'\bze\s*od\b', fold(t or '')) for t in texts)
                  and not any(re.search(r'gratis|drugi\s+kos|nakup|kupon|%', fold(t or '')) for t in texts)
                  and tile.get('badge') in (None, 'flag')
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
    crop_ocr = LeafletReading(
        'ocr', data['page'], price, name=tile.get('name'),
        text=tuple((tile.get('raw') or {}).get('text_lines') or ()), raw=tile,
    )
    crop_lp = tile.get('lidl_plus')
    model_lp = model.get('lidl_plus')
    lp_geometry_unproven = (bool((tile.get('raw') or {}).get('logo_truncated'))
                            or data.get('logo_geometry_complete') is False)
    if (isinstance(model_lp, bool) and model_lp != crop_lp
            and not lp_geometry_unproven):
        return None
    lp_proven = (crop_lp if isinstance(crop_lp, bool) and model_lp == crop_lp
                 else None)
    def page_box(box):
        if (not isinstance(box, (list, tuple)) or len(box) != 4
                or not all(isinstance(v, (int, float)) for v in box)):
            return None
        return (box[0]+x0, box[1]+y0, box[2]+x0, box[3]+y0)

    return dict(name=name, price=price, price_bbox=(a+x0, b+y0, c+x0, d+y0),
                old_pct=(old, pct) if old_proven else None, from_price=from_price,
                multi_buy=multi_buy,
                lidl_plus=lp_proven,
                lp_geometry_unproven=lp_geometry_unproven,
                logo_bbox=(page_box((tile.get('raw') or {}).get('logo_bbox'))
                           if data.get('logo_geometry_complete') is not False else None),
                badge_bbox=page_box(tile.get('badge_bbox')),
                crop_ocr_name=_ocr_text_proves_name(crop_ocr, name))


def _box_owned(box, target, anchors):
    """The ENTIRE box must lie strictly in the target's nearest-anchor cell.

    Rectangle distance is piecewise linear in x/y. Check the cell vertices
    at every anchor edge inside the box, not just its centre: a badge crossing
    a partition or tying two prices is not proven to belong to either tile.
    """
    xs = {box[0], box[2]} | {x for a in anchors for x in (a[0], a[2])
                            if box[0] < x < box[2]}
    ys = {box[1], box[3]} | {y for a in anchors for y in (a[1], a[3])
                            if box[1] < y < box[3]}

    def distance(x, y, a):
        return max(0, a[0]-x, x-a[2]) + max(0, a[1]-y, y-a[3])

    return all(distance(x, y, anchors[target]) < distance(x, y, a)
               for x in xs for y in ys for i, a in enumerate(anchors) if i != target)


def _anchor_brand_pack_owned(ocr, model, page_readings):
    """An unnamed anchor needs exact, unique brand AND printed pack evidence.

    This is the same pack-size corroboration used for brand API matches, but
    from the two leaflet readers. Never override a conflicting OCR name or
    use regional/borrowed text. Price alone and brand alone prove nothing.
    """
    if ocr.name:
        return False

    def metadata(r):
        detail = r.raw.get('raw') if isinstance(r.raw, dict) else None
        lines = detail.get('text_lines') if isinstance(detail, dict) else None
        if (not isinstance(lines, (list, tuple))
                or not all(isinstance(line, str) for line in lines)
                or tuple(lines) != r.text):
            return (), None
        brands = [words(line) for line in lines if line.isupper()
                  and all(word.isalpha() for word in line.split())]
        packs = {pack_size(line) for line in lines} - {None}
        return brands, next(iter(packs)) if len(packs) == 1 else None

    brands, pack = metadata(ocr)
    if pack is None or pack != pack_size(model.unit):
        return False
    for brand in brands:
        if not brand or words(model.name)[:len(brand)] != brand:
            continue
        # Both readers must independently locate exactly one offer with this
        # signature. Same-brand, same-size collisions remain unlocated.
        models = [r for r in page_readings if r.source == 'vlm'
                  and r.new_price == model.new_price and pack_size(r.unit) == pack
                  and words(r.name)[:len(brand)] == brand]
        anchors = [r for r in page_readings if r.source == 'ocr'
                   and r.new_price == model.new_price
                   and metadata(r)[1] == pack and brand in metadata(r)[0]]
        if len(models) == len(anchors) == 1:
            return True
    return False


def _crop_lp_owned(proof, page_readings, crop_box):
    """Bind crop-local logo geometry to ALL page prices, including off-crop.

    Price badges may extend the anchor only if their own price ownership is
    proven first. No logo geometry, clipped logo, missing page anchors or a
    tied/borrowed badge is positive LP proof.
    """
    def box(value):
        if (not isinstance(value, (list, tuple)) or len(value) != 4
                or not all(isinstance(v, (int, float)) for v in value)):
            return None
        return tuple(value) if value[0] < value[2] and value[1] < value[3] else None

    logo = box(proof['logo_bbox'])
    if logo is None or not (crop_box[0] < logo[0] and crop_box[1] < logo[1]
                            and logo[2] < crop_box[2] and logo[3] < crop_box[3]):
        return False
    # A page-VLM offer without a paired OCR price has no geometric location.
    # Its logo could be anywhere, including in this crop. Require coverage of
    # every VLM tile before the nearest-anchor partition can prove ownership.
    # A price-only leftover pairing supplies no identity evidence: its OCR
    # anchor may belong to a different product the VLM missed.
    for tile in build_tiles(page_readings):
        for v in (r for r in tile.readings if r.source == 'vlm'):
            for r in (r for r in tile.readings if r.source == 'ocr'):
                is_target = (isinstance(r.raw, dict)
                             and tuple(r.raw.get('price_bbox', ())) == proof['price_bbox'])
                # The exact target anchor has an independent crop name. Every
                # other anchor needs its own OCR identity, never crop text.
                if is_target:
                    corroborated = _share_product_word(v.name, proof['name'])
                else:
                    corroborated = (_share_product_word(r.name, v.name)
                                    or _ocr_text_proves_name(r, v.name)
                                    or _anchor_brand_pack_owned(r, v, page_readings))
                if not corroborated:
                    return False
            if not any(r.source == 'ocr' for r in tile.readings):
                return False
    prices, badges = [], []
    for r in (r for r in page_readings if r.source == 'ocr'):
        raw = r.raw if isinstance(r.raw, dict) else {}
        price = box(raw.get('price_bbox'))
        if price is None:
            return False
        detail = raw.get('raw') if isinstance(raw.get('raw'), dict) else raw
        prices.append(price)
        badges.append(box(raw.get('badge_bbox') or detail.get('badge_bbox')))
    matches = [i for i, price in enumerate(prices) if price == proof['price_bbox']]
    if len(matches) != 1:
        return False
    target = matches[0]
    badge = box(proof['badge_bbox'])
    if badge is not None and not _box_owned(badge, target, prices):
        return False
    # Use the crop's recovered target badge, and the full-page neighbours'
    # badges. Never let an unowned badge enlarge any tile's anchor.
    badges[target] = badge or badges[target]
    anchors = []
    for i, (price, badge) in enumerate(zip(prices, badges)):
        if badge is not None and _box_owned(badge, i, prices):
            anchors.append((min(price[0], badge[0]), min(price[1], badge[1]),
                            max(price[2], badge[2]), max(price[3], badge[3])))
        else:
            anchors.append(price)
    return _box_owned(logo, target, anchors)


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
            or not any(re.search(r'\bgratis\b', fold(t or '')) for t in texts)):
        return None
    seen = {D.replace(',', '.') for t in texts for D in _SINGLE.findall(t or '')}
    vm = _SINGLE.fullmatch((model.get('single_piece_text') or '').strip())
    if len(seen) != 1 or vm is None:
        return None
    single = _decimal(next(iter(seen)))
    if single is None or single != _decimal(vm[1].replace(',', '.')) or n * single != price:
        return None
    # A printed exact-quantity restriction cannot contradict the badge or
    # disappear between readers. The N+M badge itself proves the bundle size.
    oq = {int(q) for t in texts for q in _QUANTITY.findall(fold(t or ''))}
    vq = {int(q) for q in _QUANTITY.findall(fold(model.get('purchase_quantity_text') or ''))}
    if oq != vq or (oq and oq != {n + m}):
        return None
    return dict(paid=n, free=m, single_price=str(single),
                exact_quantity=next(iter(oq)) if oq else None)


def refine_readings(readings, evidence):
    """R0d: replace only names/independently proven fields, never a new price.

    The full-page OCR price geometry must equal the crop's price geometry.
    The crop name must agree with a full-page VLM, tile-owned OCR text, or an
    identical second crop. A crop VLM may supply the second price reader for
    one uniquely located OCR-only tile, but never for an ambiguous collision.
    Crop LP evidence only adds True, with two readers and target-owned logo
    geometry. A negative crop never clears a page True; conflicting negatives
    or unowned positives explicitly hold the tile. Dates/API flags stay intact.
    """
    readings = list(readings)
    out = list(readings)
    # Disagreeing crop rereads are not settled by file order. Keep the tile
    # unchanged until the conflict is reviewed. Identical names collapse;
    # volatile cache identifiers do not make otherwise equal proofs disagree.
    grouped = {}
    for data in evidence:
        proof = _proof(data)
        if proof is not None:
            grouped.setdefault((data['page'], proof['price_bbox']), []).append((data, proof))
            oi = [i for i, r in enumerate(out) if r.source == 'ocr'
                  and r.page == data['page'] and isinstance(r.raw, dict)
                  and tuple(r.raw.get('price_bbox', ())) == proof['price_bbox']]
            if len(oi) != 1:
                continue
            a = out[oi[0]]
            peers = [r for r in readings if r.source == 'vlm' and r.page == a.page
                     and r.new_price == a.new_price]
            named = [r for r in peers if _share_product_word(r.name, proof['name'])]
            peers = named if named else peers if len(peers) == 1 else []
            reason = None
            if proof['lp_geometry_unproven']:
                reason = 'crop-lidl-plus-ownership-unproven: detector logo component truncated/unverified'
            elif proof['lidl_plus'] is False and any(r.lidl_plus is True for r in [a, *peers]):
                reason = 'crop-lidl-plus-negative-conflict: crop False/False cannot clear page True'
            elif proof['lidl_plus'] is True and not _crop_lp_owned(
                    proof, [r for r in readings if r.page == a.page],
                    data['crop_box']):
                reason = 'crop-lidl-plus-ownership-unproven: logo/badge not proven target-owned'
            if reason:
                raw = dict(a.raw)
                raw['crop_lidl_plus_holds'] = sorted(set(raw.get('crop_lidl_plus_holds', ()))
                                                     | {reason})
                out[oi[0]] = replace(a, raw=raw)
    used = set()
    for group in grouped.values():
        # A smaller crop has less opportunity to borrow a neighbouring label.
        # It may therefore supersede a broader reread when every crop agrees
        # on the normalized name. Any actual name disagreement keeps the tile
        # held, independent of cache/file order.
        if len({tuple(words(p['name'])) for _, p in group}) != 1:
            continue
        data, proof = min(group, key=lambda item:
                          ((item[0]['crop_box'][2] - item[0]['crop_box'][0])
                           * (item[0]['crop_box'][3] - item[0]['crop_box'][1])))
        crop_lps = [p['lidl_plus'] for _, p in group]
        lp_consensus = (crop_lps[0] if crop_lps
                        and all(isinstance(v, bool) and v == crop_lps[0]
                                for v in crop_lps) else None)
        page, price = data['page'], proof['price']
        oi = [i for i, r in enumerate(out) if r.source == 'ocr' and r.page == page
              and r.new_price == price and isinstance(r.raw, dict)
              and tuple(r.raw.get('price_bbox', ())) == proof['price_bbox']]
        vi = [i for i, r in enumerate(out) if r.source == 'vlm' and r.page == page
              and r.new_price == price]
        named = [i for i in vi if _share_product_word(out[i].name, proof['name'])]
        if len(oi) != 1 or used.intersection(oi):
            continue
        a = out[oi[0]]
        ocr_name = (_share_product_word(a.name, proof['name'])
                    or _ocr_text_proves_name(a, proof['name']))
        crop_name = proof['crop_ocr_name']
        second_crop = len(group) > 1
        if len(named) == 1:
            vi = named
        elif len(named) == 0 and (ocr_name or crop_name or second_crop):
            if len(vi) == 1:
                vi = vi
            elif not vi:
                # The page OCR plus the crop VLM are the two independent
                # readers. This is safe only for one exact OCR geometry.
                # The LP vote must come from crop agreement too: copying
                # a.lidl_plus would manufacture a second vote from one reader.
                synthetic_raw = ({'crop_lidl_plus_holds': [
                    'crop-lidl-plus-unresolved: synthetic reader lacks crop LP consensus']}
                    if lp_consensus is None else None)
                template = LeafletReading('vlm', page, price,
                                           lidl_plus=lp_consensus,
                                           name=proof['name'], raw=synthetic_raw)
                out.append(template)
                vi = [len(out) - 1]
            else:
                # Same-price VLM variants are resolved only when their other
                # identifying metadata agree. Add the crop's common printed
                # label and leave the variants unbacked; never pick by order.
                meta = {(split_leaflet_name(out[i].name)[0], out[i].lidl_plus,
                         out[i].valid_to_text, out[i].unit) for i in vi}
                if len(meta) != 1:
                    continue
                out.append(replace(out[vi[0]], name=proof['name']))
                vi = [len(out) - 1]
        else:
            continue
        if used.intersection(vi):
            continue
        b = out[vi[0]]
        # One crop reader still needs one independent name reader. Broad OCR
        # strings are deliberately absent here because they may contain a
        # neighbouring label.
        if not (len(named) == 1 or ocr_name or crop_name or second_crop):
            continue
        canonical_name = proof['name']
        crop_brand = split_leaflet_name(canonical_name)[0]
        page_brand = split_leaflet_name(b.name)[0]
        page_brand_words = words(page_brand)
        if (not crop_brand and page_brand_words
                and words(canonical_name)[:len(page_brand_words)] != page_brand_words):
            canonical_name = f'{page_brand} {canonical_name}'
        x0, y0, x1, y1 = data['crop_box']
        # Do not accept a crop that cut off the original detected badge.
        if a.bbox is None or not (x0 <= a.bbox[0] and y0 <= a.bbox[1]
                                   and x1 >= a.bbox[2] and y1 >= a.bbox[3]):
            continue
        lp = True if lp_consensus is True and not a.raw.get('crop_lidl_plus_holds') else None
        raw = dict(a.raw)
        if proof['from_price'] and raw.get('badge') is None:
            # Both crop readers recovered a lower-bound qualifier that the
            # full-page colour segmentation missed. Mark it as a flag so R8c
            # can carry (never drop) the proven condition.
            raw['badge'] = 'flag'
        raw['crop_confirmation'] = dict(page_sha256=data['page_sha256'],
                                         crop_sha256=data['crop_sha256'],
                                         name=canonical_name, from_price=proof['from_price'],
                                         multi_buy=proof['multi_buy'], lidl_plus=lp)
        old, pct = a.old_price, a.pct
        if proof['old_pct'] is not None and a.raw.get('badge') == 'discount':
            co, cp = proof['old_pct']
            # A conflicting earlier OCR value is uncertainty, not a licence
            # to choose whichever crop reconciles.
            if (old is None or old == co) and (pct is None or pct == cp):
                old, pct = co, cp
        out[oi[0]] = replace(a, name=canonical_name, old_price=old, pct=pct,
                             lidl_plus=True if lp is True else a.lidl_plus,
                             raw=raw)
        out[vi[0]] = replace(b, name=canonical_name,
                             lidl_plus=True if lp is True else b.lidl_plus)
        used.update(oi + vi)
    return out
