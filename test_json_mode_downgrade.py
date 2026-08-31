#!/usr/bin/env python3
"""上游拒绝 JSON 模式时必须立刻降级重试，而不是当成瞬时故障重试到超时。

2026-08-31 现场：网关把"请求形状不对"的确定性错误报成 **502**
  Response input messages must contain the word 'json' in some form
  to use 'text.format' of type 'json_object'.
而降级分支只认 400，于是 502 落进"可重试服务器错误"分支，重试 9 次、耗时约 30 分钟后放弃
→ 日报走 fallback → sidecar 不落盘 → --rerender-only 空转 → 每日邮件发不出去。
"""

import os
from unittest import mock

from ai_summarizer import OpenRouterProvider


JSON_MODE_ERROR = (
    '{"error":{"message":"Response input messages must contain the word '
    "'json' in some form to use 'text.format' of type 'json_object'.\","
    '"type":"upstream_error"}}'
)


def _resp(status, text='{"ok":1}', payload=None):
    r = mock.MagicMock()
    r.status_code = status
    r.text = text
    r.json.return_value = payload or {"choices": [{"message": {"content": "OK"}}]}
    return r


def _provider():
    with mock.patch.dict(os.environ, {"AI_BASE_URL": "https://gw.example/v1"}):
        return OpenRouterProvider(api_key="sk-test", model="gpt-5.5")


def _call(responses, env=None):
    """跑一次 call_api，返回 (结果, 每次请求的 payload 列表)。"""
    sent = []

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return responses[len(sent) - 1]

    base = {"AI_RESPONSE_JSON": "1", "AI_WAIT_MAX_SECONDS": "0", "AI_MAX_RETRIES": "3"}
    base.update(env or {})
    with mock.patch.dict(os.environ, base), \
         mock.patch("ai_summarizer.requests.post", side_effect=fake_post), \
         mock.patch("ai_summarizer.time.sleep"):
        return _provider().call_api("生成日报"), sent


def test_json_mode_rejected_with_502_downgrades_immediately():
    """核心回归：502 + json_object 报错必须立刻去掉 response_format 重试并成功。"""
    out, sent = _call([_resp(502, JSON_MODE_ERROR), _resp(200)])
    assert out == "OK"
    assert len(sent) == 2, f"应当只发 2 次请求(降级后立刻成功)，实际 {len(sent)}"
    assert sent[0].get("response_format") == {"type": "json_object"}
    assert "response_format" not in sent[1], "降级后不得再带 response_format"


def test_json_mode_rejected_with_400_still_downgrades():
    """原有的 400 行为不能被破坏。"""
    out, sent = _call([_resp(400, JSON_MODE_ERROR), _resp(200)])
    assert out == "OK"
    assert "response_format" not in sent[1]


def test_genuine_502_without_json_marker_is_retried_not_downgraded():
    """真瞬时故障(报错里没有 json 模式字样)仍走重试，且保留 JSON 模式。"""
    out, sent = _call([_resp(502, '{"error":"upstream timeout"}'), _resp(200)])
    assert out == "OK"
    assert sent[1].get("response_format") == {"type": "json_object"}, "不该误降级"


def test_downgrade_happens_at_most_once():
    """降级后再遇同样报错不能无限循环。"""
    out, sent = _call([_resp(502, JSON_MODE_ERROR), _resp(502, JSON_MODE_ERROR), _resp(200)])
    assert out == "OK"
    assert len(sent) == 3
    assert "response_format" not in sent[1] and "response_format" not in sent[2]


def test_daily_prompt_contains_lowercase_json_for_json_mode():
    """启用 json_object 模式时上游要求输入含 'json'，且检查大小写敏感。"""
    from ai_summarizer import AISummarizer
    with mock.patch.dict(os.environ, {"AI_API_KEY": "sk-test"}):
        s = AISummarizer.__new__(AISummarizer)
    prompt = AISummarizer._build_prompt(s, [{
        "title": "Neural network potential", "abstract": "abs", "link": "https://x/1", "journal": "arXiv",
    }], "2026-08-30")
    assert "json" in prompt, "prompt 必须含小写 'json'(大写 JSON 不被上游认可)"


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] json mode downgrade sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
