"""Synthetic adversarial crop evidence; no answer-key fixtures."""
import copy
import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from babaemail.lidl_crops import (_proof, load_crops, refine_readings, save_crop,
                                  select_crop_boxes)
from babaemail.lidl_reconcile import LeafletReading, reconcile
from babaemail.models import Deal

WEEK = (date(2026, 9, 24), date(2026, 9, 30))
NAME = 'BRAND Sadni piškoti'


def evidence(*, name=NAME, price='2.89', badge=None, old=None, pct=None):
    return dict(version=1, page=4, page_sha256=hashlib.sha256(b'page').hexdigest(),
                crop_sha256='crop', crop_box=[10, 20, 210, 220],
                ocr_tiles=[dict(price=price, price_bbox=[40, 50, 100, 100],
                                badge=badge, old_price=old, pct=pct)],
                ocr_texts=[name + (' že od' if badge == 'flag' else '')],
                vlm=dict(response=json.dumps(dict(name=name, new_price=price,
                    old_price=old, discount_pct=pct, lidl_plus=False,
                    badge_text='že od' if badge == 'flag' else f'-{pct}%' if pct else None))))


def readings(*, price='2.89', badge=None, old=None, pct=None):
    a = LeafletReading('ocr', 4, D(price), D(old) if old else None, D(pct) if pct else None,
                       lidl_plus=False, name=None, bbox=(50, 70, 110, 120),
                       raw=dict(price_bbox=[50, 70, 110, 120], badge=badge,
                                old_price=old, pct=pct))
    b = LeafletReading('vlm', 4, D(price), lidl_plus=False, name=NAME)
    return [a, b]


def run(rs, **kw):
    return reconcile([], rs, leaflet_id='test', leaflet_window=WEEK,
                     page_validity={4: WEEK}, **kw)


def test_crop_name_repair_ships_plain_price_and_discount_stays_unknown():
    original = readings()
    assert not run(original).shipped
    refined = refine_readings(original, [evidence()])
    [d] = run(refined).shipped
    assert d.name == NAME and d.price_new == D('2.89')
    assert d.price_old is d.discount_published is d.discount_pct is d.discount_display is None
    assert 'leaflet-name-from:crop-ocr+vlm' in d.flags
    assert original[0].name is None      # cached full-page evidence is untouched


def test_brand_only_crop_name_cannot_make_tile_shippable():
    rs = readings()
    rs[1] = replace(rs[1], name='VITASIA Riževi rezanci')
    data = evidence(name='VITASIA')
    assert refine_readings(rs, [data]) == rs
    assert not run(refine_readings(rs, [data])).shipped


def test_lone_variant_crop_name_cannot_make_tile_shippable():
    rs = readings()
    rs[1] = replace(rs[1], name='SILAN Mehčalec za perilo')
    data = evidence(name='Sensitive')
    assert refine_readings(rs, [data]) == rs
    assert not run(refine_readings(rs, [data])).shipped


def test_crop_name_with_shared_non_brand_product_words_still_ships():
    refined = refine_readings(readings(), [evidence()])
    [deal] = run(refined).shipped
    assert deal.name == NAME


@pytest.mark.parametrize('change', ['ocr-price', 'vlm-price', 'missing-name', 'partial-name',
                                    'two-prices', 'different-box', 'cut-badge', 'lp', 'brand'])
def test_crop_cannot_repair_without_every_proof(change):
    data, rs = evidence(), readings()
    model = json.loads(data['vlm']['response'])
    if change == 'ocr-price': data['ocr_tiles'][0]['price'] = '3.89'
    if change == 'vlm-price': model['new_price'] = '3.89'
    if change == 'missing-name': data['ocr_texts'] = ['BRAND']
    if change == 'partial-name': data['ocr_texts'] = ['BRAND piškoti']
    if change == 'two-prices': data['ocr_tiles'] *= 2
    if change == 'different-box': data['crop_box'][0] += 10
    if change == 'cut-badge': rs[0] = replace(rs[0], bbox=(0, 0, 110, 120))
    if change == 'lp': model['lidl_plus'] = True
    if change == 'brand':
        model['name'] = 'Sadni piškoti'
        data['ocr_texts'] = ['Sadni piškoti']
    data['vlm']['response'] = json.dumps(model)
    assert refine_readings(rs, [data]) == rs


def test_crop_never_supplies_a_missing_full_page_reader():
    for rs in ([readings()[0]], [readings()[1]]):
        assert refine_readings(rs, [evidence()]) == rs


def test_unique_price_cannot_replace_name_without_shared_product_word():
    rs = readings()
    rs[1] = replace(rs[1], name='BRAND Cuor di Mela')
    assert refine_readings(rs, [evidence()]) == rs
    assert not run(refine_readings(rs, [evidence()])).shipped


def test_price_collision_needs_unique_product_name():
    rs = readings()
    rs[1] = replace(rs[1], name='BRAND Neznan izdelek')
    rs += [replace(rs[1], name='OTHER Drugi izdelek')]
    assert refine_readings(rs, [evidence()]) == rs


def test_crop_cannot_add_common_label_without_shared_product_word():
    rs = readings()
    rs[1] = replace(rs[1], name='BRAND Cuor di Mela', unit='300 g')
    rs += [replace(rs[1], name='BRAND Cuor di Bosco')]
    assert refine_readings(rs, [evidence()]) == rs


@pytest.mark.parametrize('field,value', [('unit', '500 g'), ('lidl_plus', True),
                                        ('valid_to_text', 'velja do 26.9.')])
def test_variant_metadata_conflicts_stay_held(field, value):
    rs = readings()
    rs[1] = replace(rs[1], name='BRAND Cuor di Mela')
    rs += [replace(rs[1], name='BRAND Cuor di Bosco', **{field: value})]
    assert refine_readings(rs, [evidence()]) == rs


def test_price_collision_can_repair_exactly_one_matching_product():
    rs = readings()
    rs += [replace(rs[1], name='BRAND Drugi izdelek')]
    refined = refine_readings(rs, [evidence()])
    assert refined[0].name == NAME and refined[2] == rs[2]


def test_conflicting_crops_are_not_settled_by_order():
    a, b = evidence(), evidence(name='BRAND Čokoladni piškoti')
    for data in ([a, b], [b, a]):
        assert refine_readings(readings(), data) == readings()


def test_old_price_needs_two_readers_and_truncation_arithmetic():
    data = evidence(price='1.99', badge='discount', old='3.49', pct='42')
    rs = readings(price='1.99', badge='discount')
    refined = refine_readings(rs, [data])
    [d] = run(refined).shipped
    assert (d.price_old, d.discount_published) == (D('3.49'), D('42'))
    for key, value in [('old_price', '3.59'), ('discount_pct', '43'), ('badge_text', '-42% na drugi kos')]:
        bad = copy.deepcopy(data)
        model = json.loads(bad['vlm']['response'])
        model[key] = value
        bad['vlm']['response'] = json.dumps(model)
        assert _proof(bad)['old_pct'] is None
    data['ocr_tiles'][0]['pct'] = '43'
    model = json.loads(data['vlm']['response']); model['discount_pct'] = '43'; model['badge_text'] = '-43%'
    data['vlm']['response'] = json.dumps(model)
    assert _proof(data)['old_pct'] is None


def test_vlm_old_price_alone_never_enters_a_deal():
    data = evidence()
    model = json.loads(data['vlm']['response'])
    model.update(old_price='9.63', discount_pct=69, badge_text='-69%')
    data['vlm']['response'] = json.dumps(model)
    [d] = run(refine_readings(readings(), [data])).shipped
    assert d.price_old is d.discount_pct is None


def test_crop_old_price_cannot_overrule_conflicting_original_ocr():
    rs = readings(price='1.99', badge='discount', old='3.59', pct='42')
    data = evidence(price='1.99', badge='discount', old='3.49', pct='42')
    refined = refine_readings(rs, [data])
    assert refined[0].old_price == D('3.59')
    assert not run(refined).shipped


def test_from_price_ships_only_with_qualifier_and_unknown_discount():
    [d] = run(refine_readings(readings(badge='flag'), [evidence(badge='flag')])).shipped
    assert d.condition_kind == 'from-price' and d.condition == 'že od'
    assert d.price_new == D('2.89') and d.discount_pct is None
    d.price_old, d.discount_published = D('4.99'), D('42')
    assert d.discount_pct is d.discount_display is None  # lower bounds cannot imply savings


@pytest.mark.parametrize('ocr_text,vlm_badge', [(NAME, 'že od'), (NAME+' že od', None),
    (NAME+' že od 2+1 gratis', 'že od'), (NAME+' že od', '2+1 gratis')])
def test_unknown_or_conditional_badge_never_becomes_plain_price(ocr_text, vlm_badge):
    data = evidence(badge='flag')
    data['ocr_texts'] = [ocr_text]
    model = json.loads(data['vlm']['response']); model['badge_text'] = vlm_badge
    data['vlm']['response'] = json.dumps(model)
    assert not run(refine_readings(readings(badge='flag'), [data])).shipped


def test_from_price_still_needs_lidl_plus_agreement_and_validity():
    rs = refine_readings(readings(badge='flag'), [evidence(badge='flag')])
    assert not run([rs[0], replace(rs[1], lidl_plus=True)]).shipped
    assert not reconcile([], rs, leaflet_id='test', leaflet_window=WEEK).shipped


def test_cache_is_bound_to_page_pixels_and_page_number(tmp_path):
    data = evidence()
    save_crop(data, tmp_path)
    assert load_crops({4: b'page'}, tmp_path) == [data]
    assert not load_crops({4: b'new-page'}, tmp_path)
    assert not load_crops({5: b'page'}, tmp_path)


def test_bad_crop_file_is_skipped_and_counted(tmp_path):
    digest = hashlib.sha256(b'page').hexdigest()
    (tmp_path / f'{digest}-bad.json').write_text('{not json')
    stats = {}
    assert load_crops({4: b'page'}, tmp_path, stats) == []
    assert stats == {'crop_files_bad': 1, 'crop_files_stale': 0}


def test_auto_selection_uses_geometry_only_for_crop_settleable_hold(tmp_path):
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new('RGB', (400, 300), 'white').save(buf, 'PNG')
    rs = readings()
    rs[0] = replace(rs[0], name=None, bbox=(180, 150, 260, 210),
                    raw=dict(price_bbox=[180, 150, 260, 210], badge=None,
                             name_bbox=[20, 90, 160, 140], region_lines=[], text_lines=[]))
    result = run(rs)
    assert select_crop_boxes({4: buf.getvalue()}, rs, result) == [(4, (0, 66, 284, 234))]

    # An unknown flag condition is not something X1's plain/from-price proof settles.
    rs[0].raw['badge'] = 'flag'
    rs[0].raw['flag_text'] = '2+1 gratis'
    assert select_crop_boxes({4: buf.getvalue()}, rs, run(rs)) == []

    rs[0].raw.update(flag_text='že od', badge_bbox=[170, 70, 280, 145])
    assert select_crop_boxes({4: buf.getvalue()}, rs, run(rs)) == [(4, (146, 46, 304, 234))]
