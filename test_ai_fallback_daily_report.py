#!/usr/bin/env python3
"""AI 兜底那天也必须产出可发信的日报（生成侧）。

此前 generate_daily_pages 在 AI 兜底且已有页面时直接 continue，把整份 summary
连同 sidecar 一起丢掉 → 那天 --rerender-only 找不到 summary → 收不到日报邮件。

fallback_summary 其实已经带着：翻译(title_zh/abstract_zh)、关键词与画像筛选
(focus_score/focus_relation)、核心判定(is_core_focus/core_score)，以及
ensure_relation_fields 补的规则版三段文本 —— 足以撑起一份当天的日报。
现在的约定：页面保留不覆盖，但 summary 照常落盘、邮件照发。
"""

import json
import os
import shutil
import sys
import tempfile
from unittest import mock

import generate_daily_pages


DAY = "2026-08-21"


def _article():
    return {
        "title": "Neural network potential for ferroelectric perovskites",
        "title_zh": "铁电钙钛矿的神经网络势",           # 翻译已在上游完成
        "abstract": "We train a machine learning potential on BaTiO3.",
        "abstract_zh": "我们在 BaTiO3 上训练机器学习势。",
        "link": "https://arxiv.org/abs/2608.00001",
        "journal": "arXiv",
        "pub_date": f"{DAY}T08:00:00",
        "ai_score": 8,
        "focus_score": 7,
    }


class _FallbackSummarizer:
    """模拟 AI 全部重试失败后回落到 fallback_summary 的情形。"""

    def __init__(self, *a, **kw):
        self.provider = None

    def generate_daily_summary(self, articles, date):
        from ai_summarizer import AISummarizer
        return AISummarizer.fallback_summary(self, articles, date)


def _run_generation(tmp, existing_page="旧的好页面"):
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "docs/daily"), exist_ok=True)
    with open(os.path.join(tmp, "data", "index.json"), "w", encoding="utf-8") as f:
        json.dump({"articles": [_article()]}, f, ensure_ascii=False)
    page = os.path.join(tmp, "docs/daily", f"{DAY}.html")
    if existing_page is not None:
        with open(page, "w", encoding="utf-8") as f:
            f.write(existing_page)

    argv = ["generate_daily_pages.py", "--date", DAY, "--days", "1", "--force"]
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(generate_daily_pages, "AISummarizer", _FallbackSummarizer), \
             mock.patch.dict(os.environ, {"AI_API_KEY": "sk-test", "AI_PROVIDER": "aigw",
                                          "CORE_FOCUS_ENABLED": "0", "FOCUS_ENABLED": "0"}):
            generate_daily_pages.main()
    finally:
        os.chdir(cwd)

    sidecar_path = os.path.join(tmp, "data", f"daily_summary_{DAY}.json")
    sidecar = None
    if os.path.exists(sidecar_path):
        with open(sidecar_path, encoding="utf-8") as f:
            sidecar = json.load(f)
    return sidecar, (open(page, encoding="utf-8").read() if os.path.exists(page) else None)


def test_ai_fallback_writes_sidecar_so_the_email_can_still_go_out():
    tmp = tempfile.mkdtemp()
    try:
        sidecar, page = _run_generation(tmp)
        assert sidecar is not None, "AI 兜底那天也必须落盘 sidecar，否则邮件发不出去"
        assert sidecar.get("generated_by") == "fallback"
        assert sidecar.get("rerender_ok") is False, "降级内容不得用于重画页面"
        assert page == "旧的好页面", "不得用兜底内容覆盖已有的好页面"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fallback_sidecar_carries_translation_and_filtering():
    """用户要的"至少有翻译和关键词筛选的日报"——确认这些字段真的在。"""
    tmp = tempfile.mkdtemp()
    try:
        sidecar, _ = _run_generation(tmp)
        items = sidecar.get("full_list") or []
        assert items, "兜底 summary 不能是空的"
        it = items[0]
        assert it.get("title_zh") == "铁电钙钛矿的神经网络势", "中文标题必须保留"
        assert "机器学习势" in (it.get("abstract_zh") or ""), "中文摘要必须保留"
        assert it.get("focus_score") == 7, "画像相关度必须保留"
        assert it.get("is_core_focus") is not None, "核心判定必须保留"
        assert all(str(it.get(k) or "").strip() for k in ("method_point", "related_work", "implication")), \
            "规则版三段文本必须补齐"
        assert sidecar.get("overview"), "总览不能为空"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_core_items_keep_rule_based_text_when_deep_read_fails():
    """深读失败不得把核心文献的三段文本抹成空串。

    2026-08-30 现场：full_list 5 篇里 3 篇核心的 method_point/related_work/implication
    全是空，2 篇非核心却有 270+ 字 —— 因为深读结果缺失时代码无条件赋 ""，
    把 ensure_relation_fields 填好的规则版文本覆盖掉了（core_items 与 full_list
    共享同一批 dict，full_list 也一起遭殃），直接导致质量门 relation=2/5。
    """
    tmp = tempfile.mkdtemp()
    try:
        # 该测试用的 _FallbackSummarizer 没有 generate_core_deep_fields，
        # 正好复现"深读调用抛异常 → deep_fields 为空"的路径
        sidecar, _ = _run_generation(tmp)
        items = sidecar.get("full_list") or []
        blanked = [i for i, x in enumerate(items, 1)
                   if not all(str(x.get(k) or "").strip()
                              for k in ("method_point", "related_work", "implication"))]
        assert not blanked, f"深读失败时不得抹掉规则版三段文本，被抹的条目: {blanked}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fallback_with_no_existing_page_still_writes_the_page():
    """当天本来就没有页面时，兜底内容应当照常写出来（有总比没有强）。"""
    tmp = tempfile.mkdtemp()
    try:
        sidecar, page = _run_generation(tmp, existing_page=None)
        assert sidecar is not None
        assert sidecar.get("rerender_ok") is True, "没有可保护的旧页面，就不该禁止重画"
        assert page and "铁电钙钛矿的神经网络势" in page, "应当写出兜底页面"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] AI fallback daily report sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
