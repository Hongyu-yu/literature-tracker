#!/usr/bin/env python3
"""相关性批量分析的失败必须可见，且不能被伪装成 AI 的真实判定。

原问题（relevance_enricher.py）：
1. 整批 API/解析失败被 `except Exception: mapping = {}` 无声吞掉，日志里毫无痕迹，
   该批 16 篇全部拿到本地关键词规则合成的结论，外观与模型真实判定一模一样；
   调用方于是把它们写进 deep_history.json 永久黑名单，再也不会重新评分。
2. 触发条件还特别容易达成：单条 score 写成 "8/10" 会让 int() 抛错，
   整批 16 篇一起作废；模型省掉外层 {"items": …} 直接返回数组也是同样下场。
"""

import io
import json
from contextlib import redirect_stdout
from unittest import mock

import relevance_enricher as re_mod


AI4S_ARTICLE = {
    "title": "Machine learning accelerated discovery of ferroelectric HfO2 thin films",
    "abstract": "A graph neural network predicts polarization switching in ferroelectric hafnia.",
    "journal": "arXiv",
    "authors": [],
}
PLAIN_ARTICLE = {
    "title": "A note on elliptic curves over finite fields",
    "abstract": "We prove a bound for the number of rational points.",
    "journal": "arXiv",
    "authors": [],
}


class _StubProvider:
    """call_api 依次吐出预设响应；给 None 表示抛异常（模拟 502/超时）。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def call_api(self, prompt):
        self.calls += 1
        resp = self._responses.pop(0) if self._responses else None
        if resp is None:
            raise RuntimeError("502 Bad Gateway")
        return resp


def _run(articles, responses, batch_size=16):
    provider = _StubProvider(responses)
    buf = io.StringIO()
    with mock.patch.object(re_mod, "build_provider", return_value=provider), redirect_stdout(buf):
        results = re_mod.batch_analyze_relevance(
            articles,
            provider_name="gemini",
            api_key="test-key",
            batch_size=batch_size,
        )
    return results, buf.getvalue()


def _items_json(items):
    return json.dumps({"items": items}, ensure_ascii=False)


def test_batch_failure_is_logged():
    """整批调用失败必须打印 ⚠️ 日志（此前是静默的）。"""
    results, out = _run([AI4S_ARTICLE, PLAIN_ARTICLE], [None])
    assert "⚠️" in out, f"批次失败没有任何日志输出: {out!r}"
    assert "502" in out, f"日志里应带上原始异常信息，便于排查: {out!r}"
    assert len(results) == 2


def test_batch_failure_results_are_marked_as_fallback():
    """回退结论必须自带 source=fallback，调用方据此避免写入永久黑名单。"""
    results, _ = _run([AI4S_ARTICLE, PLAIN_ARTICLE], [None])
    for r in results:
        assert r.get("source") == "fallback", f"回退结论缺少 source 标记: {r}"
    # 回退分值保持原状（不能悄悄把关键词命中的文献挤出日报）
    assert results[0]["score"] == 6
    assert results[1]["score"] == 0


def test_no_api_key_results_are_marked_as_fallback():
    """未配置 key 时同样不是模型结论，必须标记为回退。"""
    results = re_mod.batch_analyze_relevance(
        [AI4S_ARTICLE], provider_name="gemini", api_key="  "
    )
    assert results[0].get("source") == "fallback", results[0]


def test_partial_response_is_logged():
    """模型只回了一部分序号时，剩余的静默回退也要记账。"""
    resp = _items_json([
        {"index": 1, "is_relevant": True, "score": 9, "explanation": "相关", "detailed_summary": ""}
    ])
    results, out = _run([AI4S_ARTICLE, PLAIN_ARTICLE], [resp])
    assert "1/2" in out or "回退" in out, f"部分缺失没有任何日志: {out!r}"
    assert results[0]["source"] == "model"
    assert results[1]["source"] == "fallback"


def test_one_bad_score_does_not_void_the_whole_batch():
    """单条 score 为 "8/10" 只影响它自己，同批其余文献的模型判定必须保留。"""
    resp = _items_json([
        {"index": 1, "is_relevant": True, "score": "8/10", "explanation": "AI×材料", "detailed_summary": "摘要一"},
        {"index": 2, "is_relevant": False, "score": 1, "explanation": "纯数学", "detailed_summary": "摘要二"},
    ])
    results, _ = _run([AI4S_ARTICLE, PLAIN_ARTICLE], [resp])
    assert results[1]["explanation"] == "纯数学", "一条 score 格式错误让整批模型判定全部作废"
    assert results[1]["score"] == 1
    assert results[1]["source"] == "model"
    # 出错的那条自身降级为 0 分，但仍是模型返回的 is_relevant/explanation
    assert results[0]["score"] == 0
    assert results[0]["is_relevant"] is True
    assert results[0]["source"] == "model"


def test_score_is_clamped_to_0_10():
    mapping = re_mod._parse_items(
        {"items": [{"index": 1, "score": 99}, {"index": 2, "score": -4}]}
    )
    assert mapping[1]["score"] == 10, mapping[1]
    assert mapping[2]["score"] == 0, mapping[2]


def test_bare_list_response_is_accepted():
    """模型省掉外层 {"items": …} 直接返回数组时不应整批作废。"""
    resp = json.dumps([
        {"index": 1, "is_relevant": True, "score": 8, "explanation": "AI×材料", "detailed_summary": ""},
        {"index": 2, "is_relevant": False, "score": 2, "explanation": "无关", "detailed_summary": ""},
    ], ensure_ascii=False)
    results, out = _run([AI4S_ARTICLE, PLAIN_ARTICLE], [resp])
    assert results[0]["score"] == 8, f"纯数组响应被整批丢弃: {out!r}"
    assert results[1]["explanation"] == "无关"
    assert [r["source"] for r in results] == ["model", "model"]


def test_extract_json_handles_fence_and_array():
    fenced = '```json\n{"items": [{"index": 1}]}\n```'
    assert re_mod._extract_json(fenced) == {"items": [{"index": 1}]}
    assert re_mod._extract_json('[{"index": 1}, {"index": 2}]') == [{"index": 1}, {"index": 2}]
    # 前后夹带解释文字时仍能截取对象
    assert re_mod._extract_json('好的，结果如下：{"items": []} 完毕') == {"items": []}
    try:
        re_mod._extract_json("对不起，我无法回答。")
    except Exception:
        pass
    else:
        raise AssertionError("无 JSON 时应抛异常，交由上层回退")


def test_successful_batch_keeps_model_verdict():
    """成功路径行为不变：模型给什么就是什么，不触发任何回退日志。"""
    resp = _items_json([
        {"index": 1, "is_relevant": True, "score": 9, "explanation": "强相关", "detailed_summary": "详细"},
        {"index": 2, "is_relevant": False, "score": 0, "explanation": "不相关", "detailed_summary": ""},
    ])
    results, out = _run([AI4S_ARTICLE, PLAIN_ARTICLE], [resp])
    assert results[0]["is_relevant"] is True and results[0]["score"] == 9
    assert results[0]["detailed_summary"] == "详细"
    assert results[1]["is_relevant"] is False
    assert "⚠️" not in out, f"成功路径不该有告警: {out!r}"


if __name__ == "__main__":
    for name in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[name]()
        print(f"✅ {name}")
    print("✅ 全部通过")
