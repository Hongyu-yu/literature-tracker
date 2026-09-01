#!/usr/bin/env python3
"""run_optimized_sync 的回归测试：坏数据不得覆盖好数据 + 白烧的 CPU/token。

覆盖：
1. 相关性分析回退结果不得被当作「已分析」永久落盘（原有用例）。
   relevance_enricher 在整批 API 调用/JSON 解析失败时，会给该批每篇文献合成一条
   本地关键词规则结论（explanation = "AI 返回不完整…"）。run_optimized_sync 以前
   无条件把这些文献写进 data/deep_history.json，而该文件是永久黑名单，于是一次
   429/超时就把当天整批候选永久排除在 AI 评分之外。
2. data/index.json 读失败时不得被当成空索引再原样覆盖回去（会把 5000 篇的站点
   archive 抹成当天抓到的百来篇，而且 CI 全绿）。
3. data/ai_relevant.json 读失败时同上；并且此时不能更新 deep_history.json，
   否则这批文献既被标记「已处理」又没落盘。
4. 索引条目腰斩时拒绝写回。
5. index.json 必须带 total / last_update / user_keywords，否则站点页头永远是
   「共 0 篇文献 / 最后更新: -」。
6. ai_relevant.json 存量默认不再重过滤（每次同步 ~100s CPU 且会永久删数据），
   只有显式 AI_RELEVANT_REFILTER=1 才裁剪。
7. 本地 AI4S 门槛在送 LLM 之前先筛一遍，别为注定要丢的候选付 token。
8. AI_NOTIFY_MAX=0 时不构造 NotionTGNotifier，并把「开关关着」打进日志。
"""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest import mock

import run_optimized_sync as ros


FALLBACK_EXPL = "AI 返回不完整，已按本地 AI×物理/化学/材料规则回退判定。"

AI4S_TITLE = "Machine learning accelerated discovery of ferroelectric HfO2 thin films"
AI4S_ABSTRACT = "We use a graph neural network to predict polarization switching in ferroelectric hafnia."

AI4S_TITLE_2 = "Neural network potentials for superconducting hydrides"
AI4S_ABSTRACT_2 = "A deep learning interatomic potential for high-pressure superconductivity in hydrides."

# 本地 AI4S 判定过不了的选题（既非 AI×凝聚态，也不属于目标领域）
OFFTOPIC_TITLE = "Retail banking customer survey"
OFFTOPIC_ABSTRACT = "We interview customers about mortgage products."


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


def _run_sync(tmp, articles, analyses, env_extra=None):
    """在临时目录里跑一次完整的 run_optimized_sync()，只打桩外部依赖。

    返回 dict：
      relevance_calls -> 每次 batch_analyze_relevance 实际收到的条目列表
      notifier_cls    -> NotionTGNotifier 的替身（断言是否被构造）
    """

    class _StubFetcher:
        def __init__(self, keywords=None):
            pass

        def fetch_all(self, feeds):
            return list(articles)

        def filter_by_keywords(self, arts):
            return []

    relevance_calls = []

    def _fake_relevance(items, **kwargs):
        items = list(items)
        relevance_calls.append(items)
        return list(analyses)[: len(items)]

    notifier_cls = mock.MagicMock()

    env = {
        "AI_API_KEY": "test-key",
        "AI_PROVIDER": "gemini",
        "AI_MODEL": "",
        "AI_RELEVANCE_DAYS_BACK": "3",
        "AI_RELEVANCE_INCLUDE_TODAY": "0",
        "AI_RELEVANCE_THRESHOLD": "6",
        "AI_NOTIFY_SCORE_MIN": "8",
        "AI_NOTIFY_MAX": "5",
        "AI_RELEVANT_REFILTER": "0",
        "FOCUS_ENABLED": "0",
    }
    env.update(env_extra or {})
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)  # 线上仓库自带 data/
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ros, "RSSFetcher", _StubFetcher), \
                mock.patch.object(ros, "DEDUP_CONFIG", {"enabled": False}), \
                mock.patch.object(ros, "batch_analyze_relevance", _fake_relevance), \
                mock.patch.object(ros, "NotionTGNotifier", notifier_cls), \
                mock.patch.object(ros, "enrich_articles_zh", return_value=0), \
                mock.patch.object(ros, "generate_rss_feed", return_value=None):
            ros.run_optimized_sync()
    finally:
        os.chdir(cwd)

    return {"relevance_calls": relevance_calls, "notifier_cls": notifier_cls}


def _read_json(tmp, rel):
    with open(os.path.join(tmp, rel), "r", encoding="utf-8") as f:
        return json.load(f)


def _read_text(tmp, rel):
    with open(os.path.join(tmp, rel), "r", encoding="utf-8") as f:
        return f.read()


def _write_text(tmp, rel, text):
    path = os.path.join(tmp, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_fallback_analysis_is_not_marked_processed():
    """AI 回退判定的文献不能进 deep_history.json；模型真实判定的才可以。"""
    day = _yesterday()
    a_fallback = _StubArticle("https://arxiv.org/abs/9001.00001", day, AI4S_TITLE, AI4S_ABSTRACT)
    b_real = _StubArticle("https://arxiv.org/abs/9001.00002", day, AI4S_TITLE_2, AI4S_ABSTRACT_2)
    analyses = [
        {"is_relevant": False, "score": 0, "explanation": FALLBACK_EXPL, "detailed_summary": ""},
        {"is_relevant": False, "score": 2, "explanation": "纯方法学论文，与本组方向无关。", "detailed_summary": ""},
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


def test_broken_index_json_is_not_overwritten_with_this_run_only():
    """index.json 读不出来时，绝不能拿本次抓到的几篇覆盖掉磁盘上的 5000 篇。"""
    day = _yesterday()
    art = _StubArticle("https://arxiv.org/abs/9002.00001", day, AI4S_TITLE, AI4S_ABSTRACT)
    broken = '{"articles": [{"link": "https://example.org/a", "title": "trunc'

    tmp = tempfile.mkdtemp()
    try:
        _write_text(tmp, "data/index.json", broken)
        _run_sync(tmp, [art], [{"is_relevant": True, "score": 9, "explanation": "真实判定", "detailed_summary": ""}])

        assert _read_text(tmp, "data/index.json") == broken, (
            "损坏的 index.json 被当成空索引并覆盖写回，站点历史会被抹掉"
        )
        # ai_relevant.json 可读，本轮的成果照常落盘（fail-soft，不是整轮失败）
        rows = _read_json(tmp, "data/ai_relevant.json")
        assert [r for r in rows if r.get("link") == art.link], "索引不可写不应连带丢掉 ai_relevant 的新条目"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_index_json_top_level_list_is_not_overwritten():
    """顶层是数组（老格式/坏合并）时 .get 会 AttributeError，同样不许覆盖。"""
    art = _StubArticle("https://arxiv.org/abs/9002.00002", _yesterday(), AI4S_TITLE, AI4S_ABSTRACT)
    wrong_shape = '[{"link": "https://example.org/a"}]'

    tmp = tempfile.mkdtemp()
    try:
        _write_text(tmp, "data/index.json", wrong_shape)
        _run_sync(tmp, [art], [{"is_relevant": True, "score": 9, "explanation": "真实判定", "detailed_summary": ""}])
        assert _read_text(tmp, "data/index.json") == wrong_shape, "顶层结构不对时不能覆盖原文件"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_broken_ai_relevant_json_is_not_overwritten_and_deep_history_untouched():
    """ai_relevant.json 读失败时保留原文件；且不能把这批文献记成已处理。"""
    day = _yesterday()
    art = _StubArticle("https://arxiv.org/abs/9003.00001", day, AI4S_TITLE, AI4S_ABSTRACT)
    broken = '[{"link": "https://example.org/a", "ai_detailed_summary": "很贵的深'

    tmp = tempfile.mkdtemp()
    try:
        _write_text(tmp, "data/ai_relevant.json", broken)
        _run_sync(tmp, [art], [{"is_relevant": True, "score": 9, "explanation": "真实判定", "detailed_summary": ""}])

        assert _read_text(tmp, "data/ai_relevant.json") == broken, (
            "损坏的 ai_relevant.json 被空表覆盖，唯一的长期库（含 ai_detailed_summary）会被抹掉"
        )
        processed_path = os.path.join(tmp, "data", "deep_history.json")
        processed = set(_read_json(tmp, "data/deep_history.json")) if os.path.exists(processed_path) else set()
        assert art.link not in processed, (
            "ai_relevant 没落盘却把文献标记为已处理，它以后再也不会被 AI 评分"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_index_write_refused_when_count_halves():
    """条目数腰斩（过滤规则误伤等）时拒绝写回，保留磁盘上的原索引。"""
    stale = [
        {"link": f"https://example.org/{i}", "title": "Paper A", "abstract": "", "pub_date": "2026-01-01", "journal": "arXiv"}
        for i in range(200)
    ]
    original = json.dumps({"articles": stale}, ensure_ascii=False)

    tmp = tempfile.mkdtemp()
    try:
        _write_text(tmp, "data/index.json", original)
        _run_sync(tmp, [], [])
        saved = _read_json(tmp, "data/index.json")
        assert len(saved.get("articles", [])) == 200, (
            f"索引从 200 篇被写成 {len(saved.get('articles', []))} 篇，站点 archive 被抹掉"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_index_json_carries_total_and_last_update():
    """docs/app.js 读 data.total / data.last_update / data.user_keywords。"""
    day = _yesterday()
    art = _StubArticle("https://arxiv.org/abs/9004.00001", day, AI4S_TITLE, AI4S_ABSTRACT)

    tmp = tempfile.mkdtemp()
    try:
        _run_sync(tmp, [art], [{"is_relevant": True, "score": 9, "explanation": "真实判定", "detailed_summary": ""}])
        saved = _read_json(tmp, "data/index.json")

        assert "total" in saved, "index.json 缺少 total，页头永远显示「共 0 篇文献」"
        assert "last_update" in saved, "index.json 缺少 last_update，页头永远显示「最后更新: -」"
        assert "user_keywords" in saved, "index.json 缺少 user_keywords，关键词用户下拉框永远为空"

        assert saved["total"] == len(saved["articles"])
        parsed = datetime.fromisoformat(saved["last_update"])
        assert parsed.tzinfo is not None, "last_update 必须带时区偏移，否则 JS new Date() 解析结果会漂"
        assert isinstance(saved["user_keywords"], dict)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ai_relevant_history_is_kept_by_default_and_pruned_only_on_demand():
    """存量默认不再重过滤（100s CPU + 永久删数据），AI_RELEVANT_REFILTER=1 才裁剪。"""
    stale = {
        "link": "https://example.org/legacy",
        "title": OFFTOPIC_TITLE,
        "abstract": OFFTOPIC_ABSTRACT,
        "pub_date": "2026-01-01",
        "journal": "arXiv",
        "ai_score": 8,
        "ai_explanation": "模型判定：相关",
        "ai_detailed_summary": "一段很贵的深度解析",
    }
    assert ros._is_ai4science_relevant(dict(stale)) is False, "用例前提：这条过不了本地 AI4S 判定"

    tmp = tempfile.mkdtemp()
    try:
        _write_text(tmp, "data/ai_relevant.json", json.dumps([stale], ensure_ascii=False))
        _run_sync(tmp, [], [])
        rows = _read_json(tmp, "data/ai_relevant.json")
        assert [r for r in rows if r.get("link") == stale["link"]], (
            "默认跑一次同步就把存量里过不了当前词表的记录永久删掉了"
        )

        _run_sync(tmp, [], [], env_extra={"AI_RELEVANT_REFILTER": "1"})
        rows = _read_json(tmp, "data/ai_relevant.json")
        assert not [r for r in rows if r.get("link") == stale["link"]], (
            "AI_RELEVANT_REFILTER=1 时应当真的裁剪存量"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_offtopic_candidates_are_not_sent_to_the_llm():
    """本地 AI4S 判定是必要条件，注定要丢的候选不该先花 token 再丢。"""
    day = _yesterday()
    good = _StubArticle("https://arxiv.org/abs/9005.00001", day, AI4S_TITLE, AI4S_ABSTRACT)
    junk = _StubArticle("https://example.org/9005.00002", day, OFFTOPIC_TITLE, OFFTOPIC_ABSTRACT)

    tmp = tempfile.mkdtemp()
    try:
        res = _run_sync(
            tmp,
            [good, junk],
            [{"is_relevant": True, "score": 9, "explanation": "真实判定", "detailed_summary": ""}],
        )
        assert res["relevance_calls"], "batch_analyze_relevance 应当被调用一次"
        sent_links = [item.get("link") for item in res["relevance_calls"][0]]
        assert sent_links == [good.link], f"送进 LLM 的候选不对: {sent_links}"

        # 本地刷掉的 link 不进 deep_history：词表放宽后还要能重判
        processed = set(_read_json(tmp, "data/deep_history.json"))
        assert junk.link not in processed, "本地预筛掉的文献不应被永久标记为已处理"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_notifier_not_constructed_when_notify_max_is_zero():
    """AI_NOTIFY_MAX=0（fetch.yml 的现状）时不该构造 NotionTGNotifier。"""
    day = _yesterday()
    art = _StubArticle("https://arxiv.org/abs/9006.00001", day, AI4S_TITLE, AI4S_ABSTRACT)

    tmp = tempfile.mkdtemp()
    try:
        res = _run_sync(
            tmp,
            [art],
            [{"is_relevant": True, "score": 9, "explanation": "真实判定", "detailed_summary": ""}],
            env_extra={"AI_NOTIFY_MAX": "0"},
        )
        assert res["notifier_cls"].call_count == 0, (
            "AI_NOTIFY_MAX=0 时仍然构造了 NotionTGNotifier（读凭据却一条都推不出去）"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_atomic_write_json_keeps_original_when_dump_fails():
    """写失败时原文件一个字节都不能动，也不许留下 .tmp 半成品。"""
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "data", "x.json")
        original = '{"articles": [1, 2, 3]}'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)

        raised = False
        try:
            ros._atomic_write_json(path, {"bad": object()})
        except TypeError:
            raised = True
        assert raised, "不可序列化的 payload 应当抛出，而不是悄悄写坏文件"

        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == original, "写失败却把原文件截断了"
        assert not os.path.exists(path + ".tmp"), "残留了 .tmp 半成品"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in (
        test_fallback_analysis_is_not_marked_processed,
        test_fallback_entry_in_ai_relevant_is_upgraded_when_ai_recovers,
        test_is_fallback_analysis_helper,
        test_broken_index_json_is_not_overwritten_with_this_run_only,
        test_index_json_top_level_list_is_not_overwritten,
        test_broken_ai_relevant_json_is_not_overwritten_and_deep_history_untouched,
        test_index_write_refused_when_count_halves,
        test_index_json_carries_total_and_last_update,
        test_ai_relevant_history_is_kept_by_default_and_pruned_only_on_demand,
        test_offtopic_candidates_are_not_sent_to_the_llm,
        test_notifier_not_constructed_when_notify_max_is_zero,
        test_atomic_write_json_keeps_original_when_dump_fails,
    ):
        fn()
        print(f"✅ {fn.__name__}")
    print("✅ 全部通过")
