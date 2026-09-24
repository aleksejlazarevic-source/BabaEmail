# STATUS — read this first when picking the project back up

Last session: 2026-09-24. Phase 0 (extraction) is done for all five stores.
Nothing user-facing exists yet — no email has ever been generated or sent.

## Lidl — solved (the old "Lidl call" is closed)

Lidl is read by three independent readers and a deal ships only when they
prove it: lidl.si's hidden offer API (`lidl_api.py`), a local vision model
(`vlm.py`, qwen2.5vl:7b via ollama, cached in `.cache/vlm/`) and the
improved OCR (`ocr.py` + `sources/lidl.py`). The rules are R0-R8 in the
`lidl_reconcile.py` docstring. **The leaflet is authoritative**: the API's
price never ships over a printed one.

Measured against 125 hand-checked deals (`scratch/lidl_truth*/`,
`scratch/lidl_reconcile/eval.py` -> `eval.txt`):

| set | deals | shipped | WRONG | held back |
|---|---|---|---|---|
| tune (10 pages) | 61 | 57 | 0 | 4 |
| holdout (10 pages never tuned on) | 64 | 52 | 0 | 12 |

Full kw39 leaflet, live run (`scripts/run_lidl.py`, 2.6 min with the VLM
cached, ~75 min cold): 223 shipped, the rest held back (many held-back
"tiles" are junk: adverts, a till receipt, "?" names). Plus one proven
notice: Senior dan 30. 9. 2026, 10 % (`lidl_notices.py`).

The Lidl step needs this PC (the VLM is ~2 min/page cold); once per leaflet.

### Aleks's answers (2026-09-24) and what was done with them

1. Weekly run on this PC: yes. Priority is pushing held-back deals to proven.
   Done this session: R8b (tiles that print no old price), R7b (second-item
   and bonus-pack deals), R2 typo tier, R4b/R5b (leaflet over API).
2. Website-only deals: "show them". BUILT BUT OFF:
   `reconcile(ship_website_only=True)` (R6b). Nothing can cross-check them,
   and the website's price differed from the printed leaflet for ~4 of ~125
   products shown in both. **Needs Aleks's go-ahead with that number.**
   Turned on, kw39 gains 16 deals (mostly "Znižujemo redne cene" biscuits).
3. "Buy 2" deals: show them. "-X% na drugi kos" and "N+M gratis**" bonus
   packs now ship with `Deal.condition_kind`; real multi-buys ("13+5 gratis",
   "2+1 gratis") stay held: the printed number is sometimes the bundle,
   sometimes one piece, and on p10 a per-litre price.
4. Lidl Plus: she uses it. LP deals ship, labelled (`loyalty_required`).
5. Senior dan and similar: show them. `SourceResult.notices`.
6. Lidl misprints its own leaflet (3 found in the answer key). Noted.
7. GitHub: save at checkpoints. Commits are local; **push needs
   `gh auth login` + `gh auth setup-git` on this PC** (not logged in).

### What is still held back on the answer-key pages, and why

- Real multi-buys (Argus 13+5, Freshona 2+1, FRUC 4+2): meaning not provable
  from the numbers. Would need the "cena za 1 kos" line read by two readers.
- API says sold out (2), or API old prices disagree (Bon Gelati): correct.
- Vitasia tiles where OCR read no name or only "Žepki": the name has one
  reader. A crop-level second read of the name would free these.
- Ribeye/Silan: website and leaflet disagree AND the leaflet tile's own
  old price does not reconcile. Correctly held.
- Lidl Plus not agreed between readers (Alesto), ambiguous jacket (Esmara).

### For the email layer (not built)

- `condition_kind == "second-item"`: show "drugi kos -80 %: 0,39 € (prvi
  1,99 €)"; `discount_pct` is already the honest two-piece saving (40 %).
- `condition_kind == "bonus-pack"`: pack price + "10+4 gratis".
- `price_unit == "kg"`: always "/kg".
- `website-only` flag (if switched on): its own labelled section.
- `SourceResult.notices` with `proven`: a notice box; `text` is ready Slovenian.

## What works now

| Retailer | Source | Items | Declared | Quarantined | Discounts |
|---|---|---|---|---|---|
| Mercator | dated PDF, text layer | 228 | 228 | 8 (3.5%) | 220 |
| Spar | letak.spar.si PDF, text layer | 209 | 209 | 26 (12.4%) | 183 |
| Tuš | dated PDF, text layer | 377 | 377 | 68 (18.0%) | 132 |
| Hofer | Publitas PDF, text layer | 163 | 163 | 27 (16.6%) | 46 |
| **Total** | | **977** | **977** | **129 (13.2%)** | **581** |
| Lidl | API + VLM + OCR | 223 shipped (kw39) | — | see above | 0 wrong on 125 checked |

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
- `babaemail/lidl_api.py`, `vlm.py`, `lidl_reconcile.py`, `lidl_notices.py` —
  the three Lidl readers, the rules that decide what ships, and notices.
- `FINDINGS.md` — all source research and every trap found. **Read it before
  touching a source adapter.**

## NOT built

`verify.py`, `audit.py`, `categorize.py`, `select.py`, `store.py`, `render.py`,
`mailer.py`, `main.py`, `preferences.yaml`, templates, CI workflow. Tests
exist only for Lidl (`tests/`). A GitHub Actions run cannot do Lidl (the VLM
needs this PC); the other four stores can run anywhere.

Per CLAUDE.md's model gate, everything remaining except audit.py and verify.py
is Sonnet work.

## Still owed to grandma (from the plan)

`preferences.yaml` must be built FROM A CONVERSATION WITH HER, not guessed. The
interview guide is in the plan file. Its first question is the important one:
**does she actually read email at all?** That is still unvalidated and the whole
design rests on it.

## Environment

Now on Linux (this PC). Python 3.14 in `.venv/`: `.venv/bin/python`.
Tests: `.venv/bin/python -m pytest -q` (182, all passing).
Tesseract 5 + `tessdata/slv.traineddata` (gitignored, 9.9MB — re-download
from tesseract-ocr/tessdata if missing). ollama with qwen2.5vl:7b for Lidl.
Fast rule iteration: `scratch/lidl_reconcile/replay.py` re-runs reconcile on
the captured full-leaflet inputs in seconds; `eval.py` scores the truth sets.
