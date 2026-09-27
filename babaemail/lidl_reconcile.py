"""Merge Lidl's three independent readers into Deals.

Inputs
------
* API candidates (``babaemail.lidl_api``): good names, prices that usually but
  NOT always match the leaflet (ribeye: API 6.49, leaflet 6.19), and no page.
  ``candidates`` are the API's offers. ``listed`` (optional) is the API's
  ``no_signal`` list: in-window items with a price but no offer signal. Some
  leaflet tiles are plain-price items (e.g. "Alpsko mleko 5.29", the ribeye),
  so the leaflet can only be checked against them if they are passed in.
* Leaflet readings (``LeafletReading``), one per tile per reader:
  - ``vlm``: new_price, name and Lidl Plus are trustworthy; old price and %
    are NOT (the model fabricates them), so a vlm reading can never carry them.
    ``__post_init__`` enforces that.
  - ``ocr``: may carry old price and %, which count only once they reconcile.

The printed leaflet is authoritative. The API is a candidate list.

Rules (applied in this order)
-----------------------------
R0  Tiles. Readings of the same page with the same new_price are merged into
    one tile when they are the same tile: OCR and VLM readings pair by name
    similarity, or 1:1 when the page has exactly one of each at that price.
    Identical duplicate VLM readings collapse. Readers disagreeing on Lidl
    Plus leave the tile's Lidl Plus status UNKNOWN.

R0b Suspected fabrications. When >= 3 VLM-only tiles on one page share one
    price and one first word, they are treated as one range promotion that the
    model split into invented "tiles" (kw39 p41: an UNPRICED "-30% z Lidl Plus"
    Knorr promo came back as five "Knorr ... 2.69" tiles, and 2.69 is the old
    price of the neighbouring biscuits tile). Such tiles never pair with an API
    item, so a coincidental API price cannot "confirm" them. They are
    quarantined as ``vlm-suspect-repeated-price``. An OCR reading of the same
    tile lifts the suspicion.

R0c OCR backing of VLM-only tiles. Every Lidl product tile prints its price
    in the big price structure that OCR detects (125/125 truth tiles found, 0
    false detections), so a VLM-only tile (no OCR reading merged into it) can
    confirm an API item only when OCR read the SAME price on the SAME page and
    left it unexplained. Per (page, price): the OCR-only tiles that did not
    confirm an API item of their own are the spare printed prices, and they
    back the VLM-only tiles one each. If there are more VLM-only tiles than
    spare prices, the leaflet does not say which VLM tile is the real one, so
    ALL of them are unbacked. An unbacked tile may still pair (so the API item
    is not reported as unmatched), but the pair is quarantined:
      - ``vlm-only-page-without-ocr-price`` when OCR found no price at all on
        the page (kw39 p5: an app advert with a till receipt; the VLM read
        "BREZGLUT. TESTENI 1,99" off it, and a listed "Rezanci 1.99" would
        have confirmed it);
      - ``vlm-only-without-ocr-price`` otherwise (kw39 p47: the VLM invented
        "Wok Spices 0.69" from packaging text beside the real "Začimbe 0.69"
        tile, and confirmed a brand-only API "VITASIA 0.69" with it).
    In R3, pairs with a tile that can be backed are taken before pairs with
    one that cannot, so an invented duplicate cannot take the API item from
    the real tile (kw39: the p5 receipt's "SMETANA ZA KUH 1.69" had taken the
    Latteria cream from the real p34 tile as a price conflict).
    Cost on kw39: one genuine deal (p51 Napitek 0.89: the VLM also put a false
    0.89 on the čips tile, which prints 0.99, and OCR read one 0.89).
    Off when ``ocr_pages`` is None (no OCR in the run).

R0d Targeted crop rereads (``lidl_crops``). A cached crop must belong to the
    exact page image (SHA256), contain one OCR price structure, and locate
    that structure at the full-page OCR's exact price coordinates. Its new
    price must agree with both full-page readers. The crop VLM's COMPLETE
    label name, including brand, must independently occur in crop OCR text
    (case, accents and punctuation normalized; no fuzzy word matching).
    Only then may it repair the paired names. Same-price collisions require
    one unique matching VLM product name. If one OCR price has multiple VLM
    variant names with identical brand/LP/date/pack, add the crop's proven
    common label and retain the originals as unbacked readings (R0c), never
    arbitrarily pick a variant. Existing LP votes/dates stay intact.
    Crop old/% may supplement OCR only when OCR and crop VLM agree AND R1
    reconciles a plain percentage badge; a VLM value alone is never used.

R1  OCR old price / % count only when they RECONCILE: old > new, and the
    printed % is Lidl's truncated badge, so the implied reduction
    (1 - new/old) * 100 must lie in [printed, printed + 1). Otherwise the OCR
    old/% are ignored (the new price still counts).

R2  Name match. Names are compared after stripping diacritics, case-folding,
    dropping function words, freshness words (svež/sveža...) and the house
    brand words (Naše nam paše / Naša), truncating tokens to 5 characters and
    letting a token match any token it is a prefix of (API abbreviations like
    "Pišč. ... dod. bed."). API product tokens come from the API title only;
    leaflet product tokens exclude the leading all-caps brand line.
      STRONG  every (non-numeric) product token of the API title appears in
              the leaflet name ("Mango" in "Mango sorta: Osteen"), OR every
              product token of the leaflet name appears in the API name
              ("Suhi ocvirki" in "Domači suhi ocvirki", "Kuhinjske krpe" in
              "W5 Kuhinjske krpe, 10 kosov"). Sharing some words while each
              side keeps a word of its own is NOT a match: "Piščančja
              prekajena klobasa" vs "Piščančja posebna klobasa" are different
              products. (Measured on kw39: every true pair satisfies this.)
      BRAND   no shared product token, but identical brand tokens (e.g. the
              leaflet's "NAŠA RIBARNICA Sveža šarenka" and the API's "Naša
              ribarnica Sveža postrv"). Accepted only when it is the ONLY API
              item of that brand at that price and the ONLY tile of that brand
              at that price.
      BRAND-DIFFERS  the product words are identical on both sides but the
              brands contradict ("PILOS Kavni napitek" vs API "Bellarom Kavni
              napitek"; "DULANO" vs "Dal Salumiere" panceta). Exact price only.
      TYPO    no other tier, but the same words in the same order, at least
              one of them with exactly one letter missing on one side ("Sveži
              kunc" / "Sveži kunec"). Scored as STRONG, below any exact one. A
              CHANGED letter never counts ("mleko" is milk, "mleto" minced).
      When the brands contradict and the product words are NOT identical, it is
      no match ("PICKIES Piščančja prekajena klobasa" vs "Pikok Hribovska
      piščančja klobasa" share two words but are different products).
      Anything weaker is not a name match. In particular one shared generic
    noun ("klobasa") is not enough; that is how accidental same-price
    collisions (several products at 2.99) would slip through.

R3  One-to-one assignment. Every (tile, candidate) pair that is a STRONG
    match, or an exact-price BRAND match, is ranked by name similarity first,
    then exact price first, and taken greedily; each tile and each candidate
    is used at most once. Name wins over price, so a tile whose name clearly
    belongs to a differently-priced API item is reported as a conflict (R5)
    instead of being "confirmed" by some other product at the same price.
    Among equally good names, a candidate the tile's printed badge fits (R4c)
    is taken first.
R3b A BRAND or BRAND-DIFFERS match is a pair only when something beyond brand +
    price corroborates it (see R4: a reconciled OCR old price and % equal to
    the API's, or the exact printed pack size). An uncorroborated one is not a
    pair at all: the tile stays free for R8, where the leaflet may prove it
    alone, and the API item stays free for its real tile; if it finds none it
    is quarantined ``not-in-leaflet-reading`` naming the refused tile. (kw39
    p50: API brand-only "VITASIA Rižev kis" 0.79 had consumed the printed,
    twice-read "VITASIA Zeleni čaj" 0.79. Its fields must not be used.)

R4  VERIFIED. A matched pair with new_price exactly equal (Decimal equality)
    and a Lidl Plus status on the leaflet equal to the API's. When the two
    leaflet readers disagree on Lidl Plus, the API is the third vote: 2 of 3
    decide (flag ``lidl-plus-2-of-3:api+<reader>``). It ships with the
    API's name, old price, % and dates, flagged
    ``leaflet-confirmed:<sources>`` (e.g. ``leaflet-confirmed:vlm``) and
    ``leaflet-page:<n>``. R4b: when the API has no old price and no %, a
    reconciled OCR old/% (R1) is used (flag ``old-price-from-leaflet``).
    It is still quarantined when:
      - the leaflet's Lidl Plus status is unknown or differs
        (``lidl-plus-mismatch`` / ``leaflet-lidl-plus-unknown``);
      - a reconciled OCR old price or % differs from the API's
        (``api-leaflet-old-price-conflict``);
      - the tile prints its own end date ("velja do 26. 9.") that is LATER
        than the API's window end (``api-leaflet-date-conflict``). An EARLIER
        tile date replaces the API's end date (flag
        ``valid_to-from-leaflet``): the leaflet is authoritative, and a
        misread can then only hide days, never invent them;
      - a correctness-affecting API flag is present (R7).
      - a crop proves the price is a lower bound ("že od"); the API cannot
        turn that into an exact price (``leaflet-from-price-api-match``).
    Its end date is never later than the leaflet's own end
    (``leaflet_window``): a later API end is clipped, flag
    ``valid_to-clipped-to-leaflet``.
    Ambiguity: if other API items at the same price match the tile's name
    exactly as well as the chosen one (kw39: six "Esmara ... jakna" at 13.99
    for one "Jakna" tile), the leaflet does not say which one it prints. They
    are merged into ONE Deal (``ambiguous-api-match-merged:<erps>``, the
    leaflet's name) only if every one of them has the same price, Lidl Plus
    status, conditional status and windows, and no correctness flag. The old
    price and % are kept only if all of them carry the same ones
    (``:old-price-withheld`` otherwise). Otherwise it is quarantined as
    ``ambiguous-api-match``.
R4c Ambiguity settled by the printed badge: when OCR read a "-P%" badge on the
    tile, the one tied candidate that carries exactly P AND whose own
    old/new/% reconcile (R1) is the printed one; when OCR saw a bare price (no
    badge, no old/%), the one tied candidate with no old price and no %.
    Flag ``ambiguous-api-match-resolved-by-badge:<-P%|no-badge>``. The badge
    only SELECTS; the old price and % that ship are the API's. kw39: the same
    "ESMARA MEN Moška jakna" 14.99 is printed plain on p33 and "-34%" on p57;
    the API has a plain 14.99 item (24.-26.9.) and a 22.99/-34% one (28.-30.9.).
    A BRAND-DIFFERS match (R2) is treated like a BRAND match below.
    A BRAND or BRAND-DIFFERS match needs corroboration beyond price + name:
      - a reconciled OCR old price and % equal to the API's, or
      - the EXACT pack size printed on the tile ("550 g / 1 kg = 8.53") equal
        to the API's packaging ("550 g"). Approximate ("pribl. 1 kg"), per-kg
        and per-piece packs never count.
    Otherwise R3b never pairs them. Same brand + same price alone is
    not enough: the kw39 ribeye and ramsteak are both "Žar legende" at 6.19.
    (Both are 300 g too. That is why the brand tier also requires the brand
    to be unique at that price on both sides, and why a STRONG name match to
    another API item always wins over a brand match.)

R5  Disagreement. A STRONG name match whose prices differ is quarantined as
    ``api-leaflet-price-conflict: api X, leaflet Y (page N '<name>')``. The
    API item is consumed by it: it never reappears as unmatched.
R5b The leaflet is authoritative (lidl.si says so), so the conflicting TILE
    is then judged on its own by R8, exactly like a tile with no API partner,
    flagged ``leaflet-over-api:<erp> api X``. It ships only if the leaflet
    readers prove it by themselves (kw39: ribeye 6.19 printed, API 6.49).

R6  API-only. A candidate with no matched tile is quarantined as
    ``not-in-leaflet-reading``, never dropped. R6b (OFF unless
    ``ship_website_only``): a clean one ships flagged ``website-only``
    instead (``website_only_ok``). If some unmatched tile has the
    same price but an unrelated name, that tile is named in the reason for the
    reviewer (it is NOT treated as a match). Unmatched ``listed`` items are
    ordinary shelf prices, not offers; they are returned in
    ``ReconcileResult.unmatched_listed`` rather than turned into Deals.

R7  API flags. Passed through as ``api:<flag>``. These quarantine because
    they make the price or its availability uncertain: price_not_valid_in_window,
    price_changes_in_window, price_record_starts/ends, sold_out,
    deletedPrice_differs, pct_inconsistent, old_price_not_higher,
    multiple_price_regions, no_name, no_price. A ``conditional`` price
    ("-80% na drugi kos", "2+1 gratis") quarantines as ``conditional-price``:
    the number alone is not what one item costs, UNLESS R7b
    (``conditional_kind``) proves what it means from the numbers: then it
    goes on through R4 like any other offer, with ``condition_kind`` set. An API item with more than
    one window in range quarantines as ``multiple-api-windows``.
    Informational flags (bonus_quantity, open_ended_window, ...) only flag.
R7c ``deletedPrice_differs`` (the API record holds two different old prices)
    is settled by the leaflet, and then only flags
    (``api-flag-settled-by-leaflet:...``), when OCR read the struck-through
    price (consensus) equal to the API's old price AND the numbers prove the
    offer: R1 on OCR's own old/% for a plain discount, R7b's arithmetic for
    "-P% na drugi kos" (there OCR keeps the old price out of its reading, and
    the badge % is often unreadable). kw39 p10 Bon Gelati: printed 2.59 /
    1.29 "-50% na drugi kos", API old 2.59 and a stray deletedPrice 3.99.

R8  Leaflet-only tiles (no API partner):
      - OCR + VLM on the same tile, new prices equal, OCR old/% reconciled
        (R1), both readers agreeing on Lidl Plus, and a known validity for
        the page (``page_validity``): ships with the VLM name and the OCR
        old/%, flagged ``leaflet-only-confirmed:ocr+vlm``.
        When ``page_validity`` has no entry for the page, the page window is
        INFERRED from the API: at least INFER_MIN shipped, leaflet-confirmed
        API items on that page, all with one identical window (flag
        ``page-validity-inferred-from-api``). A tile's own EARLIER "velja do"
        date still shortens it, and, as in R4, the end is never later than
        the leaflet's own end (``valid_to-clipped-to-leaflet:page ...``; kw39
        Vitasia pages print no end, the API says 3.10., the leaflet 30.9.).
      - R8b, a tile that prints only a price (many Lidl tiles have no old
        price or badge at all: kw39 Coca-Cola 3.99, Laško 4.19, Vitasia
        Gyoza 2.69): the same, with "OCR old/% reconciled" replaced by
        ``plain_price_tile``: OCR joined the tile by its own name text and saw
        no badge and no old/%. Ships with no old price, flagged
        ``leaflet-only-confirmed:ocr+vlm:no-old-price``. A tile whose OCR
        name is missing, or whose OCR saw any badge (a "4+2 gratis" flag is a
        badge), stays quarantined.
      - R8c, a crop-proven "že od" lower bound: both crop readers must read
        that qualifier, and R0d must prove the name/price. The original OCR
        must have a flag badge, no old/%; all R8 name, LP and validity gates
        still apply. Ships with ``condition_kind="from-price"`` and
        ``condition="že od"``. The email MUST print the qualifier alongside
        the price; old price and discount remain unknown (None).
        A generic/unknown badge, including multi-buys, never qualifies.
      - Otherwise quarantined: ``leaflet-only-unverified`` (VLM alone, OCR
        alone, or OCR not reconciled), ``leaflet-only-no-validity``,
        ``leaflet-no-price`` (a tile reported without a readable price), or
        ``leaflet-duplicate-of:<erp>`` when a verified API item already has
        this name and price (the same product printed twice).
        ``leaflet-only-unverified`` names every missing proof: single reader,
        OCR old/% not reconciled, a badge OCR cannot prove, a name with one
        reader, Lidl Plus not agreed.

A Deal never gets an old price or a % from the VLM.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Literal, Sequence

from .lidl_api import LidlCandidate
from .models import Deal
from .vlm import VlmReading

RETAILER = "Lidl"
LIDL_PLUS = "Lidl Plus"

Source = Literal["ocr", "vlm"]


# ======================================================================
# Readings and tiles
# ======================================================================

@dataclass(frozen=True, slots=True)
class LeafletReading:
    """One tile as one reader saw it. ``bbox`` is (x0, y0, x1, y1) page pixels."""

    source: Source
    page: int | None
    new_price: Decimal | None
    old_price: Decimal | None = None
    pct: Decimal | None = None
    lidl_plus: bool | None = None          # None = this reader has no opinion
    name: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    valid_to_text: str | None = None       # tile-level "velja do 26. 9.", as printed
    unit: str | None = None                # pack text as printed, e.g. "550 g / 1 kg = 8.53"
    # OCR only: every text line printed around this tile's price that belongs
    # to it (the name block, pack and unit-price lines, "cena za 1 kos: ...").
    text: tuple[str, ...] = ()
    raw: object = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.source not in ("ocr", "vlm"):
            raise ValueError(f"unknown reading source {self.source!r}")
        if self.source == "vlm" and (self.old_price is not None or self.pct is not None):
            raise ValueError("a VLM reading may not carry old_price or pct: they are fabricated")
        for v in (self.new_price, self.old_price, self.pct):
            if v is not None and not isinstance(v, Decimal):
                raise TypeError(f"money must be Decimal, got {type(v).__name__}: {v!r}")


def from_vlm(r: VlmReading) -> LeafletReading:
    """Adapter. old_price/pct stay None by construction."""
    return LeafletReading(source="vlm", page=r.page, new_price=r.new_price,
                          lidl_plus=r.lidl_plus, name=r.name, bbox=None,
                          valid_to_text=r.valid_to_text, unit=r.unit, raw=r.raw)


def ocr_saw_bare_price(r: LeafletReading) -> bool:
    """An OCR reading of a tile that prints ONLY a price: OCR looked for a
    badge and found none (``raw["badge"] is None``) and read no old price or %.
    A reading without that record (no ``raw``) has no opinion: False."""
    return (r.source == "ocr" and isinstance(r.raw, dict) and "badge" in r.raw
            and r.raw["badge"] is None and r.old_price is None and r.pct is None)


def ocr_reconciled(r: LeafletReading) -> bool:
    """R1: old, new and the printed (truncated) % agree."""
    if r.source != "ocr" or None in (r.new_price, r.old_price, r.pct):
        return False
    new, old, pct = r.new_price, r.old_price, r.pct
    if old <= new or new <= 0 or pct != pct.to_integral_value() or not 0 < pct < 100:
        return False
    implied = (1 - new / old) * 100
    return pct <= implied < pct + 1


@dataclass(slots=True)
class LeafletTile:
    """One printed tile, possibly seen by several readers."""

    page: int | None
    new_price: Decimal | None
    readings: list[LeafletReading]
    # True when an OCR reading joined this VLM tile because its OWN text shares
    # a PRODUCT word (not a brand word) with the VLM name, not merely as the
    # last leftover pair: the name then has two independent readers too.
    ocr_name_paired: bool = False

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(sorted({r.source for r in self.readings}))

    @property
    def name(self) -> str | None:
        for src in ("vlm", "ocr"):
            for r in self.readings:
                if r.source == src and r.name:
                    return r.name
        return None

    @property
    def lidl_plus_votes(self) -> set[bool]:
        return {r.lidl_plus for r in self.readings if r.lidl_plus is not None}

    @property
    def lidl_plus_by_source(self) -> dict[str, bool]:
        """Each reader's single opinion; a reader contradicting itself has none."""
        out: dict[str, bool] = {}
        for src in self.sources:
            v = {r.lidl_plus for r in self.readings if r.source == src and r.lidl_plus is not None}
            if len(v) == 1:
                out[src] = v.pop()
        return out

    @property
    def lidl_plus(self) -> bool | None:
        """None when no reader has an opinion OR readers disagree."""
        v = self.lidl_plus_votes
        return next(iter(v)) if len(v) == 1 else None

    @property
    def ocr_old_pct(self) -> tuple[Decimal, Decimal] | None:
        """Old price and % from a reconciled OCR reading; None otherwise."""
        for r in self.readings:
            if ocr_reconciled(r) and r.new_price == self.new_price:
                return r.old_price, r.pct
        return None

    @property
    def ocr_badge_pct(self) -> Decimal | None:
        """The "-NN%" badge OCR read on this tile (consensus, a discount-shaped
        badge), whether or not an old price was read. None unless every OCR
        reading of the tile that has one agrees. Selection evidence only
        (R4 tie-break); it never ships on its own."""
        v = {r.pct for r in self.readings
             if r.source == "ocr" and r.pct is not None and r.new_price == self.new_price}
        return v.pop() if len(v) == 1 else None

    @property
    def ocr_printed_old(self) -> Decimal | None:
        """The struck-through price OCR read on this tile (consensus of all its
        renderings), INCLUDING one withheld from the reading because the badge
        is conditional ("-50% na drugi kos": there it is the first piece's
        price). Only ever compared with another reader's value, never shipped
        on its own."""
        v = set()
        for r in self.readings:
            if r.source != "ocr" or r.new_price != self.new_price:
                continue
            old = r.old_price
            if old is None and isinstance(r.raw, dict) and r.raw.get("old_price"):
                old = Decimal(str(r.raw["old_price"]))
            if old is not None:
                v.add(old)
        return v.pop() if len(v) == 1 else None

    @property
    def pack(self) -> str | None:
        """Exact pack size as printed ('550g', '12x85g'); None if absent or approximate."""
        for r in self.readings:
            p = pack_size(r.unit)
            if p:
                return p
        return None

    @property
    def valid_to_text(self) -> str | None:
        for r in self.readings:
            if r.valid_to_text:
                return r.valid_to_text
        return None

    def describe(self) -> str:
        return f"page {self.page} {self.name or '?'!r} {self.new_price} [{'+'.join(self.sources)}]"


# ======================================================================
# Names
# ======================================================================

_STOP = frozenset("""
z s v in za na iz brez od do ali ter le pri po ob k h
sve svez sveza sveze svezi svezo
nase nam pase nasa nasi
pribl kg g l ml kos kosov kom pack xxl family druzinsko pakiranje piknik
""".split())
_TOKEN = re.compile(r"[a-z0-9]+")
_STEM = 5


def fold(s: str) -> str:
    """Strip diacritics and case: 'Sveža ŠARENKA' -> 'sveza sarenka'."""
    d = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in d if not unicodedata.combining(ch)).casefold()


def _stems(text: str | None) -> list[str]:
    out = []
    for t in _TOKEN.findall(fold(text or "")):
        if t in _STOP or len(t) < 3 and not t.isdigit():
            continue
        if t.isdigit() and len(t) >= 4:
            continue                      # article numbers like "9973"
        out.append(t[:_STEM])
    return list(dict.fromkeys(out))


def split_leaflet_name(name: str | None) -> tuple[str, str]:
    """('ŽAR LEGENDE', 'Svež ribeye steak'): leading all-caps words are the brand.

    If every word is upper case there is no way to tell, so it is all product.
    """
    words = (name or "").split()
    i = 0
    while i < len(words):
        w = words[i]
        letters = [c for c in w if c.isalpha()]
        if letters and not all(c.isupper() for c in letters):
            break
        i += 1
    if i == len(words):
        return "", " ".join(words)
    return " ".join(words[:i]), " ".join(words[i:])


def _shared(a: list[str], b: list[str]) -> int:
    """Tokens of a that match some token of b (equal, or prefix of len >= 3)."""
    n = 0
    used: set[int] = set()
    for x in a:
        for j, y in enumerate(b):
            if j in used:
                continue
            if x == y or (min(len(x), len(y)) >= 3 and (x.startswith(y) or y.startswith(x))):
                used.add(j)
                n += 1
                break
    return n


@dataclass(frozen=True, slots=True)
class NameMatch:
    tier: Literal["strong", "brand", "none"]
    score: float                         # for ranking only


def brand_only(c: LidlCandidate) -> bool:
    """The API title names a brand and no product: "VITASIA" (brand VITASIA or
    none), "WHISKAS". An all-caps title with no brand counts as a brand line."""
    title = (c.title or c.name or "").strip()
    ts = _stems(title)
    if not ts:
        return False
    if c.brand and set(ts) <= set(_stems(c.brand)):
        return True
    letters = [ch for ch in title if ch.isalpha()]
    return not c.brand and all(ch.isupper() for ch in letters)


def _cand_parts(c: LidlCandidate) -> tuple[list[str], list[str]]:
    if brand_only(c):
        return list(dict.fromkeys(_stems(c.brand) + _stems(c.title or c.name))), []
    prod = _stems(c.title) or _stems(c.name)
    return _stems(c.brand), prod


def _leads_with(name: str | None, brand: list[str]) -> bool:
    """The name starts with these brand stems, whatever its case."""
    return bool(brand) and sorted(_stems(name)[:len(brand)]) == sorted(brand)


def _tile_parts(t: LeafletTile) -> tuple[list[str], list[str]]:
    brand, prod = split_leaflet_name(t.name)
    return _stems(brand), _stems(prod) or _stems(brand)


def name_match(tile: LeafletTile, cand: LidlCandidate) -> NameMatch:
    """R2."""
    t_brand, t_prod = _tile_parts(tile)
    c_brand, c_prod = _cand_parts(cand)
    if not c_prod:
        # A brand-only API name ("VITASIA") says nothing about WHICH product:
        # at best a brand match (kw39: "VITASIA 1.99", 700 ml, had "confirmed"
        # a 280 g spring-roll tile at 1.99).
        return NameMatch("brand", 0.5) if _leads_with(tile.name, c_brand) else NameMatch("none", 0.0)
    # A leaflet brand line can hold product words and vice versa ("WHISKAS"),
    # so product tokens are matched against the whole leaflet name.
    t_all = list(dict.fromkeys(t_prod + t_brand))
    shared = _shared(c_prod, t_all) if c_prod else 0
    c_words = [x for x in c_prod if not x.isdigit()]
    t_words = [x for x in t_prod if not x.isdigit()]
    api_covered = bool(c_words) and _shared(c_words, t_all) == len(c_words)
    tile_covered = bool(t_words) and _shared(t_words, c_prod + c_brand) == len(t_words)
    if shared >= 1 and (api_covered or tile_covered):
        denom = max(1, (len(c_prod) * len(t_all)) ** 0.5)
        score = round(1 + shared / denom, 3)
        brands_contradict = (t_brand and c_brand and not _shared(t_brand, c_brand + c_prod)
                             and not _shared(c_brand, t_all))
        if not brands_contradict:
            return NameMatch("strong", score)
        if shared == len(c_prod) == len(t_prod):
            return NameMatch("brand-differs", score - 1)
        return NameMatch("none", 0.0)       # "PICKIES ... piščančja klobasa" vs "Pikok ..."
    if t_brand and c_brand and sorted(t_brand) == sorted(c_brand):
        return NameMatch("brand", 0.5)
    if _typo_same(tile.name, cand.title or cand.name, c_brand):
        return NameMatch("strong", 1.0)     # below every exact STRONG score (> 1)
    return NameMatch("none", 0.0)


def _words(text: str | None, drop: Sequence[str] = ()) -> list[str]:
    """Whole folded words (not 5-letter stems), function words and brand stems dropped."""
    return [w for w in _TOKEN.findall(fold(text or ""))
            if w not in _STOP and len(w) >= 3 and not w.isdigit() and w[:_STEM] not in drop]


def _one_letter_missing(a: str, b: str) -> bool:
    """b is a with exactly one letter left out, or the other way round.
    Never a changed letter: "mleko" (milk) vs "mleto" (minced) must not match."""
    if len(a) < len(b):
        a, b = b, a
    if len(a) != len(b) + 1 or len(b) < 4:
        return False
    return any(a[:i] + a[i + 1:] == b for i in range(len(a)))


def _typo_same(tile_name: str | None, api_name: str | None, api_brand: Sequence[str]) -> bool:
    """R2 typo tier: the same words on both sides, in order, where at least one
    word differs by exactly one missing letter (kw39: the VLM's "Sveži kunc"
    for "Sveži kunec", "Masleni rogliček" for "Masleni rogljiček")."""
    brand = list(api_brand) + _stems(split_leaflet_name(tile_name)[0])
    a, b = _words(tile_name, brand), _words(api_name, brand)
    if not a or len(a) != len(b):
        return False
    typos = 0
    for x, y in zip(a, b):
        if x == y:
            continue
        if not _one_letter_missing(x, y):
            return False
        typos += 1
    return typos >= 1


# ======================================================================
# Tile building (R0)
# ======================================================================

def _name_key(name: str | None) -> tuple[str, ...]:
    return tuple(sorted(_stems(name)))


_PACK = re.compile(r"^(\d+x)?\d+(\.\d+)?(g|kg|ml|l)$")


def pack_size(text: str | None) -> str | None:
    """'550 g / 1 kg = 8.53' -> '550g'; '12x85 g' -> '12x85g'.

    None for anything approximate or per-kg ("pribl. 1 kg", "za kg", "kos"):
    only an exact printed pack size is evidence.
    """
    if not text:
        return None
    t = fold(text).split("/")[0].strip()
    if "pribl" in t or "za" in t.split():
        return None
    t = t.replace(" ", "").replace(",", ".")
    return t if _PACK.match(t) else None


SUSPECT_REPEAT = 3
INFER_MIN = 2          # confirmed API items that must agree before a page window is inferred


def suspect_repeats(tiles: Sequence[LeafletTile]) -> set[int]:
    """R0b: indexes of VLM-only tiles that look like one range promotion read as
    several priced tiles: >= SUSPECT_REPEAT tiles on one page with the same
    price and the same first word ("Knorr Porova juha", "Knorr Blejska juha",
    ... all 2.69 on kw39 p41, where the page prints NO price for Knorr: 2.69 is
    the old price of the neighbouring biscuits tile)."""
    groups: dict[tuple, list[int]] = {}
    for i, t in enumerate(tiles):
        if t.new_price is None or t.sources != ("vlm",):
            continue
        first = (_TOKEN.findall(fold(t.name or "")) or [""])[0]
        groups.setdefault((t.page, t.new_price, first), []).append(i)
    return {i for g in groups.values() if len(g) >= SUSPECT_REPEAT for i in g}


def _share_product_word(a: str | None, b: str | None) -> bool:
    """The two names share two product words, or the one word of a one-word
    product ("VITASIA Liči"). Brand words do not count: every tile on a Vitasia
    page starts "VITASIA". One generic word does not count either: an OCR name
    read as just "kompl" or "omaka" fits half the page."""
    brand = set(_stems(split_leaflet_name(a)[0])) | set(_stems(split_leaflet_name(b)[0]))
    pa = [x for x in _stems(a) if x not in brand]
    pb = [x for x in _stems(b) if x not in brand]
    need = min(2, max(len(pa), len(pb)))
    return need > 0 and _shared(pa, pb) >= need


def plain_price_tile(t: LeafletTile) -> bool:
    """R8b: a tile both readers saw, with no old price or badge to reconcile.
    The OCR reading must have joined the VLM tile by its OWN name text
    (``ocr_name_paired``), so price AND product each have two readers, and
    every OCR reading on it must have seen a bare price (no badge, no old/%)."""
    ocr = [r for r in t.readings if r.source == "ocr"]
    return (t.ocr_name_paired and bool(ocr)
            and all(ocr_saw_bare_price(r) and r.new_price == t.new_price for r in ocr))


def from_price_tile(t: LeafletTile) -> bool:
    """R8c: a two-reader crop proof of the literal lower-bound qualifier."""
    ocr = [r for r in t.readings if r.source == "ocr"]
    return (t.ocr_name_paired and bool(ocr)
            and all(r.new_price == t.new_price and r.old_price is None and r.pct is None
                    and isinstance(r.raw, dict) and r.raw.get("badge") == "flag"
                    and r.raw.get("old_price") is None and r.raw.get("pct") is None
                    and r.raw.get("crop_confirmation", {}).get("from_price") is True
                    and r.raw["crop_confirmation"].get("name") == r.name for r in ocr))


def _unverified_why(t: LeafletTile) -> list[str]:
    """Every R8 proof this tile lacks, in words (quarantine reasons)."""
    if len(t.sources) == 1:
        return ["single reader"]
    out = []
    ocr = [r for r in t.readings if r.source == "ocr"]
    if t.ocr_old_pct is None and not (plain_price_tile(t) or from_price_tile(t)):
        badges = sorted({r.raw.get("badge") for r in ocr if isinstance(r.raw, dict)
                         and r.raw.get("badge")})
        if any(r.old_price is not None or r.pct is not None for r in ocr):
            out.append("OCR old/% not reconciled")
        elif badges:
            out.append(f"OCR saw a badge ({'/'.join(badges)}) it cannot prove")
        if not t.ocr_name_paired:
            names = [r.name for r in ocr]
            out.append(f"name has one reader (OCR name {names[0] if names else None!r})")
        if not out:
            out.append("OCR old/% not reconciled")
    if len(t.lidl_plus_votes) != 1 or any(r.lidl_plus is None for r in t.readings):
        out.append("Lidl Plus not agreed")
    return out


def build_tiles(readings: Iterable[LeafletReading]) -> list[LeafletTile]:
    by_page: dict[int | None, list[LeafletReading]] = {}
    for r in readings:
        by_page.setdefault(r.page, []).append(r)
    tiles: list[LeafletTile] = []
    for page in sorted(by_page, key=lambda p: (p is None, p or 0)):
        rs = by_page[page]
        groups: dict[Decimal | None, list[LeafletReading]] = {}
        for r in rs:
            groups.setdefault(r.new_price, []).append(r)
        for price, grp in groups.items():
            if price is None:
                tiles.extend(LeafletTile(page, None, [r]) for r in grp)
                continue
            vlm_tiles: list[LeafletTile] = []
            seen: dict[tuple[str, ...], LeafletTile] = {}
            for r in (r for r in grp if r.source == "vlm"):
                k = _name_key(r.name)
                if k in seen:                       # identical duplicate emitted twice
                    seen[k].readings.append(r)
                    continue
                t = LeafletTile(page, price, [r])
                seen[k] = t
                vlm_tiles.append(t)
            ocr = [r for r in grp if r.source == "ocr"]
            free = list(vlm_tiles)
            unpaired: list[LeafletReading] = []
            for r in ocr:
                best = None
                if r.name:
                    rs_ = _stems(r.name)
                    scored = [(_shared(rs_, _stems(t.name)), t) for t in free]
                    scored = [x for x in scored if x[0] > 0]
                    if scored:
                        best = max(scored, key=lambda x: x[0])[1]
                if best is not None:
                    best.readings.append(r)
                    best.ocr_name_paired = _share_product_word(r.name, best.name)
                    free.remove(best)
                else:
                    unpaired.append(r)
            if len(unpaired) == 1 and len(free) == 1:
                free[0].readings.append(unpaired.pop())
            tiles.extend(vlm_tiles)
            tiles.extend(LeafletTile(page, price, [r]) for r in unpaired)
    return tiles


# ======================================================================
# Reconciliation
# ======================================================================

QUARANTINE_API_FLAGS = (
    "price_not_valid_in_window", "price_changes_in_window", "price_record_starts",
    "price_record_ends", "sold_out", "deletedPrice_differs", "pct_inconsistent",
    "old_price_not_higher", "multiple_price_regions", "no_name", "no_price",
)


@dataclass(slots=True)
class MatchRecord:
    """Why a Deal came out the way it did: for audit and evaluation."""

    rule: str                          # verified | conflict | api-only | leaflet-only
    deal: Deal
    candidate: LidlCandidate | None
    tile: LeafletTile | None
    name_tier: str | None = None


@dataclass(slots=True)
class ReconcileResult:
    records: list[MatchRecord]
    unmatched_listed: list[LidlCandidate]

    @property
    def deals(self) -> list[Deal]:
        return [r.deal for r in self.records]

    @property
    def shipped(self) -> list[Deal]:
        return [d for d in self.deals if not d.is_quarantined]

    @property
    def quarantined(self) -> list[Deal]:
        return [d for d in self.deals if d.is_quarantined]


_DATE_TEXT = re.compile(r"(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?")


def parse_tile_date(text: str | None, year: int) -> date | None:
    """'velja do 26. 9.' -> date(year, 9, 26). The LAST d.m. in the text is the end."""
    if not text:
        return None
    found = _DATE_TEXT.findall(text)
    if not found:
        return None
    d, m = (int(x) for x in found[-1])
    try:
        return date(year, m, d)
    except ValueError:
        return None


def _window(c: LidlCandidate) -> tuple[date, date | None, bool]:
    ws = c.windows or c.all_windows
    start = min(w[0] for w in ws)
    ends = [w[1] for w in ws]
    end = None if any(e is None for e in ends) else max(ends)
    return start, end, len(ws) > 1


_ZA_KG = re.compile(r"\bza\s*kg\b")
_ZA_KOS = re.compile(r"\bza\s*kos\b")


def api_unit(c: LidlCandidate) -> str | None:
    """R9: "kg" for the API's "za kg" (discountText or packaging), "kos" for
    packaging "za kos", else None (no statement)."""
    pack = fold(c.packaging or "").strip()
    if c.price_unit == "kg" or _ZA_KG.fullmatch(pack):
        return "kg"
    return "kos" if _ZA_KOS.fullmatch(pack) else None


def leaflet_units(t: LeafletTile) -> set[str]:
    """R9: what the tile's readers say: "za kg" -> kg, "za kos" -> kos,
    "nepakirano" (loose) without "za kos" -> kg. "pribl. 1 kg" says nothing:
    the VLM wrote it for a jacket on kw39 p1."""
    out = set()
    for r in t.readings:
        text = fold(f"{r.name or ''} {r.unit or ''}")
        if _ZA_KG.search(text):
            out.add("kg")
        elif _ZA_KOS.search(text):
            out.add("kos")
        elif "nepakirano" in text:
            out.add("kg")
    return out


_SECOND = re.compile(r"^\s*-\s*(\d{1,2})\s*%\s*na\s+drugi\s+kos\s*$", re.I)
_BONUS_PACK = re.compile(r"^\s*(\d+)\s*\+\s*(\d+)\s*gratis\s*\*\*\s*$", re.I)
_PACK_COUNT = re.compile(r"^\s*(\d+)\s*x\s*\d", re.I)


def conditional_kind(c: LidlCandidate) -> tuple[str | None, str]:
    """R7b: the conditional prices whose meaning can be proven from the numbers.

    * "-P% na drugi kos": new is the second piece, old the first. Proven when
      old > new and the printed P is Lidl's truncated badge for them (the R1
      arithmetic: (1 - new/old) * 100 in [P, P+1)). kw39 Milka 1.99 / 0.39
      (80.4%), chicken breast 2.29 / 0.99 (56.8%).
    * "N+M gratis**": the ** footnote ("compared with our regular product")
      marks a bonus PACK: one price for the pack as sold. Proven when the
      packaging counts exactly N+M pieces ("14x95 g" for 10+4).
    Anything else (a real multi-buy "13+5 gratis", "ob nakupu 2 kosov") is
    not: the printed number is sometimes the bundle, sometimes one piece, and
    on kw39 p10 sometimes a per-litre price. Returns (kind, condition text) or
    (None, why not)."""
    reason = c.conditional_reason or ""
    if m := _SECOND.match(reason):
        pct, new, old = Decimal(m.group(1)), c.new_price, c.old_price
        if new is None or old is None:
            return None, "second-item without both prices"
        if not (0 < new < old):
            return None, f"second-item prices not ordered: {old} / {new}"
        implied = (1 - new / old) * 100
        if not pct <= implied < pct + 1:
            return None, f"second-item arithmetic fails: {old} -{pct}% != {new}"
        return "second-item", f"-{pct}% na drugi kos"
    if m := _BONUS_PACK.match(reason):
        n, extra = int(m.group(1)), int(m.group(2))
        pc = _PACK_COUNT.match(c.packaging or "")
        if pc is None or int(pc.group(1)) != n + extra:
            return None, f"bonus pack {n}+{extra} but packaging {c.packaging!r}"
        return "bonus-pack", f"{n}+{extra} gratis"
    return None, "price meaning not provable"


def website_only_ok(c: LidlCandidate) -> bool:
    """R6b, only with ``ship_website_only``: an offer on Lidl's website that no
    leaflet tile names. Nothing can cross-check it (on kw39 the website's
    price differed from the printed leaflet for ~4 of ~125 products that both
    show), so it ships flagged ``website-only`` for the email to label, and
    only when the record itself is clean: a price, an old price above it, a
    name, and one window with an end date."""
    return (c.new_price is not None and c.old_price is not None
            and c.old_price > c.new_price and bool(c.name)
            and len(c.windows) == 1 and c.windows[0][1] is not None
            and not c.conditional)


def old_price_doubt_settled(t: LeafletTile, c: LidlCandidate) -> bool:
    """R7c: the API's ``deletedPrice_differs`` flag says its record holds two
    different old prices. The printed leaflet settles which one is real when
    OCR read the struck-through price (consensus) equal to the API's old price
    AND the numbers prove the offer: R1 on OCR's own old/% for a plain
    discount, R7b's arithmetic for "-P% na drugi kos" (there OCR withholds the
    old price from its reading, and the badge % is often unreadable). kw39 p10
    Bon Gelati: printed 2.59 / 1.29 "-50% na drugi kos", API old 2.59 and a
    stray deletedPrice 3.99."""
    old = t.ocr_printed_old
    if old is None or old != c.old_price:
        return False
    if c.conditional:
        return conditional_kind(c)[0] == "second-item"
    return t.ocr_old_pct == (c.old_price, c.pct)


def _deal_from_candidate(c: LidlCandidate, leaflet_id: str, settled: Sequence[str] = ()) -> Deal:
    """``settled``: API flag prefixes the leaflet has settled (R7c); they are
    kept as flags but do not quarantine."""
    start, end, multi = _window(c)
    d = Deal(
        retailer=RETAILER, name=c.name, leaflet_id=leaflet_id, source_url=c.url,
        valid_from=start, valid_to=end, price_unit="kg" if api_unit(c) == "kg" else None,
        price_new=c.new_price, price_old=c.old_price, discount_published=c.pct,
        condition=c.conditional_reason, loyalty_required=LIDL_PLUS if c.lidl_plus else None,
        brand=c.brand, unit_price=c.base_price_text, retailer_category=c.category,
        retailer_item_id=c.erp,
    )
    for f in c.flags:
        d.flag(f"api:{f}")
        if f.startswith(tuple(settled)):
            d.flag(f"api-flag-settled-by-leaflet:{f}")
        elif f.startswith(QUARANTINE_API_FLAGS):
            d.quarantine(f"api-flag:{f}")
    if c.conditional:
        kind, why = conditional_kind(c)
        if kind is None:
            d.quarantine(f"conditional-price:{c.conditional_reason}: {why}")
        else:
            d.condition_kind = kind
            d.condition = why
            d.discount_published = None
            d.flag(f"conditional-proven:{kind}")
    if multi:
        d.quarantine(f"multiple-api-windows:{c.windows}")
    return d


def _brand_only_unique(c: LidlCandidate, t: LeafletTile,
                       pool: Sequence[tuple[LidlCandidate, bool]], tiles: Sequence[LeafletTile]) -> bool:
    """R2 for a brand-only API name: the brand is the only API item and the
    only tile of that brand at that price, or, failing that, the only ones of
    that brand at that price with this exact pack size (kw39: one of three
    VITASIA offers at 1.29 is 500 ml, and one VITASIA tile at 1.29 prints
    500 ml)."""
    bk = _cand_parts(c)[0]
    c_same = [c2 for c2, _ in pool if c2.new_price == c.new_price
              and (sorted(_cand_parts(c2)[0]) == sorted(bk) or _leads_with(c2.name, bk))]
    t_same = [t2 for t2 in tiles if t2.new_price == t.new_price and _leads_with(t2.name, bk)]
    if len(c_same) == 1 and len(t_same) == 1:
        return True
    pack = pack_size(c.packaging)
    return (pack is not None and t.pack == pack
            and sum(pack_size(c2.packaging) == pack for c2 in c_same) == 1
            and sum(t2.pack == pack for t2 in t_same) == 1)


def brand_corroboration(t: LeafletTile, c: LidlCandidate) -> str | None:
    """R2/R4: what, beyond brand + price, says a BRAND or BRAND-DIFFERS match is
    the same product: the reconciled OCR old price and % equal to the API's, or
    the exact printed pack size equal to the API's packaging. The flag to record,
    or None: then it is not a match at all (R3 never pairs it)."""
    op = t.ocr_old_pct
    if op is not None and op == (c.old_price, c.pct):
        return "name-match-corroborated:ocr-old-price"
    api_pack = pack_size(c.packaging)
    if api_pack and api_pack == t.pack:
        return f"name-match-corroborated:pack-size:{api_pack}"
    return None


def badge_fit(t: LeafletTile, c: LidlCandidate) -> bool | None:
    """R4c: does the API item fit what the tile prints above its price?
    OCR read a "-P%" badge: the item must carry exactly P, and its own
    old/new/% must reconcile (R1). OCR saw a bare price (no badge, no old/%):
    the item must carry no old price and no %. None: no evidence either way."""
    pct = t.ocr_badge_pct
    if pct is not None:
        return c.pct == pct and api_triple_reconciles(c)
    if any(ocr_saw_bare_price(r) and r.new_price == t.new_price for r in t.readings):
        return c.old_price is None and c.pct is None
    return None


def api_triple_reconciles(c: LidlCandidate) -> bool:
    """R1 applied to the API's own old / new / %."""
    new, old, pct = c.new_price, c.old_price, c.pct
    if None in (new, old, pct) or old <= new or new <= 0:
        return False
    return pct <= (1 - new / old) * 100 < pct + 1


_FURNITURE = re.compile(r"(\s+(nepakirano|za\s+kg|za\s+kos))+\s*$", re.I)
_WORD = re.compile(r"[^\W\d_]+")


def leaflet_name(t: LeafletTile, c: LidlCandidate, pool: Sequence[tuple[LidlCandidate, bool]]) -> tuple[str, str]:
    """The display name for a brand-only API item: the leaflet's own name.

    Of the readers' names, the one whose words are spelt as in lidl.si's own
    product names wins: +1 for a word found there with the same diacritics,
    -1 for one found only when diacritics are ignored (a misread č/š/ž).
    Ties go to the VLM (OCR names are often cut short). Trailing leaflet
    furniture ("nepakirano za kg") is dropped (the unit is in price_unit), and
    the API brand is prefixed when the name lacks it. Returns (name, source).
    """
    vocab: set[str] = set()
    for c2, _ in pool:
        vocab.update(w.casefold() for w in _WORD.findall(c2.name or ""))
    folded = {fold(w) for w in vocab}

    def score(name: str) -> int:
        s = 0
        for w in _WORD.findall(name):
            if w.casefold() in vocab:
                s += 1
            elif fold(w) in folded:
                s -= 1
        return s

    named = [(r.source, r.name) for r in t.readings if r.name]
    if not named:
        return c.name, "api"
    src, name = max(named, key=lambda sn: (score(sn[1]), sn[0] == "vlm"))
    name = _FURNITURE.sub("", name).strip()
    brand = c.brand or c.title or ""
    if brand and not _leads_with(name, _stems(brand)):
        name = f"{brand} {name}"
    return name, src


def reconcile(candidates: Sequence[LidlCandidate], readings: Iterable[LeafletReading], *,
              leaflet_id: str, leaflet_window: tuple[date, date | None],
              listed: Sequence[LidlCandidate] = (),
              page_validity: dict[int, tuple[date, date | None]] | None = None,
              year: int | None = None, leaflet_url: str = "",
              ocr_pages: Iterable[int] | None = None,
              ship_website_only: bool = False) -> ReconcileResult:
    """Apply R0-R8. Every candidate and every tile ends up in exactly one record,
    except unmatched ``listed`` items, which are returned separately.

    ``leaflet_window`` is the leaflet's own cover validity. It only dates
    quarantined leaflet-only Deals; a leaflet-only Deal can ship only with a
    ``page_validity`` entry for its page (pages 34/37 of kw39 are Fri-Sat only).
    """
    readings = list(readings)
    tiles = build_tiles(readings)
    # R0c: pages OCR read, and those where it found no printed price at all
    ocr_read = set(ocr_pages) if ocr_pages is not None else set()
    priced_ocr_pages = {r.page for r in readings if r.source == "ocr" and r.new_price is not None}
    no_price_pages = ocr_read - priced_ocr_pages
    # per (page, price): the VLM-only tiles and the OCR-only tiles
    vlm_only_at: dict[tuple, list[int]] = {}
    ocr_only_at: dict[tuple, list[int]] = {}
    for i, t in enumerate(tiles):
        if t.new_price is None or t.page not in ocr_read:
            continue
        if t.sources == ("vlm",):
            vlm_only_at.setdefault((t.page, t.new_price), []).append(i)
        elif t.sources == ("ocr",):
            ocr_only_at.setdefault((t.page, t.new_price), []).append(i)
    # before pairing: tiles that cannot be backed however the pairing goes
    maybe_backed = {i for k, vs in vlm_only_at.items()
                    if len(vs) <= len(ocr_only_at.get(k, ())) for i in vs}
    pool: list[tuple[LidlCandidate, bool]] = ([(c, False) for c in candidates]
                                              + [(c, True) for c in listed])
    if year is None:
        years = [w[0].year for c, _ in pool for w in (c.windows or c.all_windows)]
        year = max(set(years), key=years.count) if years else date.today().year
    page_validity = page_validity or {}

    # --- R2/R3: candidate pairs --------------------------------------
    brand_key = lambda stems: tuple(sorted(stems))  # noqa: E731
    cand_brand_price: dict[tuple, int] = {}
    for c, _ in pool:
        k = (brand_key(_cand_parts(c)[0]), c.new_price)
        cand_brand_price[k] = cand_brand_price.get(k, 0) + 1
    tile_brand_price: dict[tuple, int] = {}
    for t in tiles:
        k = (brand_key(_tile_parts(t)[0]), t.new_price)
        tile_brand_price[k] = tile_brand_price.get(k, 0) + 1

    suspects = suspect_repeats(tiles)
    pairs = []
    # R3b: brand-tier matches that nothing corroborates are not pairs. The tile
    # stays free for R8 (the leaflet may prove it alone) and the API item stays
    # free for its real tile; both are named in the API item's reason.
    uncorroborated: dict[int, list[int]] = {}
    for ti, t in enumerate(tiles):
        if t.new_price is None or ti in suspects:
            continue
        for ci, (c, _) in enumerate(pool):
            if c.new_price is None:
                continue
            m = name_match(t, c)
            same = c.new_price == t.new_price
            if m.tier == "strong":
                pairs.append((m.score, same, ti, ci, m.tier))
                continue
            if not same or m.tier not in ("brand", "brand-differs"):
                continue
            if m.tier == "brand" and brand_only(c):
                eligible = _brand_only_unique(c, t, pool, tiles)
            elif m.tier == "brand":
                bk = brand_key(_tile_parts(t)[0])
                eligible = (cand_brand_price.get((bk, c.new_price)) == 1
                            and tile_brand_price.get((bk, t.new_price)) == 1)
            else:
                eligible = True
            if not eligible:
                continue
            if brand_corroboration(t, c) is None:
                uncorroborated.setdefault(ci, []).append((ti, m.tier))
                continue
            pairs.append((m.score, same, ti, ci, m.tier))
    # A tile that OCR can back is preferred over one it cannot (kw39 p47: the
    # VLM invented "Wok Spices 0.69" beside the real "Začimbe 0.69" tile).
    unbackable = lambda ti: tiles[ti].sources == ("vlm",) and ocr_pages is not None \
        and ti not in maybe_backed  # noqa: E731
    # R4c: among equally good names, the candidate the tile's printed badge
    # fits is taken first, so two prints of one product (kw39: a plain 14.99
    # jacket on p33, a "-34%" 14.99 jacket on p57) each get their own item.
    fit_cache: dict[tuple[int, int], bool | None] = {}

    def fit(ti: int, ci: int) -> bool | None:
        if (ti, ci) not in fit_cache:
            fit_cache[(ti, ci)] = badge_fit(tiles[ti], pool[ci][0])
        return fit_cache[(ti, ci)]
    pairs.sort(key=lambda p: (unbackable(p[2]), -p[0], not p[1], fit(p[2], p[3]) is not True,
                              p[2], p[3]))
    tile_of: dict[int, tuple[int, str]] = {}
    used_c: set[int] = set()
    for score, same, ti, ci, tier in pairs:
        if ti in tile_of or ci in used_c:
            continue
        tile_of[ti] = (ci, tier)
        used_c.add(ci)
    # R0c: after pairing, an OCR-only tile that confirmed an API item of its own
    # is spent; only the rest can back VLM-only tiles, one each.
    unbacked: set[int] = set()
    if ocr_pages is not None:
        for k, vs in vlm_only_at.items():
            spare = [i for i in ocr_only_at.get(k, ()) if i not in tile_of]
            if len(vs) > len(spare):
                unbacked.update(vs)
        unbacked.update(i for i, t in enumerate(tiles) if t.sources == ("vlm",)
                        and t.new_price is not None and t.page not in ocr_read)

    def backing(ti: int) -> str:
        t = tiles[ti]
        k = (t.page, t.new_price)
        return (f"page {t.page} has {len(vlm_only_at.get(k, ()))} VLM-only tile(s) at "
                f"{t.new_price} and {len([i for i in ocr_only_at.get(k, ()) if i not in tile_of])} "
                f"unexplained OCR price(s) ({t.describe()})")
    # A tile whose best candidate is tied with another candidate at the same
    # price (several "Esmara jakna" at 13.99) is ambiguous: which one is printed?
    ties: dict[int, list[int]] = {}
    badge_resolved: dict[int, str] = {}
    for ti, (ci, tier) in list(tile_of.items()):
        best = next(p for p in pairs if p[2] == ti and p[3] == ci)
        ties[ti] = [p[3] for p in pairs if p[2] == ti and p[3] != ci and p[1] == best[1]
                    and abs(p[0] - best[0]) < 1e-9 and p[4] == tier]
        # R4c: the printed badge settles the tie when the chosen candidate fits
        # it and none of the tied ones does. It only SELECTS: old/% ship from
        # the API, and only when the API's own old/new/% reconcile (R1).
        if ties[ti] and fit(ti, ci) is True and all(fit(ti, j) is False for j in ties[ti]):
            ev = tiles[ti].ocr_badge_pct
            badge_resolved[ti] = (f"ambiguous-api-match-resolved-by-badge:"
                                  + (f"-{ev}%" if ev is not None else "no-badge")
                                  + f" (also {', '.join(pool[j][0].erp for j in ties[ti])})")
            ties[ti] = []

    def _agree(ci: int, others: list[int]) -> bool:
        """Every tied candidate says the same thing about what she would pay."""
        group = [pool[j][0] for j in [ci, *others]]
        key = lambda c: (c.new_price, c.lidl_plus, c.conditional, tuple(c.windows))  # noqa: E731
        return (all(j not in used_c for j in others)
                and len({key(c) for c in group}) == 1
                and not any(f.startswith(QUARANTINE_API_FLAGS) for c in group for f in c.flags))

    merged: dict[int, list[int]] = {}
    for ti, others in ties.items():
        ci = tile_of[ti][0]
        if others and _agree(ci, others):
            merged[ti] = others
            used_c.update(others)

    records: list[MatchRecord] = []
    verified_keys: dict[tuple, str] = {}

    # --- R4/R5: matched pairs ----------------------------------------
    overruled: dict[int, str] = {}      # R5b: conflict tiles, judged again by R8
    for ti, (ci, tier) in sorted(tile_of.items()):
        t, (c, is_listed) = tiles[ti], pool[ci]
        settled = (["deletedPrice_differs"] if c.new_price == t.new_price
                   and any(f.startswith("deletedPrice_differs") for f in c.flags)
                   and old_price_doubt_settled(t, c) else [])
        d = _deal_from_candidate(c, leaflet_id, settled)
        d.source_page = t.page
        if ti in badge_resolved:
            d.flag(badge_resolved[ti])
        if t.page is not None:
            d.flag(f"leaflet-page:{t.page}")
        if is_listed:
            d.flag("api:listed-without-offer-signal")
        if c.new_price != t.new_price:
            d.quarantine(f"api-leaflet-price-conflict: api {c.new_price}, leaflet "
                         f"{t.new_price} ({t.describe()})")
            records.append(MatchRecord("conflict", d, c, t, tier))
            overruled[id(t)] = f"leaflet-over-api:{c.erp} api {c.new_price}"
            continue
        d.flag(f"leaflet-confirmed:{'+'.join(t.sources)}")
        if from_price_tile(t):
            d.quarantine("leaflet-from-price-api-match: printed price is only a lower bound")
        if brand_only(c):
            d.name, src = leaflet_name(t, c, pool)
            d.flag(f"name-from-leaflet:{src} (api name {c.name!r})")
        if ti in unbacked and t.page in no_price_pages:
            d.quarantine(f"vlm-only-page-without-ocr-price: OCR found no printed price on "
                         f"page {t.page} ({t.describe()})")
        elif ti in unbacked:
            d.quarantine(f"vlm-only-without-ocr-price: {backing(ti)}")
        if tier in ("brand", "brand-differs"):
            # R3b already refused every uncorroborated brand pair
            d.flag(f"name-match:{'brand-only' if tier == 'brand' else tier}:{t.name!r}")
            d.flag(brand_corroboration(t, c))
        if ti in merged:
            group = [pool[j][0] for j in [ci, *merged[ti]]]
            olds = {(g.old_price, g.pct) for g in group}
            # Keep an old price / % only if EVERY candidate carries the same one.
            d.price_old, d.discount_published = next(iter(olds)) if len(olds) == 1 else (None, None)
            d.name = t.name or d.name
            d.brand = None
            erps = sorted(g.erp for g in group)
            d.retailer_item_id = "+".join(erps)
            d.flag("ambiguous-api-match-merged:" + ",".join(erps)
                   + ("" if len(olds) == 1 else ":old-price-withheld"))
        elif ties.get(ti):
            others = ", ".join(f"{pool[j][0].erp} {pool[j][0].name!r}" for j in ties[ti])
            d.quarantine(f"ambiguous-api-match: equally good {others}")
        lp = t.lidl_plus
        src_votes = t.lidl_plus_by_source
        if lp is None and len(src_votes) == 2 and c.lidl_plus in src_votes.values():
            # Two independent leaflet readers disagree; the API is a third
            # independent source. 2 of 3 decide, and the dissent is recorded.
            agree = next(k for k, v in src_votes.items() if v == c.lidl_plus)
            d.flag(f"lidl-plus-2-of-3:api+{agree} (dissent: "
                   + ", ".join(f"{k}={v}" for k, v in src_votes.items() if k != agree) + ")")
            lp = c.lidl_plus
        if lp is None:
            why = "readers disagree" if len(t.lidl_plus_votes) > 1 else "no reader saw it"
            d.quarantine(f"leaflet-lidl-plus-unknown: {why} ({t.describe()})")
        elif lp != c.lidl_plus:
            d.quarantine(f"lidl-plus-mismatch: api {c.lidl_plus}, leaflet {lp} ({t.describe()})")
        op = t.ocr_old_pct
        if (op is not None and c.old_price is None and c.pct is None
                and not c.conditional):
            # R4b: the API has no old price, the leaflet prints one and OCR's
            # old/new/% reconcile (R1): the leaflet is authoritative.
            d.price_old, d.discount_published = op
            d.flag("old-price-from-leaflet")
        elif op is not None and (op[0] != c.old_price or op[1] != c.pct):
            d.quarantine(f"api-leaflet-old-price-conflict: api {c.old_price}/{c.pct}%, "
                         f"leaflet {op[0]}/{op[1]}% ({t.describe()})")
        lw_end = leaflet_window[1]
        if lw_end is not None and (d.valid_to is None or d.valid_to > lw_end):
            # The leaflet cannot vouch for days after it ends (kw39 Žepek: API
            # 24.9.-3.10., leaflet 24.-30.9.). Shorten, never lengthen.
            d.flag(f"valid_to-clipped-to-leaflet:api {d.valid_to}")
            d.valid_to = lw_end
        tile_end = parse_tile_date(t.valid_to_text, year)
        if t.valid_to_text and tile_end is None:
            d.flag(f"leaflet-date-unparsed:{t.valid_to_text!r}")
        elif tile_end is not None and tile_end != d.valid_to:
            if d.valid_from <= tile_end and (d.valid_to is None or tile_end < d.valid_to):
                # The leaflet is authoritative and ends EARLIER: show the shorter
                # window. A misread can then only hide days, never invent them.
                d.flag(f"valid_to-from-leaflet:api {d.valid_to}, leaflet {t.valid_to_text!r}")
                d.valid_to = tile_end
            else:
                d.quarantine(f"api-leaflet-date-conflict: api valid to {d.valid_to}, "
                             f"leaflet {t.valid_to_text!r} ({t.describe()})")
        records.append(MatchRecord("verified", d, c, t, tier))
        verified_keys[(_name_key(t.name), t.new_price)] = c.erp

    # --- R8a: page validity inferred from confirmed API items ----------
    by_page: dict[int, list[tuple[date, date | None]]] = {}
    for r in records:
        if r.rule == "verified" and not r.deal.is_quarantined and r.tile.page is not None:
            by_page.setdefault(r.tile.page, []).append(tuple(r.candidate.windows[0])
                                                       if len(r.candidate.windows) == 1 else None)
    inferred: dict[int, tuple[date, date | None]] = {}
    for pg, ws in by_page.items():
        if pg not in page_validity and len(ws) >= INFER_MIN and None not in ws and len(set(ws)) == 1:
            inferred[pg] = ws[0]

    # --- R6: API-only -------------------------------------------------
    free_tiles = [t for i, t in enumerate(tiles) if i not in tile_of]
    leaflet_tiles = free_tiles + [t for t in tiles if id(t) in overruled]
    unmatched_listed = []
    for ci, (c, is_listed) in enumerate(pool):
        if ci in used_c:
            continue
        if is_listed:
            unmatched_listed.append(c)
            continue
        d = _deal_from_candidate(c, leaflet_id)
        if ship_website_only and not d.is_quarantined and website_only_ok(c):
            d.flag("website-only")
            records.append(MatchRecord("api-only", d, c, None))
            continue
        same_price = [t for t in free_tiles if t.new_price is not None
                      and t.new_price == c.new_price]
        brand_tiles = [tiles[ti] for ti, _ in uncorroborated.get(ci, ())]
        tiers = "/".join(sorted({"brand-only" if tr == "brand" else tr
                                 for _, tr in uncorroborated.get(ci, ())}))
        hint = (f"; {tiers} name match NOT corroborated (no equal old price or exact "
                f"pack size; api pack {c.packaging!r}): "
                + ", ".join(t.describe() for t in brand_tiles) if brand_tiles else "")
        same_price = [t for t in same_price if t not in brand_tiles]
        hint += ("; same price, unrelated name: " + ", ".join(t.describe() for t in same_price)
                 if same_price else "")
        d.quarantine(f"not-in-leaflet-reading{hint}")
        records.append(MatchRecord("api-only", d, c, None))

    # --- R8: leaflet-only ---------------------------------------------
    suspect_tiles = {id(tiles[i]) for i in suspects}
    for t in leaflet_tiles:
        pv = page_validity.get(t.page) if t.page is not None else None
        pv_inferred = pv is None and t.page in inferred
        if pv_inferred:
            pv = inferred[t.page]
        d = Deal(retailer=RETAILER, name=t.name or "?", leaflet_id=leaflet_id,
                 source_url=leaflet_url, valid_from=(pv or leaflet_window)[0],
                 valid_to=(pv or leaflet_window)[1], price_new=t.new_price,
                 loyalty_required=LIDL_PLUS if t.lidl_plus else None, source_page=t.page)
        if t.page is not None:
            d.flag(f"leaflet-page:{t.page}")
        if id(t) in overruled:
            d.flag(overruled[id(t)])
        rule = "leaflet-only"
        dup = verified_keys.get((_name_key(t.name), t.new_price))
        op = t.ocr_old_pct
        if t.new_price is None:
            d.quarantine(f"leaflet-no-price ({t.describe()})")
        elif id(t) in suspect_tiles:
            d.quarantine(f"vlm-suspect-repeated-price: {SUSPECT_REPEAT}+ tiles on page "
                         f"{t.page} share this price and first word ({t.describe()})")
        elif dup is not None:
            d.quarantine(f"leaflet-duplicate-of:{dup} ({t.describe()})")
        elif not (set(t.sources) == {"ocr", "vlm"}
                  and (op is not None or plain_price_tile(t) or from_price_tile(t))
                  and len(t.lidl_plus_votes) == 1
                  and all(r.lidl_plus is not None for r in t.readings)):
            d.quarantine(f"leaflet-only-unverified: {'; '.join(_unverified_why(t))} "
                         f"({t.describe()})")
        elif pv is None:
            d.quarantine(f"leaflet-only-no-validity: page {t.page} validity unknown "
                         f"({t.describe()})")
        else:
            if op is not None:
                d.price_old, d.discount_published = op
                d.flag("leaflet-only-confirmed:ocr+vlm")
            else:
                d.flag("leaflet-only-confirmed:ocr+vlm:no-old-price")
                if from_price_tile(t):
                    d.condition_kind, d.condition = "from-price", "že od"
                    d.flag("leaflet-price-from:crop-ocr+vlm")
            if any(isinstance(r.raw, dict) and r.raw.get("crop_confirmation") for r in t.readings):
                d.flag("leaflet-name-from:crop-ocr+vlm")
            d.valid_from, d.valid_to = pv
            if pv_inferred:
                d.flag(f"page-validity-inferred-from-api:{pv[0]}..{pv[1]}")
            lw_end = leaflet_window[1]
            if lw_end is not None and (d.valid_to is None or d.valid_to > lw_end):
                # as in R4: the leaflet cannot vouch for days after it ends
                # (kw39 Vitasia pages: API 28.9.-3.10., no end printed)
                d.flag(f"valid_to-clipped-to-leaflet:page {d.valid_to}")
                d.valid_to = lw_end
            tile_end = parse_tile_date(t.valid_to_text, year)
            if tile_end is not None and (d.valid_to is None or tile_end < d.valid_to):
                d.valid_to = tile_end
                d.flag(f"valid_to-from-tile:{t.valid_to_text!r}")
        records.append(MatchRecord(rule, d, None, t))

    return ReconcileResult(records, unmatched_listed)
