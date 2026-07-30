"""zh_enricher 中文富化测试(无网络:build_provider 打桩返回假 provider)。"""

import json
from unittest import mock

import zh_enricher


class _FakeProv:
    def __init__(self, items):
        self.items = items
        self.prompts = []

    def call_api(self, prompt):
        self.prompts.append(prompt)
        return json.dumps({"items": self.items}, ensure_ascii=False)


def test_prompt_requests_abstract_zh_full():
    prompt = zh_enricher._build_batch_prompt(
        [{"index": 1, "title": "T", "journal": "J", "authors": "A", "abstract": "abs"}]
    )
    assert "abstract_zh_full" in prompt
    assert "完整忠实中文翻译" in prompt


def test_llm_batch_fills_full_without_overwriting_existing():
    articles = [{
        "title": "Ferroelectric paper", "link": "http://x", "abstract": "English abstract.",
        "title_zh": "已有中文标题", "abstract_zh": "已有浓缩摘要",
    }]
    prov = _FakeProv([{"index": 1, "title_zh": "新标题不应写入",
                       "abstract_zh": "新摘要不应写入",
                       "abstract_zh_full": "完整忠实中文翻译内容"}])
    with mock.patch.object(zh_enricher, "build_provider", return_value=prov):
        updated = zh_enricher.enrich_articles_zh(
            articles, provider_name="openrouter", api_key="k")
    assert updated == 1
    a = articles[0]
    assert a["abstract_zh_full"] == "完整忠实中文翻译内容"
    assert a["title_zh"] == "已有中文标题"      # 不覆盖已有字段
    assert a["abstract_zh"] == "已有浓缩摘要"
    # prompt 里要求输出 abstract_zh_full
    assert "abstract_zh_full" in prov.prompts[0]


def test_fully_enriched_article_not_a_candidate():
    articles = [{
        "title": "T", "link": "http://x", "abstract": "abs",
        "title_zh": "中文标题", "abstract_zh": "浓缩摘要", "abstract_zh_full": "完整翻译",
    }]
    with mock.patch.object(zh_enricher, "build_provider",
                           side_effect=AssertionError("provider must not be built")):
        updated = zh_enricher.enrich_articles_zh(
            articles, provider_name="openrouter", api_key="k")
    assert updated == 0


def test_enrich_rerun_is_idempotent():
    articles = [{"title": "T", "link": "http://x", "abstract": "abs"}]
    prov = _FakeProv([{"index": 1, "title_zh": "中文标题",
                       "abstract_zh": "浓缩摘要", "abstract_zh_full": "完整翻译"}])
    with mock.patch.object(zh_enricher, "build_provider", return_value=prov):
        n1 = zh_enricher.enrich_articles_zh(articles, provider_name="openrouter", api_key="k")
        n2 = zh_enricher.enrich_articles_zh(articles, provider_name="openrouter", api_key="k")
    assert n1 == 1
    assert n2 == 0  # 三字段齐备后不再是候选,第二次更新 0
    assert len(prov.prompts) == 1


def test_untranslated_english_full_is_reenriched():
    # LLM 上次把英文原文写进了 abstract_zh_full(未翻译) → 仍是候选,可重填
    articles = [{
        "title": "T", "link": "http://x", "abstract": "English abstract.",
        "title_zh": "中文标题", "abstract_zh": "浓缩摘要",
        "abstract_zh_full": "English abstract.",
    }]
    prov = _FakeProv([{"index": 1, "title_zh": "中文标题",
                       "abstract_zh": "浓缩摘要", "abstract_zh_full": "完整忠实翻译"}])
    with mock.patch.object(zh_enricher, "build_provider", return_value=prov):
        n1 = zh_enricher.enrich_articles_zh(articles, provider_name="openrouter", api_key="k")
        n2 = zh_enricher.enrich_articles_zh(articles, provider_name="openrouter", api_key="k")
    assert n1 == 1
    assert articles[0]["abstract_zh_full"] == "完整忠实翻译"
    assert n2 == 0  # 修复后恢复幂等


def test_chinese_source_full_equal_abstract_is_terminal():
    # 源摘要本身是中文,完整翻译与原文一致 → 视为正常,不重复消耗 API(幂等)
    articles = [{
        "title": "T", "link": "http://x", "abstract": "这是一段中文摘要。",
        "title_zh": "中文标题", "abstract_zh": "浓缩摘要",
        "abstract_zh_full": "这是一段中文摘要。",
    }]
    with mock.patch.object(zh_enricher, "build_provider",
                           side_effect=AssertionError("provider must not be built")):
        updated = zh_enricher.enrich_articles_zh(
            articles, provider_name="openrouter", api_key="k")
    assert updated == 0


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] zh_enricher")
