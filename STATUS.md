# STATUS — read this first when picking the project back up

Last session: 2026-09-13. Phase 0 (extraction) is largely done. Nothing
user-facing exists yet — no email has ever been generated or sent.

## OPEN DECISION — "the Lidl call" (blocks the next step)

Free OCR for Lidl plateaus at **~11% usable** (34 of 38 price groups
quarantined over six pages). Two engines and a Slovenian language model were
tried. Prices that ARE read are correct; most are never found. The bottleneck
is no longer recognition but DETECTION and PAIRING — locating each tile's badge
and old price and attaching them to the right product.

Three options, to be decided by Aleks:

1. **Ship the four, leave Lidl out** (recommended last session). Mercator,
   Spar, Tuš and Hofer all work properly. Lidl stays in the repo quarantining
   everything it cannot verify, so nothing wrong can reach grandma — it just
   contributes little. She already has the Lidl Plus app, which covers Lidl
   better than this ever could, including personalised coupons we cannot see.
2. **Vision API for Lidl only**, ~EUR 1.50–3.50/month. A leaflet is read ONCE
   per leaflet, not per day, so the daily schedule adds nothing. Aleks ruled
   this out earlier, BEFORE knowing free OCR would plateau at 11%. The
   reconcile gate would still quarantine any misread.
3. **Keep grinding Lidl detection/pairing.** Substantial work, no guarantee,
   and it delays everything downstream.

Second question, for after that: what to build next —
**audit.py** (the accuracy gate Aleks set: prints every extracted item beside
the rendered leaflet page so it can be checked by hand against the physical
reklame), an **end-to-end email into Aleks's inbox**, or **verify.py + tests**
first. Aleks did not get to answer this.

## What works now

| Retailer | Source | Items | Declared | Quarantined | Discounts |
|---|---|---|---|---|---|
| Mercator | dated PDF, text layer | 228 | 228 | 8 (3.5%) | 220 |
| Spar | letak.spar.si PDF, text layer | 209 | 209 | 26 (12.4%) | 183 |
| Tuš | dated PDF, text layer | 377 | 377 | 68 (18.0%) | 132 |
| Hofer | Publitas PDF, text layer | 163 | 163 | 27 (16.6%) | 46 |
| **Total** | | **977** | **977** | **129 (13.2%)** | **581** |
| Lidl | image-only + OCR | — | — | ~89% | ~11% usable |

`declared` is the independent completeness count and matches extraction exactly
for all four. Run any adapter with:

    .venv/Scripts/python.exe -c "from datetime import date; \
      from babaemail.sources.mercator import Mercator; \
      r=Mercator().fetch(date.today()); print(r.status, len(r.items))"

## Built

- `babaemail/models.py` — Deal / SourceResult. Decimal money, optional prices
  for conditional offers, quarantine separate from informational flags.
- `babaemail/pdftext.py` — the shared extraction engine. Badge and price
  anchoring, Voronoi label partitioning, multi-line name assembly,
  percentage-vs-cents separation, repeated-label filtering.
- `babaemail/ocr.py` — free local OCR. RapidOCR for detection, Tesseract+slv
  for digit strips and Slovenian text.
- `babaemail/sources/` — base, mercator, spar, tus, hofer, lidl.
- `FINDINGS.md` — all source research and every trap found. **Read it before
  touching a source adapter.**

## NOT built

`verify.py`, `audit.py`, `categorize.py`, `select.py`, `store.py`, `render.py`,
`mailer.py`, `main.py`, `preferences.yaml`, templates, tests, CI workflow.

Per CLAUDE.md's model gate, everything remaining except audit.py and verify.py
is Sonnet work.

## Still owed to grandma (from the plan)

`preferences.yaml` must be built FROM A CONVERSATION WITH HER, not guessed. The
interview guide is in the plan file. Its first question is the important one:
**does she actually read email at all?** That is still unvalidated and the whole
design rests on it.

## Environment

Python 3.14 in `.venv/` (no `python` on PATH — use `py` or `.venv/Scripts/`).
Always `PYTHONIOENCODING=utf-8`; Slovenian text crashes Windows cp1252 stdout.
Tesseract 5.4 installed; `slv.traineddata` is in `tessdata/` (gitignored, 9.9MB
— re-download from tesseract-ocr/tessdata if missing).
