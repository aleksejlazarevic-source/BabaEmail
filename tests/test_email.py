"""Email-layer regression tests: prices, provenance and fail-loud behaviour."""
from __future__ import annotations

import importlib
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from babaemail.categorize import categorize
from babaemail.mailer import REQUIRED, SMTPConfig, send_email
from babaemail.models import Deal, Notice, SourceResult, SourceStatus
from babaemail.render import ROOT, deal_view, euros, format_unit_price, render
from babaemail.select import Selection, load_preferences, select
from babaemail.store import load_lidl, write_history
from babaemail.verify import verify

TODAY = date(2026, 9, 26)


def _all_stores_checked(path):
    # These tests exercise multi-store behaviour, independent of which stores the
    # live preferences.yaml currently has switched on.
    data = load_preferences(ROOT / 'preferences.yaml')
    for store in data['stores']:
        store['checked'] = True
    import yaml
    path.write_text(yaml.safe_dump(data, allow_unicode=True), 'utf-8')
    return path


@pytest.fixture
def prefs(tmp_path):
    return load_preferences(_all_stores_checked(tmp_path / 'prefs-all.yaml'))


def deal(**kw):
    data = dict(retailer='lidl', name='Kruh', leaflet_id='weekly', source_url='https://example.test',
                valid_from=TODAY, valid_to=TODAY + timedelta(days=4), price_new=Decimal('1.19'),
                price_old=Decimal('1.74'), discount_published=Decimal(31))
    data.update(kw)
    return Deal(**data)


def source(items=None, **kw):
    data = dict(retailer='lidl', status=SourceStatus.OK, source_url='https://example.test',
                fetched_at=datetime.now(), items=[deal()] if items is None else items)
    data.update(kw)
    return SourceResult(**data)


@pytest.mark.parametrize(('changes', 'reason'), [
    ({'price_new': None}, 'new-price-missing-or-nonpositive'),
    ({'price_new': Decimal(0)}, 'new-price-missing-or-nonpositive'),
    ({'price_new': Decimal(-1)}, 'new-price-missing-or-nonpositive'),
    ({'price_old': Decimal('1.19')}, 'old-price-not-higher'),
    ({'price_old': Decimal('1.00')}, 'old-price-not-higher'),
    ({'discount_published': Decimal(32)}, 'published-percent-inconsistent'),
    ({'discount_published': Decimal(-1)}, 'published-percent-invalid'),
    ({'discount_published': Decimal(101)}, 'published-percent-invalid'),
    ({'discount_published': Decimal('NaN')}, 'published-percent-invalid'),
    ({'condition_kind': 'multi-buy', 'condition': '  '}, 'condition-missing'),
    ({'condition_kind': 'second-item', 'condition': '-80%', 'price_old': None}, 'second-item-first-price-missing'),
    ({'valid_to': TODAY - timedelta(days=1)}, 'expired'),
    ({'valid_from': TODAY + timedelta(days=4)}, 'starts-after-lookahead'),
    ({'valid_from': TODAY + timedelta(days=2), 'valid_to': TODAY + timedelta(days=1)}, 'invalid-validity-window'),
    ({'flags': ['quarantine:uncertain']}, 'quarantine:uncertain'),
    ({'condition_kind': 'from-price', 'condition': 'že od'}, 'from-price-with-percent'),
])
def test_verify_rejections(changes, reason):
    item = deal(**changes)
    result = verify(source([item]), TODAY, 3)
    assert result.accepted == []
    assert len(result.rejected) == 1
    assert any(r.startswith(reason) for r in result.rejected[0].reasons)


@pytest.mark.parametrize('status', [s for s in SourceStatus if s is not SourceStatus.OK])
def test_failed_source_cannot_contribute(status):
    result = verify(source(status=status), TODAY, 3)
    assert not result.accepted
    assert f'source-not-ok:{status.value}' in result.rejected[0].reasons


def test_zero_and_partial_store_fail():
    empty = verify(source([]), TODAY)
    partial = verify(source(pages_declared=2, pages_parsed=1), TODAY)
    assert empty.source.status is SourceStatus.PARSE_FAILED
    assert partial.source.status is SourceStatus.PARSE_FAILED
    assert not partial.accepted


def test_truncation_and_rounding_boundaries():
    for retailer, badge, expected in [('lidl', 31, True), ('lidl', 32, False),
                                      ('spar', 32, True), ('spar', 33, False)]:
        item = deal(retailer=retailer, discount_published=Decimal(badge))
        assert bool(verify(source([item], retailer=retailer), TODAY).accepted) is expected
    exact = deal(price_new=Decimal('3'), price_old=Decimal('4'), discount_published=Decimal(25))
    assert verify(source([exact]), TODAY).accepted
    exact.discount_published = Decimal(24)  # actual == badge+1 is outside the Lidl interval
    assert not verify(source([exact]), TODAY).accepted


def test_bonus_pack_exception():
    item = deal(price_old=Decimal('1.00'), condition_kind='bonus-pack', condition='10+4 gratis',
                discount_published=None)
    assert verify(source([item]), TODAY).accepted == [item]


def test_leaflet_deal_cannot_escape_source_window_but_website_only_can():
    item = deal(name='Spomladanski zavitki', valid_from=date(2026, 9, 28), valid_to=date(2026, 10, 7))
    result = verify(source([item], valid_from=date(2026, 10, 1), valid_to=date(2026, 10, 7)), TODAY, 14)
    assert 'outside-leaflet-window' in result.rejected[0].reasons
    item.source_kind = 'website_only'
    assert verify(source([item], valid_from=date(2026, 10, 1), valid_to=date(2026, 10, 7)), TODAY, 14).accepted == [item]


@pytest.mark.parametrize(('kind', 'kwargs', 'price', 'pct'), [
    ('second-item', {'price_new': Decimal('.39'), 'price_old': Decimal('1.99'),
                     'condition': '-80% na drugi kos', 'discount_published': Decimal(80)},
     'drugi kos −80 %: 0,39 € (prvi 1,99 €)', 40),
    ('bonus-pack', {'price_new': Decimal('3'), 'price_old': None, 'condition': '10+4 gratis'}, '3,00 €', None),
    ('from-price', {'price_new': Decimal('1.99'), 'price_old': None, 'condition': 'že od'}, 'že od 1,99 €', None),
    ('multi-buy', {'price_new': Decimal('10.27'), 'price_old': None,
                   'condition': '18 kosov za 10,27 € (plačaš 13)'}, '10,27 €', None),
])
def test_conditional_prices_html_and_text(prefs, kind, kwargs, price, pct):
    kwargs.setdefault('discount_published', None)
    item = deal(condition_kind=kind, loyalty_required='Lidl Plus', **kwargs)
    res = source([item])
    chosen = select([verify(res, TODAY, 3)], prefs)
    html, text = render(chosen, [res], prefs, TODAY)
    view = deal_view(item, TODAY)
    assert view['price'] == price and view['pct'] == pct
    for output in [html, text]:
        assert price in output
        assert kwargs['condition'] in output
        assert 'Lidl Plus' in output
    assert ('−40 %' in html) is (kind == 'second-item')


def test_kg_every_price_and_decimal():
    assert euros(Decimal('3')) == '3,00 €'
    with pytest.raises(TypeError):
        euros(3.0)
    with pytest.raises(ValueError):
        euros(Decimal('3.001'))
    view = deal_view(deal(price_unit='kg'), TODAY)
    assert view['price'] == '1,19 €/kg' and view['old'] == '1,74 €/kg'
    view = deal_view(deal(price_unit='kg', price_new=Decimal('.39'), price_old=Decimal('1.99'),
                          discount_published=None, condition_kind='second-item', condition='−80%'), TODAY)
    assert view['price'].count('€/kg') == 2
    assert format_unit_price('1 kg = 10.00') == '1 kg = 10,00'
    assert format_unit_price('Cena 10.00') == 'Cena 10.00'
    assert format_unit_price('1 kg = približno 10.00') == '1 kg = približno 10.00'


def test_unknown_and_nonfood_categories():
    assert categorize(deal(name='Xyzzy', retailer_category='Unknown')) == 'ostalo'
    for brand, key in [('Parkside', 'orodje'), ('Esmara', 'oblacila'), ('Lupilu', 'oblacila'),
                       ('SilverCrest', 'elektronika'), ('Livarno', 'gospodinjstvo')]:
        assert categorize(deal(name='izdelek', brand=brand)) == key
    assert categorize(deal(name='Xyzzy', retailer_category='F+V')) == 'sadje_zelenjava'


@pytest.mark.parametrize(('name', 'brand', 'category'), [
    ('Ribani sir za gratiniranje', 'MILBONA', 'mlecno'),
    ('Njoki', 'HARVEST BASKET', 'shramba'),
    ('Vlečeno testo', 'chef select', 'shramba'),
    ('Škampi', 'Ocean Sea', 'ribe'),
    ('Espresso Extra Dark Roast', 'BELLAROM', 'pijace'),
    ('Hop a Cao', None, 'pijace'),
    ('Kakavov napitek', 'BELLAROM', 'pijace'),
    ('Biskvitne rezine', 'REALFORNO', 'sladko'),
    ('Izbrane ribje jedi', None, 'ribe'),
    ('ZAUSTAVLJALNIK VRAT', None, 'neprehrana'),
    ('Solarna sveča Diana', 'VESTINA', 'neprehrana'),
    ('DEŽNIK', None, 'neprehrana'),
])
def test_review_category_cases(name, brand, category):
    assert categorize(deal(name=name, brand=brand)) == category


def test_website_only_is_capped_and_deduplicated_against_leaflet(prefs):
    leaflet = deal(name='Slovensko zelje', price_new=Decimal('.65'), price_old=Decimal('.94'), discount_published=Decimal(30))
    duplicate = deal(name='Belo zelje', price_new=Decimal('.65'), price_old=Decimal('.94'), discount_published=Decimal(30), source_kind='website_only')
    unique = [deal(name=f'Spletni izdelek {n}', source_kind='website_only') for n in range(1, 8)]
    result = select([verify(source([leaflet, duplicate, *unique]), TODAY)], prefs)
    websites = [d for d in result.items if d.source_kind == 'website_only']
    assert len(websites) == 6 and duplicate not in result.items
    assert {cut.reason for cut in result.cuts} >= {'website-only-duplicate-leaflet', 'website-only-limit'}


def test_verify_before_preferences_and_always_never_bypasses_safety(prefs):
    prefs['always_show'] = ['orodje']
    items = [deal(name='Parkside orodje', price_new=None), deal(name='Parkside vrtalnik')]
    result = verify(source(items), TODAY, 3)
    chosen = select([result], prefs)
    assert len(result.rejected) == 1 and len(result.accepted) == 1
    assert chosen.items == []
    assert [c.reason for c in chosen.cuts] == ['category-hidden']
    prefs['always_show'] = ['Parkside']
    assert select([result], prefs).items == [items[1]]


def test_plain_lidl_prices_other_stores_and_order(prefs):
    items = [deal(name='Xyzzy', price_old=None, discount_published=None),
             deal(name='Kruh znižan'), deal(name='Vedno', price_old=None, discount_published=None)]
    prefs['always_show'] = ['Vedno']
    chosen = select([verify(source(items), TODAY)], prefs)
    assert [d.name for d in chosen.items] == ['Vedno', 'Kruh znižan', 'Xyzzy']
    other = deal(retailer='hofer', price_old=None, discount_published=None)
    verified = verify(source([other], retailer='hofer'), TODAY)
    assert select([verified], prefs).items == []
    prefs['always_show'] = ['Kruh']
    assert select([verified], prefs).items == [other]


def test_floor_overrides_and_limits(prefs):
    prefs['default_floor'] = 40
    result = verify(source([deal()]), TODAY)
    assert select([result], prefs).cuts[0].reason == 'below-discount-floor'
    prefs['categories']['kruh']['floor'] = 10
    assert len(select([result], prefs).items) == 1
    prefs['stores'][0]['max_items'] = 0
    assert select([result], prefs).cuts[0].reason == 'store-item-limit'
    prefs['stores'][0]['max_items'] = 45
    prefs['max_items'] = 0
    assert select([result], prefs).cuts[0].reason == 'total-item-limit'


def test_sections_banners_notices_escape_and_size(prefs):
    items = [deal(name='Kruh <script>'), deal(name='Spletni kruh', source_kind='website_only'),
             deal(name='Partner', flags=['email:partner-offer']), deal(name='Xyzzy')]
    res = source(items)
    res.notices = [Notice('lidl', 'senior-day', 1, TODAY, 10, True, '', 'Senior dan: 10 %.'),
                   Notice('lidl', 'unproven', 2, TODAY, 99, False, 'no proof', 'NE PRIKAŽI'),
                   Notice('lidl', 'expired', 3, TODAY - timedelta(days=1), 10, True, '', 'STARO')]
    failed = source([], retailer='spar', status=SourceStatus.BLOCKED)
    chosen = select([verify(res, TODAY), verify(failed, TODAY)], prefs)
    html, text = render(chosen, [res, failed], prefs, TODAY)
    for output in [html, text]:
        assert 'Danes ni bilo mogoče preveriti akcij v trgovini Spar.' in output
        assert 'Senior dan: 10 %.' in output
        assert 'NE PRIKAŽI' not in output and 'STARO' not in output
        assert output.index('Še na spletni strani Lidl') > output.index('Kruh')
        assert 'Lidl Plus: ugodnosti partnerjev' in output
        assert 'Ostalo' in output
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert 'name="color-scheme" content="light"' in html
    assert '<img' not in html and '<style' not in html
    assert len(html.encode()) < 90_000


def test_quarantine_never_rendered_even_if_selection_is_forged(prefs):
    item = deal(flags=['quarantine:price-unproven'])
    res = source([item])
    html, text = render(select([verify(res, TODAY)], prefs), [res], prefs, TODAY)
    assert '1,19' not in html and '1,19' not in text
    with pytest.raises(ValueError, match='Unsafe selected item'):
        render(Selection([item]), [res], prefs, TODAY)


def test_eighty_item_email_is_small(prefs):
    prefs['stores'][0]['max_items'] = 80
    items = [deal(name=f'Kruh {i}', loyalty_required='Lidl Plus', unit_price='1 kg = 2,38') for i in range(80)]
    res = source(items)
    html, _ = render(select([verify(res, TODAY)], prefs), [res], prefs, TODAY)
    assert len(html.encode('utf-8')) < 90_000
    assert html.count('font-size:24px') == 80


def cache_doc(items=None):
    return {'retailer': 'lidl', 'status': 'ok', 'valid_from': '2026-09-24', 'valid_to': '2026-09-30',
            'leaflet_id': 'weekly', 'shipped': [asdict(d) for d in (items or [deal()])], 'quarantined': []}


def dump(path, doc):
    path.write_text(json.dumps(doc, default=str, ensure_ascii=False))
    return path


def test_cache_newest_covering_today_and_history(tmp_path):
    import os
    old = dump(tmp_path / 'lidl_old.json', cache_doc())
    newest = dump(tmp_path / 'lidl_new.json', cache_doc([deal(name='Novejši')]))
    future = cache_doc()
    future['valid_from'], future['valid_to'] = '2026-10-01', '2026-10-07'
    dump(tmp_path / 'lidl_future.json', future)
    os.utime(old, (1, 1))
    os.utime(newest, (2, 2))
    loaded = load_lidl(TODAY, tmp_path)
    assert loaded.path == newest and loaded.source.items[0].name == 'Novejši'
    path = write_history(tmp_path / 'history', TODAY, loaded.source.items)
    assert json.loads(path.read_text())['items'][0]['price_new'] == '1.19'
    assert write_history(tmp_path / 'history', TODAY, loaded.source.items) != path
    assert not load_lidl(date(2030, 1, 1), tmp_path).source.contributes


def test_malformed_and_stale_cache_fail_whole_source(tmp_path):
    doc = cache_doc()
    doc['shipped'].append({'name': 'Bad'})
    loaded = load_lidl(TODAY, path=dump(tmp_path / 'lidl_bad.json', doc))
    assert not loaded.source.contributes and loaded.held_records
    assert not verify(loaded.source, TODAY).accepted
    stale = load_lidl(date(2030, 1, 1), path=dump(tmp_path / 'lidl_old.json', cache_doc()))
    assert stale.source.status is SourceStatus.STALE


def test_cached_hold_cannot_be_laundered_and_partner_same_gate(tmp_path):
    doc = cache_doc()
    doc['quarantined'] = [asdict(deal(name='Held', flags=[]))]
    doc['partner_offers'] = [
        {'partner': 'Partner', 'what': 'Vstopnica', 'price': '3.00',
         'valid_from': '2026-09-24', 'valid_to': '2026-09-30', 'conditions': 'S kuponom'},
        {'partner': 'Partner', 'what': 'Brez cene', 'discount_pct': 20,
         'valid_from': '2026-09-24', 'valid_to': '2026-09-30'},
        {'partner': 'Partner', 'what': 'Neznan datum', 'price': '2.00', 'validity': 'do konca meseca'},
    ]
    loaded = load_lidl(TODAY, path=dump(tmp_path / 'lidl.json', doc))
    result = verify(loaded.source, TODAY)
    assert len(result.accepted) == 2
    assert len(result.rejected) == 2
    assert loaded.held_records and 'explicit valid_from' in loaded.held_records[0]
    assert any('new-price-missing-or-nonpositive' in r.reasons for r in result.rejected)


@pytest.mark.parametrize('missing', REQUIRED)
def test_mailer_refuses_missing_environment(missing):
    env = {key: 'value' for key in REQUIRED}
    env['BABAEMAIL_SMTP_PORT'] = '587'
    del env[missing]
    with pytest.raises(ValueError, match=missing):
        SMTPConfig.from_env(env)


def test_fake_smtp_tls_login_multipart_and_alert():
    events, messages = [], []
    class FakeSMTP:
        def __init__(self, host, port, timeout):
            events.append(('connect', host, port, timeout))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            events.append('close')
        def ehlo(self):
            events.append('ehlo')
        def starttls(self, context):
            assert context.check_hostname
            events.append('tls')
        def login(self, user, password):
            events.append(('login', user, password))
        def send_message(self, message):
            messages.append(message)
    config = SMTPConfig('test.invalid', 587, 'u', 'p', 'from@test.invalid', 'babi@test.invalid', 'aleks@test.invalid')
    send_email('Akcije', '<p>1,19 €</p>', '1,19 €', alert='Spar failed', config=config, smtp_factory=FakeSMTP)
    assert events == [('connect', 'test.invalid', 587, 30), 'ehlo', 'tls', 'ehlo', ('login', 'u', 'p'), 'close']
    assert messages[0]['To'] == config.alert_recipient
    assert messages[1]['To'] == config.recipient
    assert messages[1].get_content_type() == 'multipart/alternative'
    assert [p.get_content_type() for p in messages[1].iter_parts()] == ['text/plain', 'text/html']
    assert messages[1].get_body(preferencelist=('plain',)).get_content().strip() == '1,19 €'


def test_cli_dry_run_offline_exit_banner_no_smtp_and_report(tmp_path, monkeypatch):
    cli = importlib.import_module('babaemail.main')
    calls = []
    monkeypatch.setattr(cli, 'send_email', lambda *a, **kw: calls.append('sent'))
    monkeypatch.setattr(cli, 'fetch_live', lambda *a: pytest.fail('offline must not fetch'))
    path = dump(tmp_path / 'lidl_test.json', cache_doc())
    code = cli.main(['--preferences', str(_all_stores_checked(tmp_path / 'p.yaml')), '--dry-run', '--send', '--offline', '--date', str(TODAY),
                     '--lidl-json', str(path), '--out', str(tmp_path)])
    assert code == 1 and calls == []
    assert not (tmp_path / 'history').exists()
    html = (tmp_path / f'email-{TODAY}.html').read_text()
    assert html.count('Danes ni bilo mogoče preveriti') == 4
    report = (tmp_path / f'report-{TODAY}.txt').read_text()
    assert 'lidl: status=ok' in report and 'shipped=1' in report
    assert 'spar: status=blocked' in report


def test_cli_all_ok_and_store_failure_still_writes(tmp_path, monkeypatch):
    cli = importlib.import_module('babaemail.main')
    monkeypatch.setattr(cli, 'fetch_live', lambda key, today: source([deal(retailer=key)], retailer=key))
    path = dump(tmp_path / 'lidl_test.json', cache_doc())
    args = ['--preferences', str(_all_stores_checked(tmp_path / 'p.yaml')), '--dry-run', '--date', str(TODAY), '--lidl-json', str(path), '--out', str(tmp_path)]
    assert cli.main(args) == 0
    monkeypatch.setattr(cli, 'fetch_live', lambda key, today: source([], retailer=key))
    assert cli.main(args) == 1
    assert 'status=parse_failed' in (tmp_path / f'report-{TODAY}.txt').read_text()
    assert (tmp_path / f'email-{TODAY}.html').exists()


@pytest.mark.parametrize('kind', ['second-item', 'bonus-pack', 'from-price', 'multi-buy'])
def test_all_condition_kinds_need_text(kind):
    result = verify(source([deal(condition_kind=kind, condition=None, discount_published=None)]), TODAY)
    assert not result.accepted
    assert 'condition-missing' in result.rejected[0].reasons


@pytest.mark.parametrize('kind', ['leaflet', 'website_only'])
def test_website_prices_have_same_safety_gate(kind):
    result = verify(source([deal(source_kind=kind, price_new=None)]), TODAY)
    assert not result.accepted
    assert 'new-price-missing-or-nonpositive' in result.rejected[0].reasons


def test_lookahead_boundaries_and_labels(prefs):
    future = deal(valid_from=TODAY + timedelta(days=3))
    today_ends = deal(name='Danes', valid_to=TODAY)
    res = source([future, today_ends])
    result = verify(res, TODAY, 3)
    assert len(result.accepted) == 2
    html, text = render(select([result], prefs), [res], prefs, TODAY)
    for output in [html, text]:
        assert 'od tor. 29. 9.' in output
        assert 'do sob. 26. 9.' in output


@pytest.mark.parametrize('price', ['20 %', 'že od 3,00 €', '2 kosov za 3,00 €'])
def test_partner_price_prose_is_held_not_reinterpreted(tmp_path, price):
    doc = cache_doc()
    doc['partner_offers'] = [{'partner': 'Partner', 'what': 'Ponudba', 'price': price,
                              'valid_from': '2026-09-24', 'valid_to': '2026-09-30'}]
    loaded = load_lidl(TODAY, path=dump(tmp_path / 'lidl_test.json', doc))
    assert len(loaded.source.items) == 1
    assert 'without marketing qualifiers' in loaded.held_records[0]


def test_cli_mock_send_failure_alert_and_history(tmp_path, monkeypatch):
    cli = importlib.import_module('babaemail.main')
    calls = []
    monkeypatch.setattr(cli, 'fetch_live', lambda key, today: source([], retailer=key, status=SourceStatus.BLOCKED))
    monkeypatch.setattr(cli, 'send_email', lambda *a, **kw: calls.append(kw))
    path = dump(tmp_path / 'lidl_test.json', cache_doc())
    args = ['--preferences', str(_all_stores_checked(tmp_path / 'p.yaml')), '--send', '--date', str(TODAY), '--lidl-json', str(path), '--out', str(tmp_path)]
    assert cli.main(args) == 1
    assert len(calls) == 1 and 'spar: status=blocked' in calls[0]['alert']
    assert len(list((tmp_path / 'history').glob('*.json'))) == 1
    def refuse(*a, **kw):
        raise ValueError('Missing BABAEMAIL_SMTP_HOST')
    monkeypatch.setattr(cli, 'send_email', refuse)
    assert cli.main(args) == 2
    assert len(list((tmp_path / 'history').glob('*.json'))) == 1
    assert 'SEND FAILED' in (tmp_path / f'report-{TODAY}.txt').read_text()


def test_store_preference_order_and_disabled_stores_accounted(prefs):
    lidl = source([deal()])
    hofer = source([deal(retailer='hofer')], retailer='hofer')
    verified = [verify(lidl, TODAY), verify(hofer, TODAY)]
    prefs['stores'] = [{'key': 'hofer', 'max_items': 8}, {'key': 'lidl', 'max_items': 45}]
    chosen = select(verified, prefs)
    assert [d.retailer for d in chosen.items] == ['hofer', 'lidl']
    prefs['stores'] = [{'key': 'lidl', 'max_items': 45}]
    chosen = select(verified, prefs)
    assert len(chosen.items) == 1
    assert chosen.cuts[0].deal.retailer == 'hofer' and chosen.cuts[0].reason == 'store-disabled'


def test_source_informational_flags_do_not_quarantine(prefs):
    item = deal(flags=['leaflet-page:1', 'leaflet-confirmed:ocr+vlm'])
    result = verify(source([item]), TODAY)
    assert select([result], prefs).items == [item]


@pytest.mark.parametrize("name, expected", [
    ("Sveži kremni sir", "mlecno"),
    ("Pšenična bela moka", "shramba"),
    ("KREMNE JUHE", "shramba"),
    ("Rogljiček s šunko in sirom", "kruh"),
    ("Pizza štručka s salamo in mozzarello", "kruh"),
    ("KREMA ZA OBRAZ BB", "neprehrana"),
    ("Prigrizek za pse", "neprehrana"),
    ("PESEK ZA MAČKE", "neprehrana"),
    ("Sušilec las Remington", "neprehrana"),
])
def test_categorize_review_cases(name, expected):
    from babaemail.categorize import categorize
    assert categorize(_deal_for_category(name)) == expected


def _deal_for_category(name):
    from datetime import date
    from babaemail.models import Deal
    return Deal(retailer="spar", name=name, leaflet_id="x", source_url="x",
                valid_from=date(2026, 10, 1), valid_to=None)


def test_unchecked_store_is_cut_and_named_not_bannered():
    from datetime import date
    from decimal import Decimal
    from babaemail.models import Deal, SourceResult, SourceStatus
    from babaemail.render import render
    from babaemail.select import select
    from babaemail.verify import verify
    from datetime import datetime
    today = date(2026, 10, 3)
    spar = SourceResult('spar', SourceStatus.OK, 'x', datetime(2026, 10, 3), items=[
        Deal(retailer='spar', name='Na izdelke iz promocije točk zvestobe ni', leaflet_id='s',
             source_url='x', valid_from=today, valid_to=None, price_new=Decimal('7.99'),
             price_old=Decimal('15.99'))])
    tus = SourceResult('tus', SourceStatus.BLOCKED, 'x', datetime(2026, 10, 3))
    prefs = {'stores': [{'key': 'spar', 'max_items': 8, 'checked': False},
                        {'key': 'tus', 'max_items': 8, 'checked': False}],
             'default_floor': 20, 'categories': {}, 'always_show': [], 'max_items': 80,
             'lookahead_days': 3}
    verified = [verify(spar, today, 3), verify(tus, today, 3)]
    selection = select(verified, prefs)
    assert selection.items == []
    assert {c.reason for c in selection.cuts} == {'store-not-yet-checked'}
    html, text = render(selection, [spar, tus], prefs, today)
    assert 'Spar in Tuš še niso vključeni' in html and 'Spar in Tuš še niso vključeni' in text
    assert 'Danes ni bilo mogoče' not in html
    assert 'zvestobe' not in html
