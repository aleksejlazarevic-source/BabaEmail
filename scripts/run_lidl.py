"""Run the Lidl source for real and write out/lidl_<leaflet>.json + a summary.

    .venv/bin/python scripts/run_lidl.py [--date 2026-09-24] [--no-vlm]

Full pipeline: flyer -> PDF -> render -> OCR (parallel) -> VLM (ollama,
cached in .cache/vlm; started and stopped automatically if not running) ->
lidl.si API -> reconcile. --no-vlm is a diagnostic API+OCR run; its result is
always a failure, never shippable.

Environment: BABAEMAIL_VLM_MODEL, BABAEMAIL_OLLAMA_URL, BABAEMAIL_OLLAMA_AUTOSTART,
BABAEMAIL_OCR_WORKERS, BABAEMAIL_VLM_CACHE.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import sys
import time
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from babaemail.sources.lidl import Lidl  # noqa: E402


def _plain(v):
    if isinstance(v, (Decimal, date, Path)):
        return str(v) if not isinstance(v, date) else v.isoformat()
    if hasattr(v, "value") and v.__class__.__name__.endswith("Status"):
        return v.value
    raise TypeError(type(v))


def reason_key(flag: str) -> str:
    """'quarantine:api-leaflet-price-conflict: api 6.49, ...' -> 'api-leaflet-price-conflict'."""
    m = re.match(r"[\w-]+", flag.removeprefix("quarantine:"))
    return m.group(0) if m else flag


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=date.fromisoformat, default=date.today())
    ap.add_argument("--no-vlm", action="store_true", help="API+OCR only (diagnostic, never shippable)")
    ap.add_argument("--out", type=Path, default=ROOT / "out")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        datefmt="%H:%M:%S")

    src = Lidl(use_vlm=not args.no_vlm)
    t0 = time.monotonic()
    res = src.fetch(args.date)
    elapsed = time.monotonic() - t0

    rr = src.last_reconcile
    leaflet = res.leaflet_id or src.flyer_identifier(args.date)
    args.out.mkdir(parents=True, exist_ok=True)
    shipped = [d for d in res.items if not d.is_quarantined]
    quarantined = [d for d in res.items if d.is_quarantined]
    reasons = Counter(reason_key(f) for d in quarantined for f in d.flags
                      if f.startswith("quarantine:"))
    rules = Counter(r.rule for r in rr.records) if rr else Counter()

    doc = {
        "retailer": res.retailer, "status": res.status.value, "error": res.error,
        "contributes": res.contributes, "leaflet_id": leaflet, "leaflet_title": res.leaflet_title,
        "valid_from": res.valid_from, "valid_to": res.valid_to, "source_url": res.source_url,
        "pages_declared": res.pages_declared, "pages_parsed": res.pages_parsed,
        "run_date": args.date, "use_vlm": not args.no_vlm, "model": src.vlm_model,
        "elapsed_s": round(elapsed, 1), "stats": src.last_stats,
        "counts": {"items": len(res.items), "shipped": len(shipped),
                   "quarantined": len(quarantined), "by_rule": dict(rules),
                   "quarantine_reasons": dict(reasons.most_common())},
        "shipped": [dataclasses.asdict(d) for d in shipped],
        "quarantined": [dataclasses.asdict(d) for d in quarantined],
        "unmatched_listed": [{"erp": c.erp, "name": c.name, "price": c.new_price}
                             for c in (rr.unmatched_listed if rr else [])],
    }
    suffix = "_novlm" if args.no_vlm else ""
    out_json = args.out / f"lidl_{leaflet}{suffix}.json"
    out_json.write_text(json.dumps(doc, default=_plain, ensure_ascii=False, indent=1), "utf-8")

    lines = [
        f"Lidl {leaflet} ({res.leaflet_title or '-'}) valid {res.valid_from}..{res.valid_to}",
        f"status {res.status.value}" + (f": {res.error}" if res.error else "")
        + f"   contributes={res.contributes}",
        f"pages parsed {res.pages_parsed}/{res.pages_declared}   time {elapsed / 60:.1f} min  "
        + "  ".join(f"{k}={v}" for k, v in src.last_stats.items() if k.endswith("_s")),
        f"items {len(res.items)}: shipped {len(shipped)}, quarantined {len(quarantined)}"
        + (f"   records by rule: {dict(rules)}" if rules else ""),
        "quarantine reasons (an item can have several):",
        *[f"  {n:>4}  {k}" for k, n in reasons.most_common()],
        "",
        "shipped:",
        *[f"  p{d.source_page or '?':<3} {d.price_new!s:>7}"
          + (f" (was {d.price_old}, -{d.discount_published}%)" if d.price_old else "")
          + f"{'  [Lidl Plus]' if d.loyalty_required else ''}  {d.name}  {d.valid_from}..{d.valid_to}"
          for d in sorted(shipped, key=lambda d: (d.source_page or 0, d.name))],
    ]
    summary = "\n".join(lines) + "\n"
    (args.out / f"lidl_{leaflet}{suffix}_summary.txt").write_text(summary, "utf-8")
    print(summary)
    print(f"wrote {out_json}")
    return 0 if res.contributes else 1


if __name__ == "__main__":
    sys.exit(main())
