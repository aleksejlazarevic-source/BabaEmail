"""Lidl.fetch() end to end with every external piece mocked: flyer API, PDF,
OCR, VLM (ollama) and the lidl.si offer API. No network, no model, no OCR."""

from datetime import date
from decimal import Decimal as D

import pytest

from babaemail import lidl_api, lidl_crops, lidl_notices, ocr, vlm
from babaemail.lidl_api import CandidateList, LidlApiError, LidlCandidate
from babaemail.lidl_reconcile import LeafletReading
from babaemail.models import SourceStatus
from babaemail.sources import lidl as lidl_mod
from babaemail.sources.lidl import Lidl

TODAY = date(2026, 9, 24)
WEEK = (date(2026, 9, 24), date(2026, 9, 30))
META = {"pdf_url": "https://example.invalid/kw39.pdf", "title": "kw39", "pages": 2,
        "valid_from": WEEK[0], "valid_to": WEEK[1]}
PNGS = {1: b"png-page-1", 2: b"png-page-2"}


def cand(erp, title, price, old=None, pct=None, *, brand=None, lp=False):
    return LidlCandidate(
        erp=erp, name=f"{brand} {title}" if brand else title, brand=brand, title=title,
        url=f"https://www.lidl.si/p/p{erp}", category="Food", new_price=D(price),
        old_price=D(old) if old else None, pct=D(pct) if pct else None,
        discount_text=f"-{pct}%" if pct else None, lidl_plus=lp, lidl_plus_text=None,
        highlight_text=None, regular_price=None, conditional=False, conditional_reason=None,
        packaging=None, price_unit=None, base_price_text=None, windows=[WEEK],
        all_windows=[WEEK], sources=("sitemap",), data_from="gridboxes", flags=[])


CANDS = [cand("1", "Alpsko mleko", "0.99", "1.29", "23", brand="Ljubljanske mlekarne"),
         cand("2", "Svež ribeye steak", "6.19", brand="Žar legende")]

OCR = {1: [LeafletReading("ocr", 1, D("0.99"), D("1.29"), D("23"), lidl_plus=False,
                          name="LJUBLJANSKE MLEKARNE Alpsko mleko", bbox=(0, 0, 10, 10))],
       2: [LeafletReading("ocr", 2, D("6.19"), lidl_plus=False,
                          name="ŽAR LEGENDE Svež ribeye steak", bbox=(0, 0, 10, 10))]}

VLM = {1: [vlm.VlmReading(1, "LJUBLJANSKE MLEKARNE Alpsko mleko", D("0.99"), False, "1 l")],
       2: [vlm.VlmReading(2, "ŽAR LEGENDE Svež ribeye steak", D("6.19"), False, "300 g")]}


class FakeSession:
    """Stands in for vlm.ollama_session; records that it was entered."""

    def __init__(self, exc=None):
        self.exc, self.entered = exc, 0

    def __call__(self, model=None, url=None, **kw):
        if self.exc:
            raise self.exc
        self.entered += 1
        return self

    def __enter__(self):
        return False

    def __exit__(self, *a):
        return False


@pytest.fixture
def world(monkeypatch):
    """Everything mocked for a good run. Tests override single pieces."""
    w = {"meta": dict(META), "pngs": dict(PNGS), "ocr": dict(OCR), "vlm": dict(VLM),
         "cands": CANDS, "session": FakeSession(), "vlm_calls": []}

    monkeypatch.setattr(Lidl, "_flyer_meta", lambda self, ident: w["meta"])
    monkeypatch.setattr(Lidl, "_download_pdf", lambda self, url: b"%PDF-1.7 fake")
    monkeypatch.setattr(Lidl, "_render_pages", staticmethod(lambda blob: w["pngs"]))

    def read_ocr(self, pngs):
        if ocr.cv2 is None:                      # the real method's first check
            raise ocr.OcrUnavailable("opencv not installed")
        return {p: w["ocr"][p] for p in pngs}
    monkeypatch.setattr(Lidl, "_read_ocr", read_ocr)

    def read_page(png, *, model, page, allow_empty, url, **kw):
        w["vlm_calls"].append((page, model, url))
        if isinstance(w["vlm"], Exception):
            raise w["vlm"]
        return w["vlm"][page]
    monkeypatch.setattr(vlm, "read_page", read_page)
    monkeypatch.setattr(vlm, "ollama_session", lambda *a, **k: w["session"](*a, **k))

    def fetch_candidates(a, b):
        if isinstance(w["cands"], Exception):
            raise w["cands"]
        assert (a, b) == WEEK, "the API window must be the leaflet's own window"
        return CandidateList(w["cands"], no_signal=[], no_price=[], stats={})
    monkeypatch.setattr(lidl_api, "fetch_candidates", fetch_candidates)

    def page_text(png):
        if isinstance(w.get("texts"), Exception):
            raise w["texts"]
        return (w.get("texts") or {}).get(png, "")
    monkeypatch.setattr(lidl_notices, "page_text", page_text)
    return w


def test_ok_run_ships_verified_deals(world):
    res = Lidl(vlm_model="m:1", ollama_url="http://127.0.0.1:1").fetch(TODAY)
    assert res.status is SourceStatus.OK and res.contributes, res.error
    assert res.pages_declared == 2 and res.pages_parsed == 2
    assert world["session"].entered == 1
    assert [c[0] for c in world["vlm_calls"]] == [1, 2]
    assert all(c[1:] == ("m:1", "http://127.0.0.1:1") for c in world["vlm_calls"])
    shipped = {d.retailer_item_id: d for d in res.items if not d.is_quarantined}
    assert set(shipped) == {"1", "2"}
    milk = shipped["1"]
    assert (milk.price_new, milk.price_old, milk.discount_published) == (D("0.99"), D("1.29"), D("23"))
    assert milk.retailer == "lidl" and milk.source_page == 1 and milk.valid_to == WEEK[1]


def test_production_adapter_ships_website_only_in_its_structured_section(world):
    world["cands"] = [*CANDS, cand("web", "Linški keksi", "1.99", "2.19",
                                      brand="SONDEY")]
    res = Lidl().fetch(TODAY)
    web = next(d for d in res.items if d.retailer_item_id == "web")
    assert not web.is_quarantined
    assert web.source_kind == "website_only" and "website-only" in web.flags


def test_vlm_unavailable_fails_loud_not_partial(world):
    world["session"] = FakeSession(vlm.VlmUnavailable("ollama is not running"))
    res = Lidl().fetch(TODAY)
    assert res.status is SourceStatus.BLOCKED and not res.contributes
    assert "vlm unavailable" in res.error and "NOT covered" in res.error
    assert res.items == []


def test_vlm_failing_mid_run_fails_loud(world):
    world["vlm"] = vlm.VlmUnavailable("model output cut off")
    res = Lidl().fetch(TODAY)
    assert res.status is SourceStatus.BLOCKED and "cut off" in res.error


def test_vlm_empty_page_where_ocr_found_prices_is_a_failure(world):
    world["vlm"] = {1: VLM[1], 2: []}
    res = Lidl().fetch(TODAY)
    assert res.status is SourceStatus.BLOCKED and "page 2" in res.error


def test_vlm_empty_page_without_prices_is_accepted(world):
    world["meta"]["pages"] = 3
    world["pngs"] = {**PNGS, 3: b"png-page-3"}
    world["ocr"] = {**OCR, 3: [LeafletReading("ocr", 3, None)]}   # a tile without a readable price
    world["vlm"] = {**VLM, 3: []}
    res = Lidl().fetch(TODAY)
    assert res.status is SourceStatus.OK, res.error
    assert res.pages_parsed == res.pages_declared == 3


def test_no_vlm_is_a_diagnostic_run_never_shippable(world):
    res = Lidl(use_vlm=False).fetch(TODAY)
    assert res.status is SourceStatus.BLOCKED and not res.contributes
    assert "VLM disabled" in res.error
    assert res.items, "the diagnostic items are kept for inspection"
    assert world["vlm_calls"] == [] and world["session"].entered == 0


def test_ocr_unavailable_is_blocked_never_no_offers(world, monkeypatch):
    monkeypatch.setattr(ocr, "cv2", None)
    res = Lidl().fetch(TODAY)
    assert res.status is SourceStatus.BLOCKED
    assert "ocr unavailable" in res.error


def test_real_read_ocr_raises_ocr_unavailable_without_opencv(monkeypatch):
    monkeypatch.setattr(ocr, "cv2", None)
    with pytest.raises(ocr.OcrUnavailable):
        lidl_mod.Lidl._read_ocr(Lidl(), PNGS)


@pytest.mark.parametrize("exc, status", [
    (LidlApiError("HTTP 429 from x after 4 attempts", status_code=429), SourceStatus.BLOCKED),
    (LidlApiError("HTTP 403 from x", status_code=403), SourceStatus.BLOCKED),
    (LidlApiError("zero Lidl offers for 2026-09-24..2026-09-30"), SourceStatus.PARSE_FAILED),
])
def test_api_failure(world, exc, status):
    world["cands"] = exc
    res = Lidl().fetch(TODAY)
    assert res.status is status and not res.contributes
    assert res.error.startswith("lidl api:")


def test_zero_shipped_deals_is_a_failure(world):
    # the API's prices contradict every tile: everything is quarantined
    world["cands"] = [cand("1", "Alpsko mleko", "1.09", brand="Ljubljanske mlekarne"),
                      cand("2", "Svež ribeye steak", "6.49", brand="Žar legende")]
    res = Lidl().fetch(TODAY)
    assert res.status is SourceStatus.PARSE_FAILED and not res.contributes
    assert "none verified" in res.error
    assert res.items and all(d.is_quarantined for d in res.items)


def test_bad_crop_cache_does_not_fail_lidl(world, monkeypatch):
    def bad_cache(*args, **kwargs):
        raise ValueError('broken crop json')
    monkeypatch.setattr(lidl_crops, 'load_crops', bad_cache)
    lidl = Lidl()
    res = lidl.fetch(TODAY)
    assert res.status is SourceStatus.OK, res.error
    assert 'broken crop json' in lidl.last_stats['crop_load_error']


def test_crop_refine_error_keeps_unrefined_deals(world, monkeypatch):
    def broken_refinement(readings, evidence):
        raise RuntimeError('broken crop proof')
    monkeypatch.setattr(lidl_crops, 'refine_readings', broken_refinement)
    lidl = Lidl()
    res = lidl.fetch(TODAY)
    assert res.status is SourceStatus.OK, res.error
    assert {d.retailer_item_id for d in res.items if not d.is_quarantined} == {'1', '2'}
    assert lidl.last_stats['crop_refine_error'] == 2


def test_page_count_mismatch_is_a_failure(world):
    world["meta"]["pages"] = 64
    res = Lidl().fetch(TODAY)
    assert res.status is SourceStatus.PARSE_FAILED and "declares 64" in res.error


def test_stale_leaflet(world):
    res = Lidl().fetch(date(2026, 10, 1))
    assert res.status is SourceStatus.STALE


def test_no_flyer(world):
    world["meta"] = None
    assert Lidl().fetch(TODAY).status is SourceStatus.NO_LEAFLET


# ---------------------------------------------------------------- ollama_session

class FakeProc:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_session_uses_running_server_and_never_stops_it(monkeypatch):
    monkeypatch.setattr(vlm, "ollama_version", lambda url=None, timeout=3.0: "0.12.0")
    monkeypatch.setattr(vlm, "_model_available", lambda m, u: True)
    monkeypatch.setattr(vlm.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    with vlm.ollama_session("m", "http://127.0.0.1:11434") as started:
        assert started is False


def test_session_never_touches_a_port_held_by_something_else(monkeypatch):
    monkeypatch.setattr(vlm, "ollama_version", lambda url=None, timeout=3.0: None)
    monkeypatch.setattr(vlm, "_port_in_use", lambda h, p: True)
    monkeypatch.setattr(vlm.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(vlm.VlmUnavailable, match="not ollama"):
        with vlm.ollama_session("m", "http://127.0.0.1:8000", autostart=True):
            pass


def test_session_starts_and_stops_its_own_server(monkeypatch, tmp_path):
    proc = FakeProc()
    seen = {}
    ups = iter([None, None, "0.12.0"])
    monkeypatch.setattr(vlm, "ollama_version", lambda url=None, timeout=3.0: next(ups, "0.12.0"))
    monkeypatch.setattr(vlm, "_port_in_use", lambda h, p: False)
    monkeypatch.setattr(vlm, "_model_available", lambda m, u: True)
    monkeypatch.setattr(vlm.shutil, "which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr(vlm.time, "sleep", lambda s: None)

    def popen(argv, **kw):
        seen["argv"], seen["host"] = argv, kw["env"]["OLLAMA_HOST"]
        return proc
    monkeypatch.setattr(vlm.subprocess, "Popen", popen)
    with vlm.ollama_session("m", "http://127.0.0.1:11555", autostart=True,
                            log_path=tmp_path / "serve.log") as started:
        assert started is True and not proc.terminated
    assert proc.terminated
    assert seen == {"argv": ["/usr/bin/ollama", "serve"], "host": "127.0.0.1:11555"}


def test_session_model_missing_is_unavailable_and_stops_own_server(monkeypatch, tmp_path):
    proc = FakeProc()
    ups = iter([None, "0.12.0"])
    monkeypatch.setattr(vlm, "ollama_version", lambda url=None, timeout=3.0: next(ups, "0.12.0"))
    monkeypatch.setattr(vlm, "_port_in_use", lambda h, p: False)
    monkeypatch.setattr(vlm, "_model_available", lambda m, u: False)
    monkeypatch.setattr(vlm.shutil, "which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr(vlm.subprocess, "Popen", lambda *a, **k: proc)
    with pytest.raises(vlm.VlmUnavailable, match="ollama pull"):
        with vlm.ollama_session("m", "http://127.0.0.1:11555", autostart=True,
                                log_path=tmp_path / "serve.log"):
            pass
    assert proc.terminated


def test_session_autostart_off(monkeypatch):
    monkeypatch.setattr(vlm, "ollama_version", lambda url=None, timeout=3.0: None)
    with pytest.raises(vlm.VlmUnavailable, match="autostart disabled"):
        with vlm.ollama_session("m", "http://127.0.0.1:11434", autostart=False):
            pass


SENIOR_TEXT = ("Upokojenci ga lahko izkoristite ob nakupu na dan 30. 9. 2026 v vseh Lidlovih "
               "trgovinah. Kupec ... prejme 10 % popust na posamezen nakup. Uveljavljanje "
               "popusta: 10 % popust je mogoče uveljaviti zgolj na nakup, opravljen na ta dan.")


def test_notices_are_read_and_attached(world):
    world["texts"] = {PNGS[max(PNGS)]: SENIOR_TEXT}
    res = Lidl(vlm_model="m:1", ollama_url="http://127.0.0.1:1").fetch(TODAY)
    assert res.contributes
    [n] = res.notices
    assert n.proven and n.retailer == res.retailer and n.page == max(PNGS)


def test_notice_failure_costs_the_notices_never_the_deals(world):
    world["texts"] = ocr.OcrUnavailable("no tesseract")
    lidl = Lidl(vlm_model="m:1", ollama_url="http://127.0.0.1:1")
    res = lidl.fetch(TODAY)
    assert res.contributes and res.notices == []
    assert "no tesseract" in lidl.last_stats["notices_error"]


# ---------------------------------------------------------------- the OCR tile reader

def _synthetic_tile(badge=None, speck=False):
    """A zoom-3-sized Lidl tile: yellow "6.19" (raised, top-aligned cents, big
    dot) on red, a white name block to its left, optionally a yellow flag box
    above the price holding a word or only a speck."""
    import numpy as np
    cv2 = pytest.importorskip("cv2")
    red, yel, white = (237, 28, 36), (253, 241, 1), (255, 255, 255)

    def glyphs(text, scale, thick):
        m = np.zeros((600, 1400), np.uint8)
        cv2.putText(m, text, (20, 450), cv2.FONT_HERSHEY_DUPLEX, scale, 255, thick, cv2.LINE_AA)
        ys, xs = np.nonzero(m > 127)
        return (m > 127)[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    def paste(img, mask, x, y, col):
        h, w = mask.shape
        img[y:y + h, x:x + w][mask] = col

    img = np.full((1400, 1700, 3), red, np.uint8)
    e, c = glyphs("6", 9, 30), glyphs("19", 6.5, 22)
    ex, ey, he = 900, 1000, e.shape[0]
    paste(img, e, ex, ey, yel)
    paste(img, c, ex + e.shape[1] + int(0.3 * he), ey, yel)
    r = int(0.12 * he)
    cv2.circle(img, (ex + e.shape[1] + int(0.15 * he), ey + he - r), r, yel, -1)
    paste(img, glyphs("ZAR LEGENDE", 2.0, 4), 100, 1000, white)
    paste(img, glyphs("Svez ribeye steak", 2.0, 4), 100, 1080, white)
    if badge or speck:
        cv2.rectangle(img, (ex - 10, ey - 150), (ex + 520, ey - 20), yel, -1)
        if badge:
            paste(img, glyphs(badge, 2.6, 7), ex + 20, ey - 130, red)
        else:
            cv2.circle(img, (ex + 480, ey - 130), 4, red, -1)
    return img


def _read_synthetic(**kw):
    if not ocr.tesseract_available():
        pytest.skip("tesseract not installed")
    [r] = lidl_mod.tiles_to_leaflet_readings(lidl_mod.read_page_tiles(_synthetic_tile(**kw), 2))
    return r


def test_reader_a_speck_in_a_box_above_the_price_is_not_a_badge():
    """kw39 p2 ribeye: a patch of background above 6.19 held one speck; the
    tile was read as flagged and could never ship as a plain price."""
    r = _read_synthetic(speck=True)
    assert r.new_price == D("6.19")
    assert r.raw["badge"] is None and r.raw["raw"]["badge_speck"] is True
    assert r.raw["price_bbox"] and r.raw["badge_bbox"]          # geometry kept


def test_reader_a_real_flag_stays_a_flag_and_its_words_are_read():
    r = _read_synthetic(badge="za kg")
    assert r.raw["badge"] == "flag" and "za k" in r.raw["raw"]["flag_text"]


def test_reader_records_the_tiles_own_text_lines():
    r = _read_synthetic()
    assert r.raw["badge"] is None
    assert any("ribeye steak" in ln for ln in r.text)
