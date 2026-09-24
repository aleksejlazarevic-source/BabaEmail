"""Read a Lidl leaflet page with a local vision-language model (ollama).

What this reader may be trusted for, and what it may NOT
--------------------------------------------------------
Measured on hand-checked pages (scratch/lidl_vlm/notes.md, scratch/lidl_truth):

* ``new_price``: exact on every tile the model found. Prompt v3 on the kw39
  tune pages (scratch/lidl_truth): 61/61 priced tiles found, all exact.
* ``name`` (diacritics intact): reliable. ``lidl_plus``: 58/61 with v3 (v2
  was 57/61). Good, but not good enough to overrule the API on its own, so a
  disagreement quarantines.
* It INVENTS priced tiles for range promotions that print no price (kw39 p41:
  five "Knorr ... 2.69" tiles from one "-30% z Lidl Plus" promo; 2.69 is a
  neighbouring tile's old price). lidl_reconcile's R0b guards against this.
* ``old_price`` / discount %: **fabricated on ~57% of tiles that have no
  discount**, usually copied from the "1 kg = X EUR" unit-price line or from a
  neighbouring tile.

So ``VlmReading`` deliberately has NO old_price and NO pct field. The prompt
still asks the model for them, because giving the model a place to put the
struck-through and unit prices keeps them out of ``new_price``. They are
thrown away on parsing and are stripped from ``raw`` too. Nothing downstream
can trust them by accident.

The model also reports tile-level validity text ("velja do 26. 9.") as
``valid_to_text``. Treat it as a hint that can only make the reconciler MORE
cautious, never as a date to show.

Failure handling (reliability contract rule 5): every failure raises
``VlmUnavailable``. A page on which the model finds nothing also raises unless
the caller passes ``allow_empty=True``, because for a product page "no tiles"
is almost always a failure, not a result.

Caching: a leaflet is read once a week, and a 7b page read takes about two
minutes on CPU. Results are cached on disk under
sha256(image bytes) + model + PROMPT_VERSION. The prompt version also covers
the resize step and the parser, so bump it whenever any of those change.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import time
import unicodedata
from contextlib import contextmanager
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

import httpx

# Both are configurable from the environment; the defaults are what was validated.
OLLAMA_URL = os.environ.get("BABAEMAIL_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
DEFAULT_MODEL = os.environ.get("BABAEMAIL_VLM_MODEL", "qwen2.5vl:7b")
# "0" disables starting `ollama serve` when nothing answers at OLLAMA_URL.
AUTOSTART = os.environ.get("BABAEMAIL_OLLAMA_AUTOSTART", "1") != "0"
# v1 = scratch/lidl_vlm/extract.py (food only).  v2 = all product tiles,
# explicit unit-price/neighbour warnings, tile-level validity text.
# v3 = v2 + Lidl Plus scoped to the tile, range-wide LP promotions are not tiles.
PROMPT_VERSION = "v3"
MAX_WIDTH = 1100          # the prototype's validated input size; larger was not tested
NUM_CTX = 8192            # a 1100x1866 page is ~2.6k image tokens; 4096 is too tight
TIMEOUT_S = 1200.0        # CPU-only 7b: ~2 min/page typical, allow a slow machine
CACHE_DIR = Path(os.environ.get("BABAEMAIL_VLM_CACHE",
                                Path(__file__).resolve().parent.parent / ".cache" / "vlm"))

PROMPT = """You are reading ONE page of a Slovenian Lidl supermarket leaflet.
List EVERY product tile on this page that has a printed price: food, drinks,
household, cosmetics, pet food, clothing, tools, kitchenware - everything.

For each tile output an object with these fields:
  "name": brand line + product name exactly as printed, keep Slovenian letters (č š ž), e.g. "NAŠE NAM PAŠE Sveži svinjski zrezki"
  "new_price": the BIG price in EUR (the one printed largest on the tile), as a number like 3.49
  "old_price": the small crossed-out price next to the percent badge, or null if the tile has none
  "discount_pct": the printed "-NN%" badge as a negative integer, or null if no percent badge is printed
  "lidl_plus": true only if the blue "Lidl Plus" logo or the words "Z Lidl Plus" are printed INSIDE THIS TILE, next to this tile's own price or percent badge. Most tiles are NOT Lidl Plus.
  "unit": the pack size text as printed (e.g. "400 g", "6 x 1 l", "pribl. 1 kg", "za kg"), or null
  "valid_until": text like "velja do 26. 9." printed ON THIS TILE, or null (ignore the page header dates)

Rules:
- Every tile is separate. Never copy a number from a neighbouring tile.
- Lines like "1 kg = 8.73" or "1 l = 0.88" are unit prices. They are NEVER the new_price and NEVER the old_price.
- "220 g gratis", "32% gratis" mean extra quantity, not a price discount: discount_pct is null for them.
- Most tiles have NO crossed-out price; then old_price must be null.
- Ignore the page header, the page footer (it always has a Lidl Plus logo), and "Slovenski izdelek" roundels.
- A big Lidl Plus promotion for a whole range (e.g. "-30% z Lidl Plus" on all soups) does NOT make the other tiles on the page Lidl Plus, and is not a tile itself unless it prints one big price for one product.
- There are usually 4 to 9 tiles on a page. Find all of them.
- Output {"items": [ ... ]}.
"""

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "new_price": {"type": ["number", "null"]},
                    "old_price": {"type": ["number", "null"]},
                    "discount_pct": {"type": ["integer", "null"]},
                    "lidl_plus": {"type": "boolean"},
                    "unit": {"type": ["string", "null"]},
                    "valid_until": {"type": ["string", "null"]},
                },
                "required": ["name", "new_price", "old_price", "discount_pct",
                             "lidl_plus", "unit", "valid_until"],
            },
        }
    },
    "required": ["items"],
}

# Fields the model is asked for but must never be trusted. Removed from raw.
_UNTRUSTED = ("old_price", "discount_pct")
_CENT = Decimal("0.01")
_MAX_PRICE = Decimal("5000")


class VlmUnavailable(RuntimeError):
    """The VLM could not produce a reading for this page. Never swallowed as []."""


@dataclass(frozen=True, slots=True)
class VlmReading:
    """One price tile as the VLM read it. Carries NO old price and NO percentage.

    ``new_price`` is None when the model reported the tile but no usable
    price (null, non-positive, or more than two decimals). Such a reading is
    kept so the tile is not silently lost; it can never verify anything.
    """

    page: int | None
    name: str | None
    new_price: Decimal | None
    lidl_plus: bool
    unit: str | None
    valid_to_text: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


# ----------------------------------------------------------------------
# Parsing (pure)
# ----------------------------------------------------------------------

def _clean_text(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    t = unicodedata.normalize("NFC", " ".join(v.split()))
    return t or None


def _price(v: Any) -> Decimal | None:
    """A model number -> Decimal, or None if it is not a plausible shelf price."""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except InvalidOperation:
        return None
    if not d.is_finite() or d <= 0 or d > _MAX_PRICE:
        return None
    if d != d.quantize(_CENT):
        return None          # "2.989" is a misread, not a price
    return d.quantize(_CENT)


def parse_response(text: str, page: int | None) -> list[VlmReading]:
    """The model's JSON text -> readings. Raises VlmUnavailable if malformed."""
    try:
        data = json.loads(text, parse_float=Decimal)
    except (json.JSONDecodeError, TypeError) as exc:
        raise VlmUnavailable(f"model returned non-JSON: {str(text)[:200]!r}") from exc
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise VlmUnavailable(f"model JSON has no items list: {str(text)[:200]!r}")
    out: list[VlmReading] = []
    for it in items:
        if not isinstance(it, dict):
            raise VlmUnavailable(f"model item is not an object: {it!r}")
        raw = {k: (str(v) if isinstance(v, Decimal) else v)
               for k, v in it.items() if k not in _UNTRUSTED}
        lp = it.get("lidl_plus")
        out.append(VlmReading(
            page=page,
            name=_clean_text(it.get("name")),
            new_price=_price(it.get("new_price")),
            lidl_plus=lp is True,
            unit=_clean_text(it.get("unit")),
            valid_to_text=_clean_text(it.get("valid_until")),
            raw=raw,
        ))
    return out


# ----------------------------------------------------------------------
# Image + cache
# ----------------------------------------------------------------------

def prepare_image(png_bytes: bytes) -> bytes:
    """Downscale to MAX_WIDTH (as the validated prototype did), return PNG bytes."""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(png_bytes))
        im.load()
    except Exception as exc:  # PIL raises a zoo of types for bad input
        raise VlmUnavailable(f"not a readable image: {exc}") from exc
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if im.width > MAX_WIDTH:
        h = round(im.height * MAX_WIDTH / im.width)
        im = im.resize((MAX_WIDTH, h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def cache_key(png_bytes: bytes, model: str) -> str:
    h = hashlib.sha256(png_bytes).hexdigest()
    m = re.sub(r"[^A-Za-z0-9_.-]", "_", model)
    return f"{h}-{m}-{PROMPT_VERSION}"


def _cache_load(path: Path) -> str | None:
    try:
        d = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if d.get("prompt_version") != PROMPT_VERSION or not isinstance(d.get("response"), str):
        return None
    return d["response"]


def _cache_store(path: Path, model: str, response: str, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"model": model, "prompt_version": PROMPT_VERSION,
                               "response": response, **meta}, ensure_ascii=False, indent=1),
                   "utf-8")
    tmp.replace(path)          # atomic: a crash never leaves a half-written cache entry


# ----------------------------------------------------------------------
# Model call
# ----------------------------------------------------------------------

def _call_ollama(image_png: bytes, model: str, client: httpx.Client | None,
                 url: str | None = None) -> tuple[str, dict]:
    url = (url or OLLAMA_URL).rstrip("/")
    payload = {
        "model": model,
        "prompt": PROMPT,
        "images": [base64.b64encode(image_png).decode("ascii")],
        "stream": False,
        "format": JSON_SCHEMA,
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }
    own = client is None
    c = client or httpx.Client(timeout=httpx.Timeout(TIMEOUT_S, connect=10.0))
    t0 = time.monotonic()
    try:
        r = c.post(f"{url}/api/generate", json=payload)
    except httpx.HTTPError as exc:
        raise VlmUnavailable(f"ollama unreachable at {url}: {exc!r}") from exc
    finally:
        if own:
            c.close()
    if r.status_code != 200:
        raise VlmUnavailable(f"ollama HTTP {r.status_code}: {r.text[:300]!r}")
    try:
        d = r.json()
    except ValueError as exc:
        raise VlmUnavailable(f"ollama returned non-JSON envelope: {r.text[:200]!r}") from exc
    if d.get("error"):
        raise VlmUnavailable(f"ollama error: {d['error']}")
    if d.get("done_reason") not in (None, "stop"):
        # "length" = output truncated: the item list would be silently short.
        raise VlmUnavailable(f"model output cut off (done_reason={d.get('done_reason')!r})")
    resp = d.get("response")
    if not isinstance(resp, str) or not resp.strip():
        raise VlmUnavailable("ollama returned an empty response")
    meta = {"elapsed_s": round(time.monotonic() - t0, 1),
            "prompt_eval_count": d.get("prompt_eval_count"),
            "eval_count": d.get("eval_count")}
    return resp, meta


def read_page(png_bytes: bytes, *, model: str = DEFAULT_MODEL, page: int | None = None,
              cache_dir: Path | None = CACHE_DIR, allow_empty: bool = False,
              client: httpx.Client | None = None, url: str | None = None) -> list[VlmReading]:
    """Every price tile the VLM finds on one leaflet page.

    Raises VlmUnavailable on any failure (ollama down, HTTP error, truncated
    or malformed output, unreadable image) and, unless ``allow_empty``, when
    the model finds no tiles. Pass ``cache_dir=None`` to bypass the cache.
    """
    if not png_bytes:
        raise VlmUnavailable("empty image")
    path = cache_dir / f"{cache_key(png_bytes, model)}.json" if cache_dir else None
    response = _cache_load(path) if path else None
    if response is None:
        response, meta = _call_ollama(prepare_image(png_bytes), model, client, url)
        readings = parse_response(response, page)      # validate BEFORE caching
        if path:
            _cache_store(path, model, response, meta)
    else:
        readings = parse_response(response, page)
    if not readings and not allow_empty:
        raise VlmUnavailable(f"model found no price tiles on page {page}")
    return readings


# ----------------------------------------------------------------------
# Server lifecycle
# ----------------------------------------------------------------------

_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def ollama_version(url: str | None = None, timeout: float = 3.0) -> str | None:
    """The ollama version answering at ``url``, or None if nothing ollama-like does.

    ``/api/version`` returning ``{"version": ...}`` is what tells ollama apart
    from any other local server (the owner's model router on :8000, say).
    """
    url = (url or OLLAMA_URL).rstrip("/")
    try:
        r = httpx.get(f"{url}/api/version", timeout=timeout)
        v = r.json().get("version") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) else None


def _port_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _model_available(model: str, url: str) -> bool:
    try:
        r = httpx.get(f"{url}/api/tags", timeout=10.0)
        names = {m.get("name") for m in r.json().get("models", [])} if r.status_code == 200 else set()
    except (httpx.HTTPError, ValueError, AttributeError):
        return False
    return model in names or (":" not in model and f"{model}:latest" in names)


@contextmanager
def ollama_session(model: str | None = None, url: str | None = None, *,
                   autostart: bool | None = None, start_timeout: float = 60.0,
                   log_path: Path | None = None) -> Iterator[bool]:
    """Make sure ollama answers at ``url`` with ``model`` pulled, for the block.

    If nothing answers and ``autostart`` is on, ``ollama serve`` is started in
    the background on exactly that loopback host:port and stopped again when
    the block exits. A server that was already running is never stopped. The
    port is only ever used if it is FREE: anything else listening there (not
    ollama) is left alone and VlmUnavailable is raised. Yields True when this
    call started the server.

    Raises VlmUnavailable when ollama cannot be reached or started, or when the
    model is not pulled (it is never pulled automatically: several GB).
    """
    model = model or DEFAULT_MODEL
    url = (url or OLLAMA_URL).rstrip("/")
    autostart = AUTOSTART if autostart is None else autostart
    proc: subprocess.Popen | None = None
    if ollama_version(url) is None:
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port or 11434
        if not autostart:
            raise VlmUnavailable(f"ollama is not running at {url} (autostart disabled)")
        if host not in _LOOPBACK:
            raise VlmUnavailable(f"ollama is not running at {url}; not starting a server for a non-local URL")
        if _port_in_use(host, port):
            raise VlmUnavailable(f"{host}:{port} is in use by something that is not ollama; leaving it alone")
        exe = shutil.which("ollama")
        if exe is None:
            raise VlmUnavailable(f"ollama is not running at {url} and the ollama binary is not installed")
        log_path = log_path or CACHE_DIR.parent / "ollama-serve.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        bind = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        with open(log_path, "ab") as log:
            proc = subprocess.Popen([exe, "serve"], stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True,
                                    env={**os.environ, "OLLAMA_HOST": bind})
        deadline = time.monotonic() + start_timeout
        while ollama_version(url) is None:
            if proc.poll() is not None or time.monotonic() > deadline:
                _stop(proc)
                raise VlmUnavailable(f"could not start `ollama serve` on {bind} (see {log_path})")
            time.sleep(0.5)
    try:
        if not _model_available(model, url):
            raise VlmUnavailable(f"model {model!r} is not available in ollama at {url} "
                                 f"(run: ollama pull {model})")
        yield proc is not None
    finally:
        if proc is not None:
            _stop(proc)


def _stop(proc: subprocess.Popen) -> None:
    """Stop a server THIS process started (never any other)."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
