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


# ---- 截断流:中间帧(partial_image)不能当成品图 ----------------------------------
# 背景:中间帧按 partial_image_index 递增、越来越清晰,最终图走 response.output_item.done。
# 流被截断时只剩中间帧,旧代码锁死第一帧(最糊的那张)并当成功返回;这张糊图会写进
# poster.image,run_deep(有 poster 就复用)和 backfill_posters(有 image 就跳过)从此都当
# 这条已完成,永远不再重生成。

def _partial_line(idx, b64):
    return 'data: ' + json.dumps({"type": "response.image_generation_call.partial_image",
                                  "partial_image_index": idx, "partial_image_b64": b64})


def _done_line(b64=_PNG_1x1):
    return 'data: ' + json.dumps({"type": "response.output_item.done",
                                  "item": {"type": "image_generation_call", "result": b64}})


def _stream_of(lines, raise_after=False):
    class FakeStream:
        def iter_lines(self, decode_unicode=True):
            for ln in lines:
                yield ln
            if raise_after:
                raise Exception("connection reset by peer")
        def raise_for_status(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return FakeStream()


def _without_accept_partial():
    """IMAGE_ACCEPT_PARTIAL 可能被外部环境设了,测默认行为前先摘掉。"""
    ctx = mock.patch.dict(os.environ, {}, clear=False)
    ctx.start()
    os.environ.pop("IMAGE_ACCEPT_PARTIAL", None)
    return ctx


def test_generate_image_keeps_last_partial_not_first():
    """兜底收下中间帧时,拿的必须是最后(最清晰)那帧,不是第 0 帧。"""
    lines = [_partial_line(0, "COARSE0"), _partial_line(1, "BETTER1")]  # 流在 done 之前断了
    with mock.patch.dict(os.environ, {"IMAGE_ACCEPT_PARTIAL": "1"}), \
         mock.patch.object(image_provider.requests, "post",
                           side_effect=lambda *a, **k: _stream_of(lines)):
        got = generate_image_b64("x", api_key="k", base="http://h/v1", retries=0, backoff=0)
    assert got == "BETTER1"                      # 旧代码 latch 第一帧,会返回 COARSE0


def test_generate_image_retries_when_only_partial():
    """只拿到中间帧算失败:必须重试,重试拿到成品图就用成品图。"""
    calls = {"n": 0}
    def truncated_then_ok(*a, **k):
        calls["n"] += 1
        return _stream_of([_partial_line(0, "COARSE0")]) if calls["n"] == 1 \
            else _stream_of([_done_line()])
    ctx = _without_accept_partial()
    try:
        with mock.patch.object(image_provider.requests, "post", side_effect=truncated_then_ok):
            got = generate_image_b64("x", api_key="k", base="http://h/v1", retries=2, backoff=0)
    finally:
        ctx.stop()
    assert got == _PNG_1x1                       # 旧代码第 1 次就返回 COARSE0,根本不重试
    assert calls["n"] == 2


def test_generate_image_partial_only_returns_none_by_default():
    """重试用尽仍只有中间帧:默认不收半成品,返回 None 让下一轮 backfill 重生成。"""
    calls = {"n": 0}
    def always_truncated(*a, **k):
        calls["n"] += 1
        return _stream_of([_partial_line(0, "COARSE0"), _partial_line(1, "BETTER1")])
    ctx = _without_accept_partial()
    try:
        with mock.patch.object(image_provider.requests, "post", side_effect=always_truncated):
            got = generate_image_b64("x", api_key="k", base="http://h/v1", retries=1, backoff=0)
    finally:
        ctx.stop()
    assert got is None                           # 旧代码返回 COARSE0,糊图被永久写进 image
    assert calls["n"] == 2                       # 1 次初试 + 1 次重试


def test_generate_image_keeps_final_when_stream_breaks_after_result():
    """成品图已到手、流之后才断:照收,不能白扔一张已经生成好(且已付费)的图。"""
    calls = {"n": 0}
    def done_then_boom(*a, **k):
        calls["n"] += 1
        return _stream_of([_done_line()], raise_after=True)
    with mock.patch.object(image_provider.requests, "post", side_effect=done_then_boom):
        got = generate_image_b64("x", api_key="k", base="http://h/v1", retries=2, backoff=0)
    assert got == _PNG_1x1                       # 旧代码 return 在 try 内,异常把成品图丢了
    assert calls["n"] == 1                       # 拿到图就不该再重试
