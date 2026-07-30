"""focus_interest 画像匹配测试(无网络:provider 用桩,画像文件用 tempfile + chdir)。"""

import json
import os
import tempfile

import focus_interest


def _profile():
    return {
        "generated_at": "2026-07-29",
        "scholars": [{"scholar_id": "x", "name": "S", "works": [],
                      "directions_zh": "铁电与机器学习势"}],
        "our_work_zh": "我们研究铁电材料与机器学习势函数。",
        "keywords": ["ferroelectric", "machine learning potential"],
    }


def _write_profile(d, profile):
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    with open(os.path.join(d, "data", "focus_interests.json"), "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False)


class _FakeProv:
    """按批内序号回 JSON 数组;可通过 responses 定制或设 fail=True 模拟批次失败。"""

    def __init__(self, score=8, fail=False):
        self.score = score
        self.fail = fail
        self.prompts = []

    def call_api(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("boom")
        n = prompt.count("Title:")
        items = [{"index": i, "focus_score": self.score,
                  "focus_summary": f"总结{i}", "focus_relation": f"关系{i}",
                  "focus_suggestion": f"建议{i}"} for i in range(1, n + 1)]
        return json.dumps({"items": items}, ensure_ascii=False)


# ---------- prefilter ----------

def test_prefilter_scores_and_orders():
    articles = [
        {"title": "Unrelated poetry study", "abstract": "nothing"},
        {"title": "Ferroelectric domain dynamics",
         "abstract": "walls and a machine learning potential"},  # 标题×2 + 正文×1 = 3
        {"title": "Plain title", "abstract": "a ferroelectric machine learning potential study"},  # 正文 2 命中
        {"title": "Machine learning potential for ferroelectric perovskites",
         "journal": "ferroelectric journal"},  # 标题 2 命中 ×2 + 期刊 1 = 5
    ]
    out = focus_interest.prefilter_candidates(articles, _profile())
    titles = [a["title"] for a in out]
    assert "Unrelated poetry study" not in titles
    # 标题双命中(+期刊) > 标题单命中+正文 > 仅正文双命中
    assert titles[0] == "Machine learning potential for ferroelectric perovskites"
    assert titles[1] == "Ferroelectric domain dynamics"
    assert titles[2] == "Plain title"
    # 纯函数:不修改输入
    assert all("focus_score" not in a for a in articles)


def test_prefilter_empty_keywords_returns_empty():
    articles = [{"title": "ferroelectric", "abstract": "ferroelectric"}]
    assert focus_interest.prefilter_candidates(articles, {}) == []
    assert focus_interest.prefilter_candidates(articles, {"keywords": []}) == []
    assert focus_interest.prefilter_candidates(articles, None) == []


# ---------- analyze_focus_batch ----------

def test_analyze_focus_batch_maps_indices_to_fields():
    articles = [{"title": f"t{i}", "abstract": "a", "journal": "j"} for i in range(3)]
    prov = _FakeProv(score=7)
    results = focus_interest.analyze_focus_batch(articles, _profile(), prov)
    assert set(results.keys()) == {0, 1, 2}  # 返回 0 基下标
    assert results[0]["focus_score"] == 7
    assert results[1]["focus_summary"] == "总结2"
    assert results[2]["focus_relation"] == "关系3"
    assert results[0]["focus_suggestion"] == "建议1"


def test_analyze_focus_batch_clamps_score():
    articles = [{"title": "t", "abstract": "a"} for _ in range(2)]

    class ClampProv:
        def call_api(self, prompt):
            return json.dumps({"items": [
                {"index": 1, "focus_score": 99, "focus_summary": "s"},
                {"index": 2, "focus_score": -3, "focus_summary": "s"},
            ]})

    results = focus_interest.analyze_focus_batch(articles, _profile(), ClampProv())
    assert results[0]["focus_score"] == 10
    assert results[1]["focus_score"] == 0


def test_analyze_focus_batch_failure_leaves_articles_unenriched():
    articles = [{"title": "t1"}, {"title": "t2"}]
    prov = _FakeProv(fail=True)
    results = focus_interest.analyze_focus_batch(articles, _profile(), prov)
    assert results == {}  # 批次失败 → 空映射,交由调用方下次重试
    assert all("focus_score" not in a for a in articles)


def test_analyze_focus_batch_splits_batches_of_eight():
    articles = [{"title": f"t{i}"} for i in range(9)]
    prov = _FakeProv()
    results = focus_interest.analyze_focus_batch(articles, _profile(), prov)
    assert len(prov.prompts) == 2  # 8 + 1 两批
    assert len(results) == 9
    assert results[8]["focus_summary"] == "总结1"  # 第二批内序号从 1 重新计


# ---------- enrich_focus_interest(编排,幂等/fail-soft) ----------

def test_enrich_focus_interest_idempotent_second_run_zero():
    d = tempfile.mkdtemp()
    _write_profile(d, _profile())
    articles = [{"title": "Ferroelectric switching", "abstract": "domain walls",
                 "link": "http://x"}]
    cwd = os.getcwd()
    try:
        os.chdir(d)
        n1 = focus_interest.enrich_focus_interest(articles, provider=_FakeProv())
        n2 = focus_interest.enrich_focus_interest(articles, provider=_FakeProv())
    finally:
        os.chdir(cwd)
    assert n1 == 1
    assert n2 == 0  # 已有 focus_score 的文章跳过
    assert articles[0]["focus_score"] == 8
    assert articles[0]["focus_summary"] and articles[0]["focus_relation"]
    assert articles[0]["focus_suggestion"]


def test_enrich_focus_interest_none_provider_returns_zero():
    d = tempfile.mkdtemp()
    _write_profile(d, _profile())
    articles = [{"title": "Ferroelectric switching", "abstract": "x"}]
    cwd = os.getcwd()
    try:
        os.chdir(d)
        n = focus_interest.enrich_focus_interest(articles, provider=None)
    finally:
        os.chdir(cwd)
    assert n == 0
    assert "focus_score" not in articles[0]


def test_enrich_focus_interest_missing_profile_returns_zero():
    d = tempfile.mkdtemp()  # 无 data/focus_interests.json

    class Explode:
        def call_api(self, p):
            raise AssertionError("provider must not be called without profile")

    articles = [{"title": "ferroelectric", "abstract": "x"}]
    cwd = os.getcwd()
    try:
        os.chdir(d)
        n = focus_interest.enrich_focus_interest(articles, provider=Explode())
    finally:
        os.chdir(cwd)
    assert n == 0


def test_enrich_focus_interest_empty_keywords_profile_returns_zero():
    d = tempfile.mkdtemp()
    _write_profile(d, {"generated_at": "2026-07-29", "scholars": [],
                       "our_work_zh": "", "keywords": []})

    class Explode:
        def call_api(self, p):
            raise AssertionError("provider must not be called with empty keywords")

    cwd = os.getcwd()
    try:
        os.chdir(d)
        n = focus_interest.enrich_focus_interest([{"title": "ferroelectric"}],
                                                 provider=Explode())
    finally:
        os.chdir(cwd)
    assert n == 0


def test_enrich_focus_interest_max_items_cap():
    d = tempfile.mkdtemp()
    _write_profile(d, _profile())
    articles = [
        {"title": "Machine learning potential for ferroelectric perovskites", "abstract": "x"},
        {"title": "Ferroelectric domains", "abstract": "x"},
        {"title": "Ferroelectric switching", "abstract": "x"},
    ]
    cwd = os.getcwd()
    try:
        os.chdir(d)
        n = focus_interest.enrich_focus_interest(articles, provider=_FakeProv(),
                                                 max_items=2)
    finally:
        os.chdir(cwd)
    assert n == 2
    enriched = [a for a in articles if "focus_score" in a]
    assert len(enriched) == 2
    # 预筛分数最高者优先进 LLM(标题双命中排最前)
    assert articles[0]["focus_score"] == 8
    assert "focus_score" not in articles[2]


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] focus_interest")
