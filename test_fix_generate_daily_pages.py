#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""generate_daily_pages 回归测试：

1) build_unified_items 的去重键必须能把 APS 侧的裸 DOI 和索引侧的 link.aps.org 链接
   认成同一篇（否则同一篇论文在日报页里出现两次）；去重时不能把重复条目上的中文字段丢掉。
2) sync_daily_rss_feeds 不能用"重算出来的、缺中文的"文章集去覆盖磁盘上更完整的历史 feed。
3) sync_daily_rss_feeds 只重算 only_dates 指定的日期（其余日期已有 .xml 就原样保留），
   latest.xml 仍必须跟着 summaries[0]。
4) 归档页导航只在 summaries.json 的 (date, file) 序列变化时才需要全量刷新。
5) 富化预算要按【实际发出的 AI 调用】计费：provider 故障时返回 0 也得扣，
   否则 --days N 会把注定失败的调用重复 N 天。
6) _resolve_ai 必须读 config.AI_CONFIG（config.local.py 的配置由它带进来）。
7) 无人调用、会把 summaries 条目的 digest 写丢的 update_index 已删除。

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
            # 同样的一批数据再跑一次：内容没变就不该重写。
            # 这条断言原本写的是 second == 1（沿用旧行为）；后来 generate_rss_feed 加了
            # 「内容未变化则跳过写入」的短路，正是为了消掉每轮 120 个只有时间戳不同的
            # 文件改动。所以现在的正确期望是 0，而且文件内容必须原样还在。
            second = g.sync_daily_rss_feeds([], [], [{"date": "2026-06-15"}])
            with open(path, encoding="utf-8") as f:
                after_second = f.read()
        latest = os.path.exists(os.path.join("docs/daily", "latest.xml"))
        return first, second, written, latest, after_second

    first, second, written, latest, after_second = _with_tmp_docs(run)
    assert first == 1, "首次必须写出 feed"
    assert second == 0, "内容未变化时不应重写（否则每轮产生 120 个时间戳伪改动）"
    assert after_second == written, "跳过写入后文件内容必须保持不变"
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


# --------------------------------------------------------------------------
# 3. sync_daily_rss_feeds 增量：只重算本次真正生成过的日期
# --------------------------------------------------------------------------

def test_sync_daily_rss_feeds_only_recomputes_requested_dates():
    """collect_daily_articles 是 analyze_focus 密集型热函数，对 120 天全量重算 ~7 分钟，
    但每次 generate 只可能改动 --days 指定的那 1-2 天。传入 only_dates 后：
      * only_dates 里的日期照常重算；
      * 已有 .xml 且不在 only_dates 里的历史日期跳过（不再无谓重算+改写）；
      * 缺 .xml 的日期无论如何都要补生成（不能让某天永远没有 feed）。"""
    def run(tmp):
        # 2026-06-14 已经有 feed 了 → 本次不重算它
        with open(os.path.join("docs/daily", "2026-06-14.xml"), "w", encoding="utf-8") as f:
            f.write(_RICH_FEED)
        seen = []

        def fake_collect(idx, rel, day_str):
            seen.append(day_str)
            return {"daily_articles": [{
                "title": "Paper", "title_zh": "论文", "abstract_zh": "中文摘要",
                "link": f"https://arxiv.org/abs/{day_str}", "journal": "arXiv",
                "pub_date": day_str,
            }]}

        summaries = [{"date": "2026-06-16"}, {"date": "2026-06-15"}, {"date": "2026-06-14"}]
        with mock.patch.object(g, "collect_daily_articles", side_effect=fake_collect):
            g.sync_daily_rss_feeds([], [], summaries, only_dates={"2026-06-16"})
        latest = os.path.join("docs/daily", "latest.xml")
        head = os.path.join("docs/daily", "2026-06-16.xml")
        same_as_head = (os.path.exists(latest) and os.path.exists(head)
                        and open(latest, encoding="utf-8").read()
                        == open(head, encoding="utf-8").read())
        return seen, same_as_head

    seen, latest_is_head = _with_tmp_docs(run)
    assert "2026-06-16" in seen, seen                 # 本次重算的日期
    assert "2026-06-15" in seen, seen                 # 缺 .xml → 必须补生成
    assert "2026-06-14" not in seen, seen             # 已有 .xml 且未重算 → 跳过
    assert latest_is_head, "latest.xml 必须仍然是 summaries[0] 的那份"


def test_sync_daily_rss_feeds_without_only_dates_still_full_sync():
    """默认 only_dates=None（backfill_zh 的全量同步走这条）行为不变：逐日全部重算。"""
    def run(tmp):
        for day in ("2026-06-16", "2026-06-15", "2026-06-14"):
            with open(os.path.join("docs/daily", f"{day}.xml"), "w", encoding="utf-8") as f:
                f.write(_RICH_FEED)
        seen = []

        def fake_collect(idx, rel, day_str):
            seen.append(day_str)
            return {"daily_articles": []}

        summaries = [{"date": d} for d in ("2026-06-16", "2026-06-15", "2026-06-14")]
        with mock.patch.object(g, "collect_daily_articles", side_effect=fake_collect):
            g.sync_daily_rss_feeds([], [], summaries)
        return seen

    seen = _with_tmp_docs(run)
    assert sorted(seen) == ["2026-06-14", "2026-06-15", "2026-06-16"], seen


# --------------------------------------------------------------------------
# 4. 导航全量刷新的判定
# --------------------------------------------------------------------------

def test_nav_sequence_changed_gates_full_enhance():
    """归档页导航（前一天/后一天/最新一期）只依赖 summaries.json 的位置序列。
    序列不变 → 可以只 enhance 本次重写的页面；序列一变（新一期上线 / 回填插入
    中间日期 / 窗口挤掉最老一期）→ 必须全量刷新，否则别的页面会指向过时的“最新一期”。"""
    base = [{"date": "2026-06-16", "file": "2026-06-16.html"},
            {"date": "2026-06-14", "file": "2026-06-14.html"}]
    assert g._nav_sequence_changed(base, [dict(e) for e in base]) is False
    rolled = [{"date": "2026-06-17", "file": "2026-06-17.html"}] + base
    assert g._nav_sequence_changed(base, rolled) is True, "新一期上线必须全量刷新"
    inserted = [base[0], {"date": "2026-06-15", "file": "2026-06-15.html"}, base[1]]
    assert g._nav_sequence_changed(base, inserted) is True, "回填中间日期必须全量刷新"
    dropped = base[:1]
    assert g._nav_sequence_changed(base, dropped) is True, "掉出窗口必须全量刷新"
    assert g._nav_sequence_changed([], []) is False


# --------------------------------------------------------------------------
# 5. 富化预算按“实际发出的 AI 调用”计费
# --------------------------------------------------------------------------

def test_enrich_budget_charges_ai_calls_that_yielded_nothing():
    """enrich_focus_interest / ensure_highlights 把批次失败吞掉后返回 0。
    旧实现 `budget -= used` → provider 故障时预算一分不扣，--days 14 会把注定失败的
    调用重复 14 天（正是这个预算本来要防的放大）。现在按实际 call_api 次数计费。"""
    calls = {"n": 0}

    def fake_fs(items, max_items=None):
        calls["n"] += 1
        g._AI_CALL_STATS["calls"] += 3      # 三个批次都真的发出去了
        g._AI_CALL_STATS["errors"] += 3     # 三个批次全失败，被下游吞掉
        return 0                            # → 返回 0 篇

    budget = {"fs": 60, "hl": 0, "fs_zero": 0, "hl_zero": 0}
    with mock.patch.object(g, "_enrich_daily_focus", side_effect=fake_fs), \
         mock.patch.object(g, "_guarantee_daily_highlights", return_value=0):
        for _ in range(5):
            g._apply_daily_enrichment([{"title": "t"}], budget)
    assert budget["fs"] == 0, budget
    assert calls["n"] == g._ENRICH_ZERO_YIELD_LIMIT, (
        f"AI 连续颗粒无收后应停手，实际又调了 {calls['n']} 天")


def test_enrich_budget_not_charged_when_no_ai_call_happened():
    """“今天没有候选”与“调用失败”必须区分开：一次 AI 都没发出去时不能扣预算，
    否则前两天恰好没候选就会把整次运行的富化额度烧光。"""
    def fake_fs(items, max_items=None):
        return 0                            # 没候选，_AI_CALL_STATS 保持 0

    budget = {"fs": 60, "hl": 0, "fs_zero": 0, "hl_zero": 0}
    with mock.patch.object(g, "_enrich_daily_focus", side_effect=fake_fs), \
         mock.patch.object(g, "_guarantee_daily_highlights", return_value=0):
        for _ in range(4):
            g._apply_daily_enrichment([{"title": "t"}], budget)
    assert budget["fs"] == 60, budget
    assert budget["fs_zero"] == 0, budget


def test_enrich_budget_success_path_unchanged():
    """成功路径不变：按成功条数扣，扣光即止（原 test_daily_enrichment_respects_global_budget
    的等价断言，确保这次改动没动成功路径）。"""
    seen = {"fs": 0}

    def fake_fs(items, max_items=None):
        n = min(len(items), max_items or 0)
        seen["fs"] += n
        return n

    budget = {"fs": 5, "hl": 0, "fs_zero": 0, "hl_zero": 0}
    with mock.patch.object(g, "_enrich_daily_focus", side_effect=fake_fs), \
         mock.patch.object(g, "_guarantee_daily_highlights", return_value=0):
        for _ in range(3):
            g._apply_daily_enrichment([{"title": "t"}] * 4, budget)
    assert seen["fs"] == 5, seen
    assert budget["fs"] == 0


# --------------------------------------------------------------------------
# 6. AI 配置解析读 config.AI_CONFIG
# --------------------------------------------------------------------------

def test_resolve_ai_falls_back_to_config_ai_config():
    """README_CONFIG 让用户把 provider/api_key 写进 config.local.py（config.py 会合进
    AI_CONFIG），但本模块此前只认环境变量 → api_key=None → summarizer=None →
    每天都是“日报生成失败”。"""
    import config

    env = {k: v for k, v in os.environ.items()
           if k not in ("AI_PROVIDER", "AI_API_KEY", "KIMI_API_KEY", "GEMINI_API_KEY", "AI_MODEL")}
    with mock.patch.dict(os.environ, env, clear=True), \
         mock.patch.dict(config.AI_CONFIG,
                         {"provider": "aigw", "api_key": "sk-from-config", "model": "gpt-5.5"}):
        provider, api_key, model = g._resolve_ai()
    assert (provider, api_key, model) == ("aigw", "sk-from-config", "gpt-5.5")

    # 环境变量优先级仍在最前
    with mock.patch.dict(os.environ, dict(env, AI_PROVIDER="kimi", AI_API_KEY="sk-env"), clear=True), \
         mock.patch.dict(config.AI_CONFIG, {"provider": "aigw", "api_key": "sk-from-config"}):
        provider, api_key, _ = g._resolve_ai()
    assert (provider, api_key) == ("kimi", "sk-env")


def test_resolve_ai_survives_broken_config():
    """config 导入失败也不能拖垮日报：退回纯环境变量。"""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "config":
            raise ImportError("boom")
        return real_import(name, *a, **kw)

    env = {k: v for k, v in os.environ.items()
           if k not in ("AI_PROVIDER", "AI_API_KEY", "KIMI_API_KEY", "GEMINI_API_KEY", "AI_MODEL")}
    with mock.patch.dict(os.environ, dict(env, AI_API_KEY="sk-env"), clear=True), \
         mock.patch.object(builtins, "__import__", side_effect=boom):
        provider, api_key, model = g._resolve_ai()
    assert api_key == "sk-env" and provider == "aigw" and model is None


# --------------------------------------------------------------------------
# 7. 死代码删除（update_index 会把 summaries 条目的 digest 写丢）
# --------------------------------------------------------------------------

def test_digest_dropping_update_index_is_gone():
    """update_index 写出的条目没有 digest；一旦被重新接线，主循环的 should_skip 会
    永远失效 → 每次运行都全量重跑 AI。单日更新应走 save_summary_index。"""
    for name in ("update_index", "collect_focus_highlights", "build_highlight_reason"):
        assert not hasattr(g, name), f"{name} 是死代码，不应重新出现"
    assert hasattr(g, "save_summary_index")


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
