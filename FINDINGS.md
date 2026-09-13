# Phase 0 findings — source research

Everything here was established empirically on 2026-09-13 against live sites.
Do not re-derive it; re-verify only if a source breaks.

## Verdict per retailer

| Retailer | Route | Product data | Status |
|---|---|---|---|
| Mercator | direct dated PDF | **text layer, full** | VIABLE |
| Tuš | direct dated PDF | **text layer, full** | VIABLE |
| Lidl | leaflet PDF, images only | **OCR required** | VIABLE via colour segmentation (below) |
| Spar | 403 to plain fetch AND headless Chromium | unknown | BLOCKED — unsolved |
| Hofer | no PDF links on homepage | unknown | not investigated |
| Eurospin | no PDF links on homepage | unknown | not investigated |

## Lidl

Leaflet API (found by capturing the SPA's network calls):

    https://endpoints.leaflets.schwarz/v4/flyer?flyer_identifier=lidlov-katalog-2026-kw{WW}

Returns `flyer` with: `pages[]` (58 page images), `pdfUrl`, `hiResPdfUrl`,
`startDate`/`endDate`, `offerStartDate`/`offerEndDate`, `status`.

**`products`, `texts`, `topics` are ALL EMPTY.** Confirmed dead ends:
- `/v4/products`, `/v4/flyer/{uuid}/products`, `/v4/search`, `/v3/flyer` → 404
- leaflet PDF: **0 characters of text on all 58 pages** (pure images)
- lidl.si offer pages: product tiles never load (permanent skeletons), no
  product API call fires, and Lidl states *"V primeru neskladij veljajo
  informacije iz spletnega letaka"* — they disclaim their own site vs the leaflet.

So the leaflet PDF is the authoritative source and OCR is unavoidable.

### The OCR method that works (free, local, no API)

Lidl prints prices as **yellow text over product photography**, with the euros
large and the cents as raised superscript. Page-level OCR fails badly on this:
it drops decimal points and whole digits (`1.29` → `29`, `2.49` → `249`,
`0,99` → `660`) because the line detector cannot handle two baselines.

Working pipeline:

1. Render the PDF page at 3x with PyMuPDF.
2. **Yellow mask**: `R>170 & G>150 & B<130 & (R+G)/2-B>70` → clean binary image.
   (Other Lidl price styles use white-on-red and black-on-yellow; those need
   their own masks — NOT yet implemented.)
3. `cv2.connectedComponentsWithStats` → one component per digit. This is exact.
4. Keep tall components; sort left to right; **drop the decimal point blob**.
5. Normalise every digit to the same height, paste onto one baseline strip.
6. OCR the strip as a single text line.
7. Apply **last two digits are cents** (`129` → 1,29).
8. Cross-check against `old_price × (1 - pct)` where both are published.

**Gotcha that cost an hour:** RapidOCR silently returns `[]` when the image has
too little padding. Use ~120px white border. It also cannot recognise isolated
single characters at all — always feed it a multi-digit strip, never one digit.

Verified: page 4 jabolka reads `129` → 1,29, matching the implied 1,79 × 0.73.

Unsolved for Lidl: product names lose diacritics (`rdecajabolka`, `LUST`)
because RapidOCR's default model has no Slovenian charset. Cosmetic, not
factual, but a Slovenian reader will notice. Tesseract + `slv` would fix it.

Also: many Lidl offers are **"z Lidl Plus"** (app-only). She has the app, so
they apply — but they must be labelled, or she'll be surprised at the till.

## Mercator

`mercator.si/katalogi` → direct dated PDFs, validity dates in the HTML:

    /assets/Katalogi/2026-09-10-Redni-katalog-200x275mm-web.pdf

Full text layer. Font size is strongly diagnostic:

| size | meaning |
|---|---|
| 27.0 | new price, euro part |
| 15.7 | new price, cents |
| 15.0 | discount badge `-50%` |
| 7.3  | old price |
| 6.0  | `PC30:` (30-day lowest price, legally required) |
| 9.0  | product name |
| 8.0  | description |
| 5.5  | store availability (`SUPER & HIPER`) |

Tile layout **varies**: sometimes the name sits below the price, sometimes
beside it. Greedy nearest-unclaimed-name assignment handled both on page 3.

### THE most important lesson in this project

The page-3 prototype extracted 8 items and every one passed the
old × (1-pct) = new cross-check. A perfect-looking 8/8.

**The page actually has 9 products.** It silently dropped *Mehčalec Silan*,
which has no absolute price — only "buy 2+, all items -50%".

**Self-consistency cannot detect a missing item.** Completeness needs an
INDEPENDENT count. Use the discount badges: every tile has exactly one, so
`count(badges) == count(extracted)` or the store fails its gate.

Conditional-discount tiles (no absolute price) are a real product type and must
be extracted, with the condition carried into the email.

## Tuš

`tus.si/katalogi/` → direct dated PDFs, dates in the filename:

    /app/uploads/catalogues/20260904070703_36_akcija_9.9.-15.9.2026_A36_26.pdf

Full text layer. Prices render without a separator (`699` = 6,99). Page 2 is the
meat page — exactly what she buys.

**Many Tuš discounts are "s Tuš klub kartico"** (loyalty card required). A deal
she cannot actually get is a wasted trip, so this qualifier MUST be captured.

## Environment notes

- Python 3.14 at `.venv/`; there is no `python` on PATH, use `py` or the venv.
- Always set `PYTHONIOENCODING=utf-8`; Slovenian text (`č`, `š`, `ž`) crashes
  the default Windows cp1252 stdout.
- `import fitz` is deprecated — use `import pymupdf`.
- `cv2` and `numpy` come free with rapidocr-onnxruntime.
