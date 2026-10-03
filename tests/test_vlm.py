"""babaemail.vlm against a fake ollama. No model is run here."""

import io
import json
from decimal import Decimal as D

import httpx
import pytest
from PIL import Image

from babaemail import vlm


def png(w=2200, h=3732, color=(200, 20, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


ITEMS = {"items": [
    {"name": "NAŠE NAM PAŠE Sveže piščančje peruti", "new_price": 3.99, "old_price": 3.95,
     "discount_pct": None, "lidl_plus": False, "unit": "1 kg", "valid_until": None},
    {"name": "MILKA  Čokolada", "new_price": 0.39, "old_price": 1.99, "discount_pct": -80,
     "lidl_plus": True, "unit": "90 g", "valid_until": "velja do 26. 9."},
]}


class FakeOllama:
    def __init__(self, body=None, status=200, exc=None):
        self.calls = []
        self.body = body if body is not None else {
            "response": json.dumps(ITEMS), "done": True, "done_reason": "stop"}
        self.status, self.exc = status, exc

    def __call__(self, request):
        self.calls.append(json.loads(request.content))
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, json=self.body)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def read(fake, tmp_path, img=None, **kw):
    return vlm.read_page(img or png(), page=2, cache_dir=tmp_path, client=fake.client(), **kw)


def test_reads_tiles_without_old_price_or_pct(tmp_path):
    rs = read(FakeOllama(), tmp_path)
    assert [r.new_price for r in rs] == [D("3.99"), D("0.39")]
    assert all(isinstance(r.new_price, D) for r in rs)
    assert rs[1].name == "MILKA Čokolada" and rs[1].lidl_plus and rs[1].page == 2
    assert rs[1].valid_to_text == "velja do 26. 9."
    for r in rs:
        assert not hasattr(r, "old_price") and not hasattr(r, "pct")
        assert "old_price" not in r.raw and "discount_pct" not in r.raw


def test_request_resizes_and_uses_schema(tmp_path):
    fake = FakeOllama()
    read(fake, tmp_path)
    [call] = fake.calls
    assert call["model"] == "qwen2.5vl:7b" and call["format"] == vlm.JSON_SCHEMA
    assert call["options"]["temperature"] == 0
    import base64
    sent = Image.open(io.BytesIO(base64.b64decode(call["images"][0])))
    assert sent.width == vlm.MAX_WIDTH and sent.height == 1866


def test_cache_hit_skips_the_model(tmp_path):
    fake = FakeOllama()
    img = png()
    a = read(fake, tmp_path, img)
    b = read(fake, tmp_path, img)
    assert a == b and len(fake.calls) == 1
    read(fake, tmp_path, img, model="qwen2.5vl:3b")          # other model: new entry
    assert len(fake.calls) == 2
    read(fake, tmp_path, png(color=(1, 2, 3)))               # other image: new entry
    assert len(fake.calls) == 3


def test_cache_key_covers_prompt_version(tmp_path, monkeypatch):
    img = png()
    k1 = vlm.cache_key(img, "m")
    monkeypatch.setattr(vlm, "PROMPT_VERSION", "v999")
    assert vlm.cache_key(img, "m") != k1


def test_corrupt_cache_entry_is_ignored(tmp_path):
    fake, img = FakeOllama(), png()
    read(fake, tmp_path, img)
    [f] = tmp_path.glob("*.json")
    f.write_text("{not json")
    assert len(read(fake, tmp_path, img)) == 2 and len(fake.calls) == 2


def test_failures_are_not_cached(tmp_path):
    img = png()
    with pytest.raises(vlm.VlmUnavailable):
        read(FakeOllama(body={"response": "garbage", "done_reason": "stop"}), tmp_path, img)
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("fake", [
    FakeOllama(exc=httpx.ConnectError("refused")),
    FakeOllama(exc=httpx.ReadTimeout("slow")),
    FakeOllama(status=500, body={"error": "boom"}),
    FakeOllama(body={"error": "model not found"}),
    FakeOllama(body={"response": "not json", "done_reason": "stop"}),
    FakeOllama(body={"response": json.dumps(ITEMS)[:40], "done_reason": "length"}),
    FakeOllama(body={"response": "", "done_reason": "stop"}),
    FakeOllama(body={"response": '{"foo": 1}', "done_reason": "stop"}),
], ids=["down", "timeout", "http500", "error", "nonjson", "truncated", "empty", "noitems"])
def test_failures_raise_never_return_empty(fake, tmp_path):
    with pytest.raises(vlm.VlmUnavailable):
        read(fake, tmp_path)


def test_zero_tiles_raises_unless_allowed(tmp_path):
    body = {"response": '{"items": []}', "done_reason": "stop"}
    with pytest.raises(vlm.VlmUnavailable):
        read(FakeOllama(body=body), tmp_path)
    assert read(FakeOllama(body=body), tmp_path, allow_empty=True) == []


def test_unreadable_image_raises(tmp_path):
    with pytest.raises(vlm.VlmUnavailable):
        read(FakeOllama(), tmp_path, img=b"not a png")


def test_header_reader_crops_lane_uses_blind_prompt_and_full_page_cache_key(tmp_path):
    body = {"response": '{"from":"28. 9.","to":"30. 9."}', "done_reason": "stop"}
    fake = FakeOllama(body=body)
    image = png()
    assert vlm.read_header_validity(image, cache_dir=tmp_path,
                                    client=fake.client()) == ((28, 9), (30, 9))
    assert vlm.read_header_validity(image, cache_dir=tmp_path,
                                    client=fake.client()) == ((28, 9), (30, 9))
    [call] = fake.calls
    assert call["prompt"] == vlm.HEADER_PROMPT
    assert call["format"] == vlm.HEADER_JSON_SCHEMA
    assert "OCR" not in call["prompt"]
    import base64
    sent = Image.open(io.BytesIO(base64.b64decode(call["images"][0])))
    assert sent.width == vlm.MAX_WIDTH and sent.height < 200
    [cache] = tmp_path.glob("*.json")
    assert cache.name.startswith(vlm.header_cache_key(image, vlm.DEFAULT_MODEL))


@pytest.mark.parametrize("response, expected", [
    ('{"none":true}', None),
    ('{"from":"1. 1.","to":null}', ((1, 1), None)),
])
def test_header_reader_none_and_open_end(tmp_path, response, expected):
    fake = FakeOllama(body={"response": response, "done_reason": "stop"})
    assert vlm.read_header_validity(png(), cache_dir=tmp_path,
                                    client=fake.client()) == expected


@pytest.mark.parametrize("response", [
    '{}', '{"none":false}', '{"from":"32. 9.","to":null}',
    '{"from":"28. 9.","to":"tomorrow"}',
])
def test_header_reader_rejects_malformed_values(tmp_path, response):
    fake = FakeOllama(body={"response": response, "done_reason": "stop"})
    with pytest.raises(vlm.VlmUnavailable):
        vlm.read_header_validity(png(), cache_dir=tmp_path, client=fake.client())


@pytest.mark.parametrize("raw,want", [
    (2.99, D("2.99")), ("2.99", D("2.99")), (5, D("5.00")), (2.989, None), (0, None),
    (-1.5, None), (None, None), (True, None), (99999, None),
])
def test_price_parsing(raw, want):
    text = json.dumps({"items": [{"name": "x", "new_price": raw, "lidl_plus": False}]})
    [r] = vlm.parse_response(text, 1)
    assert r.new_price == want


def test_prompt_changes_require_a_version_bump():
    """The cache is keyed by PROMPT_VERSION, not by the prompt text. If this
    fails you changed the prompt, schema or resize: bump PROMPT_VERSION and
    record the new fingerprint here, or stale cached readings will be served."""
    import hashlib
    fp = hashlib.sha256((vlm.PROMPT + json.dumps(vlm.JSON_SCHEMA, sort_keys=True)
                         + str(vlm.MAX_WIDTH)).encode()).hexdigest()[:16]
    assert (vlm.PROMPT_VERSION, fp) == ("v3", "377eba99023396d6")


def test_header_prompt_changes_require_a_version_bump():
    import hashlib
    fp = hashlib.sha256((vlm.HEADER_PROMPT
                         + json.dumps(vlm.HEADER_JSON_SCHEMA, sort_keys=True)
                         + str(vlm.HEADER_BAND) + str(vlm.MAX_WIDTH)).encode()).hexdigest()[:16]
    assert (vlm.HEADER_PROMPT_VERSION, fp) == ("header-v1", "22dfd44d09763abe")
