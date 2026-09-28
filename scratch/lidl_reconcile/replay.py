"""Re-run reconcile() on the captured full-leaflet inputs (capture_run.py) and
print one line per shipped deal, for diffing rule changes in seconds.
    .venv/bin/python scratch/lidl_reconcile/replay.py [--page N]"""
import pickle, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from babaemail.lidl_reconcile import reconcile
from babaemail.sources.lidl import read_page_validities
inp = pickle.load(open(HERE / "full_inputs_kw39.pkl", "rb"))
if len(sys.argv) > 2 and sys.argv[1] == "--page":
    p = int(sys.argv[2])
    for r in inp["readings"]:
        if r.page == p:
            print(r.source, r.new_price, r.old_price, r.pct, r.lidl_plus, repr(r.name), r.unit, r.bbox)
    sys.exit()
if "--crops" in sys.argv:
    from babaemail.lidl_crops import load_crops, refine_readings
    pages = HERE.parent / "lidl_truth" / "pages"
    evidence = load_crops({int(p.stem[1:]): p.read_bytes() for p in pages.glob("p*.png")})
    inp["readings"] = refine_readings(inp["readings"], evidence)
pages = HERE.parent / "lidl_truth" / "pages"
page_bytes = {int(p.stem[1:]): p.read_bytes() for p in pages.glob("p*.png")}
page_validity = read_page_validities(page_bytes, inp["kw"]["leaflet_window"])
rr = reconcile(inp["cands"], inp["readings"], **inp["kw"],
               page_validity=page_validity, ship_website_only=True)
website = [d for d in rr.deals if d.source_kind == "website_only"]
leaflet_shipped = [d for d in rr.shipped if d.source_kind == "leaflet"]
leaflet_held = [d for d in rr.quarantined if d.source_kind == "leaflet"]
print(f"leaflet shipped {len(leaflet_shipped)} quarantined {len(leaflet_held)}; "
      f"website-only shipped {sum(not d.is_quarantined for d in website)} "
      f"held {sum(d.is_quarantined for d in website)}")
for d in sorted(leaflet_shipped, key=lambda d: (d.source_page or 0, d.name)):
    src = next((f for f in d.flags if "confirmed:" in f), "")
    print(f"p{d.source_page} {d.price_new} {getattr(d, 'price_unit', None) or ''} {d.name!r} "
          f"old={d.price_old} pct={d.discount_published} {d.valid_from}..{d.valid_to}"
          f"{' LP' if d.loyalty_required else ''}{' [' + d.condition + ']' if d.condition_kind else ''} {src}")
print("website-only (Lidl website, not leaflet):")
for d in website:
    state = "HELD" if d.is_quarantined else "SHIPPED"
    reasons = [f.removeprefix("quarantine:") for f in d.flags if f.startswith("quarantine:")]
    print(f"{state} {d.price_new} {d.name!r} old={d.price_old} pct={d.discount_display} "
          f"{d.valid_from}..{d.valid_to}"
          + (f" reason={' | '.join(reasons)}" if reasons else ""))
