# Daily email layer

The email layer reads the weekly Lidl JSON without importing or running the
Lidl extractor. It fetches the other four adapters in full, verifies every
extracted offer, then applies `preferences.yaml`. Unknown categories are
`Ostalo`. Rejections and preference cuts remain in the dated report.

Use the main worktree's environment from this worktree:

```sh
/home/forbiddentoast/Work/BabaEmail/.venv/bin/python -m babaemail.main \
  --dry-run --date 2026-09-26 \
  --lidl-json /home/forbiddentoast/Work/BabaEmail/out/lidl_lidlov-katalog-2026-kw39.json
```

The output is `out/email-<date>.html`, `out/email-<date>.txt`, and
`out/report-<date>.txt`. `--offline` deliberately fails the four live stores
and shows their banners. A store failure returns exit code 1 after writing
these files. With no `--lidl-json`, the newest JSON by modification time with
an explicit window covering the requested date is loaded from `out/`.
A malformed product fails the cache as a whole rather than claiming partial
coverage. Held cached products remain quarantined even if their flags are
missing. An OK source with zero extracted products is a failure.

The default preferences are unconfirmed. Category `floor: null` inherits
`default_floor`; a numeric category floor overrides it. `always_show` overrides
category and discount preferences, never verification or item limits.
Plain leaflet prices are eligible for Lidl; the other stores need a discount
or an `always_show` match. Store order also determines allocation of the total
item limit. `send.days` and `send.hour` are stored for a future scheduler;
this CLI does not create or change schedules.

Lidl percentage badges must match truncation exactly. The other retailers
may differ from the exact old/new saving by at most one percentage point.
Second-piece prices always show the first-piece price and original terms;
the displayed and ranked saving is the honest two-piece saving. Conditional
terms and per-kilogram qualifiers remain visible in HTML and plain text.
Proven notices with dates are shown from today through the lookahead window.

Optional cached `partner_offers` are handled without changing the core
models. Supported records have `partner`, `what`, `price` (or `price_new`),
optional `price_old` / `discount_pct`, and `conditions` (or `condition`).
Dates must be explicit ISO `valid_from` and `valid_to`, either at the top level
or inside `validity` (`from`/`to` also accepted there). Unknown date prose and
qualified price prose are held in the report, never guessed. Price-based
partner records pass the same verification and preferences as products.
Discount-only partner records lack the absolute price required by E1's gate
and are rejected and reported. The source's final partner schema still needs
integration testing when a real JSON containing these records is available.

SMTP is opt-in via `--send`. `--dry-run` takes precedence even when both flags
are present. All of these variables are required before SMTP can connect:
`BABAEMAIL_SMTP_HOST`, `BABAEMAIL_SMTP_PORT`, `BABAEMAIL_SMTP_USER`,
`BABAEMAIL_SMTP_PASSWORD`, `BABAEMAIL_FROM`, `BABAEMAIL_TO`,
`BABAEMAIL_ALERT_TO`. Delivery uses STARTTLS with certificate validation,
a multipart text/HTML message, and a separate plain alert to Aleks for any
store failure. Sent-item history is written under `out/history/` only after
successful delivery. Preview generation needs no SMTP credentials.

The regression tests use fake SMTP only. They never send email:

```sh
/home/forbiddentoast/Work/BabaEmail/.venv/bin/python -m pytest -q tests/test_email.py
```

The existing Lidl suite also needs the ignored scratch fixture
`scratch/lidl_reconcile/x3/fruc-unusable-null-quantity.json` from the main
worktree; a fresh Git worktree does not contain it.
