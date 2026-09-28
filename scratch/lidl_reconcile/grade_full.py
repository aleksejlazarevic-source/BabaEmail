"""Grade the FULL 64-page kw39 leaflet against all 293 hand-verified truth
offers (tune + holdout + full-leaflet parts A-D), reusing the same pipeline as
`replay.py --crops` and the same match-then-score algorithm as `eval.py`.

    .venv/bin/python scratch/lidl_reconcile/grade_full.py

Prints, per truth offer: SHIPPED-OK (with any HARMFUL field diff: old price, %,
LP, or a later end date than the leaflet's own), SHIPPED-WRONG, HELD (the
quarantine rule and every reason, plus which readers saw the tile), or
MISSING. Also lists shipped records that match no truth offer, and ends with a
summary line. Writes grade_full.txt next to this file.

Reuses: `babaemail.lidl_reconcile.reconcile` (the pipeline), `_shared`/`_stems`
(name similarity, exactly as eval.py uses them for matching), and
`babaemail.lidl_crops.load_crops`/`refine_readings` (the crop evidence
replay.py --crops applies). The per-truth-offer match-and-score loop is
eval.py's algorithm, copied verbatim (not refactored, since eval.py runs
argparse at import time and cannot be imported cleanly) but expanded to run
over ALL 293 offers/64 pages instead of the 20 tune+holdout pages.
"""

from __future__ import annotations

import json
import pickle
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from babaemail.lidl_crops import load_crops, refine_readings  # noqa: E402
from babaemail.lidl_reconcile import _shared, _stems, reconcile  # noqa: E402

TRUTH_DIRS = [
    ROOT / "scratch" / "lidl_truth",
    ROOT / "scratch" / "lidl_truth_holdout",
    ROOT / "scratch" / "lidl_truth_full" / "partA",
    ROOT / "scratch" / "lidl_truth_full" / "partB",
    ROOT / "scratch" / "lidl_truth_full" / "partC",
    ROOT / "scratch" / "lidl_truth_full" / "partD",
]
PAGES_DIR = ROOT / "scratch" / "lidl_truth" / "pages"
EXPECT_OFFERS = 293

out_lines: list[str] = []


def say(s: str = "") -> None:
    print(s)
    out_lines.append(s)


# ---------------------------------------------------------------- truth
def load_truth() -> list[dict]:
    """Merge every truth.json's `offers` list. Each part's pages are disjoint
    (partition of the 64-page leaflet), so offer ids never collide."""
    offers: list[dict] = []
    seen_pages: dict[int, str] = {}
    for d in TRUTH_DIRS:
        t = json.load(open(d / "truth.json"))
        for k in t["pages"]:
            p = int(k)
            if p in seen_pages:
                raise AssertionError(f"page {p} appears in both {seen_pages[p]} and {d.name}")
            seen_pages[p] = d.name
        offers += [dict(o, _truth_set=d.name) for o in t["offers"]]
    ids = [o["id"] for o in offers]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate offer ids across truth sets: {dupes}"
    return sorted(offers, key=lambda o: (o["page"], o["id"]))


def end_date(text):
    """Copied from eval.py: 'Od cetrtka, 24. 9. do 30. 9.' -> 30.9.;
    'Od cetrtka, 24. 9.' -> None (no end)."""
    if not text:
        return None
    m = re.findall(r"do\s+(\d{1,2})\.\s*(\d{1,2})\.", text)
    from datetime import date
    return date(2026, int(m[-1][1]), int(m[-1][0])) if m else None


def full(o: dict) -> str:
    return f"{o['brand'] or ''} {o['name']}".strip()


def sim(a, b) -> int:
    return _shared(_stems(a), _stems(b))


def main() -> None:
    offers = load_truth()
    assert len(offers) == EXPECT_OFFERS, (
        f"expected {EXPECT_OFFERS} truth offers, found {len(offers)} "
        "(a truth.json changed, or a truth dir is missing from TRUTH_DIRS)")

    # -------------------------------------------------- run the pipeline
    inp = pickle.load(open(HERE / "full_inputs_kw39.pkl", "rb"))
    page_bytes = {int(p.stem[1:]): p.read_bytes() for p in PAGES_DIR.glob("p*.png")}
    evidence = load_crops(page_bytes)
    readings = refine_readings(inp["readings"], evidence)
    res = reconcile(inp["cands"], readings, **inp["kw"], ship_website_only=True)

    website = [d for d in res.deals if d.source_kind == "website_only"]
    leaflet_shipped = [d for d in res.shipped if d.source_kind == "leaflet"]
    leaflet_held = [d for d in res.quarantined if d.source_kind == "leaflet"]

    say("=" * 78)
    say("Full 64-page kw39 leaflet: api+vlm+ocr+crops (replay.py --crops pipeline)")
    say("=" * 78)
    say(f"pipeline leaflet: shipped {len(leaflet_shipped)}  quarantined {len(leaflet_held)}")
    say(f"pipeline website-only: shipped {sum(not d.is_quarantined for d in website)}  "
        f"held {sum(d.is_quarantined for d in website)}")
    say(f"truth: {len(offers)} priced offers, {len({o['page'] for o in offers})} priced pages, "
        f"{len(TRUTH_DIRS)} truth sets merged: " + ", ".join(d.name for d in TRUTH_DIRS))
    say()

    # -------------------------------------------------- match (eval.py's algorithm)
    # Website-only records have no leaflet truth counterpart by definition.
    # Keep them wholly outside the leaflet match/scoring algorithm.
    recs = [r for r in res.records if r.deal.source_kind == "leaflet"]
    rec_used: set[int] = set()
    rows: list[tuple[dict, object, str]] = []
    for o in offers:
        tp = Decimal(o["new_price"])
        best, best_key = None, None
        for i, r in enumerate(recs):
            if i in rec_used:
                continue
            if r.tile is not None and r.tile.page == o["page"]:
                s = sim(r.tile.name, full(o))
                same = r.tile.new_price == tp
                if not same and s < 2:
                    continue
                key = (2, same, s, not r.deal.is_quarantined)
            elif r.tile is None and r.candidate is not None:
                s = sim(r.candidate.name, full(o))
                same = r.candidate.new_price == tp
                if s < 1 or (not same and s < 2):
                    continue
                key = (1, same, s, not r.deal.is_quarantined)
            else:
                continue
            if best_key is None or key > best_key:
                best, best_key = i, key
        if best is None:
            rows.append((o, None, "MISSING"))
            continue
        rec_used.add(best)
        r = recs[best]
        d = r.deal
        if d.is_quarantined:
            rows.append((o, r, "QUARANTINED"))
        elif d.price_new == tp:
            rows.append((o, r, "SHIPPED-OK"))
        else:
            rows.append((o, r, "SHIPPED-WRONG"))

    # -------------------------------------------------- per-offer report
    cnt = Counter(k for _, _, k in rows)
    cnt["SHIPPED-FIELD"] = 0
    cnt["SHIPPED-NAME"] = 0
    say("== Every truth offer ==")
    for o, r, k in rows:
        label = f"p{o['page']:<3}{o['new_price']:>7}  {full(o)!r}"
        if k == "MISSING":
            say(f"{label:<60} MISSING (LP={o['lidl_plus']})")
            continue
        d = r.deal
        tile = r.tile
        if tile is not None:
            readers = "+".join(tile.sources)
        elif r.candidate is not None:
            readers = "none (matched an API-only record: no leaflet tile paired with it)"
        else:
            readers = "?"
        if k == "SHIPPED-WRONG":
            say(f"{label:<60} SHIPPED-WRONG truth={o['new_price']} shipped={d.price_new} "
                f"readers=[{readers}]")
            continue
        if k == "QUARANTINED":
            qs = [f.removeprefix("quarantine:") for f in d.flags if f.startswith("quarantine:")]
            say(f"{label:<60} HELD [{r.rule}] readers=[{readers}]")
            for q in qs:
                say(f"    - {q}")
            continue
        # SHIPPED-OK: same secondary field checks as eval.py
        probs = []
        t_old = Decimal(o["old_price"]) if o["old_price"] else None
        t_pct = Decimal(o["discount_pct"]) if o["discount_pct"] else None
        if d.price_old != t_old:
            probs.append(f"old api {d.price_old} vs leaflet {t_old}")
        if d.condition_kind == "second-item":
            if t_pct is None or f"-{t_pct}% na drugi kos" != d.condition \
                    or f"-{t_pct}% na drugi kos" not in (o.get("conditions") or ""):
                probs.append(f"second-item condition {d.condition!r} vs leaflet {t_pct} "
                             f"{o.get('conditions')!r}")
        elif d.discount_published != t_pct:
            probs.append(f"pct api {d.discount_published} vs leaflet {t_pct}")
        if d.condition_kind == "bonus-pack" and d.condition not in (o.get("conditions") or ""):
            probs.append(f"bonus-pack {d.condition!r} vs leaflet {o.get('conditions')!r}")
        if bool(d.loyalty_required) != o["lidl_plus"]:
            probs.append(f"LP api {d.loyalty_required} vs leaflet {o['lidl_plus']}")
        tile_end = end_date(o["valid_on_tile"]) or end_date(o["page_header_validity"])
        harmful = bool(probs)
        if not _shared(_stems(o["name"]), _stems(d.name)):
            cnt["SHIPPED-NAME"] += 1
            probs.append(f"NAME-DIFF shipped {d.name!r} vs leaflet {full(o)!r}")
        if d.valid_to != tile_end:
            later = tile_end is not None and (d.valid_to is None or d.valid_to > tile_end)
            harmful |= later
            probs.append(f"valid_to shipped {d.valid_to} vs leaflet {tile_end}"
                         + (" <-- LATER than the leaflet" if later else " (earlier: conservative)"))
        if harmful:
            cnt["SHIPPED-FIELD"] += 1
        tag = "SHIPPED-OK" + (" HARMFUL" if harmful else "")
        say(f"{label:<60} {tag} readers=[{readers}]")
        for p in probs:
            say(f"    - {p}")
    say()

    # -------------------------------------------------- shipped, matches nothing
    say("== Shipped deals that match NO truth offer ==")
    matched = {id(r.deal) for _, r, _ in rows if r is not None}
    extra = [d for d in leaflet_shipped if id(d) not in matched]
    for d in extra:
        say(f"  p{d.source_page} {d.name} {d.price_new} flags={d.flags}")
    if not extra:
        say("  none")
    say()

    say("== Lidl website-only deals (separate from leaflet grading) ==")
    for d in website:
        state = "HELD" if d.is_quarantined else "SHIPPED"
        reasons = [f.removeprefix("quarantine:") for f in d.flags
                   if f.startswith("quarantine:")]
        say(f"  {state:<7} {d.name!r} price={d.price_new} old={d.price_old} "
            f"pct={d.discount_display}% dates={d.valid_from}..{d.valid_to}"
            + (f" reason={' | '.join(reasons)}" if reasons else ""))
    if not website:
        say("  none")
    say()

    # -------------------------------------------------- summary
    say("=" * 78)
    say(f"{'outcome':<16}{'n':>4}")
    for k in ("SHIPPED-OK", "SHIPPED-WRONG", "SHIPPED-FIELD", "QUARANTINED", "MISSING",
              "SHIPPED-NAME"):
        say(f"{k:<16}{cnt.get(k, 0):>4}")
    say()
    ok, wrong = cnt.get("SHIPPED-OK", 0), cnt.get("SHIPPED-WRONG", 0)
    say(f"SUMMARY: {ok}/{len(offers)} truth offers ship OK, {wrong} wrong, "
        f"{cnt.get('QUARANTINED', 0)} held, {cnt.get('MISSING', 0)} missing, "
        f"{cnt.get('SHIPPED-FIELD', 0)} shipped-OK with a harmful field diff, "
        f"{len(extra)} shipped records match no truth offer.")

    (HERE / "grade_full.txt").write_text("\n".join(out_lines) + "\n", "utf-8")


if __name__ == "__main__":
    main()
