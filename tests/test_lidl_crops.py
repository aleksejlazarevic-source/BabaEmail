"""Synthetic adversarial crop evidence; no answer-key fixtures."""
import copy
import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from babaemail.lidl_api import LidlCandidate
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


def lp_evidence(**kw):
    data = evidence(**kw)
    data['ocr_tiles'][0].update(lidl_plus=True, badge_bbox=[40, 30, 100, 50],
                                raw={'logo_bbox': [100, 30, 120, 50]})
    model = json.loads(data['vlm']['response'])
    model['lidl_plus'] = True
    data['vlm']['response'] = json.dumps(model)
    return data


def run(rs, **kw):
    return reconcile([], rs, leaflet_id='test', leaflet_window=WEEK,
                     page_validity={4: WEEK}, **kw)


def candidate(*, lp):
    return LidlCandidate(
        erp='1', name=NAME, brand=None, title=NAME, url='https://www.lidl.si/p/p1',
        category='Food', new_price=D('2.89'), old_price=None, pct=None,
        discount_text=None, lidl_plus=lp, lidl_plus_text=None, highlight_text=None,
        regular_price=None, conditional=False, conditional_reason=None,
        packaging=None, price_unit=None, base_price_text=None,
        windows=[WEEK], all_windows=[WEEK], sources=('sitemap',), data_from='gridboxes')


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
    data = lp_evidence(price='2.29', badge='discount', old='3.29', pct='30')

    refined = refine_readings(rs, [data])
    assert {r.lidl_plus for r in refined} == {True}
    assert refined[0].raw['crop_confirmation']['lidl_plus'] is True
    [deal] = run(refined).shipped
    assert deal.loyalty_required == 'Lidl Plus'


@pytest.mark.parametrize('votes', [(False, True), (True, False), (True, True)])
def test_false_false_crop_never_clears_positive_page_lp_and_explicitly_holds(votes):
    rs = [replace(r, lidl_plus=v) for r, v in zip(readings(), votes)]
    refined = refine_readings(rs, [evidence()])
    assert [r.lidl_plus for r in refined] == list(votes)
    result = run(refined)
    assert not result.shipped
    [deal] = result.quarantined
    assert any('crop-lidl-plus-negative-conflict' in f for f in deal.flags)


@pytest.mark.parametrize('votes', [(True, False), (False, True)])
@pytest.mark.parametrize('api_lp', [False, True])
def test_negative_crop_lp_hold_survives_api_vote_including_kw40_p44_shape(votes, api_lp):
    # p44 failure shape: OCR True, page VLM False, API False. Also check the
    # reverse page vote and API True, which would otherwise settle 2-of-3.
    rs = [replace(r, name=NAME, lidl_plus=v) for r, v in zip(readings(), votes)]
    refined = refine_readings(rs, [evidence()])
    result = reconcile([candidate(lp=api_lp)], refined, leaflet_id='test',
                       leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    [record] = result.records
    assert record.rule == 'verified' and record.candidate is not None
    assert any('crop-lidl-plus-negative-conflict' in f for f in record.deal.flags)


@pytest.mark.parametrize('logo', [None, [195, 30, 215, 50], [120, 30, 140, 50]])
def test_crop_positive_lp_without_complete_target_logo_geometry_holds(logo):
    data = lp_evidence()
    data['ocr_tiles'][0]['raw']['logo_bbox'] = logo
    rs = readings()
    # A real neighbouring price anchor, including the unreadable-price case.
    rs.append(LeafletReading('ocr', 4, None, lidl_plus=False,
                            raw={'price_bbox': [170, 70, 230, 120]}))
    result = run(refine_readings(rs, [data]))
    assert not result.shipped
    target = next(d for d in result.quarantined if d.price_new == D('2.89'))
    assert any('crop-lidl-plus-ownership-unproven' in f for f in target.flags)


def test_adjacent_tile_logo_inside_one_price_crop_cannot_label_target():
    data = lp_evidence()
    data['ocr_tiles'][0]['raw']['logo_bbox'] = [160, 30, 190, 60]
    # Crop x ends at 210; the neighbour's price starts at 220. Its logo
    # (page [170, 50, 200, 80]) is fully visible inside the target crop.
    neighbour = LeafletReading('ocr', 4, D('4.99'), lidl_plus=True,
                                raw={'price_bbox': [220, 70, 280, 120],
                                     'badge_bbox': [170, 50, 280, 70]})
    data['ocr_tiles'][0]['badge_bbox'] = None
    rs = [*readings(), neighbour]
    assert len(data['ocr_tiles']) == 1
    assert neighbour.raw['price_bbox'][0] > data['crop_box'][2]
    refined = refine_readings(rs, [data])
    assert [r.lidl_plus for r in refined[:2]] == [False, False]
    result = run(refined)
    assert not result.shipped
    target = next(d for d in result.quarantined if d.price_new == D('2.89'))
    assert any('crop-lidl-plus-ownership-unproven' in f for f in target.flags)


def test_target_owned_logo_still_adds_lp_with_neighbour_outside_crop():
    neighbour = LeafletReading('ocr', 4, D('4.99'), lidl_plus=False,
                                raw={'price_bbox': [220, 70, 280, 120]})
    result = run(refine_readings([*readings(), neighbour], [lp_evidence()]))
    [deal] = result.shipped
    assert deal.loyalty_required == 'Lidl Plus'


def test_unproven_positive_crop_lp_holds_even_when_page_and_api_already_agree():
    data = lp_evidence()
    data['ocr_tiles'][0]['raw'] = {}
    rs = [replace(r, name=NAME, lidl_plus=True) for r in readings()]
    result = reconcile([candidate(lp=True)], refine_readings(rs, [data]),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    [deal] = result.quarantined
    assert any('crop-lidl-plus-ownership-unproven' in f for f in deal.flags)


@pytest.mark.parametrize('case', ['valid', 'negative-lp', 'borrowed-lp',
                                 'missing-piece', 'wrong-total'])
def test_multibuy_crop_requires_owned_lp_and_keeps_per_piece_gates(case):
    data = lp_evidence(price='6.56', badge='flag')
    data['ocr_texts'] = [NAME, '4+2 gratis', 'cena za 1 kos: 1,64',
                         'velja za nakup točno 6 kosov']
    data['ocr_badge_count'] = '4+2'
    model = json.loads(data['vlm']['response'])
    model.update(badge_text='4+2 gratis', single_piece_text='cena za 1 kos: 1,64',
                 purchase_quantity_text='velja za nakup točno 6 kosov')
    rs = readings(price='6.56', badge='flag')
    rs[0] = replace(rs[0], name=NAME, text=('cena za 1 kos: 1,64',))
    rs[1] = replace(rs[1], lidl_plus=True)
    if case == 'negative-lp':
        data['ocr_tiles'][0]['lidl_plus'] = model['lidl_plus'] = False
    elif case == 'borrowed-lp':
        data['ocr_tiles'][0]['raw']['logo_bbox'] = [160, 30, 190, 60]
        rs.append(LeafletReading('ocr', 4, D('4.99'), lidl_plus=True,
                                 raw={'price_bbox': [220, 70, 280, 120],
                                      'badge_bbox': [170, 50, 280, 70]}))
    elif case == 'missing-piece':
        model['single_piece_text'] = None
    elif case == 'wrong-total':
        model['single_piece_text'] = 'cena za 1 kos: 1,65'
        data['ocr_texts'][2] = model['single_piece_text']
    data['vlm']['response'] = json.dumps(model)
    result = run(refine_readings(rs, [data]))
    if case == 'valid':
        [deal] = result.shipped
        assert deal.loyalty_required == 'Lidl Plus'
        assert deal.condition_kind == 'multi-buy'
        assert deal.condition == '4+2 gratis; velja za nakup točno 6 kosov; cena za 1 kos 1,64 €'
    else:
        assert not result.shipped
        target = next(d for d in result.quarantined if d.price_new == D('6.56'))
        reason = ('crop-lidl-plus-negative-conflict' if case == 'negative-lp'
                  else 'crop-lidl-plus-ownership-unproven' if case == 'borrowed-lp'
                  else 'multi-buy missing two-reader')
        assert any(reason in f for f in target.flags)


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
    proven = lp_evidence(price='2.29', badge='discount', old='3.29', pct='30')
    uncertain = copy.deepcopy(proven)
    uncertain['crop_sha256'] = 'another-crop'
    uncertain['ocr_tiles'][0]['lidl_plus'] = None
    model = json.loads(uncertain['vlm']['response'])
    model['lidl_plus'] = None
    uncertain['vlm']['response'] = json.dumps(model)

    refined = refine_readings(rs, [proven, uncertain])
    assert {r.lidl_plus for r in refined} == {False, True}
    assert not run(refined).shipped


def test_synthetic_vlm_lp_uncertainty_never_copies_page_ocr_vote():
    data = lp_evidence()
    data['ocr_tiles'][0]['raw']['text_lines'] = [NAME]
    model = json.loads(data['vlm']['response'])
    model['lidl_plus'] = None
    data['vlm']['response'] = json.dumps(model)
    rs = [replace(readings()[0], name=NAME)]  # page VLM missed this offer
    refined = refine_readings(rs, [data])
    assert [r.lidl_plus for r in refined] == [False, None]
    assert not run(refined).shipped
    result = reconcile([candidate(lp=False)], refined, leaflet_id='test',
                       leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    [record] = result.records
    assert record.rule == 'verified' and record.candidate is not None
    assert any('crop-lidl-plus-unresolved' in f for f in record.deal.flags)


def real_logo_evidence():
    """A complete LP component found by the production pixel detector."""
    import cv2
    import numpy as np
    from types import SimpleNamespace
    from babaemail.sources.lidl import Tile, _find_lp_logo

    blue = np.zeros((200, 200), dtype=np.uint8)
    yellow = blue.copy()
    cv2.rectangle(blue, (110, 10), (154, 54), 1, -1)
    cv2.circle(yellow, (132, 32), 13, 1, -1)
    blue[yellow == 1] = 0
    ps = SimpleNamespace(bbox=(40, 50, 100, 100), height=50)
    tile = Tile(4, 'Y', ps.bbox, ps.bbox)
    _find_lp_logo(tile, {'Y': yellow}, blue, ps)
    assert tile.lp_logo and tile.lidl_plus is True
    assert not tile.raw.get('logo_truncated')
    data = lp_evidence()
    data['ocr_tiles'][0]['raw'] = tile.raw
    data['ocr_tiles'][0]['badge_bbox'] = None
    return data


@pytest.mark.parametrize('page_vlm,proof_failure,crop_vote', [
    (page_vlm, failure, vote) for page_vlm in (True, False)
    for failure in (None, 'name', 'price') for vote in (None, False)
    if (page_vlm, failure, vote) != (False, None, None)  # covered by existing regression
])
def test_crop_logo_uncertainty_holds_api_match_before_proof_discard(
        crop_vote, proof_failure, page_vlm):
    data = real_logo_evidence()
    model = json.loads(data['vlm']['response'])
    model['lidl_plus'] = crop_vote
    if proof_failure == 'name':
        model['name'] = None
    elif proof_failure == 'price':
        model['new_price'] = '3.89'
    data['vlm']['response'] = json.dumps(model)
    rs = [replace(r, name=NAME) for r in readings()]
    if not page_vlm:
        rs = rs[:1]
    result = reconcile([candidate(lp=False)], refine_readings(rs, [data]),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    [record] = result.records
    assert record.candidate is not None
    reason = 'crop-lidl-plus-unresolved' if crop_vote is None else 'crop-lidl-plus-conflict'
    assert any(reason in f for f in record.deal.flags)


@pytest.mark.parametrize('brand_case,name_path,conflict', [
    (case, path, conflict) for case in ('upper', 'title') for path in ('shared', 'text')
    for conflict in ('brand', 'pack', 'both')
    # The old exact-text fallback already rejects a missing title-cased brand.
    if not (case == 'title' and path == 'text' and conflict != 'pack')
])
def test_three_product_shared_coffee_cannot_hide_missing_neighbour_anchor(conflict, name_path, brand_case):
    from babaemail.lidl_crops import _crop_lp_owned

    data = real_logo_evidence()
    # Nearby BELLAROM's anchor is missed by OCR; distant BARCAFFE's offer
    # is missed by VLM. Shared "Mleta kava" must not locate either offer.
    coffee = LeafletReading('vlm', 4, D('4.99'), name='BELLAROM Mleta kava',
                            lidl_plus=True, unit='500 g')
    if brand_case == 'title':
        coffee = replace(coffee, name='Bellarom Mleta kava')
    brand = 'BARCAFFE' if conflict in ('brand', 'both') else 'BELLAROM'
    pack = '250 g' if conflict in ('pack', 'both') else '500 g'
    text = (brand, 'Mleta kava', pack)
    distant = LeafletReading('ocr', 4, D('4.99'),
                             name=f'{brand} Mleta kava' if name_path == 'shared' else 'Classic',
                             lidl_plus=False, unit=pack if name_path == 'shared' else None, text=text,
                             raw={'price_bbox': [700, 70, 760, 120],
                                  'raw': {'text_lines': list(text)}})
    rs = [*readings(), coffee, distant]
    actual = replace(distant, name=coffee.name, unit=coffee.unit, text=(),
                     raw={'price_bbox': [170, 70, 230, 120]})
    assert not _crop_lp_owned(_proof(data), [*rs, actual], data['crop_box'])
    result = reconcile([candidate(lp=False)], refine_readings(rs, [data]),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    target = next(r for r in result.records if r.candidate is not None)
    assert target.deal.is_quarantined
    assert any('crop-lidl-plus-ownership-unproven' in f for f in target.deal.flags)


@pytest.mark.parametrize('reverse', [False, True])
def test_shared_word_anchor_pairings_must_be_unique_in_both_readers(reverse):
    from babaemail.lidl_crops import _crop_lp_owned

    data = real_logo_evidence()
    models = [LeafletReading('vlm', 4, D('4.99'), name=f'COFFEE Mleta kava {variant}',
                             lidl_plus=False) for variant in ('Classic', 'Premium')]
    anchors = [LeafletReading('ocr', 4, D('4.99'), name=f'COFFEE Mleta kava {variant}',
                              lidl_plus=False, raw={'price_bbox': [x, 70, x+60, 120]})
               for variant, x in [('Classic', 700), ('Premium', 800)]]
    if reverse:
        anchors.reverse()
        models.reverse()
    rs = [*readings(), *anchors, *models]
    assert not _crop_lp_owned(_proof(data), rs, data['crop_box'])
    target = next(d for d in run(refine_readings(rs, [data])).deals
                  if d.price_new == D('2.89'))
    assert target.is_quarantined
    assert any('crop-lidl-plus-ownership-unproven' in f for f in target.flags)


@pytest.mark.parametrize('reverse', [False, True])
def test_crop_logo_hold_survives_rejected_name_and_other_crop_order(reverse):
    uncertain = real_logo_evidence()
    model = json.loads(uncertain['vlm']['response'])
    model.update(name=None, lidl_plus=None)
    uncertain['vlm']['response'] = json.dumps(model)
    crops = [uncertain, real_logo_evidence()]
    if reverse:
        crops.reverse()
    rs = [replace(r, name=NAME) for r in readings()]
    result = reconcile([candidate(lp=False)], refine_readings(rs, crops),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    [deal] = result.quarantined
    assert any('crop-lidl-plus-unresolved' in f for f in deal.flags)


def test_crop_logo_hold_is_retained_on_every_duplicate_target_anchor():
    data = real_logo_evidence()
    model = json.loads(data['vlm']['response'])
    model['lidl_plus'] = False
    data['vlm']['response'] = json.dumps(model)
    rs = [replace(r, name=NAME) for r in readings()]
    rs.append(rs[0])
    refined = refine_readings(rs, [data])
    for r in refined:
        if r.source == 'ocr':
            assert any('crop-lidl-plus-conflict' in f for f in r.raw['crop_lidl_plus_holds'])
    assert not reconcile([candidate(lp=False)], refined, leaflet_id='test',
                         leaflet_window=WEEK, page_validity={4: WEEK}).shipped


def test_target_anchor_cannot_ignore_conflicting_selected_brand():
    rs = readings()
    rs[0] = replace(rs[0], name='OTHER Sadni piškoti')
    result = reconcile([candidate(lp=False)], refine_readings(rs, [real_logo_evidence()]),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    assert any('crop-lidl-plus-ownership-unproven' in f
               for d in result.quarantined for f in d.flags)


@pytest.mark.parametrize('reverse', [False, True])
def test_opposite_complete_crop_lp_votes_hold_even_with_conflicting_names(reverse):
    crops = [real_logo_evidence(), evidence(name='BRAND Sladki piškoti')]
    if reverse:
        crops.reverse()
    rs = [replace(r, name=NAME) for r in readings()]
    result = reconcile([candidate(lp=False)], refine_readings(rs, crops),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    assert any('crop-lidl-plus-conflict' in f
               for d in result.quarantined for f in d.flags)


@pytest.mark.parametrize('failure', ['json', 'condition'])
def test_unusable_crop_response_cannot_drop_located_logo_warning(failure):
    data = real_logo_evidence()
    if failure == 'json':
        data['vlm']['response'] = '{'
    else:
        model = json.loads(data['vlm']['response'])
        model.update(lidl_plus=None, badge_text=42)
        data['vlm']['response'] = json.dumps(model)
    rs = [replace(r, name=NAME) for r in readings()]
    result = reconcile([candidate(lp=False)], refine_readings(rs, [data]),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    assert any('crop-lidl-plus-unresolved' in f
               for d in result.quarantined for f in d.flags)


def test_negative_crop_votes_cannot_discard_saved_detector_logo():
    data = real_logo_evidence()
    data['ocr_tiles'][0]['lidl_plus'] = False
    model = json.loads(data['vlm']['response'])
    model['lidl_plus'] = False
    data['vlm']['response'] = json.dumps(model)
    rs = [replace(r, name=NAME) for r in readings()]
    result = reconcile([candidate(lp=False)], refine_readings(rs, [data]),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    assert any('crop-lidl-plus-conflict' in f
               for d in result.quarantined for f in d.flags)


def test_conflicting_pack_in_selected_anchor_name_vetoes_shared_words():
    data = real_logo_evidence()
    model = LeafletReading('vlm', 4, D('4.99'), name='COFFEE Mleta kava',
                           unit='500 g', lidl_plus=True)
    anchor = LeafletReading('ocr', 4, D('4.99'), name='COFFEE Mleta kava 250 g',
                            lidl_plus=False, raw={'price_bbox': [700, 70, 760, 120]})
    rs = [*readings(), model, anchor]
    target = next(d for d in run(refine_readings(rs, [data])).deals
                  if d.price_new == D('2.89'))
    assert target.is_quarantined
    assert any('crop-lidl-plus-ownership-unproven' in f for f in target.flags)


def test_real_logo_detector_leftover_neighbour_price_cannot_prove_coverage():
    import cv2
    import numpy as np
    from types import SimpleNamespace
    from babaemail.lidl_crops import _crop_lp_owned
    from babaemail.lidl_reconcile import build_tiles
    from babaemail.sources.lidl import Tile, _find_lp_logo

    blue = np.zeros((200, 200), dtype=np.uint8)
    yellow = blue.copy()
    cv2.rectangle(blue, (110, 10), (154, 54), 1, -1)
    cv2.circle(yellow, (132, 32), 13, 1, -1)
    blue[yellow == 1] = 0
    ps = SimpleNamespace(bbox=(40, 50, 100, 100), height=50)
    tile = Tile(4, 'Y', ps.bbox, ps.bbox)
    _find_lp_logo(tile, {'Y': yellow}, blue, ps)
    assert tile.lp_logo and not tile.raw.get('logo_truncated')
    data = lp_evidence()
    data['ocr_tiles'][0]['raw'] = tile.raw
    data['ocr_tiles'][0]['badge_bbox'] = None

    # OCR missed the coffee's 4.99 anchor beside the target; the page VLM
    # missed distant detergent at the same price. Price fallback pairs them.
    coffee = LeafletReading('vlm', 4, D('4.99'), name='OTHER Sosednja kava',
                            lidl_plus=True)
    detergent = LeafletReading('ocr', 4, D('4.99'), name='CLEAN Pralni detergent',
                               lidl_plus=False,
                               raw={'price_bbox': [700, 70, 760, 120]})
    rs = [*readings(), coffee, detergent]
    paired = next(t for t in build_tiles(rs) if t.new_price == D('4.99'))
    assert paired.sources == ('ocr', 'vlm') and not paired.ocr_name_paired

    # With the coffee's actual anchor present, geometry rejects its logo.
    coffee_ocr = replace(detergent, name=coffee.name, lidl_plus=True,
                         raw={'price_bbox': [170, 70, 230, 120]})
    assert not _crop_lp_owned(_proof(data), [*rs, coffee_ocr], data['crop_box'])
    # Missing geometry cannot turn that neighbour's logo into target proof.
    result = run(refine_readings(rs, [data]))
    target = next(d for d in result.deals if d.price_new == D('2.89'))
    assert target.is_quarantined
    assert any('crop-lidl-plus-ownership-unproven' in f for f in target.flags)


def test_unnamed_neighbour_requires_guarded_unique_brand_and_pack():
    from babaemail.lidl_crops import _crop_lp_owned

    texts = ('OTHER', '500 g / 1 kg = 9.98')
    anchor = LeafletReading('ocr', 4, D('4.99'), lidl_plus=False, text=texts,
                            raw={'price_bbox': [700, 70, 760, 120],
                                 'raw': {'text_lines': list(texts)}})
    model = LeafletReading('vlm', 4, D('4.99'), name='OTHER Sosednja kava',
                           lidl_plus=False, unit='500 g')
    data = lp_evidence()
    proof = _proof(data)
    assert _crop_lp_owned(proof, [*readings(), anchor, model], data['crop_box'])

    for bad_anchor, bad_model in [
        (anchor, replace(model, unit=None)),
        (anchor, replace(model, unit='600 g')),
        (anchor, replace(model, name='CLEAN Pralni detergent')),
        (replace(anchor, name='OTHER Pralni detergent'), model),
        (replace(anchor, text=()), model),  # no matching tile-owned audit text
        (replace(anchor, raw={**anchor.raw, 'raw': {'region_lines': list(texts)}}), model),
    ]:
        assert not _crop_lp_owned(proof, [*readings(), bad_anchor, bad_model],
                                  data['crop_box'])
    # Another product with the same brand and pack cannot share the anchor.
    duplicate = replace(model, name='OTHER Instant kava')
    assert not _crop_lp_owned(proof, [*readings(), anchor, model, duplicate],
                              data['crop_box'])
    second_anchor = replace(anchor, raw={**anchor.raw, 'price_bbox': [800, 70, 860, 120]})
    assert not _crop_lp_owned(proof, [*readings(), anchor, second_anchor, model],
                              data['crop_box'])


@pytest.mark.parametrize('price', ['4.99', '2.89'])
def test_unanchored_page_vlm_neighbour_blocks_positive_crop_lp(price):
    rs = [*readings(), LeafletReading('vlm', 4, D(price),
                                    name='OTHER Sosednja kava', lidl_plus=True)]
    refined = refine_readings(rs, [lp_evidence()])
    assert [r.lidl_plus for r in refined[:2]] == [False, False]
    result = run(refined)
    assert not result.shipped
    target = next(d for d in result.quarantined if d.name == NAME)
    assert any('crop-lidl-plus-ownership-unproven' in f for f in target.flags)


def test_same_price_vlm_neighbour_cannot_borrow_missing_targets_geometry():
    rs = [replace(readings()[0], name=NAME),
          LeafletReading('vlm', 4, D('2.89'), name='OTHER Sosednja kava', lidl_plus=True)]
    result = run(refine_readings(rs, [lp_evidence()]))
    assert not result.shipped
    assert any('crop-lidl-plus-ownership-unproven' in f
               for d in result.quarantined for f in d.flags)


@pytest.mark.parametrize('edge', ['search', 'crop'])
def test_real_logo_detector_cannot_prove_truncated_logo_owned(edge):
    import cv2
    import numpy as np
    from types import SimpleNamespace
    from babaemail.lidl_crops import _box_owned
    from babaemail.sources.lidl import Tile, _find_lp_logo

    blue = np.zeros((240, 360), dtype=np.uint8)
    yellow = blue.copy()
    # The full logo crosses the price ownership boundary x=195. The old
    # detector clips it to x=190 at its search window, or at a crop edge.
    cv2.rectangle(blue, (155, 32), (199, 76), 1, -1)
    cv2.circle(yellow, (177, 54), 13, 1, -1)
    blue[yellow == 1] = 0
    crop_right = 210 if edge == 'search' else 190
    ps = SimpleNamespace(bbox=(40, 50, 100, 100), height=50)
    tile = Tile(4, 'Y', ps.bbox, ps.bbox)
    _find_lp_logo(tile, {'Y': yellow[20:220, 10:crop_right]},
                  blue[20:220, 10:crop_right], ps)
    data = lp_evidence()
    data['crop_box'][2] = crop_right
    data['ocr_tiles'][0]['raw'] = tile.raw
    data['ocr_tiles'][0]['badge_bbox'] = None
    neighbour = LeafletReading('ocr', 4, D('4.99'), lidl_plus=True,
                               raw={'price_bbox': [280, 70, 340, 120]})
    assert not _box_owned((155, 32, 200, 77), 0,
                          [(50, 70, 110, 120), (280, 70, 340, 120)])
    result = run(refine_readings([*readings(), neighbour], [data]))
    assert not result.shipped
    target = next(d for d in result.quarantined if d.price_new == D('2.89'))
    assert any('crop-lidl-plus-ownership-unproven' in f for f in target.flags)
    assert tile.lidl_plus is None
    assert tile.raw.get('logo_truncated') is True

    # Fresh OCR records preserve the uncertainty too, including when a crop
    # VLM incorrectly says False and page readers/API otherwise agree.
    data['ocr_tiles'][0]['lidl_plus'] = tile.lidl_plus
    model = json.loads(data['vlm']['response'])
    model['lidl_plus'] = False
    data['vlm']['response'] = json.dumps(model)
    assert _proof(data)['lidl_plus'] is None
    result = run(refine_readings(readings(), [data]))
    assert not result.shipped

    from babaemail.sources.lidl import tile_to_leaflet_reading
    tile.price = D('2.89')
    tile.raw['name'] = NAME
    page_ocr = tile_to_leaflet_reading(tile)
    assert page_ocr.lidl_plus is None
    page_vlm = replace(readings()[1], lidl_plus=False)
    result = reconcile([candidate(lp=False)], [page_ocr, page_vlm], leaflet_id='test',
                       leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    assert any('crop-lidl-plus-ownership-unproven' in f
               for d in result.quarantined for f in d.flags)


def test_real_logo_detector_keeps_complete_interior_logo():
    import cv2
    import numpy as np
    from types import SimpleNamespace
    from babaemail.sources.lidl import Tile, _find_lp_logo

    blue = np.zeros((200, 200), dtype=np.uint8)
    yellow = blue.copy()
    cv2.rectangle(blue, (110, 10), (154, 54), 1, -1)
    cv2.circle(yellow, (132, 32), 13, 1, -1)
    blue[yellow == 1] = 0
    ps = SimpleNamespace(bbox=(40, 50, 100, 100), height=50)
    tile = Tile(4, 'Y', ps.bbox, ps.bbox)
    _find_lp_logo(tile, {'Y': yellow}, blue, ps)
    assert tile.lp_logo and tile.lidl_plus
    assert tile.raw['logo_bbox'] == [110, 10, 155, 55]


@pytest.mark.parametrize('truncated', [False, True])
def test_cached_logo_bounds_must_be_complete_in_page_pixels(tmp_path, truncated):
    import io
    from PIL import Image, ImageDraw

    image = Image.new('RGB', (360, 240), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((155, 32, 199, 76), fill=(10, 90, 159))
    draw.ellipse((164, 41, 190, 67), fill=(255, 230, 0))
    buf = io.BytesIO()
    image.save(buf, 'PNG')
    png = buf.getvalue()
    data = lp_evidence()
    data['page_sha256'] = hashlib.sha256(png).hexdigest()
    data['ocr_tiles'][0]['badge_bbox'] = None
    # Reproduce a pre-fix cached detector rectangle; saved files stay intact.
    data['ocr_tiles'][0]['raw']['logo_bbox'] = [145, 12, 180 if truncated else 190, 57]
    path = save_crop(data, tmp_path)
    [loaded] = load_crops({4: png}, tmp_path)
    assert loaded['logo_geometry_complete'] is not truncated
    assert (_proof(loaded)['logo_bbox'] is None) is truncated
    assert json.loads(path.read_text()) == data


@pytest.mark.parametrize('edge', ['left', 'top', 'right', 'bottom'])
def test_saved_logo_touching_crop_edge_is_not_positive_ownership(edge):
    from babaemail.lidl_crops import _crop_lp_owned

    data = lp_evidence()
    x0, y0, x1, y1 = data['crop_box']
    data['ocr_tiles'][0]['raw']['logo_bbox'] = {
        'left': [0, 30, 20, 50], 'top': [100, 0, 120, 20],
        'right': [x1 - x0 - 20, 30, x1 - x0, 50],
        'bottom': [100, y1 - y0 - 20, 120, y1 - y0],
    }[edge]
    # One page anchor would otherwise claim even a clipped component.
    assert not _crop_lp_owned(_proof(data), readings(), data['crop_box'])


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


# --- y4fix5: one fail-closed crop-LP decision --------------------------------

def _crop_votes(data, crop_ocr, crop_vlm):
    tile = data['ocr_tiles'][0]
    tile['lidl_plus'] = crop_ocr
    if crop_ocr is True:
        tile.update(badge_bbox=[40, 30, 100, 50], raw={'logo_bbox': [100, 30, 120, 50]})
    model = json.loads(data['vlm']['response'])
    model['lidl_plus'] = crop_vlm
    data['vlm']['response'] = json.dumps(model)
    return data


@pytest.mark.parametrize('page_vlm', [True, False])
def test_crop_vlm_true_without_logo_geometry_holds_against_negative_page_and_api(page_vlm):
    # Review y4fix4 #1: a complete logo outside crop OCR's detector window.
    # Crop OCR False (no geometry) + crop VLM True + page/API False = hold.
    data = _crop_votes(evidence(), False, True)
    assert (data['ocr_tiles'][0].get('raw') or {}).get('logo_bbox') is None
    rs = [replace(r, name=NAME) for r in readings()]
    if not page_vlm:
        rs = rs[:1]
    result = reconcile([candidate(lp=False)], refine_readings(rs, [data]),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    assert not result.shipped
    assert any('crop-lidl-plus-conflict' in f for d in result.quarantined for f in d.flags)


def test_adapter_unit_none_guarded_pack_vetoes_shared_word_corroboration():
    # Review y4fix4 #2 through the production OCR adapter: the distant coffee
    # anchor has unit=None and "250 g" only in its guarded tile text.
    from babaemail.lidl_crops import _crop_lp_owned
    from babaemail.sources.lidl import Tile, tile_to_leaflet_reading

    data = real_logo_evidence()
    coffee = LeafletReading('vlm', 4, D('4.99'), name='BELLAROM Mleta kava',
                            lidl_plus=True, unit='500 g')
    tile = Tile(4, 'Y', (700, 70, 760, 120), (700, 70, 760, 120), price=D('4.99'),
                raw={'brand': 'BELLAROM', 'name': 'Mleta kava',
                     'text_lines': ['BELLAROM', 'Mleta kava', '250 g']})
    distant = tile_to_leaflet_reading(tile)
    assert distant.unit is None and '250 g' in distant.text
    rs = [*readings(), coffee, distant]
    # The coffee's real anchor beside the target rejects ownership.
    actual = replace(distant, unit='500 g', text=(),
                     raw={'price_bbox': [170, 70, 230, 120]})
    assert not _crop_lp_owned(_proof(data), [*rs, actual], data['crop_box'])
    assert not _crop_lp_owned(_proof(data), rs, data['crop_box'])
    result = reconcile([candidate(lp=False)], refine_readings(rs, [data]),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    target = next(r for r in result.records if r.candidate is not None)
    assert target.deal.is_quarantined
    assert any('crop-lidl-plus-ownership-unproven' in f for f in target.deal.flags)


@pytest.mark.parametrize('text_pack,owned', [('18 kg', True), ('1,8 kg', True),
                                             ('250 g', False), ('8 kg', False)])
def test_guarded_pack_tolerates_only_a_dropped_decimal_comma(text_pack, owned):
    from babaemail.lidl_crops import _crop_lp_owned
    from babaemail.sources.lidl import Tile, tile_to_leaflet_reading

    data = real_logo_evidence()
    cereal = LeafletReading('vlm', 4, D('9.99'), name='PODRAVKA Čokolešnik',
                            lidl_plus=False, unit='1,8 kg / 1 kg')
    tile = Tile(4, 'Y', (700, 70, 760, 120), (700, 70, 760, 120), price=D('9.99'),
                raw={'brand': 'PODRAVKA', 'name': 'Čokolešnik',
                     'text_lines': ['PODRAVKA', 'Čokolešnik', text_pack]})
    rs = [*readings(), cereal, tile_to_leaflet_reading(tile)]
    assert _crop_lp_owned(_proof(data), rs, data['crop_box']) is owned


def test_permitted_crop_lp_that_refinement_cannot_apply_holds():
    # Both crop readers say True with owned geometry, but the crop clipped the
    # page anchor, so refinement skips it. 46fcab8 silently dropped the LP.
    rs = readings()
    rs[0] = replace(rs[0], bbox=(5, 70, 110, 120))
    result = run(refine_readings(rs, [lp_evidence()]))
    assert not result.shipped
    assert any('crop-lidl-plus-unresolved' in f for d in result.quarantined for f in d.flags)


def test_unlocatable_crop_with_lp_claim_holds_overlapping_anchor():
    # Crop OCR found two tiles, so the reread cannot bind to one anchor; its
    # VLM still says True. The overlapping anchor is held, never shipped.
    data = lp_evidence()
    data['ocr_tiles'].append(dict(data['ocr_tiles'][0], price_bbox=[140, 50, 190, 100]))
    far = LeafletReading('ocr', 4, D('4.99'), lidl_plus=False, name='OTHER Kava',
                         raw={'price_bbox': [900, 900, 960, 950]})
    refined = refine_readings([*readings(), far], [data])
    assert refined[0].raw['crop_lidl_plus_holds']
    assert not refined[2].raw.get('crop_lidl_plus_holds')
    assert not [d for d in run(refined).shipped if d.price_new == D('2.89')]


def test_refinement_failure_fallback_keeps_crop_lp_holds():
    from babaemail.lidl_crops import apply_crop_lp_holds
    data = _crop_votes(evidence(), False, True)
    held = apply_crop_lp_holds(readings(), [data])
    assert any('crop-lidl-plus-conflict' in f for f in held[0].raw['crop_lidl_plus_holds'])
    assert not run(held).shipped


@pytest.mark.parametrize('page_ocr,page_vlm,api,crop_ocr,crop_vlm', list(
    __import__('itertools').product((True, False, None), repeat=5)))
def test_property_crop_lp_only_adds_under_the_full_positive_rule(
        page_ocr, page_vlm, api, crop_ocr, crop_vlm):
    data = _crop_votes(evidence(), crop_ocr, crop_vlm)
    rs = [replace(r, name=NAME, lidl_plus=v)
          for r, v in zip(readings(), (page_ocr, page_vlm))]
    result = reconcile([candidate(lp=api)], refine_readings(rs, [data]),
                       leaflet_id='test', leaflet_window=WEEK, page_validity={4: WEEK})
    shipped = [d for d in result.shipped if d.price_new == D('2.89')]
    assert len(shipped) <= 1
    for deal in shipped:
        lp = deal.loyalty_required == 'Lidl Plus'
        if crop_ocr is True or crop_vlm is True:
            assert lp, 'a crop reader said True but the item shipped without LP'
        if lp:
            assert crop_ocr is True and crop_vlm is True, 'LP shipped without full crop proof'
    if (page_ocr, page_vlm, api, crop_ocr, crop_vlm) == (False,) * 5:
        assert shipped and shipped[0].loyalty_required is None  # not vacuous
