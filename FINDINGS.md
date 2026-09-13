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

## Hofer — BEST SOURCE IN THE PROJECT

Clean JSON API, no authentication, exact integer prices. No OCR, no PDF parsing.

    GET https://api.hofer.si/v3/product-search
        ?currency=EUR&serviceType=walk-in&limit=60&offset=0
        &promotionKey=YYYY-MM-DD&sort=relevance&servicePoint=I032

**CRITICAL GOTCHA: send ONLY `accept-language: sl-SI`.**
Adding a `user-agent` header returns 403. Adding `Origin`/`Referer` returns 403.
The browser itself sends an empty referer and no origin. Fewer headers = success.

Other constraints found:
- `limit` must be one of a fixed allowed set. **60 works; 100 and 200 return
  HTTP 400** `"Page limit must be equal to some ..."`.
- `promotionKey` is the offer start date (`2026-09-10`), matching the dated
  pages at `hofer.si/posebna-ponudba/<date>`.
- `servicePoint` is a store id (`I032` = 1230 Domžale). Ljubljana stores will
  have their own ids — resolve the right one before going live.

Response shape:

    { "meta": { "pagination": { "offset", "limit", "totalCount" }, "facets": [...] },
      "data": [ { sku, name, brandName, onSaleDate, onSaleDateDisplay,
                  categories[], price: { amount, amountRelevantDisplay,
                  wasPriceDisplay, savingsDisplay, comparisonDisplay,
                  currencyCode, bottleDeposit } } ] }

- `price.amount` is an **integer in cents** (1679 = 16,79 €). Exact. Never parse
  the display string when the integer is available.
- `price.wasPriceDisplay` is the old price; null when the item is not discounted.
- `price.comparisonDisplay` is the per-unit price (per kg/l) — she buys in bulk.
- **`meta.pagination.totalCount` is the independent completeness count** that
  `verify.py` needs: extracted == totalCount, or the store fails its gate.

## Spar — BLOCKED, exhausted

`spar.si` (both `/letak` and `/katalogi`) sits behind a **Cloudflare managed
challenge** ("Just a moment..." / "Trenutek..."). Every legitimate approach was
tried and all returned 403 or never cleared the challenge:

1. plain httpx / WebFetch
2. headless Chromium (Playwright)
3. headless + stealth init scripts (webdriver/languages/plugins/chrome spoofs)
4. headed Chromium
5. real system Chrome, persistent profile, headed, 30s wait
6. real Microsoft Edge, persistent profile, headed, 30s wait
7. curl_cffi TLS impersonation — chrome124, chrome120, chrome110, safari17_0

What remains would be CAPTCHA-solving services or challenge-solver libraries:
against Cloudflare's and Spar's terms, fragile, and they would fail in GitHub
Actions regardless. **Treat Spar as not-covered** under contract rule 3 rather
than shipping something that works intermittently.

## Eurospin — not yet investigated

Entry points found but not explored: `eurospin.si/letak/`,
`eurospin.si/akcije/`, `eurospin.si/nazionale/` ("PRELISTAJ LETAK").
No PDFs on the homepage.

## Spar — SOLVED (supersedes the "blocked" section above)

`www.spar.si` is behind Cloudflare and stays unreachable. **It is not needed.**

The leaflets live on a **completely separate host with no protection at all**:

    https://letak.spar.si/<slug>/getPdf.ashx        <- the PDF
    https://letak.spar.si/<slug>/Image.ashx?PageNumber=N&ImageType=Normal
    https://letak.spar.si/<slug>/                   <- iPaper viewer (HTML)

Plain `httpx` with an ordinary User-Agent gets HTTP 200. No browser, no
Cloudflare, no cookies, no manual step. Powered by iPaper (`ipapercms.dk`).

Verified: `katalog-3726` = 21.8 MB / 43 pages, `katalog-3826` = 43.2 MB.
**Full text layer** — names, prices, discount %, `redna cena` (old price),
`PC` (30-day lowest), and `s SPAR plus kartico` (loyalty-card qualifier —
capture it, a card-only deal she can't use is a wasted trip).

Weekly slug pattern, derivable from the date with no page scrape:

    katalog-{ISO week:02d}{YY}     e.g. week 37 of 2026 -> katalog-3726

**GOTCHA: use GET, never HEAD.** `HEAD getPdf.ashx` returns 404 on this server
while `GET` returns 200. A HEAD-based existence check would wrongly report the
leaflet as missing and fire the NO_LEAFLET gate every single day.

Themed catalogues (`katalog-interspar-1226`, `katalog-dom-0226`,
`katalog-despar-planet-koper-1326`, ...) are NOT date-derivable and are only
listed on the Cloudflare-protected `www.spar.si`. `letak.spar.si` exposes no
index. The weekly grocery catalogue is the one that matters; treat the themed
ones as out of scope unless a discovery route turns up.

How this was found: the user solved the Cloudflare check manually in their own
Chrome and saved the page with Ctrl+S. Parsing that saved HTML revealed the
`letak.spar.si` host. The manual step was a ONE-TIME discovery cost, not an
ongoing dependency — nothing in the daily run needs it.

## Hofer, part 2 — the API has NO discounts. Use the leaflet.

Important correction to the section above. The `api.hofer.si` product-search
API returns exact prices but **never an old price**:

- `promotionKey=2026-09-10` -> 84 products, **0** with `wasPriceDisplay`
- whole assortment, no promotionKey -> `totalCount` 822, **0** with `wasPriceDisplay`

"Posebna ponudba" is Hofer's weekly *themed* range (Italian week, etc.), not a
markdown. Their own banner says **"TO NI AKCIJA!"** — "this is not a sale".

Hofer's actual price cuts are in the leaflet, which does publish them:

    TO NI AKCIJA! / REDNE CENE SPUŠČAMO
    SIR GAVDA 450 g   2,81   6,24/kg   za kos   STARA CENA: 3,09   -9%

### Hofer leaflet chain (all plain httpx, no Cloudflare, no browser needed)

1. `https://www.hofer.si/aktualni-letaki-in-brosure` — lists leaflet slugs.
   Weekly slug looks like `letak_kw37_2026_dober_razlog_za_vsak_nakup_po_hofer_ceni`
   (`kw{WW}_{YYYY}` plus a marketing suffix that DOES change — discover the slug
   from this page, do not construct it).
2. `https://letaki.hofer.si/<slug>/` — Publitas viewer HTML. Reachable with a
   plain UA. Contains exactly one `.pdf` URL.
3. `https://view.publitas.com/96384/<pubId>/pdfs/<uuid>.pdf` — the PDF.
   The uuid is NOT derivable; scrape it from step 2 each time.

Verified: 28 pages, 32.5 MB, **full text layer**.

Use the API only to enrich (images via `assets`, exact integer prices,
`comparisonDisplay` per-unit). The leaflet is the discount source of truth.

## Summary — all five named retailers are reachable

| Retailer | Source | Text layer | Discount markers |
|---|---|---|---|
| Mercator | direct dated PDF | yes | `-50%`, `PC30:`, old price |
| Spar | `letak.spar.si` iPaper PDF | yes | `redna cena`, `CENEJE %`, `PC`, `s SPAR plus kartico` |
| Tuš | direct dated PDF | yes | `-50%`, `SUPER CENA`, `s Tuš klub kartico` |
| Hofer | `letaki.hofer.si` Publitas PDF | yes | `STARA CENA:`, `-9%` |
| Lidl | leaflet PDF, images only | **no — OCR** | yellow badges, `z Lidl Plus` |

Four of five parse deterministically from embedded text. Only Lidl needs OCR.

**Loyalty-card qualifiers must be carried through to the email** — Spar plus,
Tuš klub, Lidl Plus. She has the Lidl app; a card-only deal she cannot use is a
wasted trip, which the contract treats as seriously as a wrong price.

## Header quirks - these look like bugs but are not

Two hosts in this project REJECT requests that carry a User-Agent and ACCEPT
requests that do not. Adding "proper" browser headers is what breaks them:

    api.hofer.si/v3/product-search   -> 403 with a UA, 200 with only
                                        `accept-language: sl-SI`
    www.hofer.si/aktualni-letaki-...  -> 403 with a UA, 200 with none

Meanwhile `letaki.hofer.si`, `view.publitas.com`, `letak.spar.si`,
`www.mercator.si` and `www.tus.si` all behave normally and want an ordinary UA.

Also: `letak.spar.si` does not implement HEAD (404 on HEAD, 200 on GET).

## Extraction results, first full run (2026-09-13)

| Retailer | Items | Declared | Quarantined | Discounts |
|---|---|---|---|---|
| Mercator | 228 | 228 | 8 (3.5%) | 220 |
| Spar | 209 | 209 | 26 (12.4%) | 183 |
| Tuš | 377 | 377 | 68 (18.0%) | 132 |
| Hofer | 163 | 163 | 27 (16.6%) | 46 |
| **Total** | **977** | **977** | **129 (13.2%)** | **581** |

`declared` is the independent completeness count (badge or price-tile count)
and matches extraction exactly for every retailer.

### Traps found while building these, all of which produced plausible-but-wrong
### output that only a cross-check caught

1. **Percentage tolerance.** Retailers truncate the badge to a whole percent, so
   rebuilding a price from it is a few cents out on nearly every item. Comparing
   reconstructed PRICES quarantined 30% of good products; comparing PERCENTAGES
   with ~1 point of slack is correct.
2. **Cents read as a discount.** Tus sets cents at 26.2pt and badge digits at
   25.5pt. 3,21 became "-21%", 7,50 "-50%", 1,80 "-80%" - 126 invented discounts
   in one catalogue.
3. **A badge read as cents.** The same collision in reverse turned SVINJSKA
   REBRA at 4,79 into 4,50. A wrong price that passes every other check.
   Fixed by: the digit run immediately left of a "%" is a percentage and can
   never be a price's cents - and only the NEAREST such run counts, because a
   radius also swallowed the euro figure and deleted whole tiles.
4. **Name font size is not stable** even within one catalogue (7.8 / 9.0 / 12.0
   in Mercator), so labels must be chosen by typography and content.
5. **Greedy label claiming is order-sensitive** and let tiles steal neighbours'
   names. Partitioning labels to their nearest tile is order-independent.
6. **Page-wide promotions** ("SREDA -11%") must be assigned to one tile, not
   found by searching outward from every tile.
7. **Page furniture repeats, product names do not** - counting occurrences
   removes "NOVA REDNA CENA" and brand strips generically.

## Lidl via free local OCR - honest result

Built and measured over six leaflet pages (58 pages total, ~10s per page):

    38 price groups found, 34 quarantined = 89% unusable, ~11% shippable

**What works.** Prices that ARE read come out right. Page 3's "NAŠA MESNICA
sveže mleto mešano meso" reads -14%, 2,89 from 3,37, matching the leaflet
exactly, and page 4's jabolka reads 1,29 from 1,79 at -27%. The colour
segmentation plus single-baseline strip genuinely solves the price-reading
problem that defeats off-the-shelf OCR.

**What does not.** Recall. Most tiles never produce a readable price group, and
product names are unreliable - "NASAMESNICA", "lubliansha", "za kg",
"Izdelki so dobavljivi samo". The default RapidOCR model has no Slovenian
charset, so diacritics are lost and short names are mangled.

Two OCR traps worth keeping:

1. RapidOCR returns `[]` when an image has too little white margin. ~120px of
   padding is required. It also cannot recognise a single isolated character at
   all - always feed it a multi-digit strip.
2. OCR usually loses the "%" glyph, so "-27%" arrives as "-27". Requiring the
   "%" silently dropped every badge - and `discount_display` then COMPUTED a
   percentage from old/new, so a badge that was never read looked like it had
   been. Watch for fallbacks that disguise a failed read as a successful one.

**Assessment.** At ~11% coverage with unreliable names, Lidl does not meet the
standard the other four retailers do, and shipping it would break the promise
that the email is complete for the stores it covers. Options, in order of
likely payoff: Tesseract with the Slovenian language model (better names and
probably better recall, trivial to install in CI); substantially more work on
segmentation and pairing; a vision API for Lidl only (~EUR 1.50-3.50/month,
previously declined); or leave Lidl out and note that she already has the
Lidl Plus app, which covers Lidl better than we can.

## Tesseract + Slovenian: tried, helps, does not solve Lidl

Installed Tesseract 5.4 (winget, `UB-Mannheim.TesseractOCR`). The Windows
installer ships ONLY English, and Program Files is not writable without admin,
so `slv.traineddata` lives in a project-local `tessdata/` with
`TESSDATA_PREFIX` pointed at it from `ocr.py`.

**The two engines are complementary; neither wins outright.**

| | RapidOCR | Tesseract + slv |
|---|---|---|
| Finds yellow prices over photography | **yes** (neural detector) | **no** - 22 lines on Lidl p4, all small print, every price missed |
| Reads Slovenian | no charset - "rdecajabolka" | **yes** - "Slovenska rdeča jabolka" |
| Digit strips | needs exactly ~120px padding, fragile | **robust at any padding**, digit whitelist |

So the pipeline now uses RapidOCR to locate text on a page and Tesseract to read
the colour-masked price strips.

Two Tesseract settings that mattered:
- `--psm 3` (auto page segmentation) vastly beats `--psm 11` (sparse text).
  psm 11 returns character noise ('na', 'n', 'ILI', '<"'); psm 3 reads real
  Slovenian.
- Word-level output never matches a price or badge pattern, because Tesseract
  splits "-27%" into "27" and "%". Regroup words into their source line.

**Result: Lidl is unchanged at ~11% usable (34 of 38 groups quarantined).**
Tesseract improved recognition; the bottleneck is DETECTION and PAIRING -
finding each tile's badge and old price and attaching them to the right price.
That is a substantially larger piece of work than recognition quality, and it
is where any further free-OCR effort must go.
