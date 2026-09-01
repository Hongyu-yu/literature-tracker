#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""generate_daily_pages 回归测试：

1) build_unified_items 的去重键必须能把 APS 侧的裸 DOI 和索引侧的 link.aps.org 链接
   认成同一篇（否则同一篇论文在日报页里出现两次）；去重时不能把重复条目上的中文字段丢掉。
2) sync_daily_rss_feeds 不能用"重算出来的、缺中文的"文章集去覆盖磁盘上更完整的历史 feed。

run_tests.py 只跑模块级、无必填参数的 test_*，故全部写成 def test_xxx()。
"""
import json
import os
import shutil
import tempfile
from unittest import mock

import generate_daily_pages as g


# --------------------------------------------------------------------------
# 1. build_unified_items 去重
# --------------------------------------------------------------------------

def test_dedup_key_folds_doi_url_variants():
    """裸 DOI / doi.org / link.aps.org 三种形态折算成同一个键。"""
    bare = g._dedup_key(g.normalize_link("10.1103/99yr-nqbx"))
    doi_org = g._dedup_key("https://doi.org/10.1103/99yr-nqbx")
    aps = g._dedup_key("http://link.aps.org/doi/10.1103/99yr-nqbx")
    assert bare == doi_org == aps, (bare, doi_org, aps)
    # 无 DOI 的链接原样保留，不同 arXiv 条目不会被误合并
    assert g._dedup_key("http://arxiv.org/abs/2603.24177") == "http://arxiv.org/abs/2603.24177"
    assert g._dedup_key("http://arxiv.org/abs/1") != g._dedup_key("http://arxiv.org/abs/2")
    assert g._dedup_key("") == "" and g._dedup_key(None) == ""


def test_build_unified_items_dedups_aps_doi_against_link_aps_org():
    """data/aps_*.json 只带 doi，索引侧同一篇是 http://link.aps.org/doi/...，
    旧实现按原始串比对 → 同一篇被渲染两次（文献总数虚高、书签重复）。"""
    aps_items = [{
        "title": "Electronic Origin of Delicate Antiferromagnetism",
        "doi": "10.1103/99yr-nqbx",
        "deep_analysis": "## 深析",
        "poster": {"image": "images/posters/aps.webp", "elements": {"关键结果": "r"}},
    }]
    full_list = [{
        "title": "Electronic Origin of Delicate Antiferromagnetism",
        "link": "http://link.aps.org/doi/10.1103/99yr-nqbx",
        "summary": "一句话亮点",
    }]
    out = g.build_unified_items(full_list, {}, aps_items)
    assert len(out) == 1, [it.get("link") for it in out]
    assert out[0]["_tier"] == 0
    assert out[0]["_enrich"]["image"] == "images/posters/aps.webp"


def test_build_unified_items_keeps_zh_fields_of_deduped_row():
    """去重不能变成丢数据：APS 行没有 title_zh/abstract_zh/focus_score，
    重复的 full_list 行有 —— 合并后这些中文字段必须还在。"""
    aps_items = [{
        "title": "Anyon Superfluidity",
        "doi": "10.1103/bvfl-ff5t",
        "deep_analysis": "## 深析",
        "poster": {"image": "images/posters/x.webp"},
        "category": "拓扑·电子结构",
    }]
    full_list = [{
        "title": "Anyon Superfluidity",
        "link": "http://link.aps.org/doi/10.1103/bvfl-ff5t",
        "title_zh": "量子霍尔双层中激子的任意子超流",
        "abstract_zh": "中文摘要",
        "summary": "中文亮点",
        "focus_score": 8.5,
        "category": "不应覆盖已有分类",
    }]
    out = g.build_unified_items(full_list, {}, aps_items)
    assert len(out) == 1
    it = out[0]
    assert it["title_zh"] == "量子霍尔双层中激子的任意子超流"
    assert it["abstract_zh"] == "中文摘要"
    assert it["summary"] == "中文亮点"
    assert it["focus_score"] == 8.5
    # 只补空缺，绝不覆盖 APS 行已有的值
    assert it["category"] == "拓扑·电子结构"
    assert it["_tier"] == 0 and it["_enrich"]["image"] == "images/posters/x.webp"
    assert it["link"] == "https://doi.org/10.1103/bvfl-ff5t"


def test_build_unified_items_keeps_distinct_papers_and_tiers():
    """成功路径不变：不同论文照旧各占一条，tier/富化映射不受影响。"""
    full_list = [
        {"title": "Plain paper", "link": "http://arxiv.org/abs/plain", "summary": "x"},
        {"title": "Cross paper", "link": "http://arxiv.org/abs/cross", "summary": "y"},
    ]
    enrich_map = {"http://arxiv.org/abs/cross": {"deep_analysis": "## d", "image": "c.webp"}}
    aps_items = [{"title": "APS", "doi": "10.1103/abc", "deep_analysis": "## aps",
                  "poster": {"image": "aps.webp"}}]
    out = g.build_unified_items(full_list, enrich_map, aps_items)
    assert len(out) == 3
    by_link = {it["link"]: it for it in out}
    assert by_link["http://arxiv.org/abs/cross"]["_tier"] == 1
    assert by_link["http://arxiv.org/abs/plain"]["_tier"] == 2
    assert by_link["https://doi.org/10.1103/abc"]["_tier"] == 0


# --------------------------------------------------------------------------
# 2. sync_daily_rss_feeds 不覆盖更完整的历史 feed
# --------------------------------------------------------------------------

_RICH_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
    <title>2026-06-15 AI × Science 文献日报 RSS</title>
    <item>
        <title>利用深度强化学习实现稳定斯格明子生成的优化控制协议</title>
        <link>https://arxiv.org/abs/2603.24177</link>
        <description><![CDATA[本文提出一种深度强化学习方法，用于寻找动态磁场-温度路径。]]></description>
    </item>
    <item>
        <title>机器学习势函数用于铁电畴壁动力学</title>
        <link>https://arxiv.org/abs/2603.24178</link>
        <description><![CDATA[中文摘要正文。]]></description>
    </item>
</channel></rss>"""


def _with_tmp_docs(fn):
    """在临时目录里跑（sync_daily_rss_feeds 用相对路径 docs/daily）。"""
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    try:
        os.chdir(tmp)
        os.makedirs("docs/daily", exist_ok=True)
        return fn(tmp)
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def test_sync_daily_rss_feeds_preserves_richer_existing_feed():
    """index.json 窗口滑过老日期后重算只剩英文（ai_relevant.json 是中文富化之前的那份），
    旧实现无条件改写 → 历史 feed 的中文标题/摘要被冲掉，只能从 git 里找回。"""
    def run(tmp):
        path = os.path.join("docs/daily", "2026-06-15.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_RICH_FEED)
        degraded = {"daily_articles": [
            {"title": "Optimized control protocols for skyrmion generation",
             "link": "https://arxiv.org/abs/2603.24177", "abstract": "English abstract",
             "journal": "arXiv", "pub_date": "2026-06-15"},
            {"title": "Machine learning potentials for domain walls",
             "link": "https://arxiv.org/abs/2603.24178", "abstract": "English abstract",
             "journal": "arXiv", "pub_date": "2026-06-15"},
        ]}
        with mock.patch.object(g, "collect_daily_articles", return_value=degraded):
            changed = g.sync_daily_rss_feeds([], [], [{"date": "2026-06-15"}])
        with open(path, encoding="utf-8") as f:
            after = f.read()
        return changed, after

    changed, after = _with_tmp_docs(run)
    assert "利用深度强化学习实现稳定斯格明子生成的优化控制协议" in after, "历史 feed 的中文标题被覆盖了"
    assert "机器学习势函数用于铁电畴壁动力学" in after
    assert changed == 0


def test_sync_daily_rss_feeds_preserves_feed_when_articles_vanish():
    """极端情况：某天在两份数据源里都查不到了 → 绝不能把 feed 清空。"""
    def run(tmp):
        path = os.path.join("docs/daily", "2026-06-15.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_RICH_FEED)
        with mock.patch.object(g, "collect_daily_articles", return_value={"daily_articles": []}):
            changed = g.sync_daily_rss_feeds([], [], [{"date": "2026-06-15"}])
        with open(path, encoding="utf-8") as f:
            return changed, f.read()

    changed, after = _with_tmp_docs(run)
    assert "利用深度强化学习实现稳定斯格明子生成的优化控制协议" in after
    assert changed == 0


def test_sync_daily_rss_feeds_still_writes_new_and_improved_feeds():
    """成功路径不变：没有旧文件、或重算结果不劣于旧文件时照常写入并同步 latest.xml。"""
    def run(tmp):
        fresh = {"daily_articles": [
            {"title": "Optimized control protocols", "title_zh": "优化控制协议",
             "link": "https://arxiv.org/abs/2603.24177", "abstract_zh": "中文摘要",
             "journal": "arXiv", "pub_date": "2026-06-15"},
            {"title": "ML potentials", "title_zh": "机器学习势函数",
             "link": "https://arxiv.org/abs/2603.24178", "abstract_zh": "中文摘要二",
             "journal": "arXiv", "pub_date": "2026-06-15"},
            {"title": "Third paper", "title_zh": "第三篇",
             "link": "https://arxiv.org/abs/2603.24179", "abstract_zh": "中文摘要三",
             "journal": "arXiv", "pub_date": "2026-06-15"},
        ]}
        path = os.path.join("docs/daily", "2026-06-15.xml")
        with mock.patch.object(g, "collect_daily_articles", return_value=fresh):
            first = g.sync_daily_rss_feeds([], [], [{"date": "2026-06-15"}])
            with open(path, encoding="utf-8") as f:
                written = f.read()
            # 同样的一批数据再跑一次（覆盖率相等）→ 仍然允许改写
            second = g.sync_daily_rss_feeds([], [], [{"date": "2026-06-15"}])
        latest = os.path.exists(os.path.join("docs/daily", "latest.xml"))
        return first, second, written, latest

    first, second, written, latest = _with_tmp_docs(run)
    assert first == 1 and second == 1
    assert "优化控制协议" in written and "第三篇" in written
    assert latest


def test_rss_downgrade_reason_no_baseline_writes():
    """没有已落盘文件（或文件坏了）→ 无基线 → 照常写，绝不因为护栏卡死新日期。"""
    def run(tmp):
        missing = os.path.join("docs/daily", "2099-01-01.xml")
        assert g._rss_downgrade_reason(missing, []) == ""
        broken = os.path.join("docs/daily", "broken.xml")
        with open(broken, "w", encoding="utf-8") as f:
            f.write("<rss><channel><item><title>坏文件")
        assert g._rss_downgrade_reason(broken, []) == ""
        return True

    assert _with_tmp_docs(run)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✅ {name}")
            except Exception as e:
                fails += 1
                print(f"❌ {name}: {type(e).__name__}: {e}")
    print("FAILED" if fails else "ALL PASS")
    raise SystemExit(1 if fails else 0)
