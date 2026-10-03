"""Synthetic adversarial crop evidence; no answer-key fixtures."""
import copy
import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from babaemail.lidl_crops import (_crop_box, _proof, load_crops, refine_readings, save_crop,
                                  select_crop_boxes)
from babaemail.lidl_reconcile import LeafletReading, reconcile
from babaemail.models import Deal

WEEK = (date(2026, 9, 24), date(2026, 9, 30))
NAME = 'BRAND Sadni piškoti'


def test_null_quantity_is_unproven_and_does_not_crash():
    with open('scratch/lidl_reconcile/x3/fruc-unusable-null-quantity.json') as f:
        data = json.load(f)
    assert _proof(data) is not None
    assert refine_readings([], [data]) == []


def evidence(*, name=NAME, price='2.89', badge=None, old=None, pct=None):
    return dict(version=1, page=4, page_sha256=hashlib.sha256(b'page').hexdigest(),
                crop_sha256='crop', crop_box=[10, 20, 210, 220],
                ocr_tiles=[dict(price=price, price_bbox=[40, 50, 100, 100],
                                badge=badge, old_price=old, pct=pct, lidl_plus=False)],
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


@pytest.mark.parametrize('change', ['ocr-price', 'vlm-price', 'two-prices',
                                    'different-box', 'cut-badge', 'lp'])
def test_crop_cannot_repair_without_every_proof(change):
    data, rs = evidence(), readings()
    model = json.loads(data['vlm']['response'])
    if change == 'ocr-price': data['ocr_tiles'][0]['price'] = '3.89'
    if change == 'vlm-price': model['new_price'] = '3.89'
    if change == 'two-prices': data['ocr_tiles'] *= 2
    if change == 'different-box': data['crop_box'][0] += 10
    if change == 'cut-badge': rs[0] = replace(rs[0], bbox=(0, 0, 110, 120))
    if change == 'lp': model['lidl_plus'] = True
    data['vlm']['response'] = json.dumps(model)
    assert refine_readings(rs, [data]) == rs


def test_crop_name_and_tile_owned_ocr_can_replace_bilingual_page_name():
    rs = readings()
    rs[1] = replace(rs[1], name='BRAND Cuor di Mela')
    data = evidence()
    data['ocr_tiles'][0]['raw'] = {'text_lines': ['BRAND', 'Sadni piškoti']}
    refined = refine_readings(rs, [data])
    [deal] = run(refined).shipped
    assert deal.name == NAME


def test_borrowed_neighbour_line_cannot_prove_crop_name():
    rs = readings()
    rs[1] = replace(rs[1], name='BRAND Cuor di Mela')
    data = evidence()
    data['ocr_tiles'][0]['raw'] = {
        'region_lines': [{'text': NAME, 'bbox': [0, 0, 10, 10]}],
        'text_lines': [],
    }
    data['ocr_texts'] = [NAME]
    assert refine_readings(rs, [data]) == rs
    assert not run(refine_readings(rs, [data])).shipped


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


def test_crop_can_recover_from_price_when_page_badge_segmentation_missed_it():
    data = evidence()
    data['ocr_texts'] = [NAME + ' že od']
    data['ocr_tiles'][0]['raw'] = {'text_lines': [NAME, 'že od']}
    model = json.loads(data['vlm']['response'])
    model['badge_text'] = 'že od 2.89'
    data['vlm']['response'] = json.dumps(model)
    [d] = run(refine_readings(readings(), [data])).shipped
    assert (d.condition_kind, d.condition) == ('from-price', 'že od')


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


def test_crop_lp_badge_settles_page_reader_disagreement_only_with_two_crop_readers():
    rs = readings(price='2.29', old='3.29', pct='30')
    rs[1] = replace(rs[1], lidl_plus=True)
    data = evidence(price='2.29', badge='discount', old='3.29', pct='30')
    data['ocr_tiles'][0]['lidl_plus'] = True
    model = json.loads(data['vlm']['response'])
    model['lidl_plus'] = True
    data['vlm']['response'] = json.dumps(model)

    refined = refine_readings(rs, [data])
    assert {r.lidl_plus for r in refined} == {True}
    assert refined[0].raw['crop_confirmation']['lidl_plus'] is True
    [deal] = run(refined).shipped
    assert deal.loyalty_required == 'Lidl Plus'


def test_crop_lp_badge_disagreement_cannot_settle_page_readers():
    rs = readings(price='2.29', old='3.29', pct='30')
    rs[1] = replace(rs[1], lidl_plus=True)
    data = evidence(price='2.29', badge='discount', old='3.29', pct='30')
    data['ocr_tiles'][0]['lidl_plus'] = True
    model = json.loads(data['vlm']['response'])
    model['lidl_plus'] = False
    data['vlm']['response'] = json.dumps(model)

    refined = refine_readings(rs, [data])
    assert {r.lidl_plus for r in refined} == {False, True}
    assert not run(refined).shipped


def test_one_uncertain_crop_keeps_lp_disagreement_held():
    rs = readings(price='2.29', old='3.29', pct='30')
    rs[1] = replace(rs[1], lidl_plus=True)
    proven = evidence(price='2.29', badge='discount', old='3.29', pct='30')
    proven['ocr_tiles'][0]['lidl_plus'] = True
    model = json.loads(proven['vlm']['response'])
    model['lidl_plus'] = True
    proven['vlm']['response'] = json.dumps(model)
    uncertain = copy.deepcopy(proven)
    uncertain['crop_sha256'] = 'another-crop'
    uncertain['ocr_tiles'][0]['lidl_plus'] = None
    model = json.loads(uncertain['vlm']['response'])
    model['lidl_plus'] = None
    uncertain['vlm']['response'] = json.dumps(model)

    refined = refine_readings(rs, [proven, uncertain])
    assert {r.lidl_plus for r in refined} == {False, True}
    assert not run(refined).shipped


def test_multibuy_proof_keeps_crop_proven_exact_quantity():
    data = evidence(name='ARGUS Svetlo pivo', price='10.27', badge='flag')
    data['ocr_texts'] = [
        'ARGUS Svetlo pivo', '13+5 gratis', 'cena za 1 kos: 0,79',
        'velja za nakup točno 18 kosov',
    ]
    data['ocr_badge_count'] = '13+5'
    model = json.loads(data['vlm']['response'])
    model.update(badge_text='13+5 gratis', single_piece_text='cena za 1 kos: 0,79',
                 purchase_quantity_text='velja za nakup točno 18 kosov')
    data['vlm']['response'] = json.dumps(model)

    assert _proof(data)['multi_buy']['exact_quantity'] == 18


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
    assert select_crop_boxes({4: buf.getvalue()}, rs, result) == [(4, (0, 6, 284, 234))]

    # An unknown flag can still be a misclassified label panel; reread it, but
    # refinement/reconciliation must prove its exact condition before shipping.
    rs[0].raw['badge'] = 'flag'
    rs[0].raw['flag_text'] = '2+1 gratis'
    assert select_crop_boxes({4: buf.getvalue()}, rs, run(rs)) == [(4, (156, 126, 284, 234))]

    rs[0].raw.update(flag_text='že od', badge_bbox=[170, 70, 280, 145])
    assert select_crop_boxes({4: buf.getvalue()}, rs, run(rs)) == [(4, (146, 46, 304, 234))]


def test_auto_selection_includes_lp_disagreement_and_named_multibuy():
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new('RGB', (400, 300), 'white').save(buf, 'PNG')

    lp = readings(price='1.99', badge='discount', old='2.99', pct='33')
    lp[0] = replace(lp[0], name=NAME)
    lp[1] = replace(lp[1], lidl_plus=True)
    assert len(select_crop_boxes({4: buf.getvalue()}, lp, run(lp))) == 1

    mb = readings(price='6.56', badge='flag')
    mb[0] = replace(mb[0], name=NAME, text=('cena za 1 kos: 1.64',),
                    raw=dict(price_bbox=[180, 150, 260, 210], badge='flag',
                             badge_bbox=[170, 70, 280, 145], flag_text='4+2 gratis',
                             text_lines=['cena za 1 kos: 1.64'],
                             region_lines=[dict(text='cena za 1 kos: 1.64',
                                                bbox=[120, 30, 280, 60])]))
    mb[1] = replace(mb[1], new_price=D('6.56'))
    boxes = select_crop_boxes({4: buf.getvalue()}, mb, run(mb))
    assert len(boxes) == 1 and boxes[0][1][1] <= 30


def test_multibuy_crop_box_includes_tile_owned_per_piece_line():
    r = readings(price='6.56', badge='flag')[0]
    r = replace(r, bbox=(180, 150, 260, 210), text=('cena za 1 kos: 1.64',),
                raw=dict(price_bbox=[180, 150, 260, 210], badge='flag',
                         badge_bbox=[170, 70, 280, 145],
                         text_lines=['cena za 1 kos: 1.64'],
                         region_lines=[dict(text='cena za 1 kos: 1.64',
                                            bbox=[120, 30, 280, 60])]))
    assert _crop_box(r, 400, 300)[1] <= 30
