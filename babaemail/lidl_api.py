"""Lidl SI offer candidates from lidl.si's own JSON endpoints.

This module produces a CANDIDATE LIST, not verified deals. The printed leaflet
stays authoritative: a hand check (2026-09-23, see scratch/lidl_api/REPORT.md)
found the API covers ~89% of leaflet offers, and it disagrees with the leaflet
on occasion (ribeye: API 6.49, leaflet 6.19; two "do 26.9." leaflet dates that
the API reports as 30.9). Every candidate must still be checked against the
leaflet page before it can reach grandma.

Discovery is the union of three sources, because none is complete alone:

  1. product sitemap  /p/export/SI/sl/product_sitemap.xml.gz   (~970 erpNumbers)
  2. search API       /q/api/search?q=&offset=N                 (~325, 60 per page)
  3. campaign pages   /c/<slug>/a<id>, server-rendered data-grid-data JSON

Fresh records for every erpNumber are then pulled from
/p/api/gridboxes/SI/sl?erpNumbers=a,b,c (100 per call). A record the gridbox
endpoint does not return falls back to the copy embedded in the search result
or campaign page; one found nowhere is listed in `stats["unresolved"]`.

Traps this module handles (each one is covered by a test):

* Validity comes from stockAvailability.badgeInfoV2[].validFrom/validUntil
  (epoch seconds, meaning Europe/Ljubljana calendar days). NOT from
  currentPrice.startDate/endDate, which describe the price record. A window
  starting at local midnight is 22:00 UTC the day before; converting in UTC
  would put every offer on the wrong day.
* A window may have validFrom but no validUntil ("Od 09.07.", in store since).
  That is kept as (from, None). An end date is never invented.
* Lidl Plus offers carry their price in currentLidlPlusPrice.price, not in
  currentPrice. Some records have both; the Lidl Plus one is the headline, and
  the plain one is kept as `regular_price`.
* "-50% na drugi kos", "2+1 gratis", "14+10 gratis" mean the price shown is NOT
  a plain unit price. Those are `conditional`, and `pct` is withheld for them,
  so a "-50% on the second item" can never be shown as a 50% discount.
* "za kg" is a price basis, not a discount: `price_unit == "kg"`.
* "25% gratis", "220 g gratis" are bonus-quantity packs. The price is a real
  pack price. They are flagged `bonus_quantity`, not conditional.
* Money is Decimal throughout. JSON is parsed with parse_float=Decimal, so a
  price never passes through a binary float.

Contract rules applied here: an empty sitemap, an empty gridbox batch, or zero
candidates in the window all RAISE (rule 6). In-window records with no price,
or with a price but no offer signal, are returned on separate lists, never
dropped (rule 4).
"""

from __future__ import annotations

import gzip
import html
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("Europe/Ljubljana")
BASE = "https://www.lidl.si"
SITEMAP_URL = f"{BASE}/p/export/SI/sl/product_sitemap.xml.gz"
PAGES_SITEMAP_URL = f"{BASE}/explore/assets/s/pages_sl-SI_si.xml.gz"
SEARCH_URL = f"{BASE}/q/api/search"
GRIDBOX_URL = f"{BASE}/p/api/gridboxes/SI/sl"
SEARCH_PARAMS = {"assortment": "SI", "locale": "sl_SI", "version": "v2.0.0", "q": ""}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept-Language": "sl-SI,sl;q=0.9",
}

GRIDBOX_BATCH = 100          # the endpoint's own page size
SEARCH_PAGE = 60             # the search API caps fetchsize at 60 whatever we ask
MAX_CONCURRENCY = 4          # be polite: never more than this many requests in flight
ATTEMPTS = 4                 # 1 try + 3 retries
BACKOFF_S = 1.0              # 1s, 2s, 4s between attempts
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_SEARCH_PAGES = 100       # hard stop in case numFound lies

# Tests replace this so retries do not actually wait.
_sleep = time.sleep

_ERP_IN_SITEMAP = re.compile(r"/p(\d+)</loc>")
_CAMPAIGN_LOC = re.compile(r"<loc>\s*([^<\s]+/a\d+)\s*</loc>")
_CAMPAIGN_HREF = re.compile(r'href="(/c/[^"#?]+/a\d+)"')
_GRID_DATA = re.compile(r'data-grid-data="([^"]*)"')
_NA_DRUGI = re.compile(r"\bna\s+drug", re.I)                     # "-50% na drugi kos"
_MULTIBUY = re.compile(r"\b\d+\s*\+\s*\d+\b")                     # "2+1 gratis", "14+10 gratis"
_OB_NAKUPU = re.compile(r"\bob\s+nakupu\b", re.I)                 # "ob nakupu 2 kosov"
_GRATIS = re.compile(r"gratis", re.I)
_ZA_KG = re.compile(r"^\s*za\s+kg\s*$", re.I)
_SOLD_OUT = re.compile(r"ni\s+na\s+voljo", re.I)                  # "IZDELEK ŽAL NI NA VOLJO"


class LidlApiError(RuntimeError):
    """Any failure to obtain a complete, trustworthy candidate list.

    `status_code` is set for HTTP failures so the adapter can map it with
    sources.base.classify_http_failure (429/403 -> BLOCKED, and so on).
    """

    def __init__(self, message: str, status_code: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


Window = tuple[date, date | None]    # (first day, last day); None = no published end


@dataclass(slots=True)
class LidlCandidate:
    """One Lidl product as the API describes it. A candidate, not a verified deal."""

    erp: str
    name: str                          # brand + title, diacritics preserved
    brand: str | None
    title: str
    url: str
    category: str | None

    new_price: Decimal | None          # headline price: Lidl Plus price when there is one
    old_price: Decimal | None
    pct: Decimal | None                # the API's own percentage; None when conditional
    discount_text: str | None          # e.g. "-11%", "ceneje", "2+1 gratis", "za kg"
    lidl_plus: bool
    lidl_plus_text: str | None         # e.g. "z Lidl Plus"
    highlight_text: str | None         # Lidl Plus badge, e.g. "-40%", "4+2 gratis"
    regular_price: Decimal | None      # the non-Lidl-Plus price when both are published

    conditional: bool                  # True: new_price is NOT a plain unit price
    conditional_reason: str | None     # the badge text that made it conditional
    packaging: str | None              # e.g. "500g", "za kos", "6x100 ml"
    price_unit: str | None             # "kg" when the price is per kilogram ("za kg")
    base_price_text: str | None        # e.g. "1 kg = 1,50", exactly as published

    windows: list[Window]              # in-store windows; after selection only overlapping ones
    all_windows: list[Window]          # every window the record lists
    sources: tuple[str, ...]           # "sitemap", "search", "campaign:/c/..."
    data_from: str                     # "gridboxes" | "search-embedded" | "campaign-embedded"
    flags: list[str] = field(default_factory=list)

    @property
    def has_price(self) -> bool:
        return self.new_price is not None

    @property
    def offer_signals(self) -> tuple[str, ...]:
        """Why this counts as an offer. Empty means it is only a listed price."""
        s = []
        if self.old_price is not None:
            s.append("old_price")
        if self.pct is not None:
            s.append("pct")
        if self.discount_text:
            s.append("discount_text")
        if self.lidl_plus:
            s.append("lidl_plus")
        return tuple(s)

    def flag(self, reason: str) -> None:
        if reason not in self.flags:
            self.flags.append(reason)


class CandidateList(list):
    """The offers in the window, plus everything in the window that is not one.

    It is a plain list of LidlCandidate (the offers), with the rest attached so
    nothing is dropped silently:

    * `no_price`  - in the window, but the API publishes no price at all.
    * `no_signal` - in the window with a price, but no old price, percentage,
                    discount text or Lidl Plus price: a listed item, not an offer.
    * `stats`     - discovery and filtering counts, including `unresolved`
                    erpNumbers that no endpoint returned data for.
    """

    def __init__(self, offers: Iterable[LidlCandidate] = (), *,
                 no_price: list[LidlCandidate] | None = None,
                 no_signal: list[LidlCandidate] | None = None,
                 stats: dict[str, Any] | None = None):
        super().__init__(offers)
        self.no_price: list[LidlCandidate] = no_price or []
        self.no_signal: list[LidlCandidate] = no_signal or []
        self.stats: dict[str, Any] = stats or {}


# ======================================================================
# Parsing (pure; no network)
# ======================================================================

def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise TypeError(f"boolean where money was expected: {v!r}")
    if isinstance(v, Decimal):
        return v
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, str):
        return Decimal(v.strip())
    # A float here means someone parsed JSON without parse_float=Decimal.
    raise TypeError(f"refusing to build money from {type(v).__name__}: {v!r}")


def _text(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    t = unicodedata.normalize("NFC", " ".join(v.split()))
    return t or None


def local_day(epoch_s: int | Decimal | None) -> date | None:
    """Epoch seconds -> the Ljubljana calendar day it falls on."""
    if epoch_s is None:
        return None
    return datetime.fromtimestamp(int(epoch_s), TZ).date()


def _iso_local_day(s: str | None) -> date | None:
    """'2026-10-04T21:59:59Z' -> 2026-10-04 (in Ljubljana)."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ).date()


def parse_windows(raw: dict) -> list[Window]:
    out: list[Window] = []
    for bi in (raw.get("stockAvailability") or {}).get("badgeInfoV2") or []:
        start = local_day(bi.get("validFrom"))
        if start is None:
            continue                     # "V trgovini": permanent assortment, no window
        end = local_day(bi.get("validUntil"))
        out.append((start, end))
    return sorted(set(out), key=lambda w: (w[0], w[1] or date.max))


def overlaps(w: Window, window_from: date, window_to: date) -> bool:
    start, end = w
    return start <= window_to and (end is None or end >= window_from)


def _name(raw: dict) -> tuple[str, str | None, str]:
    brand = _text((raw.get("brand") or {}).get("name"))
    if brand and not re.search(r"\w", brand):
        brand = None                     # Lidl uses "-" as a no-brand placeholder
    title = None
    for cand in (raw.get("title"), (raw.get("keyfacts") or {}).get("title"), raw.get("fullTitle")):
        t = _text(cand)
        if t:
            t = t.lstrip("-– ").strip()
        if t and re.search(r"\w", t):
            title = t
            break
    title = title or ""
    if not brand:
        name = title
    elif not title:
        name = brand
    elif brand.casefold() in title.casefold():
        name = title
    else:
        name = f"{brand} {title}"
    return name, brand, title


def _conditional_reason(*texts: str | None) -> str | None:
    for t in texts:
        if t and (_NA_DRUGI.search(t) or _MULTIBUY.search(t) or _OB_NAKUPU.search(t)):
            return t
    return None


def parse_record(raw: dict, sources: Iterable[str] = (), data_from: str = "gridboxes") -> LidlCandidate:
    """Turn one gridbox record into a candidate. Keeps ALL windows; no filtering."""
    erp = str(raw["erpNumber"])
    name, brand, title = _name(raw)
    flags: list[str] = []
    if not name:
        flags.append("no_name")

    regions = raw.get("regionsPrices") or {}
    if set(regions) - {"1"}:
        flags.append("multiple_price_regions:" + ",".join(sorted(regions)))
    r1 = regions.get("1") or (next(iter(regions.values())) if regions else {})
    cp = r1.get("currentPrice") or None
    lp = r1.get("currentLidlPlusPrice") or None
    lp_block = (lp or {}).get("price") or None

    block = lp_block or cp
    lidl_plus = lp_block is not None
    disc = (block or {}).get("discount") or {}

    new_price = _dec((block or {}).get("price"))
    old_price = _dec((block or {}).get("oldPrice"))
    deleted = _dec(disc.get("deletedPrice"))
    if old_price is None and deleted is not None:
        old_price = deleted
        flags.append("old_price_from_deletedPrice")
    elif old_price is not None and deleted is not None and deleted != old_price:
        flags.append(f"deletedPrice_differs:{deleted}")
    pct = _dec(disc.get("percentageDiscount"))

    discount_text = _text(disc.get("discountText"))
    highlight = _text((lp or {}).get("highlightText"))
    if lidl_plus and not discount_text:
        discount_text = highlight

    reason = _conditional_reason(discount_text, highlight)
    if reason and pct is not None:
        flags.append(f"pct_withheld_conditional:{pct}")
        pct = None
    if not reason and any(_GRATIS.search(t or "") for t in (discount_text, highlight)):
        flags.append("bonus_quantity")

    price_unit = "kg" if discount_text and _ZA_KG.match(discount_text) else None
    packaging = _text(((block or {}).get("packaging") or {}).get("text"))
    if block is not None and not packaging and price_unit is None:
        flags.append("no_packaging")

    if new_price is not None and old_price is not None:
        if old_price <= new_price:
            flags.append("old_price_not_higher")
        elif pct is not None:
            implied = (old_price - new_price) / old_price * 100
            if abs(implied - pct) > Decimal("1.5"):
                flags.append(f"pct_inconsistent:implied {implied:.1f}")

    for rb in raw.get("ribbons") or []:
        t = _text(rb.get("text"))
        if t:
            flags.append(("sold_out:" if _SOLD_OUT.search(t) else "ribbon:") + t)

    windows = parse_windows(raw)
    return LidlCandidate(
        erp=erp, name=name, brand=brand, title=title,
        url=BASE + (raw.get("canonicalUrl") or raw.get("canonicalPath") or f"/p/p{erp}"),
        category=_text(raw.get("category")),
        new_price=new_price, old_price=old_price, pct=pct,
        discount_text=discount_text, lidl_plus=lidl_plus,
        lidl_plus_text=_text((lp or {}).get("lidlPlusText")),
        highlight_text=highlight,
        regular_price=_dec(cp.get("price")) if (cp and lidl_plus) else None,
        conditional=reason is not None, conditional_reason=reason,
        packaging=packaging, price_unit=price_unit,
        base_price_text=_text(((block or {}).get("basePrice") or {}).get("text")),
        windows=list(windows), all_windows=list(windows),
        sources=tuple(sources), data_from=data_from, flags=flags,
    )


def _price_record_flags(raw: dict, cand: LidlCandidate, window_from: date, window_to: date) -> list[str]:
    """Flag when the price record does not cover the part of the window we show.

    The badge window says when the item is in store; the price record says
    when THIS price applies. If they disagree, the price may be wrong for some
    of those days (e.g. a future price record that starts mid-window).
    """
    flags: list[str] = []
    r1 = (raw.get("regionsPrices") or {}).get("1") or {}
    rec = ((r1.get("currentLidlPlusPrice") or {}).get("price") if cand.lidl_plus
           else r1.get("currentPrice")) or {}
    for w in cand.windows:
        eff_from = max(w[0], window_from)
        eff_to = min(w[1] or window_to, window_to)
        p_from, p_to = _iso_local_day(rec.get("startDate")), _iso_local_day(rec.get("endDate"))
        if (p_to and p_to < eff_from) or (p_from and p_from > eff_to):
            # The published price does not apply on ANY day of this window:
            # it is typically last week's offer on an item that returns later.
            flags.append(f"price_not_valid_in_window:{p_from}..{p_to}")
        else:
            if p_from and p_from > eff_from:
                flags.append(f"price_record_starts:{p_from.isoformat()}")
            if p_to and p_to < eff_to:
                flags.append(f"price_record_ends:{p_to.isoformat()}")
        for key in ("futurePrices", "futureLidlPlusPrices"):
            for fp in r1.get(key) or []:
                f_from = _iso_local_day(fp.get("startDate") or (fp.get("price") or {}).get("startDate"))
                if f_from and eff_from <= f_from <= eff_to:
                    flags.append(f"price_changes_in_window:{key}:{f_from.isoformat()}")
    return list(dict.fromkeys(flags))


def select(records: dict[str, tuple[dict, list[str], str]], window_from: date,
           window_to: date) -> CandidateList:
    """Split parsed records into offers / no-price / no-signal for the window.

    `records` maps erp -> (raw record, sources, data_from).
    """
    if window_to < window_from:
        raise ValueError(f"window_to {window_to} is before window_from {window_from}")
    offers, no_price, no_signal = [], [], []
    out_of_window = no_window = 0
    for erp in sorted(records):
        raw, sources, data_from = records[erp]
        cand = parse_record(raw, sources, data_from)
        if not cand.all_windows:
            no_window += 1
            continue
        in_win = [w for w in cand.all_windows if overlaps(w, window_from, window_to)]
        if not in_win:
            out_of_window += 1
            continue
        cand = replace(cand, windows=in_win, flags=list(cand.flags))
        if any(w[1] is None for w in in_win):
            cand.flag("open_ended_window")
        for f in _price_record_flags(raw, cand, window_from, window_to):
            cand.flag(f)
        if not cand.has_price:
            cand.flag("no_price")
            no_price.append(cand)
        elif not cand.offer_signals:
            no_signal.append(cand)
        else:
            offers.append(cand)
    stats = {
        "window": [window_from.isoformat(), window_to.isoformat()],
        "records": len(records),
        "no_window": no_window,
        "out_of_window": out_of_window,
        "in_window": len(offers) + len(no_price) + len(no_signal),
        "offers": len(offers),
        "no_price": len(no_price),
        "no_signal": len(no_signal),
        "lidl_plus": sum(c.lidl_plus for c in offers),
        "conditional": sum(c.conditional for c in offers),
    }
    return CandidateList(offers, no_price=no_price, no_signal=no_signal, stats=stats)


# ======================================================================
# Network
# ======================================================================

def make_client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)


def _get(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    """GET with retries on transport errors and 429/5xx; raise on anything else."""
    last: Exception | None = None
    status: int | None = None
    for attempt in range(ATTEMPTS):
        try:
            r = client.get(url, params=params)
        except httpx.TransportError as exc:          # timeouts, resets, DNS
            last, status = exc, None
        else:
            if r.status_code < 400:
                return r
            if r.status_code not in RETRY_STATUS:
                raise LidlApiError(f"HTTP {r.status_code} from {url}",
                                   status_code=r.status_code, url=url)
            last, status = None, r.status_code
        if attempt < ATTEMPTS - 1:
            _sleep(BACKOFF_S * 2 ** attempt)
    what = f"HTTP {status}" if status else repr(last)
    raise LidlApiError(f"{what} from {url} after {ATTEMPTS} attempts",
                       status_code=status, url=url) from last


def _json(r: httpx.Response) -> Any:
    try:
        return json.loads(r.content, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LidlApiError(f"non-JSON response from {r.request.url}: {r.text[:120]!r}",
                           status_code=r.status_code, url=str(r.request.url)) from exc


def _maybe_gunzip(r: httpx.Response) -> str:
    b = r.content
    return gzip.decompress(b).decode("utf-8") if b[:2] == b"\x1f\x8b" else r.text


def sitemap_ids(client: httpx.Client) -> list[str]:
    ids = sorted(set(_ERP_IN_SITEMAP.findall(_maybe_gunzip(_get(client, SITEMAP_URL)))))
    if not ids:
        raise LidlApiError(f"product sitemap listed zero products: {SITEMAP_URL}")
    return ids


def search_items(client: httpx.Client) -> dict[str, dict | None]:
    """erp -> embedded gridbox data (or None) for every product the search lists."""
    out: dict[str, dict | None] = {}
    offset, num_found = 0, None
    for _ in range(MAX_SEARCH_PAGES):
        d = _json(_get(client, SEARCH_URL, {**SEARCH_PARAMS, "fetchsize": SEARCH_PAGE,
                                            "offset": offset}))
        num_found = int(d.get("numFound") or 0)
        items = d.get("items") or []
        if not items:
            break
        for it in items:
            if it.get("type") == "product" and it.get("code"):
                out[str(it["code"])] = ((it.get("gridbox") or {}).get("data")) or None
        offset += len(items)
        if offset >= num_found:
            break
    if not num_found:
        raise LidlApiError("search API reported zero products")
    if offset < num_found:
        raise LidlApiError(f"search API stopped at {offset} of {num_found} items")
    return out


def campaign_paths(client: httpx.Client) -> list[str]:
    paths = {u.replace(BASE, "") for u in _CAMPAIGN_LOC.findall(
        _maybe_gunzip(_get(client, PAGES_SITEMAP_URL)))}
    paths |= set(_CAMPAIGN_HREF.findall(_get(client, BASE + "/").text))
    paths = {p for p in paths if p.startswith("/c/")}
    if not paths:
        raise LidlApiError("found zero campaign pages")
    return sorted(paths)


def campaign_grid(client: httpx.Client, path: str) -> list[dict] | None:
    """Products embedded in one campaign page; None if the page is gone.

    The pages sitemap is stale: it lists retired campaigns (some with German
    slugs) that answer 404. A 404/410 on such a page is returned as None and
    listed in stats["campaign_pages_gone"]; any other failure raises.
    """
    try:
        body = _get(client, BASE + path).text
    except LidlApiError as exc:
        if exc.status_code in (404, 410):
            return None
        raise
    out = []
    for m in _GRID_DATA.finditer(body):
        try:
            d = json.loads(html.unescape(m.group(1)), parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise LidlApiError(f"unparsable data-grid-data on {path}") from exc
        if isinstance(d, dict) and d.get("erpNumber"):
            out.append(d)
    return out


def gridboxes(client: httpx.Client, erps: list[str]) -> dict[str, dict]:
    batches = [erps[i:i + GRIDBOX_BATCH] for i in range(0, len(erps), GRIDBOX_BATCH)]

    def one(batch: list[str]) -> list[dict]:
        d = _json(_get(client, GRIDBOX_URL, {"erpNumbers": ",".join(batch)}))
        if not isinstance(d, list) or not d:
            raise LidlApiError(f"gridboxes returned nothing for a batch of {len(batch)} "
                               f"(first {batch[0]})")
        return d

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(MAX_CONCURRENCY) as pool:
        for recs in pool.map(one, batches):
            for x in recs:
                out[str(x["erpNumber"])] = x
    if not out:
        raise LidlApiError("gridboxes returned zero records")
    return out


def discover(client: httpx.Client) -> tuple[dict[str, tuple[dict, list[str], str]], dict[str, Any]]:
    """Fetch every product record Lidl publishes. Raises LidlApiError on any failure."""
    src: dict[str, list[str]] = {}
    embedded: dict[str, tuple[dict, str]] = {}

    for e in sitemap_ids(client):
        src.setdefault(e, []).append("sitemap")
    n_sitemap = len(src)

    search = search_items(client)
    for e, data in search.items():
        src.setdefault(e, []).append("search")
        if data:
            embedded.setdefault(e, (data, "search-embedded"))

    paths = campaign_paths(client)
    with ThreadPoolExecutor(MAX_CONCURRENCY) as pool:
        grids = list(pool.map(lambda p: campaign_grid(client, p), paths))
    gone = [p for p, g in zip(paths, grids) if g is None]
    if len(gone) == len(paths):
        raise LidlApiError(f"all {len(paths)} campaign pages answered 404/410")
    campaign_ids: set[str] = set()
    for path, items in zip(paths, grids):
        for x in items or ():
            e = str(x["erpNumber"])
            campaign_ids.add(e)
            src.setdefault(e, []).append("campaign:" + path)
            embedded.setdefault(e, (x, "campaign-embedded"))
    if not campaign_ids:
        raise LidlApiError(f"{len(paths)} campaign pages contained zero products")

    fresh = gridboxes(client, sorted(src))
    records: dict[str, tuple[dict, list[str], str]] = {}
    unresolved = []
    for e, s in src.items():
        if e in fresh:
            records[e] = (fresh[e], s, "gridboxes")
        elif e in embedded:
            records[e] = (embedded[e][0], s, embedded[e][1])
        else:
            unresolved.append(e)
    stats = {
        "sitemap_ids": n_sitemap,
        "search_ids": len(search),
        "campaign_pages": len(paths),
        "campaign_pages_gone": gone,
        "campaign_ids": len(campaign_ids),
        "union_ids": len(src),
        "gridbox_records": len(fresh),
        "embedded_fallback": sum(1 for r in records.values() if r[2] != "gridboxes"),
        "unresolved": sorted(unresolved),
    }
    return records, stats


def fetch_candidates(window_from: date, window_to: date,
                     client: httpx.Client | None = None) -> CandidateList:
    """Every Lidl offer whose in-store window overlaps [window_from, window_to].

    Returns a CandidateList (a list of LidlCandidate) with `.no_price`,
    `.no_signal` and `.stats` attached. Raises LidlApiError on any HTTP
    failure, empty discovery, or zero offers in the window. Never returns an
    empty list.
    """
    own = client is None
    c = client or make_client()
    try:
        records, disc_stats = discover(c)
    finally:
        if own:
            c.close()
    result = select(records, window_from, window_to)
    result.stats = {**disc_stats, **result.stats}
    if not result:
        raise LidlApiError(
            f"zero Lidl offers for {window_from}..{window_to} out of {len(records)} records "
            f"({result.stats['no_price']} in window without price, "
            f"{result.stats['no_signal']} without offer signal)")
    return result
