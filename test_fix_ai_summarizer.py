#!/usr/bin/env python3
"""ai_summarizer 的四项回归测试（全部无网络，假时钟）。

1) 确定性 HTTP 错误（400/401/403/404…）必须立刻失败，不能吃掉整份重试预算。
   三个 provider 里那句"这不可重试，立刻放弃"的 raise 都写在 try 内部，
   被同一层的 `except Exception` 原地接住 → 坏模型名 / 过期密钥与真正的上游抖动
   表现完全一样：AI_WAIT_MAX_SECONDS=600 时发 7 次请求、空等 10 分钟。
   Kimi 那段甚至带着 "Fatal: don't waste retry budget" 的注释，描述的行为并不存在。
2) 分片日报：一个分片失败会把已经成功（已付费）的分片全部丢弃重算。
3) 除 _build_prompt 外的三个提示词缺少小写 "json"，json_object 模式下每次调用
   都要白挨一次上游拒绝再降级重试。
4) summary["summaries"] 只是 full_list 的同一个列表对象，却会被 sidecar 写第二遍
   （实测占 data/daily_summary_<date>.json 的 45%）。
"""

import io
import json
import os
import re
from contextlib import redirect_stdout
from typing import List
from unittest import mock

import ai_summarizer as ais
from ai_summarizer import AISummarizer, GeminiProvider, KimiClaudeCodeProvider, OpenRouterProvider


# --------------------------------------------------------------------------------------
# provider 层：假 HTTP + 假时钟
# --------------------------------------------------------------------------------------

def _resp(status, text="boom", payload=None):
    r = mock.MagicMock()
    r.status_code = status
    r.text = text
    r.headers = {}
    r.json.return_value = payload or {"choices": [{"message": {"content": "OK"}}]}
    return r


def _gemini_ok():
    return _resp(200, "ok", {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})


def _kimi_ok():
    return _resp(200, "ok", {"content": [{"type": "text", "text": "OK"}]})


def _drive(make_provider, responses, env=None):
    """跑一次 call_api：返回 (结果或异常, 各次请求体, 各次 sleep 秒数)。

    time.sleep / time.monotonic 一起打桩成假时钟，否则修复前的版本在 sleep 被打空后
    永远等不到 AI_WAIT_MAX_SECONDS 到期，会真的空转十分钟。
    """
    sent: List = []
    slept: List[float] = []
    clock = {"t": 1000.0}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        sent.append(json)
        return responses[min(len(sent) - 1, len(responses) - 1)]

    def fake_sleep(seconds):
        slept.append(float(seconds))
        clock["t"] += float(seconds)

    base = {
        "AI_BASE_URL": "https://gw.example/v1",
        "AI_WAIT_MAX_SECONDS": "600",
        "AI_MAX_RETRIES": "3",
        "AI_RESPONSE_JSON": "",
    }
    base.update(env or {})
    with mock.patch.dict(os.environ, base), \
         mock.patch("ai_summarizer.requests.post", side_effect=fake_post), \
         mock.patch("ai_summarizer.time.sleep", side_effect=fake_sleep), \
         mock.patch("ai_summarizer.time.monotonic", side_effect=lambda: clock["t"]), \
         redirect_stdout(io.StringIO()):
        provider = make_provider()
        try:
            out = provider.call_api("生成日报")
        except Exception as exc:  # noqa: BLE001 - 测试要断言异常本身
            out = exc
    return out, sent, slept


def test_openrouter_bad_model_404_fails_after_one_request():
    """坏模型名（404）是确定性的：只能发 1 次请求，不许睡 600 秒重试预算。"""
    out, sent, slept = _drive(
        lambda: OpenRouterProvider(api_key="sk-test", model="does-not-exist"),
        [_resp(404, '{"error":{"message":"model does-not-exist not found"}}')],
    )
    assert len(sent) == 1, f"确定性 404 只该发 1 次请求，实际 {len(sent)}"
    assert slept == [], f"确定性 404 不该有任何等待，实际 {slept}"
    assert isinstance(out, ais.AIProviderFatalError), f"应抛 AIProviderFatalError，实际 {out!r}"
    assert "404" in str(out), "报错必须保留状态码，供上层 is_fatal 与日志识别"


def test_kimi_auth_401_fails_after_one_request():
    """Kimi 里 'Fatal: don't waste retry budget on auth failure' 的注释必须真的成立。"""
    out, sent, slept = _drive(
        lambda: KimiClaudeCodeProvider(api_key="sk-test", model="kimi-k2"),
        [_resp(401, '{"error":"invalid api key"}')],
        env={"KIMI_BASE_URL": "https://kimi.example/coding"},
    )
    assert len(sent) == 1, f"鉴权失败只该发 1 次请求，实际 {len(sent)}"
    assert slept == []
    assert isinstance(out, ais.AIProviderFatalError), f"应抛 AIProviderFatalError，实际 {out!r}"


def test_gemini_bad_request_400_fails_after_one_request():
    out, sent, slept = _drive(
        lambda: GeminiProvider(api_key="k", model="gemini-3-flash-preview"),
        [_resp(400, '{"error":{"message":"invalid argument"}}')],
    )
    assert len(sent) == 1, f"确定性 400 只该发 1 次请求，实际 {len(sent)}"
    assert slept == []
    assert isinstance(out, ais.AIProviderFatalError), f"应抛 AIProviderFatalError，实际 {out!r}"


def test_transient_500_is_still_retried():
    """反向保护：真瞬时故障仍然按 AI_MAX_RETRIES 重试，不能被误判成确定性错误。"""
    out, sent, slept = _drive(
        lambda: OpenRouterProvider(api_key="sk-test", model="gpt-5.5"),
        [_resp(500, '{"error":"upstream boom"}')],
        env={"AI_WAIT_MAX_SECONDS": "0"},
    )
    # 若被误判成确定性错误，这里只会有 1 次请求（本用例修复前后都必须通过）
    assert len(sent) == 3, f"500 应重试到 AI_MAX_RETRIES=3，实际 {len(sent)}"
    assert len(slept) == 2
    assert isinstance(out, Exception), f"重试耗尽后仍应抛异常，实际 {out!r}"


def test_transient_429_is_still_retried_and_can_succeed():
    out, sent, slept = _drive(
        lambda: OpenRouterProvider(api_key="sk-test", model="gpt-5.5"),
        [_resp(429, "rate limited"), _resp(200)],
        env={"AI_WAIT_MAX_SECONDS": "0"},
    )
    assert out == "OK", f"429 后应重试成功，实际 {out!r}"
    assert len(sent) == 2


def test_json_mode_downgrade_still_beats_the_fatal_classification():
    """400 + json_object 报错必须先走降级（它排在确定性判定之前），不能直接判死。"""
    marker = (
        '{"error":{"message":"input messages must contain the word '
        "'json' in some form to use 'json_object'\"}}"
    )
    out, sent, slept = _drive(
        lambda: OpenRouterProvider(api_key="sk-test", model="gpt-5.5"),
        [_resp(400, marker), _resp(200)],
        env={"AI_RESPONSE_JSON": "1", "AI_WAIT_MAX_SECONDS": "0"},
    )
    assert out == "OK", f"应降级后成功，实际 {out!r}"
    assert len(sent) == 2
    assert sent[0].get("response_format") == {"type": "json_object"}
    assert "response_format" not in sent[1]


# --------------------------------------------------------------------------------------
# 摘要层：假 provider
# --------------------------------------------------------------------------------------

class _FakeAIProvider:
    """按提示词类型返回合法 JSON；可让含某个标记的提示词失败若干次。"""

    def __init__(self, fail_marker: str = "", fail_times: int = 0):
        self.prompts: List[str] = []
        self.fail_marker = fail_marker
        self.fail_times = fail_times
        self.failed = 0

    def call_api(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.fail_marker and self.fail_marker in prompt and self.failed < self.fail_times:
            self.failed += 1
            raise Exception("上游 502：分片调用失败")
        if "【写作硬性要求】" in prompt:  # 日报分片提示词
            idx = [int(x) for x in re.findall(r"^\[(\d+)\] Title:", prompt, flags=re.M)]
            return json.dumps(
                {
                    "overview": "总览",
                    "trends": "热点",
                    "summaries": [
                        {
                            "index": i,
                            "title_zh": f"中文标题{i}",
                            "abstract_zh": f"中文摘要{i}：BaTiO3 铁电翻转，矫顽场 0.3 V/nm。",
                            "one_sentence_summary": f"中文一句话总结{i}：等变图网络拟合势能面。",
                        }
                        for i in idx
                    ],
                    "highlights": [],
                },
                ensure_ascii=False,
            )
        # 总览/热点 与 核心深度字段 共用一份返回（各取所需的键）
        return json.dumps(
            {"overview": "总览", "trends": "热点", "direction_note": "方向点评", "items": []},
            ensure_ascii=False,
        )


def _summarizer(provider) -> AISummarizer:
    s = AISummarizer.__new__(AISummarizer)  # 绕开 build_provider，避免真实网络配置
    s.provider = provider
    s.provider_name = "aigw"
    return s


def _articles(n: int) -> List[dict]:
    letters = "ABCDEFGHIJ"
    return [
        {
            "title": f"Paper {letters[i]} on ferroelectric switching",
            "abstract": f"Abstract {letters[i]} about BaTiO3 and neural network potentials.",
            "link": f"https://example.org/paper-{letters[i]}",
            "journal": "arXiv",
            "authors": ["A. Author"],
            "pub_date": "2026-08-30",
        }
        for i in range(n)
    ]


def test_failed_chunk_does_not_recall_the_successful_chunks():
    """第 3 个分片失败重试时，前两个已成功的分片必须复用缓存，不再调用 AI。"""
    arts = _articles(6)
    provider = _FakeAIProvider(fail_marker="Paper E", fail_times=1)  # 第 3 片（E、F）先失败一次
    env = {
        "AI_DAILY_MAX_PER_CALL": "2",
        "AI_DAILY_WAIT_MAX_SECONDS": "600",
        "AI_DAILY_NO_FALLBACK": "",
        "AI_NO_FALLBACK": "",
    }
    with mock.patch.dict(os.environ, env), \
         mock.patch("ai_summarizer.time.sleep"), \
         redirect_stdout(io.StringIO()):
        summary = _summarizer(provider).generate_daily_summary(arts, "2026-08-30")

    chunk_prompts = [p for p in provider.prompts if "【写作硬性要求】" in p]
    first_chunk_calls = [p for p in chunk_prompts if "Paper A" in p]
    assert len(first_chunk_calls) == 1, (
        f"已成功的第 1 个分片不该因为第 3 片失败而重发，实际发了 {len(first_chunk_calls)} 次"
    )
    assert len([p for p in chunk_prompts if "Paper C" in p]) == 1
    assert len([p for p in chunk_prompts if "Paper E" in p]) == 2, "只有失败的分片才该重试"
    assert len(chunk_prompts) == 4, f"3 个分片 + 1 次重试 = 4 次调用，实际 {len(chunk_prompts)}"

    # 分片重算逻辑不能破坏条目映射
    links = [row.get("link") for row in summary["full_list"]]
    assert links == [a["link"] for a in arts], f"full_list 顺序/内容错乱: {links}"
    assert summary["total"] == 6
    assert all(row.get("title_zh") for row in summary["full_list"])


def test_daily_summary_drops_the_duplicated_summaries_alias():
    """summaries 与 full_list 逐字节相同，会被 sidecar 序列化两遍——不再产出这个别名。"""
    arts = _articles(2)
    provider = _FakeAIProvider()
    with mock.patch.dict(os.environ, {"AI_DAILY_MAX_PER_CALL": "40"}), redirect_stdout(io.StringIO()):
        summary = _summarizer(provider).generate_daily_summary(arts, "2026-08-30")
    assert len(summary["full_list"]) == 2
    assert "summaries" not in summary, "summaries 是 full_list 的重复拷贝，不应写进日报 summary"


def test_chunked_summary_drops_the_duplicated_summaries_alias():
    arts = _articles(4)
    provider = _FakeAIProvider()
    with mock.patch.dict(os.environ, {"AI_DAILY_MAX_PER_CALL": "2"}), redirect_stdout(io.StringIO()):
        summary = _summarizer(provider).generate_daily_summary(arts, "2026-08-30")
    assert len(summary["full_list"]) == 4
    assert "summaries" not in summary


def test_fallback_summary_drops_the_duplicated_summaries_alias():
    with redirect_stdout(io.StringIO()):
        data = _summarizer(_FakeAIProvider()).fallback_summary(_articles(2), "2026-08-30")
    assert len(data["full_list"]) == 2
    assert "summaries" not in data


def test_daily_summary_gives_up_immediately_on_fatal_provider_error():
    """坏模型名/过期密钥：立刻兜底，不要按 AI_DAILY_WAIT_MAX_SECONDS=3600 空转一小时。"""
    calls: List[str] = []

    class _Fatal:
        def call_api(self, prompt):
            calls.append(prompt)
            raise ais.AIProviderFatalError("OpenRouter API错误 (404): model not found")

    slept: List[float] = []
    env = {
        "AI_DAILY_WAIT_MAX_SECONDS": "3600",
        "AI_DAILY_MAX_PER_CALL": "40",
        "AI_DAILY_NO_FALLBACK": "",
        "AI_NO_FALLBACK": "",
    }
    with mock.patch.dict(os.environ, env), \
         mock.patch("ai_summarizer.time.sleep", side_effect=lambda s: slept.append(s)), \
         redirect_stdout(io.StringIO()):
        summary = _summarizer(_Fatal()).generate_daily_summary(_articles(2), "2026-08-30")

    assert len(calls) == 1, f"确定性错误只该调用 1 次，实际 {len(calls)}"
    assert slept == [], f"不该等待重试，实际 {slept}"
    assert summary.get("generated_by") == "fallback", "仍要 fail-soft 地兜底出日报"
    assert len(summary["full_list"]) == 2, "兜底内容不能丢文章"


def test_daily_summary_raises_fast_when_no_fallback_is_set():
    """backfill-daily.yml 设了 AI_DAILY_NO_FALLBACK=1：应立刻带原因失败，而不是耗满一小时。"""
    calls: List[str] = []

    class _Fatal:
        def call_api(self, prompt):
            calls.append(prompt)
            raise ais.AIProviderFatalError("OpenRouter API错误 (404): model not found")

    slept: List[float] = []
    env = {"AI_DAILY_WAIT_MAX_SECONDS": "3600", "AI_DAILY_NO_FALLBACK": "1"}
    raised = None
    with mock.patch.dict(os.environ, env), \
         mock.patch("ai_summarizer.time.sleep", side_effect=lambda s: slept.append(s)), \
         redirect_stdout(io.StringIO()):
        try:
            _summarizer(_Fatal()).generate_daily_summary(_articles(2), "2026-08-30")
        except Exception as exc:  # noqa: BLE001
            raised = exc
    assert isinstance(raised, ais.AIProviderFatalError), f"应原样抛出确定性错误，实际 {raised!r}"
    assert len(calls) == 1 and slept == []


def test_overview_trends_gives_up_immediately_on_fatal_provider_error():
    calls: List[str] = []

    class _Fatal:
        def call_api(self, prompt):
            calls.append(prompt)
            raise ais.AIProviderFatalError("OpenRouter API错误 (401): bad key")

    slept: List[float] = []
    with mock.patch.dict(os.environ, {"AI_DAILY_WAIT_MAX_SECONDS": "3600"}), \
         mock.patch("ai_summarizer.time.sleep", side_effect=lambda s: slept.append(s)), \
         redirect_stdout(io.StringIO()):
        overview, trends = _summarizer(_Fatal())._build_overview_trends(_articles(2), "2026-08-30")
    assert (overview, trends) == ("", "")
    assert len(calls) == 1 and slept == []


def test_all_prompt_builders_contain_lowercase_json():
    """AI_RESPONSE_JSON=1 时上游要求输入含小写 'json'（大小写敏感），四个提示词都得有。"""
    provider = _FakeAIProvider()
    s = _summarizer(provider)
    arts = _articles(1)

    prompts = {
        "_build_prompt": s._build_prompt(arts, "2026-08-30"),
        "_build_missing_summaries_prompt": s._build_missing_summaries_prompt(arts, [1], "2026-08-30"),
    }
    with redirect_stdout(io.StringIO()):
        s._build_overview_trends(arts, "2026-08-30")
        prompts["_build_overview_trends"] = provider.prompts[-1]
        s.generate_core_deep_fields(
            [{"title_en": arts[0]["title"], "title_zh": "中文标题", "link": arts[0]["link"]}],
            "2026-08-30",
        )
        prompts["generate_core_deep_fields"] = provider.prompts[-1]

    for name, prompt in sorted(prompts.items()):
        assert "json" in prompt, f"{name} 缺少小写 'json'（大写 JSON 不被 json_object 模式认可）"


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("[OK] ai_summarizer 回归测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
