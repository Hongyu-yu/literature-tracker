#!/usr/bin/env python3
"""相关性分析回退结果不得被当作「已分析」永久落盘。

原问题：relevance_enricher 在整批 API 调用/JSON 解析失败时，会给该批每篇文献
合成一条本地关键词规则结论（explanation = "AI 返回不完整…"）。run_optimized_sync
以前无条件把这些文献写进 data/deep_history.json，而该文件是永久黑名单
(recent_candidates 会过滤掉里面的 link)，于是一次 429/超时就把当天整批候选
永久排除在 AI 评分之外 —— 它们再也进不了 ai_relevant.json 和日报。
"""

import json
import os
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

import run_optimized_sync as ros


FALLBACK_EXPL = "AI 返回不完整，已按本地 AI×物理/化学/材料规则回退判定。"

AI4S_TITLE = "Machine learning accelerated discovery of ferroelectric HfO2 thin films"
AI4S_ABSTRACT = "We use a graph neural network to predict polarization switching in ferroelectric hafnia."


class _StubArticle:
    def __init__(self, link, pub_date, title, abstract=""):
        self.link = link
        self.pub_date = pub_date
        self.title = title
        self.title_zh = ""
        self.abstract = abstract
        self.journal = "arXiv"

    def to_dict(self):
        return {
            "link": self.link,
            "pub_date": self.pub_date,
            "title": self.title,
            "title_zh": "",
            "abstract": self.abstract,
            "journal": self.journal,
            "authors": [],
            "source_url": "",
        }


def _yesterday():
    return (ros.get_beijing_time() - timedelta(days=1)).strftime("%Y-%m-%d")


def _run_sync(tmp, articles, analyses):
    """在临时目录里跑一次完整的 run_optimized_sync()，只打桩外部依赖。"""

    class _StubFetcher:
        def __init__(self, keywords=None):
            pass

        def fetch_all(self, feeds):
            return list(articles)

        def filter_by_keywords(self, arts):
            return []

    env = {
        "AI_API_KEY": "test-key",
        "AI_PROVIDER": "gemini",
        "AI_MODEL": "",
        "AI_RELEVANCE_DAYS_BACK": "3",
        "AI_RELEVANCE_INCLUDE_TODAY": "0",
        "FOCUS_ENABLED": "0",
    }
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)  # 线上仓库自带 data/
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ros, "RSSFetcher", _StubFetcher), \
                mock.patch.object(ros, "DEDUP_CONFIG", {"enabled": False}), \
                mock.patch.object(ros, "batch_analyze_relevance", return_value=list(analyses)), \
                mock.patch.object(ros, "NotionTGNotifier", mock.MagicMock()), \
                mock.patch.object(ros, "enrich_articles_zh", return_value=0), \
                mock.patch.object(ros, "generate_rss_feed", return_value=None):
            ros.run_optimized_sync()
    finally:
        os.chdir(cwd)


def _read_json(tmp, rel):
    with open(os.path.join(tmp, rel), "r", encoding="utf-8") as f:
        return json.load(f)


def test_fallback_analysis_is_not_marked_processed():
    """AI 回退判定的文献不能进 deep_history.json；模型真实判定的才可以。"""
    day = _yesterday()
    a_fallback = _StubArticle("https://arxiv.org/abs/9001.00001", day, "Paper A")
    b_real = _StubArticle("https://arxiv.org/abs/9001.00002", day, "Paper B")
    analyses = [
        {"is_relevant": False, "score": 0, "explanation": FALLBACK_EXPL, "detailed_summary": ""},
        {"is_relevant": False, "score": 2, "explanation": "纯数学论文，与 AI×材料无关。", "detailed_summary": ""},
    ]

    tmp = tempfile.mkdtemp()
    try:
        _run_sync(tmp, [a_fallback, b_real], analyses)
        processed = set(_read_json(tmp, "data/deep_history.json"))
        assert b_real.link in processed, "模型真实判定为不相关的文献应标记为已处理"
        assert a_fallback.link not in processed, (
            "回退判定的文献被写进 deep_history.json，将永远无法再被 AI 评分"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fallback_entry_in_ai_relevant_is_upgraded_when_ai_recovers():
    """上次因 API 失败落盘的回退结论，AI 恢复后必须被真实结论覆盖。"""
    day = _yesterday()
    art = _StubArticle("https://arxiv.org/abs/9001.00003", day, AI4S_TITLE, AI4S_ABSTRACT)

    stale = art.to_dict()
    stale.update({"ai_score": 6, "ai_explanation": FALLBACK_EXPL, "ai_detailed_summary": ""})

    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
        with open(os.path.join(tmp, "data", "ai_relevant.json"), "w", encoding="utf-8") as f:
            json.dump([stale], f, ensure_ascii=False)

        real = {
            "is_relevant": True,
            "score": 9,
            "explanation": "图神经网络用于铁电材料性质预测，属于 AI×材料交叉。",
            "detailed_summary": "研究对象为铁电 HfO2 薄膜。",
        }
        _run_sync(tmp, [art], [real])

        rows = _read_json(tmp, "data/ai_relevant.json")
        row = [r for r in rows if r.get("link") == art.link]
        assert row, "原有条目不能被丢弃"
        assert row[0]["ai_score"] == 9, f"回退占位的 ai_score 未被真实结论覆盖: {row[0]['ai_score']}"
        assert row[0]["ai_explanation"] == real["explanation"], "回退占位的 ai_explanation 未被覆盖"
        assert art.link in set(_read_json(tmp, "data/deep_history.json"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_is_fallback_analysis_helper():
    """回退识别的边界：非 dict / 显式 source 标记 / 无 key 占位文案。"""
    assert ros._is_fallback_analysis({"explanation": FALLBACK_EXPL}) is True
    assert ros._is_fallback_analysis({"explanation": "未配置 AI_API_KEY，跳过相关性分析"}) is True
    assert ros._is_fallback_analysis({"source": "fallback", "explanation": "看起来像真的"}) is True
    assert ros._is_fallback_analysis(None) is True
    assert ros._is_fallback_analysis({"explanation": "模型判定：与 AI×材料强相关。"}) is False


if __name__ == "__main__":
    for fn in (
        test_fallback_analysis_is_not_marked_processed,
        test_fallback_entry_in_ai_relevant_is_upgraded_when_ai_recovers,
        test_is_fallback_analysis_helper,
    ):
        fn()
        print(f"✅ {fn.__name__}")
    print("✅ 全部通过")
