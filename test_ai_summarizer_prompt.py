#!/usr/bin/env python3
"""AI 日报 prompt 字数要求与 clamp 阈值回归（stdlib-only, 无网络）。
确保：摘要 ≤200 字、亮点 3~5 句 ≤250 字、深度三字段 ≤150 字的硬性要求写进 prompt；
clamp 阈值放行新长度（否则 AI 写够了也会被截断）。"""
from ai_summarizer import AISummarizer, _clamp_text

# 构造一段摘要：前 300 字符是普通文本，300 字符后插入唯一标记子串（UNIQUE_TOKEN_XQ7Z），
# 这样只有 [:600] 截断才能把 UNIQUE_TOKEN_XQ7Z 包含进 prompt，[:300] 不可能包含。
_UNIQUE_MARKER = "UNIQUE_TOKEN_XQ7Z_BEYOND_300"
_PREFIX = "A" * 300            # 恰好 300 个字符
_SUFFIX = _UNIQUE_MARKER + "B" * 300  # marker 在 300 字符之后
_LONG_ABSTRACT = _PREFIX + _SUFFIX    # 总长 > 600

_ARTS = [{"title": "Room-temperature ferroelectricity in 2D NbOI2",
          "journal": "arXiv", "authors": ["A", "B"],
          "abstract": _LONG_ABSTRACT}]


def _make_summarizer():
    """构造 AISummarizer 测试实例（不依赖真实 API key，不发网络请求）。"""
    return AISummarizer(api_provider="gemini", api_key="test-dummy-key")


def test_daily_prompt_requires_longer_abstract_and_highlight():
    prompt = _make_summarizer()._build_prompt(_ARTS, "2026-01-01")
    assert "≤200" in prompt, "abstract_zh 应要求 ≤200 字"
    assert "3~5" in prompt, "one_sentence_summary 应要求 3~5 句"
    assert "≤250" in prompt, "亮点应要求 ≤250 字"
    # 旧的过短上限不应再出现在硬性要求里
    assert "≤120 字" not in prompt and "≤40 字" not in prompt and "≤100" not in prompt


def test_daily_prompt_feeds_more_abstract_source():
    """喂给 AI 的原文摘要应放宽到 600 字符（否则写不出 200 字概括）。
    验证：_UNIQUE_MARKER 位于原文第 300 字符之后，只有 [:600] 截断才能把它纳入 prompt。
    若仍用 [:300]，UNIQUE_MARKER 不会出现在 prompt 中，测试会失败。"""
    prompt = _make_summarizer()._build_prompt(_ARTS, "2026-01-01")
    assert _UNIQUE_MARKER in prompt, (
        f"prompt 应包含摘要 300 字符之后的内容（验证 [:600] 截断生效），"
        f"但未找到标记: {_UNIQUE_MARKER!r}"
    )


def test_clamp_allows_100_chinese_chars_for_highlight():
    long_zh = "创" * 110
    result = _clamp_text(long_zh, 120)
    assert len(result) >= 100, (
        f"clamp(120) 应保留 ≥100 个字符，实际保留 {len(result)}"
    )


def test_core_deep_prompt_uses_detailed_limits():
    """深度三字段 prompt 应要求 2~3 句、≤150 字（2026-07-30 用户反馈"太简要"后放宽）。"""
    import inspect
    src = inspect.getsource(AISummarizer.generate_core_deep_fields)
    assert "180~320" in src, "method_point/related_work/implication 应要求详细段落"
    assert "2~3" in src, "深度三字段应要求每条 2~3 句"
    assert "≤60" not in src and "≤70" not in src, "旧的 60/70 字上限不应保留"


def test_core_deep_clamp_allows_detailed_fields():
    """deep_fields clamp 应放行 200 字（否则 AI 写够 150 字也会被截断）。"""
    long_zh = "方" * 180
    result = _clamp_text(long_zh, 200)
    assert len(result) >= 150, (
        f"clamp(200) 应保留 ≥150 个字符，实际保留 {len(result)}"
    )


def test_daily_prompt_requires_research_relation_fields():
    prompt = _make_summarizer()._build_prompt(_ARTS, "2026-01-01")
    assert "method_point" in prompt
    assert "related_work" in prompt
    assert "implication" in prompt
    assert "DREAM 尚未开展" in prompt
    assert "团队研究方向背景" in prompt
    assert "机器学习势" in prompt


def test_fallback_summary_has_nonempty_content():
    s = _make_summarizer()
    out = s.fallback_summary([{
        "title": "Machine learning potential for ferroelectric switching",
        "title_zh": "铁电翻转机器学习势",
        "abstract": "We use molecular dynamics to study polarization switching.",
        "abstract_zh": "用分子动力学研究极化翻转。",
        "abstract_zh_full": "我们使用分子动力学研究极化翻转。",
        "link": "https://example.com/1",
    }], "2026-01-01")
    item = out["full_list"][0]
    assert out["overview"] and out["trends"] and out["research_direction_note"]
    assert all(item.get(k) for k in ("title_zh", "abstract_zh", "abstract_zh_full", "summary", "method_point", "related_work", "implication"))


if __name__ == "__main__":
    for _fn in sorted(k for k in dir() if k.startswith("test_")):
        globals()[_fn](); print(f"✓ {_fn}")
    print("OK")
