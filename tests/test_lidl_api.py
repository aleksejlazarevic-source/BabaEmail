"""Offline tests for babaemail.lidl_api.

The gridbox records in fixtures/lidl/gridboxes.json are real lidl.si responses
captured on 2026-09-23 (scratch/lidl_api/sitemap_gridboxes.json), unedited.
The network tests serve them through httpx.MockTransport; nothing touches the
internet.
"""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from babaemail import lidl_api as L

FIX = Path(__file__).parent / "fixtures" / "lidl"
WEEK = (date(2026, 9, 24), date(2026, 9, 30))


def load_records() -> dict[str, dict]:
    raw = json.loads((FIX / "gridboxes.json").read_text(encoding="utf-8"), parse_float=Decimal)
    return {x["erpNumber"]: x for x in raw}


REC = load_records()
# As they come off the wire (floats in the JSON text), for the fake server.
WIRE = {x["erpNumber"]: x for x in json.loads((FIX / "gridboxes.json").read_text(encoding="utf-8"))}


def rec(erp: str) -> L.LidlCandidate:
    return L.parse_record(REC[erp], ["sitemap"])


def run_select(window=WEEK, erps=None) -> L.CandidateList:
    erps = erps or list(REC)
    return L.select({e: (REC[e], ["sitemap"], "gridboxes") for e in erps}, *window)


def by_erp(cands) -> dict[str, L.LidlCandidate]:
    return {c.erp: c for c in cands}


# ---------------------------------------------------------------- parsing

def test_regular_discount():
    c = rec("11255022")                          # "- Obročki lignjev", -25%
    assert c.name == "Obročki lignjev"           # "-" placeholder brand dropped
    assert c.new_price == Decimal("4.49")
    assert c.old_price == Decimal("5.99")
    assert c.pct == Decimal(25)
    assert c.discount_text is None
    assert not c.lidl_plus and not c.conditional
    assert c.packaging == "400 g"
    assert c.base_price_text == "1 kg = 11,23"
    assert (date(2026, 9, 24), date(2026, 9, 26)) in c.all_windows
    assert set(c.offer_signals) == {"old_price", "pct"}


def test_money_is_decimal_never_float():
    for erp in REC:
        c = rec(erp)
        for v in (c.new_price, c.old_price, c.pct, c.regular_price):
            assert v is None or isinstance(v, Decimal), (erp, v)
    with pytest.raises(TypeError):
        L._dec(4.99)


def test_lidl_plus_price():
    c = rec("11000617")                          # HARVEST BASKET Pomfrit
    assert c.name == "HARVEST BASKET Pomfrit"
    assert c.lidl_plus
    assert c.lidl_plus_text == "z Lidl Plus"
    assert c.highlight_text == "-40%"
    assert (c.new_price, c.old_price, c.pct) == (Decimal("1.19"), Decimal("1.99"), Decimal(40))
    assert c.discount_text == "-40%"             # falls back to the highlight badge
    assert c.regular_price is None
    assert not c.conditional


def test_lidl_plus_with_regular_price_alongside():
    c = rec("11029883")                          # Mango: plain 1.19 and Lidl Plus 0.99
    assert c.lidl_plus
    assert c.new_price == Decimal("0.99")
    assert c.old_price == Decimal("1.49")
    assert c.regular_price == Decimal("1.19")
    assert c.packaging == "za kos"


@pytest.mark.parametrize("erp,reason", [
    ("11259243", "-56% na drugi kos"),           # Lidl Plus, second item
    ("11007967", "-50% na drugi kos"),           # Lidl Plus, alongside a plain price
    ("11266203", "4+2 gratis"),                  # only in highlightText
    ("11256502", "2+1 gratis"),                  # plain currentPrice
])
def test_conditional_badges(erp, reason):
    c = rec(erp)
    assert c.conditional
    assert c.conditional_reason == reason
    assert c.pct is None                         # never let "-50% on the 2nd" read as 50%
    assert c.new_price is not None               # the price is kept, just not trusted as unit price


def test_conditional_keeps_deleted_price_disagreement_visible():
    c = rec("11007967")                          # oldPrice 2.59 vs deletedPrice 3.99
    assert c.old_price == Decimal("2.59")
    assert "deletedPrice_differs:3.99" in c.flags


def test_za_kg_is_a_price_basis_not_a_discount():
    c = rec("11457289")                          # Naša mesnica Svež telečji vrat
    assert c.name == "Naša mesnica Svež telečji vrat"
    assert c.price_unit == "kg"
    assert c.new_price == Decimal("12.99")
    assert c.old_price is None and c.pct is None
    assert not c.conditional
    assert "no_packaging" not in c.flags
    assert c.offer_signals == ("discount_text",)


def test_bonus_quantity_gratis_is_not_conditional():
    c = rec("11271569")                          # "20% gratis**" bigger pack, same price
    assert c.discount_text == "20% gratis**"
    assert not c.conditional
    assert "bonus_quantity" in c.flags
    assert c.new_price == Decimal("1.89")


def test_diacritics_preserved():
    names = {rec(e).name for e in REC}
    assert "Piščančje prsi" in names
    assert "Naše nam paše Slovenska namenska moka" in names


# ---------------------------------------------------------------- windows

def test_ljubljana_timezone_boundary():
    # 1790200800 is 2026-09-23 22:00 UTC == 2026-09-24 00:00 in Ljubljana.
    assert datetime.fromtimestamp(1790200800, timezone.utc).date() == date(2026, 9, 23)
    assert L.local_day(1790200800) == date(2026, 9, 24)
    # validUntil 1790459999 is 26.9. 23:59:59 local (21:59:59 UTC).
    assert L.local_day(1790459999) == date(2026, 9, 26)

    # Obročki lignjev, "Od 24.09. do 26.09.": not an offer on 23.9. or 27.9.
    assert "11255022" not in by_erp(run_select((date(2026, 9, 23), date(2026, 9, 23))))
    assert "11255022" not in by_erp(run_select((date(2026, 9, 27), date(2026, 9, 27))))
    assert "11255022" in by_erp(run_select((date(2026, 9, 24), date(2026, 9, 24))))
    assert "11255022" in by_erp(run_select((date(2026, 9, 26), date(2026, 9, 26))))


def test_window_filtering_keeps_only_overlapping_windows():
    got = by_erp(run_select())
    c = got["11000617"]                          # Pomfrit: 25-26.9. and 27-28.11.
    assert c.windows == [(date(2026, 9, 25), date(2026, 9, 26))]
    assert len(c.all_windows) == 2

    later = by_erp(run_select((date(2026, 10, 1), date(2026, 10, 7))))
    assert "11000617" not in later
    assert "11259243" not in later               # 28-30.9. only


def test_open_ended_window_is_kept_and_flagged():
    c = by_erp(run_select())["11276469"]         # BELBAKE Drobtine, "Od 09.07.", no end
    assert c.windows == [(date(2026, 7, 9), None)]
    assert "open_ended_window" in c.flags
    # No end date means it overlaps any later window too; the price record
    # (ends 7.10.2026) is what exposes that the price no longer applies.
    far = by_erp(run_select((date(2031, 1, 1), date(2031, 1, 7))))["11276469"]
    assert any(f.startswith("price_not_valid_in_window") for f in far.flags)


def test_price_not_valid_in_window_is_flagged():
    # Grape price record ends 27.9., but the in-store window is 28.9.-3.10.
    # The published 1.09 is last week's price, not this window's.
    c = by_erp(run_select())["11030020"]
    assert any(f.startswith("price_not_valid_in_window") for f in c.flags)


def test_no_price_and_no_signal_are_kept_separately():
    res = run_select()
    offers = by_erp(res)
    assert "11000034" not in offers
    assert "11000034" in by_erp(res.no_price)                  # in window, no price at all
    assert "no_price" in by_erp(res.no_price)["11000034"].flags
    assert "11000214" in by_erp(res.no_signal)                 # price, but nothing says "offer"
    assert "10043885" not in by_erp(list(res) + res.no_price + res.no_signal)
    assert res.stats["no_window"] >= 1                         # counted, not silently gone
    s = res.stats
    assert s["records"] == s["no_window"] + s["out_of_window"] + s["in_window"]
    assert s["in_window"] == s["offers"] + s["no_price"] + s["no_signal"]


def test_every_offer_has_a_signal():
    for c in run_select():
        assert c.offer_signals and c.has_price


# ---------------------------------------------------------------- network

CAMPAIGN_PATH = (FIX / "campaign_path.txt").read_text().strip()
SEARCH_PAGES = [json.loads((FIX / f"search_page{i}.json").read_text(encoding="utf-8")) for i in (0, 1)]


def gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


def sitemap(erps) -> bytes:
    return gz("<urlset>" + "".join(
        f"<url><loc>https://www.lidl.si/p/x/p{e}</loc></url>" for e in erps) + "</urlset>")


class FakeLidl:
    """A lidl.si stand-in. Override attributes to inject failures."""

    def __init__(self):
        self.sitemap_erps = [e for e in REC if e != "11259243"]   # that one only via search
        self.gridbox_records = dict(WIRE)
        self.calls: list[str] = []
        self.fail: dict[str, list[int]] = {}   # path -> status codes to return first

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.fail.get(path):
            return httpx.Response(self.fail[path].pop(0), text="nope")
        q = parse_qs(urlparse(str(request.url)).query)
        if path == urlparse(L.SITEMAP_URL).path:
            return httpx.Response(200, content=sitemap(self.sitemap_erps))
        if path == urlparse(L.PAGES_SITEMAP_URL).path:
            return httpx.Response(200, content=gz(
                f"<urlset><url><loc>https://www.lidl.si{CAMPAIGN_PATH}</loc></url></urlset>"))
        if path == "/":
            return httpx.Response(200, text="<html>no campaign links here</html>")
        if path == urlparse(L.SEARCH_URL).path:
            off = int(q["offset"][0])
            page = SEARCH_PAGES[off] if off < len(SEARCH_PAGES) else {"numFound": 2, "items": []}
            return httpx.Response(200, json=page)
        if path == CAMPAIGN_PATH:
            return httpx.Response(200, text=(FIX / "campaign_page.html").read_text(encoding="utf-8"))
        if path == urlparse(L.GRIDBOX_URL).path:
            want = q["erpNumbers"][0].split(",")
            body = [self.gridbox_records[e] for e in want if e in self.gridbox_records]
            return httpx.Response(200, content=json.dumps(body, ensure_ascii=False).encode())
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(L, "_sleep", sleeps.append)
    f = FakeLidl()
    f.sleeps = sleeps
    return f


def client_for(fake) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake), base_url=L.BASE)


def test_fetch_candidates_end_to_end(fake):
    res = L.fetch_candidates(*WEEK, client=client_for(fake))
    got = by_erp(res)
    # Found only through search, then refreshed from gridboxes.
    assert got["11259243"].sources == ("search",)
    assert got["11259243"].data_from == "gridboxes"
    # Campaign-only variant (erp with a 001 suffix) that gridboxes does not return:
    campaign_only = [c for c in res if c.data_from == "campaign-embedded"]
    assert campaign_only and campaign_only[0].sources == ("campaign:" + CAMPAIGN_PATH,)
    assert got["11276469"].sources == ("sitemap", "search")
    assert got["11255022"].sources == ("sitemap",)
    assert res.stats["unresolved"] == []
    assert res.stats["sitemap_ids"] == len(REC) - 1
    assert res.stats["offers"] == len(res)
    assert "11000034" in by_erp(res.no_price)


def test_prices_survive_the_wire_as_decimal(fake):
    res = L.fetch_candidates(*WEEK, client=client_for(fake))
    assert by_erp(res)["11255022"].new_price == Decimal("4.49")
    assert all(isinstance(c.new_price, Decimal) for c in res)


def test_empty_sitemap_raises(fake):
    fake.sitemap_erps = []
    with pytest.raises(L.LidlApiError, match="zero products"):
        L.fetch_candidates(*WEEK, client=client_for(fake))


def test_empty_gridbox_batch_raises(fake):
    fake.gridbox_records = {}
    with pytest.raises(L.LidlApiError, match="gridboxes returned nothing"):
        L.fetch_candidates(*WEEK, client=client_for(fake))


def test_zero_offers_in_window_raises_not_empty_list(fake):
    with pytest.raises(L.LidlApiError, match="zero Lidl offers"):
        # Before every fixture window. (A far-FUTURE window would still hit the
        # open-ended "Od 09.07." record, which is correct: it has no end date.)
        L.fetch_candidates(date(2020, 1, 1), date(2020, 1, 7), client=client_for(fake))


def test_retries_transient_errors_with_backoff(fake):
    fake.fail[urlparse(L.SITEMAP_URL).path] = [503, 502]
    res = L.fetch_candidates(*WEEK, client=client_for(fake))
    assert res
    assert fake.sleeps[:2] == [1.0, 2.0]


def test_persistent_429_raises_with_status(fake):
    fake.fail[urlparse(L.GRIDBOX_URL).path] = [429] * 50
    with pytest.raises(L.LidlApiError) as ei:
        L.fetch_candidates(*WEEK, client=client_for(fake))
    assert ei.value.status_code == 429


def test_404_raises_immediately_without_retry(fake):
    fake.fail[urlparse(L.SEARCH_URL).path] = [404]
    with pytest.raises(L.LidlApiError) as ei:
        L.fetch_candidates(*WEEK, client=client_for(fake))
    assert ei.value.status_code == 404
    assert fake.calls.count(urlparse(L.SEARCH_URL).path) == 1


def test_timeouts_are_retried_then_raise(monkeypatch):
    monkeypatch.setattr(L, "_sleep", lambda s: None)
    calls = []

    def boom(request):
        calls.append(1)
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(L.LidlApiError, match="after 4 attempts"):
        L.fetch_candidates(*WEEK, client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert len(calls) == L.ATTEMPTS


def test_non_json_block_page_raises(fake):
    orig = fake.__call__

    def cloudflare(request):
        if request.url.path == urlparse(L.SEARCH_URL).path:
            return httpx.Response(200, text="<html>Just a moment...</html>")
        return orig(request)

    with pytest.raises(L.LidlApiError, match="non-JSON"):
        L.fetch_candidates(*WEEK, client=httpx.Client(transport=httpx.MockTransport(cloudflare)))


def test_stale_campaign_page_404_is_recorded_not_fatal(fake):
    orig = fake.__call__
    stale = "/c/badespass-spielparadies-am-ufer/a10097813"

    def with_stale(request):
        if request.url.path == urlparse(L.PAGES_SITEMAP_URL).path:
            return httpx.Response(200, content=gz(
                f"<urlset><url><loc>https://www.lidl.si{CAMPAIGN_PATH}</loc></url>"
                f"<url><loc>https://www.lidl.si{stale}</loc></url></urlset>"))
        return orig(request)

    res = L.fetch_candidates(*WEEK, client=httpx.Client(transport=httpx.MockTransport(with_stale)))
    assert res.stats["campaign_pages_gone"] == [stale]
    assert res.stats["campaign_pages"] == 2


def test_campaign_page_500_still_raises(fake):
    fake.fail[CAMPAIGN_PATH] = [500] * 10
    with pytest.raises(L.LidlApiError) as ei:
        L.fetch_candidates(*WEEK, client=client_for(fake))
    assert ei.value.status_code == 500
