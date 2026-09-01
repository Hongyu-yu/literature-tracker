"""zh_enricher 中文富化测试(无网络:build_provider 打桩返回假 provider)。"""

import contextlib
import io
import json
import sys
import types
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


# --- JSON 噪声/截断导致整批译文被静默丢弃的回归测试 ---


class _RawProv:
    """按固定原始字符串作答的 provider(用于构造 JSON 噪声/截断响应)。"""

    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def call_api(self, prompt):
        self.calls += 1
        return self.raw


class _BoomProv:
    def __init__(self, exc):
        self.exc = exc

    def call_api(self, prompt):
        raise self.exc


def _run_enrich(articles, prov):
    """跑一次 LLM 富化,返回 (updated, 打印出来的日志)。"""
    buf = io.StringIO()
    with mock.patch.object(zh_enricher, "build_provider", return_value=prov), \
            mock.patch("time.sleep"), contextlib.redirect_stdout(buf):
        updated = zh_enricher.enrich_articles_zh(
            articles, provider_name="openrouter", api_key="k")
    return updated, buf.getvalue()


def test_batch_survives_trailing_prose_after_json():
    # 模型在 JSON 后补一句含花括号的客套话:旧的贪婪正则会一路吃到最后一个 `}`,
    # 解析失败 → 整批(最多 12 篇)译文被丢弃且不打印任何东西。
    payload = json.dumps({"items": [{
        "index": 1, "title_zh": "中文标题", "abstract_zh": "浓缩摘要",
        "abstract_zh_full": "完整忠实中文翻译",
    }]}, ensure_ascii=False)
    articles = [{"title": "T", "link": "http://x", "abstract": "English abstract."}]
    updated, _log = _run_enrich(articles, _RawProv(payload + "\n\n说明：{以上为翻译}"))
    assert updated == 1
    assert articles[0]["title_zh"] == "中文标题"
    assert articles[0]["abstract_zh_full"] == "完整忠实中文翻译"


def test_batch_survives_trailing_comma_and_smart_quotes():
    raw = '{“items”: [{“index”: 1, “title_zh”: “中文标题”, “abstract_zh”: “浓缩摘要”, ' \
          '“abstract_zh_full”: “完整忠实中文翻译”,}]}'
    articles = [{"title": "T", "link": "http://x", "abstract": "English abstract."}]
    updated, _log = _run_enrich(articles, _RawProv(raw))
    assert updated == 1
    assert articles[0]["abstract_zh_full"] == "完整忠实中文翻译"


def test_truncated_response_keeps_complete_items_and_drops_partial_tail():
    # max_tokens 截断:完整的前几条要留下,半截的最后一条必须丢弃 ——
    # 半截译文写进 abstract_zh_full 后会被认作"已翻译",永远不再重试。
    items = [{
        "index": i, "title_zh": f"中文标题{i}", "abstract_zh": f"浓缩摘要{i}",
        "abstract_zh_full": f"第{i}篇的完整忠实中文翻译，逐句对应原文，不删减不浓缩。",
    } for i in (1, 2, 3)]
    full = json.dumps({"items": items}, ensure_ascii=False)
    truncated = full[:full.index("第3篇的完整忠实中文翻译") + 8]  # 在第 3 条译文中间切断

    articles = [{"title": f"T{i}", "link": f"http://x/{i}", "abstract": "English abstract."}
                for i in (1, 2, 3)]
    updated, log = _run_enrich(articles, _RawProv(truncated))

    assert updated == 2
    assert articles[0]["abstract_zh_full"] == "第1篇的完整忠实中文翻译，逐句对应原文，不删减不浓缩。"
    assert articles[1]["abstract_zh_full"] == "第2篇的完整忠实中文翻译，逐句对应原文，不删减不浓缩。"
    # 第 3 篇宁可留空等下次重试,也不能写入半截译文
    assert not (articles[2].get("abstract_zh_full") or "").strip()
    assert not (articles[2].get("title_zh") or "").strip()
    assert "截断" in log


def test_batch_failure_is_logged_and_leaves_fields_intact():
    # 整批被丢弃时必须留痕,否则只表现为 updated 偏小,与"没有待翻译条目"无法区分。
    articles = [{"title": "T", "link": "http://x", "abstract": "English abstract.",
                 "title_zh": "已有中文标题"}]
    updated, log = _run_enrich(articles, _BoomProv(RuntimeError("gateway 502")))
    assert updated == 0
    assert "⚠️" in log and "跳过" in log
    assert "gateway 502" in log
    assert articles[0]["title_zh"] == "已有中文标题"   # 失败不得破坏已有数据


def test_unparsable_response_is_logged():
    articles = [{"title": "T", "link": "http://x", "abstract": "English abstract."}]
    updated, log = _run_enrich(articles, _RawProv("抱歉，我无法完成该请求。"))
    assert updated == 0
    assert "⚠️" in log
    assert not (articles[0].get("title_zh") or "").strip()


def test_partially_returned_batch_is_logged():
    # 模型只回了 2 篇里的 1 篇:剩下那篇的字段保持原样,并且要打印出来。
    payload = json.dumps({"items": [{
        "index": 1, "title_zh": "中文标题", "abstract_zh": "浓缩摘要",
        "abstract_zh_full": "完整忠实中文翻译",
    }]}, ensure_ascii=False)
    articles = [{"title": f"T{i}", "link": f"http://x/{i}", "abstract": "English abstract."}
                for i in (1, 2)]
    updated, log = _run_enrich(articles, _RawProv(payload))
    assert updated == 1
    assert "未拿到译文" in log
    assert not (articles[1].get("title_zh") or "").strip()


# --- updated 计数必须反映"真正写入",否则回填看似推进实则原地打转 ---


def test_updated_counts_only_real_writes():
    """模型漏掉 abstract_zh_full 时不得计入 updated。

    文章已有 title_zh/abstract_zh、只缺 abstract_zh_full(最长、最容易被 max_tokens 截掉),
    模型把前两个字段照抄回来、abstract_zh_full 留空:三处写入的守卫全部不成立,
    一个字段都没改。旧代码按"模型回了非空字符串"计数(`if title_zh or abstract_zh or
    abstract_zh_full`),于是每轮都报 updated=N 而 missing_after 纹丝不动 ——
    backfill_zh 只在 missing==0 时才 break,这种不可能收敛的状态会烧光全部 pass,
    日志却像在推进。
    """
    articles = [{
        "title": "Ferroelectric domain walls", "link": "http://x",
        "abstract": "English abstract.",
        "title_zh": "已有中文标题", "abstract_zh": "已有浓缩摘要",
    }]
    prov = _RawProv(json.dumps({"items": [{
        "index": 1, "title_zh": "已有中文标题", "abstract_zh": "已有浓缩摘要",
        "abstract_zh_full": "",
    }]}, ensure_ascii=False))
    updated, log = _run_enrich(articles, prov)

    assert updated == 0, f"一个字段都没写入却计了 {updated} 篇"
    a = articles[0]
    assert a["title_zh"] == "已有中文标题"          # 已有内容不得被覆盖
    assert a["abstract_zh"] == "已有浓缩摘要"
    assert not (a.get("abstract_zh_full") or "").strip()
    assert zh_enricher._full_needs_translation(a)   # 仍是候选,下次运行继续重试
    assert "⚠️" in log and "未落盘" in log           # 无进展必须留痕


def test_partial_write_still_counts_as_updated():
    """只写进一个字段也算更新 —— 修计数不能矫枉过正成"必须三个字段齐全"。"""
    articles = [{"title": "T", "link": "http://x", "abstract": "English abstract."}]
    prov = _RawProv(json.dumps({"items": [{
        "index": 1, "title_zh": "中文标题", "abstract_zh": "", "abstract_zh_full": "",
    }]}, ensure_ascii=False))
    updated, _log = _run_enrich(articles, prov)

    assert updated == 1
    assert articles[0]["title_zh"] == "中文标题"


# --- 兜底(GoogleTranslator)路径:同样只统计真正写入 ---


def _run_fallback(articles, translate_fn):
    """不配 AI key 时走 translator 兜底;translator 未安装依赖,这里注入假模块。"""
    fake = types.ModuleType("translator")
    fake.translate_text = translate_fn
    buf = io.StringIO()
    with mock.patch.dict(sys.modules, {"translator": fake}), \
            mock.patch.object(zh_enricher, "build_provider",
                              side_effect=AssertionError("no api_key: provider must not be built")), \
            contextlib.redirect_stdout(buf):
        updated = zh_enricher.enrich_articles_zh(articles, provider_name="openrouter", api_key="")
    return updated, buf.getvalue()


def test_fallback_keeps_and_counts_partial_write_before_failure():
    """兜底路径中途抛错:已写入的字段要保留,并且要计入 updated。

    旧代码把 `updated += 1` 放在 try 末尾、异常时 continue,于是"标题译好了、摘要翻译挂了"
    这类真实进展被报成 0 篇 —— backfill_zh 据此认定本轮毫无进展。
    """
    def _translate(text):
        if text.startswith("Ferroelectric"):
            return "铁电畴壁"
        raise RuntimeError("google translate 502")

    articles = [{"title": "Ferroelectric domain walls", "link": "http://x",
                 "abstract": "English abstract."}]
    updated, _log = _run_fallback(articles, _translate)

    assert updated == 1
    assert articles[0]["title_zh"] == "铁电畴壁"      # 已完成的翻译不能丢
    assert not (articles[0].get("abstract_zh") or "").strip()   # 失败的字段留空待重试


def test_fallback_never_writes_empty_translation():
    """空摘要 → translate_text 返回 ""(契约如此),不得把空串盖进 *_zh 字段。"""
    def _translate(text):
        return "铁电畴壁" if (text or "").strip() else ""

    articles = [{"title": "Ferroelectric domain walls", "link": "http://x", "abstract": ""}]
    updated, _log = _run_fallback(articles, _translate)

    assert updated == 1
    a = articles[0]
    assert a["title_zh"] == "铁电畴壁"
    assert "abstract_zh" not in a          # 旧代码会写成 ""
    assert "abstract_zh_full" not in a


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] zh_enricher")


def test_truncated_top_level_array_is_detected_as_truncated():
    """顶层是数组、在最后一条中途被 max_tokens 截断时必须判为截断。

    旧实现从第一个 `{` 开始扫、且一见 depth 归零就返回 True：数组的首个元素正常闭合，
    于是整段被误判为完整，半截译文被写进 abstract_zh_full，而 _full_needs_translation
    认它「已翻译」从此永不重试 —— 比整批丢弃更糟。
    """
    from zh_enricher import _json_object_is_balanced, _parse_llm_batch
    text = ('[{"index":1,"abstract_zh_full":"甲"},{"index":2,"abstract_zh_full":"乙"},'
            '{"index":3,"abstract_zh_full":"第3篇只写到一半')
    assert _json_object_is_balanced(text) is False
    _items, truncated = _parse_llm_batch(text)
    assert truncated is True


def test_brace_containing_preamble_before_truncated_json_is_detected():
    """响应前面有含花括号的客套话时，不能因为那对花括号先闭合就误判为完整。"""
    from zh_enricher import _json_object_is_balanced
    text = '好的，我将按 {index} 格式输出：\n{"items":[{"index":1,"abstract_zh_full":"甲"'
    assert _json_object_is_balanced(text) is False


def test_complete_payloads_are_still_accepted():
    """修复不能把正常响应误判成截断。"""
    from zh_enricher import _json_object_is_balanced
    assert _json_object_is_balanced('{"items":[{"index":1,"abstract_zh_full":"甲"}]}') is True
    assert _json_object_is_balanced('[{"index":1,"abstract_zh_full":"甲"}]') is True
    # 字符串里的花括号不参与配平
    assert _json_object_is_balanced('{"a":"含 } 的字符串"}') is True
    # 客套话在前、JSON 完整
    assert _json_object_is_balanced('说明：{以上为翻译}\n{"items":[{"index":1,"abstract_zh_full":"甲"}]}') is True
