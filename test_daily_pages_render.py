#!/usr/bin/env python3
"""
Deterministic sanity test (no network):
- Verify daily page renderer uses `full_list` (and falls back to `summaries`).
- Verify daily page renderer carries the layout fixes that avoid global header/body bleed-through.
"""

from generate_daily_pages import render_daily_html


def main() -> int:
    date_str = "2026-03-15"

    summary_full_list = {
        "overview": "总览测试",
        "trends": "热点测试",
        "full_list": [
            {
                "title_en": "Test English Title",
                "title_zh": "测试中文标题",
                "summary": "测试中文一句话总结",
                "link": "https://example.com/paper",
                "journal": "arXiv",
                "authors": list("Alice Smith, Bob Jones, Carol Chen"),
            }
        ],
    }
    html = render_daily_html(date_str, summary_full_list)
    assert "Test English Title" in html
    assert "测试中文标题" in html
    assert "测试中文一句话总结" in html
    assert "与我们研究方向的关系" in html
    assert "方法要点" in html
    assert "https://example.com/paper" in html
    assert "arXiv" in html
    assert "Alice Smith, Bob Jones, Carol Chen" in html
    assert "AI × Science 文献日报" in html
    assert 'application/rss+xml' in html
    assert '2026-03-15.xml' in html
    assert "今日摘要" in html
    assert "今日文献" in html
    assert "测试中文标题" in html
    assert '<div class="daily-hero">' in html
    assert '<header class="daily-hero">' not in html
    # 2026-06 起公共样式外提:页面引用 daily-common.css,防全局 header/body 串样式的
    # 规则(body::before 等)在该文件中(见 docs/daily-common.css)
    assert '<link rel="stylesheet" href="../daily-common.css" />' in html
    assert "<style" not in html.split("daily-enhancement-style")[0], "恒定 CSS 不应再内联"

    summary_summaries_only = {
        "overview": "总览测试2",
        "trends": "热点测试2",
        "summaries": [
            {
                "title_en": "Test2 EN",
                "title_zh": "测试2中文",
                "summary": "测试2总结",
                "link": "https://example.com/paper2",
            }
        ],
    }
    html2 = render_daily_html(date_str, summary_summaries_only)
    assert "Test2 EN" in html2
    assert "测试2中文" in html2
    assert "今日文献" in html2

    # ----- Core-focus section -----
    from generate_daily_pages import render_daily_html as _rdh
    summary_with_core = {
        'date':'2026-04-15','total':0,'overview':'','trends':'',
        'full_list':[],'summaries':[],'ml_highlights':[],'ferro_highlights':[],
        'core_items':[{
            'title_en':'Equivariant neural network potential for ferroelectric perovskites',
            'title_zh':'用于铁电钙钛矿的等变神经网络势','abstract_zh':'为 BaTiO3 训练 MACE。',
            'summary':'一句话总结。','link':'https://ex/1','journal':'Nature',
            'method_point':'MACE 等变势训练','related_work':'与 NequIP/Allegro 同族','implication':'可迁移反铁磁'
        }],
        'core_direction_note':'本日 ML×ferro 方向出现 MACE 势应用于 BaTiO3。'
    }
    html_core = _rdh('2026-04-15', summary_with_core)
    if 'id="core-focus"' not in html_core:
        print('FAIL: missing core-focus section when core_items present'); return 1
    if '核心关注（ML' not in html_core:
        print('FAIL: missing core heading'); return 1
    if '方法要点' not in html_core or '启示' not in html_core:
        print('FAIL: missing deep fields'); return 1

    summary_no_core = dict(summary_with_core)
    summary_no_core['core_items'] = []
    summary_no_core['core_direction_note'] = ''
    html_empty = _rdh('2026-04-15', summary_no_core)
    if 'id="core-focus"' in html_empty:
        print('FAIL: should NOT render core section when empty'); return 1

    print("[OK] daily renderer sanity checks passed")
    return 0


def test_daily_html_unified_list_includes_enriched():
    import json, os, tempfile
    from generate_daily_pages import render_daily_html
    d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "data"))
    with open(os.path.join(d, "data", "arxiv_core_2026-06-01.json"), "w", encoding="utf-8") as f:
        json.dump([{"link": "http://arxiv.org/abs/x", "deep_analysis": "## 深",
                    "image": "images/posters/x.webp",
                    "poster": {"elements": {"研究问题": "q"}},
                    "category": "AI×物理", "title_zh": "交叉中文"}], f, ensure_ascii=False)
    summary = {"overview": "ov", "trends": "tr", "full_list": [
        {"title_en": "X", "title_zh": "交叉中文", "summary": "亮点",
         "link": "http://arxiv.org/abs/x", "journal": "arXiv"},
        {"title_en": "Plain", "summary": "普通", "link": "http://y", "journal": "arXiv"}]}
    cwd = os.getcwd()
    try:
        os.chdir(d)
        html = render_daily_html("2026-06-01", summary)
    finally:
        os.chdir(cwd)
    assert "今日文献" in html
    assert "enrich-badge" in html and "<details" in html
    assert "../images/posters/x.webp" in html
    assert "poster-overlay" not in html
    # 交叉重点/完整速览 sections removed
    assert "完整速览" not in html and "交叉重点" not in html


def test_daily_html_prefers_full_translation_and_renders_relation():
    html = render_daily_html("2026-06-02", {
        "overview": "总览", "trends": "热点",
        "full_list": [{
            "title_en": "Ferroelectric switching", "title_zh": "铁电翻转",
            "abstract": "English source", "abstract_zh": "浓缩摘要",
            "abstract_zh_full": "完整中文翻译优先", "summary": "中文亮点",
            "method_point": "方法关系" + "。该方法需要核对输入、标签、训练策略、物理约束和验证集，并与团队已有的第一性原理或机器学习势基准进行对照。" * 3,
            "related_work": "关联关系" + "。它与团队已有材料、有效 Hamiltonian、有限温度动力学或电子结构计算的连接，需要通过同一体系和跨分布测试确认。" * 3,
            "implication": "启示关系" + "。建议先在已有 DFT 数据的铁电、磁性或缺陷体系上做小规模复现，再比较跨温度、应变和缺陷浓度的可迁移性。" * 3,
            "link": "https://example.com/full", "journal": "arXiv",
        }],
    })
    assert "完整中文翻译优先" in html
    assert "浓缩摘要" not in html
    assert "方法上" in html and "关联关系" in html and "对当前研究最具体的启示" in html


def test_build_core_export_has_category_and_link():
    from generate_daily_pages import build_core_export
    items = [{"title": "ML interatomic potential for perovskite",
              "title_zh": "钙钛矿的机器学习势", "summary": "图神经网络势",
              "abstract": "graph neural network potential for materials",
              "abstract_zh": "用于材料的图神经网络势",
              "link": "http://arxiv.org/abs/2601.001", "journal": "arXiv"}]
    out = build_core_export(items)
    assert out[0]["category"] in ("AI×物理", "AI×化学·材料")
    assert out[0]["abstract"]
    assert out[0]["link"].startswith("http")
    assert out[0]["title_zh"] == "钙钛矿的机器学习势"

def test_build_tier2_candidates_picks_ai_cross():
    from generate_daily_pages import build_tier2_candidates
    full = [
        {"title": "Deep learning for catalyst discovery", "summary": "neural network",
         "abstract": "graph neural network for chemistry catalyst", "link": "http://x", "journal": "arXiv"},
        {"title": "A study of medieval poetry", "summary": "", "abstract": "", "link": "http://y"},
    ]
    cand = build_tier2_candidates(full)
    links = [c["link"] for c in cand]
    assert "http://x" in links and "http://y" not in links
    assert cand and cand[0]["category"]


def test_md_to_html_headers_bold_lists_and_escapes():
    from generate_daily_pages import md_to_html
    md = ("## 第一部分：核心概览\n"
          "这是 **关键** 贡献。\n"
          "- 要点一\n- 要点二\n"
          "### 小节\n普通段落 <script>alert(1)</script>")
    html = md_to_html(md)
    # markdown headers are shifted down one level to avoid h1 (page title): ## → h3, ### → h4
    assert "<h3" in html and "第一部分：核心概览" in html
    assert "<h4" in html and "小节" in html
    assert "<strong>关键</strong>" in html
    assert "<li>要点一</li>" in html and "<li>要点二</li>" in html
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_md_to_html_empty():
    from generate_daily_pages import md_to_html
    assert md_to_html("") == ""
    assert md_to_html(None) == ""


def test_render_unified_item_deep_body_is_structured_html():
    from generate_daily_pages import render_unified_item
    item = {"title_en": "E", "title_zh": "标题", "summary": "亮点", "link": "http://x",
            "journal": "arXiv", "_tier": 1,
            "_enrich": {"deep_analysis": "## 核心概览\n**粗体**要点", "image": "images/posters/x.webp",
                        "elements": {"研究问题": "q"}, "category": "AI×物理", "title_zh": "标题"}}
    html = render_unified_item(item, 1)
    assert "<h3" in html and "<strong>粗体</strong>" in html  # ## → h3
    assert "## 核心概览" not in html


def test_load_enrichment_keys_by_link_and_skips_plain():
    import json, os, tempfile
    from generate_daily_pages import load_enrichment
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    rows = [
        {"link": "http://arxiv.org/abs/1", "deep_analysis": "## 深析",
         "image": "images/posters/a.webp",
         "poster": {"elements": {"研究问题": "q", "创新方法": "m"}},
         "category": "AI×物理", "title_zh": "中文一"},
        {"link": "10.1103/xyz", "deep_analysis": "## 二",
         "poster": {"image": "images/posters/b.webp", "elements": {"关键结果": "r"}},
         "category": "AI×化学·材料", "title_zh": "中文二"},
        {"link": "http://arxiv.org/abs/3", "summary": "no enrichment"},  # plain → skipped
    ]
    with open(os.path.join(d, "data", "arxiv_core_2026-06-01.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    cwd = os.getcwd()
    try:
        os.chdir(d)
        m = load_enrichment("2026-06-01")
    finally:
        os.chdir(cwd)
    assert "http://arxiv.org/abs/1" in m
    assert m["http://arxiv.org/abs/1"]["image"] == "images/posters/a.webp"
    assert m["http://arxiv.org/abs/1"]["elements"]["研究问题"] == "q"
    # bare DOI normalized as key
    assert "https://doi.org/10.1103/xyz" in m
    assert m["https://doi.org/10.1103/xyz"]["image"] == "images/posters/b.webp"
    # plain row (no deep, no image) skipped
    assert "http://arxiv.org/abs/3" not in m


def test_load_enrichment_missing_file_returns_empty():
    from generate_daily_pages import load_enrichment
    assert load_enrichment("1999-01-01") == {}


def test_build_unified_items_merges_and_sorts():
    from generate_daily_pages import build_unified_items
    full_list = [
        {"title": "Plain paper", "title_en": "Plain paper", "summary": "x",
         "link": "http://arxiv.org/abs/plain"},
        {"title": "AI cross arxiv", "title_en": "AI cross arxiv", "title_zh": "交叉",
         "summary": "y", "link": "http://arxiv.org/abs/cross"},
    ]
    enrich_map = {
        "http://arxiv.org/abs/cross": {"deep_analysis": "## d", "image": "images/posters/c.webp",
                                       "elements": {"研究问题": "q"}, "category": "AI×物理",
                                       "title_zh": "交叉"},
    }
    aps_items = [
        {"title": "APS fulltext", "title_zh": None, "doi": "10.1103/abc", "category": "软物质",
         "deep_analysis": "## aps", "poster": {"image": "images/posters/aps.webp",
                                               "elements": {"创新方法": "m"}}},
        {"title": "APS plain", "doi": "10.1103/plain", "poster": {}},  # no enrichment → skipped
    ]
    out = build_unified_items(full_list, enrich_map, aps_items)
    tiers = [it["_tier"] for it in out]
    assert tiers == sorted(tiers)
    assert out[0]["_tier"] == 0 and out[0]["_enrich"]["image"] == "images/posters/aps.webp"
    assert out[0]["link"] == "https://doi.org/10.1103/abc"  # bare DOI normalized
    cross = next(it for it in out if it["link"] == "http://arxiv.org/abs/cross")
    assert cross["_tier"] == 1 and cross["_enrich"]["category"] == "AI×物理"
    plain = next(it for it in out if it["link"] == "http://arxiv.org/abs/plain")
    assert plain["_tier"] == 2 and plain["_enrich"] is None
    assert all("APS plain" != it.get("title") for it in out)


def test_build_unified_items_dedups_aps_already_in_full_list():
    from generate_daily_pages import build_unified_items
    full_list = [{"title": "Dup", "link": "https://doi.org/10.1103/dup", "summary": "s"}]
    aps_items = [{"title": "Dup", "doi": "10.1103/dup", "deep_analysis": "d",
                  "poster": {"image": "p.webp"}}]
    out = build_unified_items(full_list, {}, aps_items)
    assert len(out) == 1 and out[0]["_tier"] == 0


def test_render_unified_item_enriched_has_details_and_image():
    from generate_daily_pages import render_unified_item
    item = {"title": "Cross", "title_en": "Cross", "title_zh": "交叉标题",
            "summary": "一句话亮点", "link": "http://arxiv.org/abs/cross", "journal": "arXiv",
            "_tier": 1, "_enrich": {"deep_analysis": "## 深析正文", "image": "images/posters/c.webp",
                                    "elements": {"研究问题": "q", "创新方法": "m", "工作流程": "f",
                                                 "关键结果": "r", "应用价值": "v"},
                                    "category": "AI×物理", "title_zh": "交叉标题"}}
    html = render_unified_item(item, 1)
    assert "交叉标题" in html
    assert "enrich-badge" in html and "含图深析" in html
    assert "<details" in html
    assert 'src="../images/posters/c.webp"' in html
    assert "daily-deep-elements" in html and "研究问题" in html
    assert "深析正文" in html
    assert 'data-bookmark-key="http://arxiv.org/abs/cross"' in html
    assert "poster-overlay" not in html


def test_render_unified_item_plain_has_no_details():
    from generate_daily_pages import render_unified_item
    item = {"title": "Plain", "title_en": "Plain", "summary": "brief",
            "link": "http://x", "journal": "arXiv", "_tier": 2, "_enrich": None}
    html = render_unified_item(item, 2)
    assert 'class="daily-research-relation"' in html
    assert "enrich-badge" not in html
    assert "brief" in html
    assert 'data-bookmark-key="http://x"' in html


if __name__ == "__main__":
    raise SystemExit(main())


def test_render_unified_item_shows_abstract_then_highlight():
    from generate_daily_pages import render_unified_item
    item = {"title": "P", "title_en": "P", "title_zh": "标题",
            "abstract_zh": "该工作在 BaTiO3 中用 MACE 等变势复现相变温度，误差 < 5K。",
            "summary": "首次把等变势用于钙钛矿相变预测，精度接近第一性原理。",
            "link": "http://x", "journal": "arXiv", "_tier": 2, "_enrich": None}
    html = render_unified_item(item, 1)
    assert "daily-paper-abstract" in html and "📄 摘要" in html
    assert "MACE 等变势复现相变温度" in html
    assert "daily-paper-highlight" in html and "💡 亮点" in html
    assert "首次把等变势用于钙钛矿相变预测" in html
    # 摘要块在亮点块之前
    assert html.index("📄 摘要") < html.index("💡 亮点")


def test_render_unified_item_no_abstract_block_when_empty():
    from generate_daily_pages import render_unified_item
    item = {"title": "P", "title_en": "P", "summary": "只有亮点",
            "link": "http://x", "journal": "arXiv", "_tier": 2, "_enrich": None}
    html = render_unified_item(item, 1)
    assert "📄 摘要" not in html           # 无 abstract_zh 不出摘要块
    assert "💡 亮点" in html and "只有亮点" in html
    # 亮点不再被 abstract_zh 兜底污染(此处无 abstract_zh,亮点仍是 summary)


def test_classify_uses_title_en_when_title_empty():
    # full_list stores English under title_en with title empty → must still classify AI×
    from generate_daily_pages import build_tier2_candidates, build_core_export
    full = [{"title": "", "title_en": "Physics-Informed Machine Learning for quantum materials",
             "title_zh": "面向量子材料的物理信息机器学习", "summary": "机器学习",
             "abstract_zh": "用神经网络预测材料性质", "link": "http://x", "is_core_focus": False}]
    cand = build_tier2_candidates(full)
    assert cand, "AI-cross paper with title_en must be selected"
    assert cand[0]["category"] in ("AI×物理", "AI×化学·材料")
    assert cand[0]["title"].startswith("Physics-Informed")  # English title resolved
    assert cand[0]["abstract"]  # non-empty (falls back to abstract_zh)
    ce = build_core_export(full)
    assert ce[0]["category"] in ("AI×物理", "AI×化学·材料")
    assert ce[0]["abstract"]


def test_render_unified_item_prefers_abstract_zh_full():
    from generate_daily_pages import render_unified_item
    item = {"title": "P", "title_en": "P", "title_zh": "标题",
            "abstract_zh": "浓缩摘要不应出现", "abstract_zh_full": "完整中文翻译应当出现",
            "summary": "亮点", "link": "http://x", "journal": "arXiv",
            "_tier": 2, "_enrich": None}
    html = render_unified_item(item, 1)
    assert "完整中文翻译应当出现" in html
    assert "浓缩摘要不应出现" not in html  # 完整翻译优先于浓缩版


def test_render_unified_item_falls_back_to_concise_zh():
    from generate_daily_pages import render_unified_item
    item = {"title": "P", "title_en": "P", "title_zh": "标题",
            "abstract_zh": "旧数据浓缩摘要",
            "summary": "亮点", "link": "http://x", "journal": "arXiv",
            "_tier": 2, "_enrich": None}
    html = render_unified_item(item, 1)
    assert "旧数据浓缩摘要" in html  # 无 abstract_zh_full 时回退旧行为


def test_render_unified_item_en_details_shown_and_hidden():
    from generate_daily_pages import render_unified_item
    base = {"title": "P", "title_en": "P", "title_zh": "标题",
            "summary": "亮点", "link": "http://x", "journal": "arXiv",
            "_tier": 2, "_enrich": None}
    with_en = render_unified_item(dict(base, abstract="English abstract body"), 1)
    assert '<details class="daily-abstract-en">' in with_en
    assert "English abstract body" in with_en
    without_en = render_unified_item(base, 1)
    assert "daily-abstract-en" not in without_en


def test_render_focus_section_cards_sorted_with_three_lines():
    from generate_daily_pages import render_focus_section
    items = [
        {"title_zh": "低分文", "focus_score": 5, "focus_summary": "总结A",
         "focus_relation": "关系A", "focus_suggestion": "建议A",
         "link": "https://ex/a", "journal": "arXiv"},
        {"title_zh": "高分文", "focus_score": 9, "focus_summary": "总结B",
         "focus_relation": "关系B", "focus_suggestion": "建议B",
         "link": "https://ex/b", "journal": "Nature"},
    ]
    html = render_focus_section(items)
    assert 'id="focus-interest"' in html and "与你方向相关" in html
    assert "2 篇" in html
    assert "📝 简单总结" in html and "🔗 与我们工作的关系" in html
    assert "💡 进一步工作建议" in html
    assert "相关度 9" in html and "相关度 5" in html
    assert html.index("高分文") < html.index("低分文")  # 按分数降序
    assert 'href="https://ex/b"' in html


def test_render_focus_section_skips_empty_analysis_lines():
    from generate_daily_pages import render_focus_section
    items = [{"title_zh": "半空文", "focus_score": 7, "focus_summary": "只有总结",
              "focus_relation": "", "focus_suggestion": "",
              "link": "https://ex/c", "journal": "arXiv"}]
    html = render_focus_section(items)
    assert "只有总结" in html
    assert "与我们工作的关系" not in html  # 空字段跳过
    assert "进一步工作建议" not in html


def test_render_focus_section_hidden_when_no_focus_items():
    from generate_daily_pages import render_focus_section
    assert render_focus_section([]) == ""
    assert render_focus_section(None) == ""
    # 旧数据(无 focus 字段) → 区块整体隐藏
    assert render_focus_section([{"title": "旧文章", "link": "http://x"}]) == ""


def test_render_daily_html_focus_section_wired_shown_and_hidden():
    import os, tempfile
    from generate_daily_pages import render_daily_html
    d = tempfile.mkdtemp()
    focus_item = {"title_en": "Focus Paper", "title_zh": "焦点文", "summary": "s",
                  "link": "https://ex/f", "journal": "arXiv",
                  "focus_score": 8, "focus_summary": "总结", "focus_relation": "关系",
                  "focus_suggestion": "建议"}
    plain_item = {"title_en": "Plain", "summary": "s",
                  "link": "https://ex/p", "journal": "arXiv"}
    cwd = os.getcwd()
    try:
        os.chdir(d)
        html = render_daily_html("2026-07-29",
                                 {"overview": "o", "trends": "t", "full_list": [focus_item]})
        assert 'id="focus-interest"' in html
        assert 'href="#focus-interest"' in html  # 目录链接同步出现
        # 旧数据(无新字段)渲染与之前一致:无 focus 区块、无目录链接
        html2 = render_daily_html("2026-07-29",
                                  {"overview": "o", "trends": "t", "full_list": [plain_item]})
        assert 'id="focus-interest"' not in html2
        assert 'href="#focus-interest"' not in html2
        assert "今日文献" in html2 and "Plain" in html2
    finally:
        os.chdir(cwd)


def test_collect_daily_articles_merges_zh_and_focus_from_index():
    """ai_relevant 条目(富化前写入,缺 zh/focus 字段)应按 link 从 index.json 条目补齐。"""
    from generate_daily_pages import collect_daily_articles

    link = "https://example.com/paper-1"
    relevant = [{
        "id": "x1", "link": link, "pub_date": "2026-07-29",
        "title": "Ferroelectric test paper", "abstract": "Some abstract",
        "journal": "PRL", "ai_score": 8,
    }]
    index = [{
        "id": "x1", "link": link, "pub_date": "2026-07-29",
        "title": "Ferroelectric test paper", "abstract": "Some abstract",
        "journal": "PRL", "title_zh": "铁电测试", "abstract_zh": "浓缩摘要",
        "abstract_zh_full": "完整翻译摘要", "focus_score": 8,
        "focus_summary": "总结", "focus_relation": "关系", "focus_suggestion": "建议",
    }]
    out = collect_daily_articles(index, relevant, "2026-07-29")
    item = next(a for a in out["raw_day_articles"] if a.get("link") == link)
    assert item.get("abstract_zh") == "浓缩摘要"
    assert item.get("abstract_zh_full") == "完整翻译摘要"
    assert item.get("focus_score") == 8
    assert item.get("focus_relation") == "关系"


def test_collect_daily_articles_keeps_existing_fields_over_index():
    """relevant 条目已有字段时不被 index 版本覆盖。"""
    from generate_daily_pages import collect_daily_articles

    link = "https://example.com/paper-2"
    relevant = [{
        "id": "y1", "link": link, "pub_date": "2026-07-29",
        "title": "T", "abstract": "A", "journal": "J",
        "abstract_zh": "已有摘要", "focus_score": 5,
    }]
    index = [{
        "id": "y1", "link": link, "pub_date": "2026-07-29",
        "title": "T", "abstract": "A", "journal": "J",
        "abstract_zh": "index 版本摘要", "focus_score": 9,
    }]
    out = collect_daily_articles(index, relevant, "2026-07-29")
    item = next(a for a in out["raw_day_articles"] if a.get("link") == link)
    assert item.get("abstract_zh") == "已有摘要"
    assert item.get("focus_score") == 5


def test_unified_sort_prioritizes_research_layer_before_enrichment_tier():
    from generate_daily_pages import build_unified_items
    p1_plain = {
        "title": "Neural network potential for electronic structure",
        "link": "https://example.com/p1",
    }
    p2_enriched = {
        "title": "Ferroelectric polarization",
        "link": "https://example.com/p2",
    }
    enriched = {"https://example.com/p2": {"image": "images/posters/p2.webp"}}
    result = build_unified_items([p2_enriched, p1_plain], enriched, [])
    assert result[0]["link"] == "https://example.com/p1"


def test_daily_html_renders_three_priority_groups_with_p1_first():
    from generate_daily_pages import render_daily_html
    summary = {
        "overview": "o", "trends": "t",
        "full_list": [
            {"title": "General materials workflow", "summary": "general", "link": "https://ex/3"},
            {"title": "Ferroelectric polarization", "summary": "ferro", "link": "https://ex/2"},
            {"title": "Neural network potential for electronic structure", "summary": "p1", "link": "https://ex/1"},
        ],
    }
    page = render_daily_html("2026-07-29", summary)
    headings = [
        "神经网络势 · 电子结构（重点）",
        "铁电 · 铁磁 · 多铁（物理）",
        "其他交叉 / 方法",
    ]
    assert all(heading in page for heading in headings)
    assert page.index(headings[0]) < page.index(headings[1]) < page.index(headings[2])
    assert page.index("Neural network potential") < page.index("Ferroelectric polarization")


def test_daily_focus_enrichment_failure_is_soft():
    import os
    from unittest import mock
    import generate_daily_pages

    with mock.patch.dict(os.environ, {"AI_API_KEY": "test", "AI_FOCUS_DAILY_MAX": "60"}, clear=False), \
         mock.patch("ai_summarizer.build_provider", return_value=object()), \
         mock.patch("focus_interest.enrich_focus_interest", side_effect=RuntimeError("offline")):
        assert generate_daily_pages._enrich_daily_focus([{"title": "x"}]) == 0


def test_daily_html_has_overview_svgs_relevance_bar_and_category_chip():
    from generate_daily_pages import render_daily_html
    item = {
        "title": "Neural network potential for electronic structure",
        "summary": "亮点", "link": "https://ex/1", "journal": "arXiv",
        "focus_score": 8.5,
    }
    page = render_daily_html("2026-07-29", {"overview": "o", "trends": "t", "full_list": [item]})
    assert "📊 今日概览" in page
    assert page.count("class=\"daily-viz-svg\"") == 3
    assert "daily-relevance-bar" in page and "8.5" in page
    assert "daily-chip-category" in page and "AI×物理" in page
