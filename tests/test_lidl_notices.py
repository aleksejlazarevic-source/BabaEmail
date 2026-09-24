"""Leaflet notices (lidl_notices): shown only when the fine print proves them."""

from datetime import date

from babaemail.lidl_notices import find_notices, senior_day

WEEK = (date(2026, 9, 24), date(2026, 9, 30))

# kw39 p57 as Tesseract read it (abridged, OCR noise kept)
P57 = ("\"Dosedanje cene so naše redne prodajne cene pu r Upokojenci ga lahko izkoristite "
       "ob nakupu Š na dan 30. 9. 2026 v vseh Lidlovih trgovinah. m : Kupec, ki ob nakupu "
       "3 0.9 - e P y 026 pri blagajni katerekoli Lidlove trgovine na območju Republike "
       "Slovenije predloži dokazilo o statusu upokojenca (upokojenska kartica ali pokojninski "
       "odrezek), prejme 10 % popust na posamezen nakup\". Uveljavljanje pN :\\' popusta: "
       "10 % popust je mogoče uveljaviti zgolj na nakup, opravljen na ta dan.")


def test_kw39_senior_day_is_proven():
    n = senior_day(57, P57, WEEK)
    assert n.proven and (n.day, n.pct) == (date(2026, 9, 30), 10)
    assert "30. 9. 2026" in n.text and "10 %" in n.text


def test_pages_without_pensioners_have_no_notice():
    assert find_notices({1: "Svež piščančji file 5.99", 2: ""}, WEEK) == []


def test_percentage_read_once_is_held_back():
    n = senior_day(57, P57.replace("10 % popust je", "popust je"), WEEK)
    assert not n.proven and "only once" in n.reason


def test_disagreeing_percentages_are_held_back():
    n = senior_day(57, P57.replace("10 % popust je", "16 % popust je"), WEEK)
    assert not n.proven and "disagree" in n.reason


def test_date_outside_the_leaflet_is_held_back():
    n = senior_day(57, P57.replace("30. 9. 2026", "30. 10. 2026"), WEEK)
    assert not n.proven and "outside" in n.reason


def test_no_date_is_held_back_never_dropped():
    [n] = find_notices({57: P57.replace("na dan 30. 9. 2026", "")}, WEEK)
    assert not n.proven and n.text is None
