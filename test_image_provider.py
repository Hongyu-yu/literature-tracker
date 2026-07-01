import base64, json, os, tempfile
from unittest import mock
import image_provider
from image_provider import generate_image_b64, compress_to_webp

try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False

# A small valid PNG (1x1) base64 — enough for stream-parse equality test (no PIL needed).
_PNG_1x1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

def _make_png_b64(size=(1600, 900)):
    import io
    buf = io.BytesIO(); Image.new("RGB", size, (10, 80, 180)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()

def test_compress_to_webp_shrinks_and_resizes():
    if not HAS_PIL:
        return  # skipped locally; validated in CI
    src = base64.b64decode(_make_png_b64((1600, 900)))
    out = os.path.join(tempfile.mkdtemp(), "p.webp")
    compress_to_webp(src, out, max_edge=768, quality=80)
    assert os.path.exists(out)
    im = Image.open(out)
    assert im.format == "WEBP"
    assert max(im.size) <= 768

def test_generate_image_parses_stream():
    line = 'data: ' + json.dumps({
        "type": "response.output_item.done",
        "item": {"type": "image_generation_call", "result": _PNG_1x1}})
    class FakeStream:
        status_code = 200
        def iter_lines(self, decode_unicode=True): yield line
        def raise_for_status(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    with mock.patch.object(image_provider.requests, "post", return_value=FakeStream()):
        got = generate_image_b64("draw a crystal", api_key="k", base="http://h/v1")
    assert got == _PNG_1x1

def test_generate_image_returns_none_on_failure():
    def boom(*a, **k): raise Exception("down")
    with mock.patch.object(image_provider.time, "sleep", lambda *a: None), \
         mock.patch.object(image_provider.requests, "post", side_effect=boom):
        assert generate_image_b64("x", api_key="k", base="http://h/v1") is None


def _ok_stream():
    line = 'data: ' + json.dumps({
        "type": "response.output_item.done",
        "item": {"type": "image_generation_call", "result": _PNG_1x1}})
    class FakeStream:
        def iter_lines(self, decode_unicode=True): yield line
        def raise_for_status(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return FakeStream()


def test_generate_image_uses_env_timeout():
    captured = {}
    def fake_post(*a, **k):
        captured["timeout"] = k.get("timeout")
        return _ok_stream()
    with mock.patch.dict(os.environ, {"IMAGE_TIMEOUT_SECONDS": "321"}), \
         mock.patch.object(image_provider.requests, "post", side_effect=fake_post):
        got = generate_image_b64("x", api_key="k", base="http://h/v1")
    assert got == _PNG_1x1
    assert captured["timeout"] == 321           # 读超时来自环境变量,不再硬编码 180


def test_generate_image_timeout_falls_back_to_ai_timeout():
    captured = {}
    def fake_post(*a, **k):
        captured["timeout"] = k.get("timeout"); return _ok_stream()
    env = {"AI_TIMEOUT_SECONDS": "555"}
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch.object(image_provider.requests, "post", side_effect=fake_post):
        os.environ.pop("IMAGE_TIMEOUT_SECONDS", None)
        generate_image_b64("x", api_key="k", base="http://h/v1")
    assert captured["timeout"] == 555           # 无 IMAGE_TIMEOUT_SECONDS 时回退 AI_TIMEOUT_SECONDS


def test_generate_image_retries_then_succeeds():
    calls = {"n": 0}
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("Read timed out")
        return _ok_stream()
    with mock.patch.object(image_provider.time, "sleep", lambda *a: None), \
         mock.patch.object(image_provider.requests, "post", side_effect=flaky):
        got = generate_image_b64("x", api_key="k", base="http://h/v1", retries=2)
    assert got == _PNG_1x1
    assert calls["n"] == 2                       # 第1次超时,第2次成功


def test_generate_image_gives_up_after_retries():
    calls = {"n": 0}
    def counting_boom(*a, **k):
        calls["n"] += 1; raise Exception("Read timed out")
    with mock.patch.object(image_provider.time, "sleep", lambda *a: None), \
         mock.patch.object(image_provider.requests, "post", side_effect=counting_boom):
        got = generate_image_b64("x", api_key="k", base="http://h/v1", retries=2)
    assert got is None
    assert calls["n"] == 3                       # 1 次初试 + 2 次重试


def test_generate_image_retries_on_empty_stream():
    calls = {"n": 0}
    class EmptyStream:
        def iter_lines(self, decode_unicode=True):
            return iter(())                      # 流里没有图片事件
        def raise_for_status(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def sometimes_empty(*a, **k):
        calls["n"] += 1
        return EmptyStream() if calls["n"] == 1 else _ok_stream()
    with mock.patch.object(image_provider.time, "sleep", lambda *a: None), \
         mock.patch.object(image_provider.requests, "post", side_effect=sometimes_empty):
        got = generate_image_b64("x", api_key="k", base="http://h/v1", retries=2)
    assert got == _PNG_1x1
    assert calls["n"] == 2                       # 空流也算失败,触发重试
