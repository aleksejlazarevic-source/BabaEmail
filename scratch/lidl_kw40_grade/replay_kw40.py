"""Offline kw40 replay; fail closed when historical inputs are unavailable.

Run from any directory:
    .venv/bin/python scratch/lidl_kw40_grade/replay_kw40.py [OUTPUT.json]

A genuine full_inputs_kw40.pkl must contain ``cands``, ``readings`` and ``kw``
(the same captured-input format as kw39 grade_full.py). ``kw`` must include
all listed candidates and the historical two-reader ``page_validity`` map.
Use --inputs PATH if that capture is recovered elsewhere. Never reconstruct
API candidates from Deals: reconciliation overwrites prices, dates and names,
and Deals discard packaging and multiple API windows. The shipped output is
used only for cache inventory, never as an extraction input or answer key.

No sockets, Ollama startup, cache writes, truth-key reads or product edits.
Missing inputs exit 2 without writing JSON. Existing OUTPUT is left alone.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import pickle
import socket
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from babaemail import lidl_api, lidl_crops, vlm  # noqa: E402
from babaemail.lidl_reconcile import reconcile  # noqa: E402
from babaemail.sources.lidl import Lidl  # noqa: E402

HERE = Path(__file__).resolve().parent
IDENT = 'lidlov-katalog-2026-kw40'
MODEL = 'qwen2.5vl:7b'
WINDOW = (date(2026, 10, 1), date(2026, 10, 7))
RECORDED = ROOT / 'out/kw40_fix' / f'lidl_{IDENT}.json'
DEFAULT_INPUTS = ROOT / 'scratch/lidl_reconcile/full_inputs_kw40.pkl'


class MissingInput(RuntimeError):
    pass


def blocked_network(*args, **kwargs):
    raise MissingInput('network/model call forbidden in offline replay')


@contextlib.contextmanager
def offline():
    with patch.object(socket.socket, 'connect', blocked_network), \
            patch.object(socket.socket, 'connect_ex', blocked_network), \
            patch.object(socket, 'create_connection', blocked_network), \
            patch.object(vlm, '_call_ollama', blocked_network):
        yield


def plain(value):
    if isinstance(value, (Decimal, date)):
        return str(value)
    raise TypeError(type(value))


def inventory():
    pdf = ROOT / 'scratch/lidl_truth_kw40/kw40.pdf'
    if not pdf.is_file():
        raise MissingInput(f'PDF render: missing {pdf.relative_to(ROOT)}')
    pages = Lidl._render_pages(pdf.read_bytes())
    if set(pages) != set(range(1, 67)):
        raise MissingInput(f'PDF render: expected 66 pages, got {len(pages)}')
    folder = ROOT / 'scratch/lidl_truth_kw40/pages'
    mismatched = [p for p, png in pages.items()
                  if not (folder / f'p{p:02d}.png').is_file()
                  or (folder / f'p{p:02d}.png').read_bytes() != png]
    print(f'PDF: 66 zoom-3 pages; cached PNG mismatches/missing: {mismatched}')
    missing = []
    missing_headers = []
    for p, png in pages.items():
        path = vlm.CACHE_DIR / f'{vlm.cache_key(png, MODEL)}.json'
        if vlm._cache_load(path) is None:
            missing.append(p)
        path = vlm.CACHE_DIR / f'{vlm.header_cache_key(png, MODEL)}.json'
        if vlm._cache_load(path, vlm.HEADER_PROMPT_VERSION) is None:
            missing_headers.append(p)
    print(f'VLM full-page cache: {66-len(missing)}/66; missing pages: {missing}')
    print(f'VLM raw header cache: {66-len(missing_headers)}/66; missing pages: {missing_headers}')
    stats = {}
    evidence = lidl_crops.load_crops(pages, stats=stats)
    print(f'Current load_crops: {len(evidence)} page-hash-bound entries; {stats}')
    print('OCR: no kw40 reading pickle found in scratch/lidl_ocr or scratch/lidl_reconcile; '
          'local OCR can be rerun from the PDF (no network needed).')
    recorded = json.loads(RECORDED.read_text())
    print(f'Recorded kw40_fix API counts: {recorded["stats"]["api"]}')
    print('Recorded header evidence: '
          f'{len(recorded["stats"]["page_validity"])} agreed windows, '
          f'{len(recorded["stats"]["page_validity_reasons"])} outcomes; '
          'derived evidence survives, but not all raw header responses.')
    # September 23 captures are not kw40 captures. Quantify insufficiency with
    # the CURRENT selector; preserve Decimal money while decoding raw JSON.
    raw_dir = ROOT / 'scratch/lidl_api'
    records = {}
    for filename in ('sitemap_gridboxes.json', 'campaign_grids.json', 'all_items.json'):
        data = json.loads((raw_dir / filename).read_text(), parse_float=Decimal)
        if filename == 'all_items.json':
            rows = [r.get('gridbox', {}).get('data', {}) for r in data]
        else:
            rows = data if isinstance(data, list) else [r for rs in data.values() for r in rs]
        for row in rows:
            if 'erpNumber' in row:
                records.setdefault(str(row['erpNumber']), (row, [], 'gridboxes'))
    old = lidl_api.select(records, *WINDOW)
    ids = {str(r['retailer_item_id']) for r in recorded['shipped'] + recorded['quarantined']
           if r.get('retailer_item_id')}
    ids.update(str(r['erp']) for r in recorded['unmatched_listed'])
    old_ids = {c.erp for c in list(old) + list(old.no_signal)}
    print(f'Sep23 raw API caches selected for kw40: {len(old)} offers, '
          f'{len(old.no_signal)} listed, {len(old.no_price)} no-price; '
          f'{len(ids-old_ids)}/{len(ids)} recorded priced API IDs absent.')
    print('out/kw40_* retains reconciled Deals, not original LidlCandidate objects; '
          f'{len(recorded["unmatched_listed"])} unmatched listed records retain only ERP/name/price.')
    print('runs/ and scratch/: captured-input pickles found only for kw39; '
          'no full_inputs_kw40.pkl or candidates_kw40.pkl.')
    return pages, missing


def replay(inputs, pages, missing_vlm):
    if not inputs.is_file():
        raise MissingInput(
            f'API candidate input to reconcile: missing {inputs}. Need original '
            '125 offer + 230 listed candidates (including packaging, API title/brand, '
            'prices/LP/conditions, flags and all validity windows). Historical run '
            'also counted 59 no-price records. Reconciled Deals are lossy and cannot '
            'restore these fields; September 23 caches are incomplete. '
            'No CURRENT baseline JSON can be measured from the available inputs.')
    # Only load a trusted local historical capture, as kw39 grade_full does.
    with inputs.open('rb') as stream:
        inp = pickle.load(stream)
    kw = dict(inp['kw'])
    if kw.get('leaflet_id') != IDENT or kw.get('leaflet_window') != WINDOW:
        raise MissingInput('capture is not the kw40 leaflet/window')
    if 'listed' not in kw or 'page_validity' not in kw:
        raise MissingInput('capture lacks listed API candidates or derived two-reader page_validity')
    if missing_vlm:
        raise MissingInput(f'full-page VLM cache missing pages {missing_vlm}')
    candidates = inp['cands']
    if len(candidates) != 125 or len(kw['listed']) != 230:
        raise MissingInput('capture lacks the recorded 125 offers / 230 listed candidates')
    readings = inp['readings']
    if {r.page for r in readings if r.source == 'ocr'} != set(pages):
        raise MissingInput('capture lacks OCR readings for one or more pages')
    evidence = lidl_crops.load_crops(pages)
    readings = lidl_crops.refine_readings(readings, evidence)
    kw['ship_website_only'] = True
    preliminary = reconcile(list(candidates), readings, **kw)
    boxes = lidl_crops.select_crop_boxes(pages, readings, preliminary, evidence, limit=40)
    if boxes:
        # read_crop requires uncached network inference. Do not simulate its
        # success/failure or substitute a differently bounded crop.
        raise MissingInput('automatic crop reread: missing cached evidence for selected boxes: '
                           + repr(boxes))
    readings = lidl_crops.refine_readings(readings, [])
    result = reconcile(list(candidates), readings, **kw)
    for deal in result.deals:
        deal.retailer = 'lidl'
    return {'leaflet_id': IDENT, 'measurement_only': True,
            'input_sha256': hashlib.sha256(inputs.read_bytes()).hexdigest(),
            'shipped': [dataclasses.asdict(d) for d in result.shipped],
            'quarantined': [dataclasses.asdict(d) for d in result.quarantined]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, default=HERE / 'replayed_kw40.json')
    parser.add_argument('--inputs', type=Path, default=DEFAULT_INPUTS)
    args = parser.parse_args()
    try:
        with offline():
            pages, missing = inventory()
            doc = replay(args.inputs, pages, missing)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(doc, default=plain, ensure_ascii=False, indent=1) + '\n')
        print(f'wrote {args.output}')
        return 0
    except MissingInput as exc:
        print(f'BLOCKED: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
