# STATUS — read this first when picking the project back up

Last session: 2026-09-27 (Lidl). Before that: 2026-09-24. Phase 0 (extraction) is done for all five stores.
Nothing user-facing exists yet — no email has ever been generated or sent.

## Lidl: state after the night of 2026-09-27 (HEAD 11d6c43, local only)

The printed leaflet is authoritative. Three readers (the lidl.si API, the
local VLM qwen2.5vl:7b, and OCR) are reconciled under R0–R8 in
`babaemail/lidl_reconcile.py`.

On top of those, `babaemail/lidl_crops.py` adds targeted second reads.
Held leaflet-only tiles get crops chosen **automatically** from OCR geometry
(at most 40 per leaflet; kw39 took 32 crops in about 111 s). Each crop is read
by OCR and then, blind, by the VLM. The evidence is bound to the page SHA256.
A bad cache or a refine error falls back to the no-crop result.

**Measured with `eval.py --crops` / `replay.py --crops`, api+vlm+ocr, kw39:**

| set | deals | shipped | wrong | harmful fields | held | missing |
|---|---:|---:|---:|---:|---:|---:|
| tune (10 pages) | 61 | 59 | 0 | 0 | 2 | 0 |
| holdout (10 pages) | 64 | 59 | 0 | 0 | 5 | 0 |

**Full 64-page leaflet: 240 shipped.** It was 227 without crops.
- Every shipped price exists on its page in the 64-page hand-made answer keys
  (`scratch/lidl_truth*/`, gitignored).
- Old price, % and LP: 0 mismatches.
- 248 of the 293 priced offers in the keys ship; 45 are held back.
- The holdout pages have now been tuned on. The fair test is the kw40 leaflet.

**New tonight:**
- Multi-buys ship only when they're proven exactly: two readers, "cena za 1
  kos", bundle = paid × single to the cent, and "točno N kosov" = paid+free.
  kw39 Argus 13+5 ships, with the condition text.
- The printed leaflet name replaces the API name when they share no product
  word (Böreklik, šarenka, Svetlo pivo).
- An OCR tile's own text can prove a name, with guards: brand-stripped, at
  least 2 stems, no borrowed lines, consecutive lines only. Silan p15 5.89 ships.
- **Email rule for later:** `condition_kind`/`condition` must always be shown.
  Never show `price_new` alone for multi-buy, from-price or second-item deals.

**Still held on the 125 scored deals:**
- sold out on the website (owner decision #9): Slim & Fit, Chef Select
- Alesto: Lidl Plus not agreed
- Freshona: 2 × 0.57 ≠ 0.76
- FRUC: no two-reader multi-buy proof
- Mulino Bianco: the name readers disagree
- Spomladanski zavitki: "že od" not recovered automatically

Logs: `scratch/lidl_reconcile/x1 … x6b/`. Coordination notes are in
`~/Work/babaemail-coordination/CLAUDE_RESULTS.md`.

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
7. GitHub: the push was completed on 24 Sep 2026. The old login blocker
   is stale. X1 commits locally only; no push is authorized for this task.

### What is still held back on the checked pages, and why

- p8 Slim & Fit 2.99 and p9 Chef Select medaljoni 7.99: existing API
  sold-out quarantine; policy remains the owner's decision #9.
- p15 Silan 5.89: API/leaflet price conflict and unreconciled leaflet old/%.
- p37 Alesto 2.29: leaflet readers disagree on Lidl Plus.
- p10 Freshona 2+1, p35 Argus 13+5 and FRUC 4+2: the conditional price
  meaning still lacks the required two-reader proof.

### For the email layer (not built)

- `condition_kind == "second-item"`: show "drugi kos -80 %: 0,39 € (prvi
  1,99 €)"; `discount_pct` is already the honest two-piece saving (40 %).
- `condition_kind == "bonus-pack"`: pack price + "10+4 gratis".
- `condition_kind == "from-price"`: always print "že od" next to the price;
  it is a lower bound, and its discount is unknown.
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
| Lidl | API + VLM + OCR + cached crop proofs | 232 shipped (kw39 replay) | — | see above | 0 wrong on 125 checked |

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
