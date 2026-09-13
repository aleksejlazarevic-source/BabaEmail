# BabaEmail

**Read STATUS.md first** - current progress, and the open decision
("the Lidl call") that blocks the next step.

Daily email of real supermarket discounts for a 73-year-old in Ljubljana who
reads Apple Mail on an iPhone 12. Slovenian. ~40-80 items. She never types and
never has to tap anything — scrolling alone shows her the whole thing.

**Accuracy is existential.** If the email ever misses a qualifying item or shows
a price that isn't real, she stops trusting it and stops using it. Nothing in
this project is worth trading for that.

## Grandma feedback → ONE file, no code reading

If the request is about WHAT she sees — thresholds, categories, stores,
which days, always/never show — the answer is `preferences.yaml` and
nothing else. Do not read source. Do not explore the repo.
Edit the file, add a changelog line, run `--dry-run`, done.

  "10% meat, 5% fish"      -> categories.meso.floor, categories.ribe.floor
  "she never buys fish"    -> categories.ribe.show: false
  "only weekends"          -> send.days
  "Hofer first"            -> stores order
  "always show chicken"    -> always_show
  "too many items"         -> raise default_floor
  "not enough items"       -> lower default_floor
  "send it at 6"           -> send.hour

Read source ONLY when the request is about HOW it works: a store stopped
working, prices look wrong, layout/formatting, email didn't arrive.

## Model gate — STOP and ask, do not continue on the wrong model

Before starting any block below, check which model you are running as.
If it does not match, STOP, say which model is needed and why, and WAIT
for the user to confirm the swap. Do not proceed on the wrong model.
When a block finishes, announce it plainly:
"Opus part done. Sonnet part follows — are you on Sonnet?"

OPUS  (hard; expensive to get wrong)
  sources/*.py     reverse-engineering leaflet APIs, PDF spatial parsing
  verify.py        the accuracy gates — silently wrong = project over
  audit.py         the Phase 0 accuracy harness
  diagnosing a scraper that broke
  any change to the reliability contract

SONNET  (well-trodden)
  templates/*.j2, email HTML + CSS
  mailer.py, store.py, categorize.py, select.py
  CLI arguments, GitHub Actions workflow, tests

SONNET or HAIKU  (trivial)
  preferences.yaml edits from grandma's feedback

## The reliability contract — do not weaken these

1. Extract everything, filter last. Sources are scraped and verified in FULL;
   preferences are applied only at the final render step.
2. Copy, never select, at the extraction layer.
3. A store is 100% covered or explicitly not covered. Never partially.
4. Never drop an item silently. Unknown category -> `Ostalo`. Suspicious price
   -> quarantine AND flag. Never discard.
5. Fail loud. Any failure -> plain banner in her email + alert email to Aleks.
6. Zero items from a store is ALWAYS a failure, never a valid result.

## Environment

Python 3.14 at `.venv/` (no system `python` on PATH; use `py` or `.venv/Scripts/python.exe`).
