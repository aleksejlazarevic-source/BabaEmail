"""Reconciliation rules, one synthetic case per rule (see lidl_reconcile docstring)."""

import dataclasses
from datetime import date
from decimal import Decimal as D

import pytest

from babaemail.lidl_api import LidlCandidate
from babaemail.lidl_reconcile import (
    LeafletReading, build_tiles, fold, from_vlm, ocr_reconciled, parse_tile_date,
    reconcile, split_leaflet_name,
)
from babaemail.vlm import VlmReading

WEEK = (date(2026, 9, 24), date(2026, 9, 30))


def cand(erp, title, price, old=None, pct=None, *, lp=False, brand=None, flags=(),
         conditional=None, windows=(WEEK,)):
    name = f"{brand} {title}" if brand else title
    return LidlCandidate(
        erp=erp, name=name, brand=brand, title=title, url=f"https://www.lidl.si/p/p{erp}",
        category="Food", new_price=D(price), old_price=D(old) if old else None,
        pct=D(pct) if pct else None, discount_text=f"-{pct}%" if pct else None,
        lidl_plus=lp, lidl_plus_text=None, highlight_text=None, regular_price=None,
        conditional=conditional is not None, conditional_reason=conditional,
        packaging=None, price_unit=None, base_price_text=None,
        windows=list(windows), all_windows=list(windows), sources=("sitemap",),
        data_from="gridboxes", flags=list(flags))


def vlm(page, name, price, lp=False, until=None):
    return LeafletReading("vlm", page, D(price) if price else None, lidl_plus=lp,
                          name=name, valid_to_text=until)


def ocr(page, price, old=None, pct=None, lp=False, name=None):
    return LeafletReading("ocr", page, D(price), D(old) if old else None,
                          D(pct) if pct else None, lidl_plus=lp, name=name,
                          bbox=(0, 0, 100, 100))


def run(cands, readings, **kw):
    kw.setdefault("leaflet_id", "kw39")
    kw.setdefault("leaflet_window", WEEK)
    return reconcile(cands, readings, **kw)


def by_erp(res, erp):
    [r] = [r for r in res.records if r.candidate is not None and r.candidate.erp == erp]
    return r.deal


def reasons(deal):
    return [f for f in deal.flags if f.startswith("quarantine:")]


# ---------------------------------------------------------------- VERIFIED

def test_verified_ships_with_api_name_and_old_price():
    res = run([cand("1", "Kranjska klobasa", "1.99", "3.49", "42")],
              [vlm(1, "Z'DEŽELE Kranjska klobasa", "1.99")])
    d = by_erp(res, "1")
    assert not d.is_quarantined
    assert d.name == "Kranjska klobasa"
    assert (d.price_new, d.price_old, d.discount_published) == (D("1.99"), D("3.49"), D("42"))
    assert "leaflet-confirmed:vlm" in d.flags and "leaflet-page:1" in d.flags
    assert d.source_page == 1 and d.valid_to == date(2026, 9, 30)
    assert len(res.deals) == 1


def test_verified_with_both_readers_names_both_sources():
    res = run([cand("1", "Kranjska klobasa", "1.99", "3.49", "42")],
              [vlm(1, "Z'DEŽELE Kranjska klobasa", "1.99"), ocr(1, "1.99", "3.49", "42")])
    assert "leaflet-confirmed:ocr+vlm" in by_erp(res, "1").flags
    assert len(res.deals) == 1


def test_brand_only_match_sarenka_vs_postrv_ships_with_ocr_corroboration():
    res = run([cand("1", "Sveža postrv", "4.69", "5.69", "17", lp=True, brand="Naša ribarnica")],
              [vlm(3, "NAŠA RIBARNICA Sveža šarenka", "4.69", lp=True),
               ocr(3, "4.69", "5.69", "17", lp=True)])
    d = by_erp(res, "1")
    assert not d.is_quarantined
    assert any(f.startswith("name-match:brand-only") for f in d.flags)


def test_brand_only_match_without_corroboration_is_not_a_pair():
    # R3b: nothing but brand + price ties them, so neither consumes the other.
    res = run([cand("1", "Sveža postrv", "4.69", "5.69", "17", lp=True, brand="Naša ribarnica")],
              [vlm(3, "NAŠA RIBARNICA Sveža šarenka", "4.69", lp=True)])
    d = by_erp(res, "1")
    assert "not-in-leaflet-reading" in reasons(d)[0]
    assert "brand-only name match NOT corroborated" in reasons(d)[0]
    [tile] = [r.deal for r in res.records if r.rule == "leaflet-only"]
    assert tile.is_quarantined                        # one reader: R8 cannot prove it
    assert len(res.deals) == 2


def test_same_brand_same_price_different_product_is_not_confirmed():
    # No ribeye in the API at all: the ribeye tile must not confirm the ramsteak.
    res = run([cand("2", "Svež ramsteak", "6.19", "8.59", "27", brand="Žar legende")],
              [vlm(2, "ŽAR LEGENDE Svež ribeye steak", "6.19")])
    assert by_erp(res, "2").is_quarantined


def test_brand_only_match_refused_when_brand_not_unique_at_price():
    res = run([cand("1", "Sveža postrv", "4.69", lp=True, brand="Naša ribarnica"),
               cand("2", "Sveži brancin", "4.69", lp=True, brand="Naša ribarnica")],
              [vlm(3, "NAŠA RIBARNICA Sveža šarenka", "4.69", lp=True)])
    assert all(d.is_quarantined for d in res.deals)
    assert len(res.deals) == 3


def test_abbreviated_api_name_matches():
    res = run([cand("1", "IK Pišč. brez drob dod. bed. 9973", "3.19", "3.95", "19",
                    brand="Naše nam paše")],
              [vlm(2, "NAŠE NAM PAŠE Svež piščanec z dodanimi bedri", "3.19")])
    assert not by_erp(res, "1").is_quarantined


def test_listed_item_confirmed_by_leaflet_ships():
    res = run([], [vlm(1, "LJUBLJANSKE MLEKARNE Alpsko mleko", "5.29")],
              listed=[cand("9", "Alpsko mleko", "5.29")])
    d = by_erp(res, "9")
    assert not d.is_quarantined and d.price_old is None
    assert "api:listed-without-offer-signal" in d.flags


def test_unmatched_listed_items_are_returned_not_turned_into_deals():
    res = run([cand("1", "Mango", "0.99", "1.49", "33")], [vlm(1, "Mango", "0.99")],
              listed=[cand("9", "Alpsko mleko", "5.29")])
    assert [c.erp for c in res.unmatched_listed] == ["9"]
    assert len(res.deals) == 1


# ---------------------------------------------------------------- DISAGREEMENT

def test_ribeye_price_conflict_quarantined_with_both_numbers():
    res = run([cand("2", "Svež ramsteak", "6.19", "8.59", "27", lp=True, brand="Žar legende")],
              [vlm(2, "ŽAR LEGENDE Svež ribeye steak", "6.19"),
               vlm(34, "ŽAR LEGENDE Svež ramsteak", "6.19", lp=True)],
              listed=[cand("1", "Ribeye steak", "6.49", brand="Žar legende")])
    rib = by_erp(res, "1")
    [why] = reasons(rib)
    assert "api-leaflet-price-conflict" in why and "6.49" in why and "6.19" in why
    # The ribeye tile is used up by the conflict, NOT given to the ramsteak at 6.19.
    ram = by_erp(res, "2")
    assert not ram.is_quarantined and ram.source_page == 34
    # R5b: the tile is judged on its own; one reader (VLM) cannot prove it
    [tile] = leaflet_only(res)
    assert "leaflet-over-api:1 api 6.49" in tile.flags and tile.is_quarantined
    assert len(res.deals) == 3


def test_leaflet_price_overrules_the_api_when_both_readers_prove_it():
    res = run([], [vlm(2, "ŽAR LEGENDE Svež ribeye steak", "6.19"),
                   bare(2, "6.19", "ŽAR LEGENDE Svež ribeye steak")],
              listed=[cand("1", "Ribeye steak", "6.49", brand="Žar legende")],
              page_validity={2: WEEK})
    assert by_erp(res, "1").is_quarantined                  # the API's 6.49 never ships
    [d] = leaflet_only(res)
    assert not d.is_quarantined and d.price_new == D("6.19")
    assert "leaflet-over-api:1 api 6.49" in d.flags


def test_leaflet_old_price_fills_a_missing_api_one():
    # kw39 p9 Culinea: API 3.99 with no old price; the leaflet prints 5.49 / -27%
    res = run([cand("1", "Piščančje perutničke", "3.99", brand="Culinea")],
              [vlm(9, "CULINEA Piščančje perutničke", "3.99"),
               ocr(9, "3.99", "5.49", "27", name="CULINEA Piščančje perutničke")])
    d = by_erp(res, "1")
    assert not d.is_quarantined and "old-price-from-leaflet" in d.flags
    assert (d.price_old, d.discount_published) == (D("5.49"), D("27"))


def test_leaflet_old_price_never_overrides_a_different_api_one():
    res = run([cand("1", "Piščančje perutničke", "3.99", "4.99", "20", brand="Culinea")],
              [vlm(9, "CULINEA Piščančje perutničke", "3.99"),
               ocr(9, "3.99", "5.49", "27", name="CULINEA Piščančje perutničke")])
    assert any("old-price-conflict" in r for r in reasons(by_erp(res, "1")))


def test_name_beats_price_even_when_another_product_has_the_leaflet_price():
    # Only the ramsteak is at 6.19 in the API, and the ramsteak tile is missing.
    res = run([cand("2", "Svež ramsteak", "6.19", "8.59", "27", brand="Žar legende")],
              [vlm(2, "ŽAR LEGENDE Svež ribeye steak", "6.19")],
              listed=[cand("1", "Ribeye steak", "6.49", brand="Žar legende")])
    assert by_erp(res, "2").is_quarantined           # same brand, same price: still not confirmed
    assert "api-leaflet-price-conflict" in reasons(by_erp(res, "1"))[0]


# ---------------------------------------------------------------- COLLISIONS

def test_price_collision_at_299_is_one_to_one_and_name_driven():
    cands = [cand("k", "Prekajena prednja svinjska krača", "2.99"),
             cand("b", "Dimljena klobasa", "2.99", "4.49", "33", brand="Baroni"),
             cand("p", "Poetovio narezek Family pack", "2.99")]
    tiles = [vlm(8, "PICKIES Piščančja prekajena klobasa", "2.99"),
             vlm(8, "Z'DEŽELE Prekajena prednja svinjska krača", "2.99")]
    res = run(cands, tiles)
    assert not by_erp(res, "k").is_quarantined
    baroni = by_erp(res, "b")
    assert baroni.is_quarantined                        # one shared noun is not a match
    assert "not-in-leaflet-reading" in reasons(baroni)[0]
    assert "PICKIES" in reasons(baroni)[0]              # reviewer sees the same-price tile
    assert by_erp(res, "p").is_quarantined
    [pickies] = [r.deal for r in res.records if r.rule == "leaflet-only"]
    assert "leaflet-only-unverified" in reasons(pickies)[0]
    assert len(res.deals) == 4


def test_two_same_price_products_pair_by_name_not_by_order():
    cands = [cand("a", "Smetana za kuhanje", "0.69", "0.99", "30", brand="Milbona"),
             cand("b", "Skutin žepek", "0.69", "0.99", "30")]
    tiles = [vlm(41, "Skutin žepek", "0.69"), vlm(34, "MILBONA Smetana za kuhanje", "0.69")]
    res = run(cands, tiles)
    assert by_erp(res, "a").source_page == 34 and by_erp(res, "b").source_page == 41
    assert not res.quarantined


# ---------------------------------------------------------------- API-only

def test_api_only_is_quarantined_never_dropped():
    res = run([cand("1", "Mango", "0.99", "1.49", "33"), cand("2", "Hruške", "1.39", "1.99", "30")],
              [vlm(1, "Mango", "0.99")])
    d = by_erp(res, "2")
    assert reasons(d) == ["quarantine:not-in-leaflet-reading"]
    assert len(res.deals) == 2


# ---------------------------------------------------------------- Lidl Plus

def test_lidl_plus_mismatch_quarantines():
    res = run([cand("1", "Mango", "0.99", "1.49", "33", lp=True)], [vlm(1, "Mango", "0.99", lp=False)])
    assert "lidl-plus-mismatch" in reasons(by_erp(res, "1"))[0]


def test_readers_disagreeing_on_lidl_plus_are_decided_2_of_3_with_the_api():
    res = run([cand("1", "Mango", "0.99", "1.49", "33", lp=True)],
              [vlm(1, "Mango", "0.99", lp=True), ocr(1, "0.99", "1.49", "33", lp=False)])
    d = by_erp(res, "1")
    assert not d.is_quarantined and d.loyalty_required == "Lidl Plus"
    assert any(f.startswith("lidl-plus-2-of-3:api+vlm") for f in d.flags)


def test_lidl_plus_unknown_when_no_majority():
    res = run([cand("1", "Mango", "0.99", "1.49", "33", lp=True)],
              [vlm(1, "Mango", "0.99", lp=True), vlm(1, "Mango", "0.99", lp=False)])
    assert "leaflet-lidl-plus-unknown" in reasons(by_erp(res, "1"))[0]


def test_single_reader_against_api_on_lidl_plus_is_a_mismatch_not_a_vote():
    res = run([cand("1", "Mango", "0.99", "1.49", "33", lp=True)],
              [ocr(1, "0.99", "1.49", "33", lp=False, name="Mango")])
    assert "lidl-plus-mismatch" in reasons(by_erp(res, "1"))[0]


# ---------------------------------------------------------------- API flags

@pytest.mark.parametrize("flag", ["sold_out:IZDELEK ŽAL NI NA VOLJO",
                                  "price_not_valid_in_window:2026-09-11..2026-09-27",
                                  "deletedPrice_differs:3.99", "price_changes_in_window:x:2026-09-28"])
def test_correctness_flags_quarantine_even_when_confirmed(flag):
    res = run([cand("1", "Mango", "0.99", "1.49", "33", flags=[flag])], [vlm(1, "Mango", "0.99")])
    d = by_erp(res, "1")
    assert d.is_quarantined and f"api:{flag}" in d.flags
    assert "leaflet-confirmed:vlm" in d.flags


def test_informational_flags_pass_through_without_quarantine():
    res = run([cand("1", "Sveže piščančje krače", "3.59", flags=["bonus_quantity"])],
              [vlm(2, "NAŠE NAM PAŠE Sveže piščančje krače", "3.59")])
    d = by_erp(res, "1")
    assert not d.is_quarantined and "api:bonus_quantity" in d.flags


def test_second_item_deal_ships_when_the_arithmetic_holds():
    # kw39 Milka: first bar 1.99, second 0.39 = -80.4%, badge "-80%"
    res = run([cand("1", "Čokolada", "0.39", "1.99", lp=True, conditional="-80% na drugi kos")],
              [vlm(1, "MILKA Čokolada", "0.39", lp=True)])
    d = by_erp(res, "1")
    assert not d.is_quarantined
    assert (d.condition_kind, d.condition) == ("second-item", "-80% na drugi kos")
    assert (d.price_new, d.price_old, d.discount_published) == (D("0.39"), D("1.99"), None)
    assert d.discount_display == 40          # over two bars, never the badge's 80


def test_second_item_deal_with_truncated_badge_ships():
    # kw39 chicken breast: 2.29 / 0.99 is -56.8%, Lidl prints "-56%"
    res = run([cand("1", "Piščančje prsi", "0.99", "2.29", lp=True, conditional="-56% na drugi kos")],
              [vlm(38, "Piščančje prsi", "0.99", lp=True)])
    assert not by_erp(res, "1").is_quarantined


def test_second_item_deal_with_wrong_arithmetic_is_quarantined():
    res = run([cand("1", "Čokolada", "0.59", "1.99", lp=True, conditional="-80% na drugi kos")],
              [vlm(1, "MILKA Čokolada", "0.59", lp=True)])
    d = by_erp(res, "1")
    assert any("second-item arithmetic fails" in r for r in reasons(d))
    assert d.condition_kind is None


def test_second_item_deal_without_old_price_is_quarantined():
    res = run([cand("1", "Čokolada", "0.39", lp=True, conditional="-80% na drugi kos")],
              [vlm(1, "MILKA Čokolada", "0.39", lp=True)])
    assert any("conditional-price" in r for r in reasons(by_erp(res, "1")))


def bonus(erp, title, price, reason, packaging):
    return dataclasses.replace(cand(erp, title, price, conditional=reason), packaging=packaging)


def test_bonus_pack_ships_when_the_pack_counts_n_plus_m():
    res = run([bonus("1", "Kokošja pašteta XXL", "14.99", "10+4 gratis**", "14x95 g")],
              [vlm(35, "ARGETA Kokošja pašteta XXL", "14.99")])
    d = by_erp(res, "1")
    assert not d.is_quarantined
    assert (d.condition_kind, d.condition, d.price_new) == ("bonus-pack", "10+4 gratis", D("14.99"))
    assert d.discount_pct is None


def test_bonus_pack_with_other_pack_count_is_quarantined():
    res = run([bonus("1", "Kokošja pašteta XXL", "14.99", "10+4 gratis**", "12x95 g")],
              [vlm(35, "ARGETA Kokošja pašteta XXL", "14.99")])
    assert any("bonus pack 10+4" in r for r in reasons(by_erp(res, "1")))


def test_real_multibuy_stays_quarantined():
    # kw39 Argus "13+5 gratis" (no **): 10.27 is the price of 18 cans; on p10
    # "2+1 gratis" 0.76 is a per-litre price. The number's meaning is not provable.
    res = run([bonus("1", "Argus Premium lager pivo", "10.27", "13+5 gratis", "0,5 l")],
              [vlm(35, "ARGUS Svetlo pivo", "10.27", lp=False)])
    d = by_erp(res, "1")
    assert any("price meaning not provable" in r for r in reasons(d))


# ---------------------------------------------------------------- dates

def test_tile_end_date_earlier_than_api_ships_with_the_leaflet_date():
    res = run([cand("1", "Rolana panceta", "1.19", "1.99", "40", lp=True, brand="Dulano")],
              [vlm(8, "DULANO SELECTION Rolana panceta", "1.19", lp=True, until="velja do 26. 9.")])
    d = by_erp(res, "1")
    assert not d.is_quarantined and d.valid_to == date(2026, 9, 26)
    assert any(f.startswith("valid_to-from-leaflet") for f in d.flags)


def test_tile_end_date_later_than_api_quarantines():
    res = run([cand("1", "Rolana panceta", "1.19", "1.99", "40", lp=True, brand="Dulano",
                    windows=[(date(2026, 9, 24), date(2026, 9, 26))])],
              [vlm(8, "DULANO SELECTION Rolana panceta", "1.19", lp=True, until="velja do 30. 9.")])
    why = reasons(by_erp(res, "1"))[0]
    assert "api-leaflet-date-conflict" in why and "2026-09-26" in why


def test_tile_end_date_agreeing_with_api_ships():
    res = run([cand("1", "Sveži file lososa", "8.99", "11.99", "25",
                    windows=[(date(2026, 9, 24), date(2026, 9, 26))])],
              [vlm(3, "Svež file lososa", "8.99", until="velja do 26. 9.")])
    assert not by_erp(res, "1").is_quarantined


def test_parse_tile_date():
    assert parse_tile_date("velja do 26. 9.", 2026) == date(2026, 9, 26)
    assert parse_tile_date("od 25.9. do 26.9.", 2026) == date(2026, 9, 26)
    assert parse_tile_date("do 31. 2.", 2026) is None
    assert parse_tile_date(None, 2026) is None


# ---------------------------------------------------------------- OCR old price

@pytest.mark.parametrize("new,old,pct,ok", [
    ("1.99", "3.49", "42", True),    # 42.98% prints -42%
    ("1.99", "3.49", "43", False),   # rounded, not truncated
    ("3.29", "4.69", "29", True),    # 29.85%
    ("3.29", "4.69", "30", False),
    ("2.99", "2.99", "0", False),
    ("3.99", "3.49", "10", False),   # old below new
])
def test_ocr_reconcile_uses_truncated_badge(new, old, pct, ok):
    assert ocr_reconciled(ocr(1, new, old, pct)) is ok


def test_ocr_old_price_disagreeing_with_api_quarantines():
    res = run([cand("1", "Kranjska klobasa", "1.99", "3.29", "39")],
              [vlm(1, "Z'DEŽELE Kranjska klobasa", "1.99"), ocr(1, "1.99", "3.49", "42")])
    assert "api-leaflet-old-price-conflict" in reasons(by_erp(res, "1"))[0]


def test_unreconciled_ocr_old_price_is_ignored_not_a_conflict():
    res = run([cand("1", "Kranjska klobasa", "1.99", "3.49", "42")],
              [vlm(1, "Z'DEŽELE Kranjska klobasa", "1.99"), ocr(1, "1.99", "8.79", "42")])
    assert not by_erp(res, "1").is_quarantined


# ---------------------------------------------------------------- leaflet-only

def leaflet_only(res):
    return [r.deal for r in res.records if r.rule == "leaflet-only"]


def test_vlm_alone_is_quarantined():
    [d] = leaflet_only(run([], [vlm(4, "Bučke", "1.29")]))
    assert "leaflet-only-unverified" in reasons(d)[0]
    assert d.price_old is None and d.discount_published is None


def test_ocr_plus_vlm_reconciled_ships_with_page_validity():
    res = run([], [vlm(4, "Bučke", "1.29"), ocr(4, "1.29", "1.79", "27")],
              page_validity={4: (date(2026, 9, 24), date(2026, 9, 26))})
    [d] = leaflet_only(res)
    assert not d.is_quarantined
    assert (d.name, d.price_new, d.price_old, d.discount_published) == \
           ("Bučke", D("1.29"), D("1.79"), D("27"))
    assert d.valid_to == date(2026, 9, 26)
    assert "leaflet-only-confirmed:ocr+vlm" in d.flags


def test_ocr_plus_vlm_without_page_validity_is_quarantined():
    [d] = leaflet_only(run([], [vlm(4, "Bučke", "1.29"), ocr(4, "1.29", "1.79", "27")]))
    assert "leaflet-only-no-validity" in reasons(d)[0]


def test_ocr_plus_vlm_unreconciled_is_quarantined():
    [d] = leaflet_only(run([], [vlm(4, "Bučke", "1.29"), ocr(4, "1.29", "1.79", "28")],
                           page_validity={4: WEEK}))
    assert "not reconciled" in reasons(d)[0]


def test_ocr_alone_is_quarantined_even_if_reconciled():
    [d] = leaflet_only(run([], [ocr(4, "1.29", "1.79", "27")], page_validity={4: WEEK}))
    assert "leaflet-only-unverified" in reasons(d)[0]


def test_ocr_and_vlm_prices_differ_are_two_unverified_tiles():
    res = run([], [vlm(4, "Bučke", "1.29"), ocr(4, "1.79", "2.29", "21")], page_validity={4: WEEK})
    assert len(leaflet_only(res)) == 2 and all(d.is_quarantined for d in res.deals)


# R8b: a tile that prints only a price (no old price, no badge)

def bare(page, price, name, badge=None, old=None, pct=None):
    """OCR reading carrying the adapter's raw record, so 'saw no badge' is known."""
    return LeafletReading("ocr", page, D(price), D(old) if old else None,
                          D(pct) if pct else None, lidl_plus=False, name=name,
                          bbox=(0, 0, 100, 100), raw={"badge": badge})


def test_plain_price_tile_ships_without_old_price():
    res = run([], [vlm(11, "LAŠKO Svetlo pivo", "4.19"), bare(11, "4.19", "LAŠKO Svetlo pivo")],
              page_validity={11: WEEK})
    [d] = leaflet_only(res)
    assert not d.is_quarantined
    assert (d.price_new, d.price_old, d.discount_published) == (D("4.19"), None, None)
    assert "leaflet-only-confirmed:ocr+vlm:no-old-price" in d.flags


def test_plain_price_tile_with_a_badge_stays_quarantined():
    # FRUC "Z Lidl Plus 4+2 gratis": a flag badge, the price is per pack of 6
    res = run([], [vlm(35, "FRUC Napitek pomaranča-limona", "6.56"),
                   bare(35, "6.56", "FRUC Napitek pomaranča-limona", badge="flag")],
              page_validity={35: WEEK})
    assert "OCR saw a badge (flag) it cannot prove" in reasons(leaflet_only(res)[0])[0]


def test_plain_price_tile_needs_product_words_not_just_the_brand():
    # every Vitasia tile starts "VITASIA": a shared brand does not tie OCR to the tile
    res = run([], [vlm(44, "VITASIA Riževi rezanci", "0.99"),
                   bare(44, "0.99", "VITASIA Stekleni juha")], page_validity={44: WEEK})
    assert all(d.is_quarantined for d in res.deals)


def test_plain_price_tile_one_generic_ocr_word_is_not_enough():
    res = run([], [vlm(24, "SILVERCREST Komplet paličnega mešalnika", "19.99"),
                   bare(24, "19.99", "kompl")], page_validity={24: WEEK})
    assert all(d.is_quarantined for d in res.deals)


def test_plain_price_tile_one_word_product_is_enough():
    res = run([], [vlm(45, "VITASIA Liči", "1.89"), bare(45, "1.89", "VITASIA Liči")],
              page_validity={45: WEEK})
    [d] = leaflet_only(res)
    assert not d.is_quarantined


def test_plain_price_tile_needs_the_adapter_record():
    # no raw record: OCR's opinion on the badge is unknown
    res = run([], [vlm(11, "LAŠKO Svetlo pivo", "4.19"), ocr(11, "4.19", name="LAŠKO Svetlo pivo")],
              page_validity={11: WEEK})
    assert all(d.is_quarantined for d in res.deals)


def test_plain_price_tile_where_ocr_read_an_old_price_is_not_plain():
    res = run([], [vlm(11, "LAŠKO Svetlo pivo", "4.19"),
                   bare(11, "4.19", "LAŠKO Svetlo pivo", badge="discount", old="5.99", pct="20")],
              page_validity={11: WEEK})
    assert all(d.is_quarantined for d in res.deals)


def test_tile_without_price_is_kept_and_quarantined():
    [d] = leaflet_only(run([], [vlm(4, "Knorr juhe", None)]))
    assert "leaflet-no-price" in reasons(d)[0]


def test_same_product_printed_twice_is_a_duplicate_not_a_new_offer():
    res = run([cand("1", "Mango", "0.99", "1.49", "33")],
              [vlm(1, "Mango", "0.99"), vlm(7, "Mango", "0.99")])
    [d] = leaflet_only(res)
    assert "leaflet-duplicate-of:1" in reasons(d)[0]


# ---------------------------------------------------------------- plumbing

def test_vlm_reading_cannot_carry_old_price():
    with pytest.raises(ValueError):
        LeafletReading("vlm", 1, D("1.99"), old_price=D("3.49"))
    with pytest.raises(TypeError):
        LeafletReading("ocr", 1, 1.99)


def test_adapter_from_vlm_reading():
    r = from_vlm(VlmReading(page=3, name="Sveža šarenka", new_price=D("4.69"), lidl_plus=True,
                            unit="1 kg", valid_to_text="velja do 26. 9."))
    assert (r.source, r.page, r.new_price, r.old_price, r.pct, r.lidl_plus, r.bbox) == \
           ("vlm", 3, D("4.69"), None, None, True, None)
    assert r.valid_to_text == "velja do 26. 9."


def test_duplicate_vlm_readings_collapse_into_one_tile():
    tiles = build_tiles([vlm(8, "PICKIES klobasa", "2.99"), vlm(8, "Pickies klobasa", "2.99")])
    assert len(tiles) == 1


def test_name_helpers():
    assert fold("Sveža ŠARENKA") == "sveza sarenka"
    assert split_leaflet_name("ŽAR LEGENDE Svež ribeye steak") == ("ŽAR LEGENDE", "Svež ribeye steak")
    assert split_leaflet_name("SLIM & FIT Poetovio narezek") == ("SLIM & FIT", "Poetovio narezek")
    assert split_leaflet_name("MANGO") == ("", "MANGO")
    assert split_leaflet_name("Mango") == ("", "Mango")


# ---------------------------------------------------------------- brands / ambiguity

def test_contradicting_brands_with_partial_overlap_are_not_a_match():
    res = run([], [vlm(8, "PICKIES Piščančja prekajena klobasa", "2.99")],
              listed=[cand("1", "Hribovska piščančja klobasa", "1.39", brand="Pikok")])
    assert not [r for r in res.records if r.rule == "conflict"]
    [d] = res.deals
    assert "leaflet-only-unverified" in reasons(d)[0]


def test_same_product_words_different_brand_needs_corroboration():
    c = cand("1", "Kavni napitek", "0.55", "0.79", "30", brand="Bellarom")
    res = run([c], [vlm(37, "PILOS Kavni napitek", "0.55")])
    assert "brand-differs name match NOT corroborated" in reasons(by_erp(res, "1"))[0]
    res = run([c], [vlm(37, "PILOS Kavni napitek", "0.55"), ocr(37, "0.55", "0.79", "30")])
    assert not by_erp(res, "1").is_quarantined


def test_brand_differs_never_produces_a_price_conflict():
    res = run([cand("1", "Kavni napitek", "0.65", brand="Bellarom")],
              [vlm(37, "PILOS Kavni napitek", "0.55")])
    assert not [r for r in res.records if r.rule == "conflict"]


def test_tied_candidates_that_agree_ship_merged_without_disputed_old_price():
    res = run([cand("1", "Moška jakna", "13.99", "14.99", "6", brand="Esmara Men")],
              [vlm(1, "ESMARA & ESMARA MEN Jakna", "13.99")],
              listed=[cand("2", "Ženska jakna", "13.99", brand="Esmara")])
    [r] = [r for r in res.records if r.rule == "verified"]
    d = r.deal
    assert not d.is_quarantined
    assert (d.price_new, d.price_old, d.discount_published) == (D("13.99"), None, None)
    assert d.retailer_item_id == "1+2" and d.name == "ESMARA & ESMARA MEN Jakna"
    assert any(f.endswith(":old-price-withheld") for f in d.flags)
    assert len(res.deals) == 1 and not res.unmatched_listed      # "2" is inside the merge


def test_tied_candidates_that_disagree_stay_ambiguous():
    res = run([cand("1", "Moška jakna", "13.99", "14.99", "6", brand="Esmara Men")],
              [vlm(1, "ESMARA & ESMARA MEN Jakna", "13.99")],
              listed=[cand("2", "Ženska jakna", "13.99", brand="Esmara",
                           windows=[(date(2026, 9, 28), date(2026, 9, 30))])])
    [r] = [r for r in res.records if r.rule == "verified"]
    assert any("ambiguous-api-match" in q for q in reasons(r.deal))


# ---------------------------------------------------------------- pack-size corroboration

def cand_pack(*a, pack, **kw):
    c = cand(*a, **kw)
    c.packaging = pack
    return c


def test_brand_only_match_ships_when_exact_pack_size_agrees():
    res = run([cand_pack("1", "Sveža postrv", "4.69", "5.69", "17", lp=True,
                         brand="Naša ribarnica", pack="550 g")],
              [LeafletReading("vlm", 3, D("4.69"), lidl_plus=True, name="NAŠA RIBARNICA Sveža šarenka",
                              unit="550 g / 1 kg = 8.53")])
    d = by_erp(res, "1")
    assert not d.is_quarantined
    assert "name-match-corroborated:pack-size:550g" in d.flags


@pytest.mark.parametrize("unit", ["500 g / 1 kg = 9.38", "pribl. 550 g", "za kg", None])
def test_brand_only_match_with_different_or_approximate_pack_stays_quarantined(unit):
    res = run([cand_pack("1", "Sveža postrv", "4.69", lp=True, brand="Naša ribarnica", pack="550 g")],
              [LeafletReading("vlm", 3, D("4.69"), lidl_plus=True, name="NAŠA RIBARNICA Sveža šarenka",
                              unit=unit)])
    assert "brand-only name match NOT corroborated" in reasons(by_erp(res, "1"))[0]


def test_pack_size_parsing():
    from babaemail.lidl_reconcile import pack_size
    assert pack_size("550 g / 1 kg = 8.53") == "550g"
    assert pack_size("12x85 g") == pack_size("12 x 85 g / 1 kg") == "12x85g"
    assert pack_size("1,5 l") == "1.5l"
    assert pack_size("pribl. 1 kg") is None and pack_size("za kg") is None
    assert pack_size("kos") is None and pack_size(None) is None


# ---------------------------------------------------------------- fabricated range promos

def test_repeated_same_price_same_brand_vlm_tiles_can_never_ship():
    """kw39 p41: the VLM turned one unpriced "-30% z Lidl Plus" Knorr range promo
    into five "Knorr ... 2.69" tiles (2.69 is a neighbour's old price). Even an
    API item that happens to be Knorr at 2.69 must not be confirmed by them."""
    knorr = [vlm(41, f"Knorr {n}", "2.69", lp=True)
             for n in ("Porova juha", "Blejska juha", "Kremna juha", "Kokošja kocka", "Goveja kocka")]
    res = run([cand("1", "Blejska juha", "2.69", "3.85", "30", lp=True, brand="Knorr"),
               cand("2", "Mango", "0.99", "1.49", "33")],
              knorr + [vlm(41, "Mango", "0.99")])
    assert by_erp(res, "1").is_quarantined
    assert "not-in-leaflet-reading" in reasons(by_erp(res, "1"))[0]
    assert not by_erp(res, "2").is_quarantined
    fab = [r.deal for r in res.records if r.rule == "leaflet-only"]
    assert len(fab) == 5 and all("vlm-suspect-repeated-price" in reasons(d)[0] for d in fab)
    assert len(res.shipped) == 1


def test_repeated_prices_confirmed_by_ocr_are_not_suspect():
    tiles = [vlm(41, f"Knorr {n}", "2.69") for n in ("A juha", "B juha", "C juha")]
    res = run([cand("1", "A juha", "2.69", brand="Knorr")],
              tiles + [ocr(41, "2.69", name="Knorr A juha")])
    assert "leaflet-confirmed:ocr+vlm" in by_erp(res, "1").flags


def test_two_same_brand_tiles_at_one_price_are_fine():
    res = run([cand("1", "Smetanov namaz", "0.69", brand="Milbona"),
               cand("2", "Smetana za kuhanje", "0.69", brand="Milbona")],
              [vlm(34, "MILBONA Smetanov namaz", "0.69"), vlm(34, "MILBONA Smetana za kuhanje", "0.69")])
    assert not res.quarantined
def test_two_generic_shared_words_with_own_words_on_both_sides_is_not_a_match():
    res = run([], [vlm(8, "PICKIES Piščančja prekajena klobasa", "2.99")],
              listed=[cand("1", "Piščančja posebna klobasa", "2.99")])
    assert not [r for r in res.records if r.rule in ("verified", "conflict")]


def test_leaflet_name_contained_in_longer_api_name_matches():
    res = run([cand("1", "Domači suhi ocvirki", "2.23", "2.79", "20")],
              [vlm(8, "MESARSTVO OBLAK Suhi ocvirki", "2.23")])
    assert not by_erp(res, "1").is_quarantined


# ---------------------------------------------------------------- page validity inference

def test_leaflet_only_ocr_vlm_uses_page_window_inferred_from_api():
    fri_sat = (date(2026, 9, 25), date(2026, 9, 26))
    res = run([cand("1", "Mango", "0.99", "1.49", "33", windows=[fri_sat]),
               cand("2", "Hruške", "1.39", "1.99", "30", windows=[fri_sat])],
              [vlm(34, "Mango", "0.99"), vlm(34, "Hruške", "1.39"),
               vlm(34, "Bučke", "1.29"), ocr(34, "1.29", "1.79", "27")])
    [d] = leaflet_only(res)
    assert not d.is_quarantined
    assert (d.valid_from, d.valid_to) == fri_sat
    assert any(f.startswith("page-validity-inferred-from-api") for f in d.flags)


def test_no_inference_when_confirmed_items_disagree_on_window():
    res = run([cand("1", "Mango", "0.99", "1.49", "33", windows=[(date(2026, 9, 25), date(2026, 9, 26))]),
               cand("2", "Hruške", "1.39", "1.99", "30")],
              [vlm(34, "Mango", "0.99"), vlm(34, "Hruške", "1.39"),
               vlm(34, "Bučke", "1.29"), ocr(34, "1.29", "1.79", "27")])
    [d] = leaflet_only(res)
    assert "leaflet-only-no-validity" in reasons(d)[0]


def test_no_inference_from_a_single_item():
    res = run([cand("1", "Mango", "0.99", "1.49", "33")],
              [vlm(34, "Mango", "0.99"), vlm(34, "Bučke", "1.29"), ocr(34, "1.29", "1.79", "27")])
    [d] = leaflet_only(res)
    assert "leaflet-only-no-validity" in reasons(d)[0]


def test_api_end_date_after_the_leaflet_is_clipped_to_the_leaflet():
    res = run([cand("1", "Žepek s šunko in sirom", "0.49", "0.69", "28",
                    windows=[(date(2026, 9, 24), date(2026, 10, 3))])],
              [vlm(7, "Žepek s šunko in sirom", "0.49")])
    d = by_erp(res, "1")
    assert not d.is_quarantined and d.valid_to == date(2026, 9, 30)
    assert "valid_to-clipped-to-leaflet:api 2026-10-03" in d.flags


def test_open_ended_api_window_is_clipped_to_the_leaflet():
    res = run([cand("1", "Drobtine", "0.75", "0.85", "11", windows=[(date(2026, 7, 9), None)])],
              [vlm(7, "Drobtine", "0.75")])
    assert by_erp(res, "1").valid_to == date(2026, 9, 30)


# ---------------------------------------------------------------- R0c

def test_vlm_only_tile_on_page_without_ocr_price_is_quarantined():
    # kw39 p5: an app advert with a till receipt; the VLM "reads" a tile off it
    # and a listed API item at the same price and name would confirm it.
    listed = [cand("9", "Rezanci", "1.99")]
    readings = [vlm(5, "Rezanci", "1.99")]
    res = run([], readings, listed=listed, ocr_pages=[5])
    d = by_erp(res, "9")
    assert d.is_quarantined
    assert any("vlm-only-page-without-ocr-price" in r for r in reasons(d))


def test_vlm_only_tile_needs_an_ocr_price_equal_to_its_own():
    # OCR read prices on the page, but none at 1.99: the VLM tile is unbacked.
    readings = [vlm(5, "Rezanci", "1.99"), ocr(5, "3.49", name="Druga stvar")]
    res = run([], readings, listed=[cand("9", "Rezanci", "1.99")], ocr_pages=[5])
    d = by_erp(res, "9")
    assert d.is_quarantined
    assert any("vlm-only-without-ocr-price" in r for r in reasons(d))


def test_vlm_only_tiles_backed_one_each_by_unpaired_ocr_prices_verify():
    # kw39 p58: two 9.99 tiles; OCR read both prices but named them "kos", so
    # neither pairs by name. Two OCR prices back two VLM tiles.
    readings = [vlm(58, "ESMARA Ženska jakna", "9.99"), vlm(58, "ESMARA Ženske kavbojke", "9.99"),
                ocr(58, "9.99", name="kos"), ocr(58, "9.99", name="kos")]
    res = run([cand("1", "Ženska jakna", "9.99", brand="Esmara"),
               cand("2", "Ženske kavbojke", "9.99", brand="Esmara")], readings, ocr_pages=[58])
    assert not by_erp(res, "1").is_quarantined and not by_erp(res, "2").is_quarantined


def test_more_vlm_tiles_than_ocr_prices_at_one_price_quarantines_all_of_them():
    # kw39 p51: the VLM put a second, false 0.89 on the page (the čips print
    # 0.99). OCR read ONE 0.89. Nothing says which VLM tile is the real one.
    readings = [vlm(51, "VITASIA Napitek s semeni bazilike", "0.89"),
                vlm(51, "VITASIA Tapiokin čips", "0.89"), ocr(51, "0.89")]
    res = run([cand("1", "Napitek s semeni bazilike", "0.89"),
               cand("2", "Tapiokin čips", "0.89", brand="VITASIA")], readings, ocr_pages=[51])
    for erp in ("1", "2"):
        assert any("vlm-only-without-ocr-price" in r for r in reasons(by_erp(res, erp)))
    assert not res.shipped


def test_ocr_price_that_confirmed_its_own_api_item_cannot_back_a_vlm_tile():
    # Two OCR 2.99s, but one is spent confirming "Kmečka salama" on its own:
    # one printed price is left for two VLM-only 2.99 tiles.
    readings = [vlm(8, "Prekajena krača", "2.99"), vlm(8, "Svinjska rebra", "2.99"),
                ocr(8, "2.99", name="Kmečka salama"), ocr(8, "2.99")]
    res = run([cand("1", "Prekajena krača", "2.99"), cand("2", "Kmečka salama", "2.99")],
              readings, ocr_pages=[8])
    assert "leaflet-confirmed:ocr" in by_erp(res, "2").flags
    assert any("vlm-only-without-ocr-price" in r for r in reasons(by_erp(res, "1")))


def test_backed_tile_wins_the_api_item_over_an_unbacked_duplicate():
    # kw39 p47: the VLM read the real "Začimbe 0.69" tile AND invented "Wok
    # Spices 0.69" from the packaging. The real tile must get the API item.
    readings = [vlm(47, "VITASIA Začimbe za wok", "0.69"), ocr(47, "0.69", name="VITASIA Začimbe za wok"),
                vlm(47, "VITASIA Začimbe", "0.69")]
    res = run([cand("1", "Začimbe", "0.69", brand="VITASIA")], readings, ocr_pages=[47])
    d = by_erp(res, "1")
    assert not d.is_quarantined and "leaflet-confirmed:ocr+vlm" in d.flags


def test_r0c_is_off_without_ocr_pages():
    res = run([], [vlm(5, "Rezanci", "1.99")], listed=[cand("9", "Rezanci", "1.99")])
    assert not by_erp(res, "9").is_quarantined


# ---------------------------------------------------------------- R2 typo tier

def test_one_missing_letter_still_verifies():
    res = run([cand("1", "Sveži kunec", "8.99")], [vlm(38, "OLIVIA Sveži kunc", "8.99")])
    assert not by_erp(res, "1").is_quarantined
    res = run([cand("2", "Masleni rogljiček", "0.29", "0.42", "30")],
              [vlm(6, "Masleni rogliček", "0.29")])
    assert not by_erp(res, "2").is_quarantined


def test_a_changed_letter_is_a_different_product():
    res = run([cand("1", "Piščančje mleko", "1.99")], [vlm(3, "Piščančje mleto", "1.99")])
    assert any("not-in-leaflet-reading" in r for r in reasons(by_erp(res, "1")))


def test_typo_tier_needs_the_same_number_of_words():
    res = run([cand("1", "Sveži kunec", "8.99")], [vlm(38, "Sveži kunc s prilogo", "8.99")])
    assert any("not-in-leaflet-reading" in r for r in reasons(by_erp(res, "1")))


# ---------------------------------------------------------------- R6b website-only

def test_website_only_is_off_by_default():
    res = run([cand("1", "SONDEY Linški keksi", "1.99", "2.19")], [])
    assert any("not-in-leaflet-reading" in r for r in reasons(by_erp(res, "1")))


def test_website_only_ships_flagged_when_switched_on():
    res = run([cand("1", "SONDEY Linški keksi", "1.99", "2.19")], [], ship_website_only=True)
    d = by_erp(res, "1")
    assert not d.is_quarantined and "website-only" in d.flags


def test_website_only_needs_an_old_price_and_a_clean_record():
    res = run([cand("1", "Sveži kunec", "8.99"),
               cand("2", "Tastino", "1.79", "2.59", flags=["sold_out:x"]),
               cand("3", "Sadna pijača", "6.56", conditional="4+2 gratis")],
              [], ship_website_only=True)
    assert all(d.is_quarantined for d in res.deals)


# ---------------------------------------------------------------- R3b / R4c / R7c (2026-09-27)

def test_uncorroborated_brand_match_leaves_the_proven_tile_to_the_leaflet():
    """kw39 p50: API brand-only "VITASIA Rižev kis" 0.79 vs the printed, twice-read
    "VITASIA Zeleni čaj" 0.79. The API item's name and fields must not be used;
    the tile ships on the leaflet's own proof (R8b) and the API item is held."""
    kis = cand("1", "Rižev kis", "0.79", brand="VITASIA")
    kis.packaging = "250 ml"
    res = run([kis], [vlm(50, "VITASIA Zeleni čaj", "0.79"),
                      bare(50, "0.79", "VITASIA Zeleni čaj")], page_validity={50: WEEK})
    [tile] = leaflet_only(res)
    assert not tile.is_quarantined and tile.name == "VITASIA Zeleni čaj"
    assert tile.price_new == D("0.79") and tile.price_old is None
    api = by_erp(res, "1")
    assert api.is_quarantined and "NOT corroborated" in reasons(api)[0]
    assert len(res.shipped) == 1


def test_badge_pct_resolves_a_tie_between_same_name_same_price_candidates():
    """kw39 p57: printed "-34%" jacket 14.99; the API has a -34% offer (old 22.99)
    and an un-discounted 14.99 jacket of the previous week, same name."""
    offer = cand("1", "Moška jakna", "14.99", "22.99", "34", brand="Esmara Men",
                 windows=[(date(2026, 9, 28), date(2026, 9, 30))])
    other = cand("2", "Moška jakna", "14.99", brand="Esmara Men",
                 windows=[(date(2026, 9, 24), date(2026, 9, 26))])
    readings = [vlm(57, "ESMARA MEN Moška jakna", "14.99"), ocr(57, "14.99", pct="34")]
    for order in ([offer], [other]):
        res = run(order, readings, listed=[other] if order == [offer] else [offer])
        [r] = [r for r in res.records if r.rule == "verified"]
        d = r.deal
        assert not d.is_quarantined, d.flags
        assert d.retailer_item_id == "1"
        assert (d.price_old, d.discount_published) == (D("22.99"), D("34"))
        assert any(f.startswith("ambiguous-api-match-resolved-by-badge:-34%") for f in d.flags)


def test_badge_pct_does_not_resolve_when_it_fits_none_or_the_numbers_fail():
    a = cand("1", "Moška jakna", "14.99", "22.99", "34", brand="Esmara Men",
             windows=[(date(2026, 9, 28), date(2026, 9, 30))])
    b = cand("2", "Moška jakna", "14.99", brand="Esmara Men",
             windows=[(date(2026, 9, 24), date(2026, 9, 26))])
    for pct in ("30", None):
        res = run([a], [vlm(57, "ESMARA MEN Moška jakna", "14.99"), ocr(57, "14.99", pct=pct)],
                  listed=[b])
        [r] = [r for r in res.records if r.rule == "verified"]
        assert any("ambiguous-api-match" in q for q in reasons(r.deal))
    bad = cand("1", "Moška jakna", "14.99", "22.99", "40", brand="Esmara Men",   # 34.8% != 40
               windows=[(date(2026, 9, 28), date(2026, 9, 30))])
    res = run([bad], [vlm(57, "ESMARA MEN Moška jakna", "14.99"), ocr(57, "14.99", pct="40")],
              listed=[b])
    [r] = [r for r in res.records if r.rule == "verified"]
    assert any("ambiguous-api-match" in q for q in reasons(r.deal))


def _gelati(flags=("deletedPrice_differs:3.99",)):
    return cand("1", "Sladoled na palčki", "1.29", "2.59", lp=True, brand="Gelatelli",
                conditional="-50% na drugi kos", flags=flags)


def _gelati_ocr(old):
    # the adapter withholds old/% from a conditional badge's reading; raw keeps it
    return LeafletReading("ocr", 10, D("1.29"), lidl_plus=True, name="BON GELATI Sladoled na palčki",
                          bbox=(0, 0, 100, 100), raw={"badge": "discount", "old_price": old})


def test_leaflet_old_price_settles_the_api_deletedprice_doubt():
    res = run([_gelati()], [vlm(10, "BON GELATI Sladoled na palčki", "1.29", lp=True),
                            _gelati_ocr("2.59")])
    d = by_erp(res, "1")
    assert not d.is_quarantined, d.flags
    assert d.condition_kind == "second-item" and (d.price_new, d.price_old) == (D("1.29"), D("2.59"))
    assert "api-flag-settled-by-leaflet:deletedPrice_differs:3.99" in d.flags


@pytest.mark.parametrize("old", ["3.99", None])
def test_deletedprice_doubt_stays_when_the_leaflet_does_not_settle_it(old):
    res = run([_gelati()], [vlm(10, "BON GELATI Sladoled na palčki", "1.29", lp=True),
                            _gelati_ocr(old)])
    assert "api-flag:deletedPrice_differs:3.99" in reasons(by_erp(res, "1"))[0]


def test_deletedprice_doubt_on_plain_discount_needs_reconciled_ocr():
    c = cand("1", "Mango", "0.99", "1.49", "33", flags=["deletedPrice_differs:1.99"])
    res = run([c], [vlm(1, "Mango", "0.99"), ocr(1, "0.99", "1.49", "33", name="Mango")])
    assert not by_erp(res, "1").is_quarantined
    res = run([c], [vlm(1, "Mango", "0.99"), ocr(1, "0.99", "1.49", None, name="Mango")])
    assert by_erp(res, "1").is_quarantined


def test_inferred_page_window_is_clipped_to_the_leaflet_like_api_items():
    """kw39 Vitasia pages print no end; the API says 28.9.-3.10.; the leaflet ends
    30.9. A leaflet-only tile must not outlive what an API item on the same page
    may show (R4 clips those to 30.9.)."""
    nxt = (date(2026, 9, 28), date(2026, 10, 3))
    res = run([cand("1", "Mango", "0.99", "1.49", "33", windows=[nxt]),
               cand("2", "Hruške", "1.39", "1.99", "30", windows=[nxt])],
              [vlm(50, "Mango", "0.99"), vlm(50, "Hruške", "1.39"),
               vlm(50, "Bučke", "1.29"), ocr(50, "1.29", "1.79", "27")])
    [d] = leaflet_only(res)
    assert not d.is_quarantined
    assert (d.valid_from, d.valid_to) == (date(2026, 9, 28), date(2026, 9, 30))
    assert "valid_to-clipped-to-leaflet:page 2026-10-03" in d.flags
    assert all(x.valid_to == date(2026, 9, 30) for x in res.shipped)


def test_two_prints_of_one_product_each_get_the_item_their_badge_fits():
    """kw39: the same "ESMARA MEN Moška jakna" 14.99 is printed plain on p33
    (Thursday section) and "-34%" on p57 (Monday section). The API has a plain
    14.99 item (24.-26.9.) and a -34% item (28.-30.9.)."""
    plain = cand("2", "Moška jakna", "14.99", brand="Esmara Men",
                 windows=[(date(2026, 9, 24), date(2026, 9, 26))])
    offer = cand("1", "Moška jakna", "14.99", "22.99", "34", brand="Esmara Men",
                 windows=[(date(2026, 9, 28), date(2026, 9, 30))])
    res = run([offer], [vlm(33, "ESMARA MEN Moška jakna", "14.99"),
                        bare(33, "14.99", "ESMARA MEN Moška jakna"),
                        vlm(57, "ESMARA MEN Moška jakna", "14.99"),
                        ocr(57, "14.99", pct="34")], listed=[plain])
    got = {r.tile.page: r.deal for r in res.records if r.rule == "verified"}
    assert not got[33].is_quarantined and not got[57].is_quarantined
    assert got[33].retailer_item_id == "2" and got[33].price_old is None
    assert got[57].retailer_item_id == "1" and got[57].price_old == D("22.99")
    assert got[33].valid_to == date(2026, 9, 26) and got[57].valid_from == date(2026, 9, 28)
