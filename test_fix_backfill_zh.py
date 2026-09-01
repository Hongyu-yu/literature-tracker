#!/usr/bin/env python3
"""backfill_zh 回归测试：翻译失败绝不能销毁已有好数据,落盘必须原子。

覆盖两类缺陷:
1. `_prepare_targets` 在调用 LLM *之前* 就把 title_zh/abstract_zh 清空,
   随后 `save_index` 无条件落盘 —— 网关 502 时一整批好中文被写成 ""。
2. `save_index` 直接 open(path,"w") 截断重写十几 MB 的 index.json,
   job 超时被 kill 会留下半截非法 JSON, 而 `if: always()` 的提交步骤照推不误。

按 run_tests.py 的约定: 模块级 `def test_xxx():`, 无参数, 只用 unittest.mock + tempfile。
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest import mock

import backfill_zh

# 一段完整可读的中文摘要,只夹了一个 U+FFFD —— is_suspicious_text 判 True,
# 但它显然远好于空字符串。线上索引里真实存在这样的条目。
SUSPICIOUS_BUT_GOOD_ZH = (
    "�文提出开源高性能框架 TDSE-Z,用于模拟原子、分子和半导体有效质量体系中的强场量子动力学,"
    "并给出与实验一致的高次谐波谱。"
)


def _article(**kw):
    a = {
        "link": "https://example.org/a",
        "title": "Ferroelectric switching in HfO2 thin films",
        "abstract": "We report ferroelectric switching in hafnia thin films using machine learning potentials.",
        "pub_date": "2026-08-30",
        "title_zh": "",
        "abstract_zh": "",
        "abstract_zh_full": "",
    }
    a.update(kw)
    return a


# --------------------------------------------------------------------------
# 缺陷 1: 翻译前清空 + 失败后照样落盘
# --------------------------------------------------------------------------


def test_prepare_targets_keeps_suspicious_but_readable_chinese():
    """夹了一个 U+FFFD 的完整中文摘要不该被清空。

    zh_enricher 的写入守卫本来就会就地覆盖 is_suspicious_text 的字段,
    清空毫无收益, 却让"翻译失败"退化成"数据被删"。
    修复前 `_target_needs_abstract` 为真 → abstract_zh 被置空, 本用例失败。
    """
    art = _article(title_zh="铪基薄膜中的铁电翻转", abstract_zh=SUSPICIOUS_BUT_GOOD_ZH)
    originals = backfill_zh._prepare_targets([art])

    assert art["abstract_zh"] == SUSPICIOUS_BUT_GOOD_ZH, f"可疑但可读的中文摘要被清空了: {art['abstract_zh']!r}"
    assert art["title_zh"] == "铪基薄膜中的铁电翻转"
    assert originals == {}, f"没有清空任何字段就不该产生快照: {originals!r}"


def test_prepare_targets_still_clears_english_fallback_and_snapshots_it():
    """英文回退值仍必须清空(否则 zh_enricher 覆盖不了),但要留下快照。"""
    art = _article(title_zh="Ferroelectric switching in HfO2 thin films")
    art["abstract_zh"] = art["abstract"]

    originals = backfill_zh._prepare_targets([art])

    assert art["title_zh"] == "", "英文回退标题仍应被清空,否则 zh_enricher 的守卫会挡住覆盖"
    assert art["abstract_zh"] == "", "与英文原文完全相同的 abstract_zh 仍应被清空"
    # 修复前 `_prepare_targets` 返回 None,下面这一行会抛 TypeError。
    snapshot = originals[id(art)]
    assert snapshot["title_zh"] == "Ferroelectric switching in HfO2 thin films"
    assert snapshot["abstract_zh"] == art["abstract"]


def test_restore_unfilled_puts_back_values_that_were_never_translated():
    """轮次跑完仍为空 → 就地恢复原值;已译出的字段不许被覆盖回去。"""
    failed = _article(link="https://example.org/failed", title_zh="Failed English Title")
    ok = _article(link="https://example.org/ok", title_zh="Translated English Title")
    articles = [failed, ok]

    originals = backfill_zh._prepare_targets(articles)
    ok["title_zh"] = "已成功翻译的中文标题"  # 模拟这一篇翻译成功

    restored = backfill_zh._restore_unfilled(articles, originals)

    assert restored == 1, f"应只恢复未译出的那一篇,实际 {restored}"
    assert failed["title_zh"] == "Failed English Title", "未译出的字段必须恢复原值,不能留空"
    assert ok["title_zh"] == "已成功翻译的中文标题", "已译出的字段不得被快照覆盖"


def test_checkpoint_save_never_writes_blank_fields_to_disk():
    """每批的 checkpoint 落盘同样不能把清空后的空值写进磁盘。

    工作流 timeout-minutes: 360 + cancel-in-progress + `if: always()` 提交,
    意味着中途被取消时磁盘上的那一版就是被推上 main 的那一版。
    """
    art = _article(title_zh="Ferroelectric switching in HfO2 thin films")
    articles = [art]
    originals = backfill_zh._prepare_targets(articles)

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "data", "index.json")
        backfill_zh._save_index_restoring(path, articles, originals)

        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)["articles"][0]

    assert saved["title_zh"] == "Ferroelectric switching in HfO2 thin films", (
        f"checkpoint 把未译出的字段写成了空值: {saved['title_zh']!r}"
    )
    # 内存里必须保持为空,否则后续批次会被 zh_enricher 的守卫挡住,永远译不成
    assert art["title_zh"] == "", "checkpoint 不得就地恢复内存中的字段"


def test_main_with_totally_failing_provider_preserves_existing_zh():
    """端到端: 每一批都失败(updated=0)时,磁盘上的中文字段一个都不能丢。"""
    good_zh = _article(
        link="https://example.org/good",
        title_zh="铪基薄膜中的铁电翻转",
        abstract_zh=SUSPICIOUS_BUT_GOOD_ZH,
    )
    fallback = _article(
        link="https://example.org/fallback",
        title="Machine learning interatomic potentials for perovskites",
        abstract="A benchmark of machine learning interatomic potentials for perovskite oxides.",
    )
    fallback["title_zh"] = fallback["title"]
    fallback["abstract_zh"] = fallback["abstract"]

    with tempfile.TemporaryDirectory() as tmpdir:
        index_path = os.path.join(tmpdir, "data", "index.json")
        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump({"articles": [good_zh, fallback]}, f, ensure_ascii=False)

        env = {
            "BACKFILL_INDEX_PATH": index_path,
            "BACKFILL_DOCS_PATH": "",
            "BACKFILL_SCOPE": "all_missing",
            "AI_ZH_MAX_PASSES": "1",
            "AI_ZH_PASS_SLEEP_SECONDS": "0",
            "AI_API_KEY": "",
        }

        def _all_batches_fail(targets, **kwargs):
            # zh_enricher 在每批 call_api 抛异常时就是这个表现:打日志、跳过、返回 0
            on_progress = kwargs.get("on_progress")
            if on_progress:
                on_progress()
            return 0

        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(backfill_zh, "enrich_articles_zh", _all_batches_fail), \
                mock.patch.object(backfill_zh, "_sync_site_outputs", lambda articles: None):
            rc = backfill_zh.main()

        with open(index_path, "r", encoding="utf-8") as f:
            saved = {a["link"]: a for a in json.load(f)["articles"]}

    assert rc == 2, f"一条都没译出来必须以非零退出码报警,实际 {rc}"
    assert saved["https://example.org/good"]["abstract_zh"] == SUSPICIOUS_BUT_GOOD_ZH, (
        "可疑但可读的中文摘要被回填失败的运行删掉了"
    )
    assert saved["https://example.org/good"]["title_zh"] == "铪基薄膜中的铁电翻转"
    assert saved["https://example.org/fallback"]["title_zh"] == fallback["title"], (
        "英文回退标题被写成了空值 —— 空标题比英文标题更糟"
    )
    assert saved["https://example.org/fallback"]["abstract_zh"] == fallback["abstract"], (
        "英文回退摘要被写成了空值,daily 质量门槛会因此掐掉当天的邮件"
    )


# --------------------------------------------------------------------------
# 缺陷 2: 非原子落盘
# --------------------------------------------------------------------------


def test_save_index_leaves_old_file_intact_when_write_dies_midway():
    """写到一半被 kill(job 超时/取消)时,正式文件必须还是上一版完整 JSON。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "data", "index.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        original = {"articles": [{"link": "https://example.org/keep", "title_zh": "保留我"}]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(original, f, ensure_ascii=False)

        def _die_midway(obj, fp, **kwargs):
            fp.write('{\n  "articles": [\n    {\n      "link": "https://example.org/tr')
            raise KeyboardInterrupt("模拟 job 超时被 SIGKILL")

        with mock.patch.object(backfill_zh.json, "dump", _die_midway):
            try:
                backfill_zh.save_index(path, [{"link": "https://example.org/new"}])
            except KeyboardInterrupt:
                pass

        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()

    # 修复前:open(path,"w") 立刻截断 → 这里是半截 JSON,json.loads 抛异常
    assert json.loads(raw) == original, "半途失败的写入损坏了正式索引文件"


def test_save_index_replaces_atomically_and_cleans_up_tmp():
    """成功路径:内容正确写入,且不留下 .tmp 残留。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "data", "index.json")
        backfill_zh.save_index(path, [{"link": "https://example.org/a", "title_zh": "中文"}])

        with open(path, "r", encoding="utf-8") as f:
            assert json.load(f)["articles"][0]["title_zh"] == "中文"
        assert not os.path.exists(path + ".tmp"), "成功落盘后不该留下临时文件"


def test_save_index_accepts_bare_filename():
    """BACKFILL_INDEX_PATH 允许是裸文件名 —— 旧代码在 os.makedirs("") 上直接崩。"""
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            os.chdir(tmpdir)
            backfill_zh.save_index("index.json", [{"link": "https://example.org/a"}])
            with open("index.json", "r", encoding="utf-8") as f:
                assert json.load(f)["articles"][0]["link"] == "https://example.org/a"
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✅ {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"❌ {name}: {type(exc).__name__}: {exc}")
    raise SystemExit(1 if failures else 0)
