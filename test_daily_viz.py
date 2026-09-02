from daily_viz import render_priority_svg, render_source_split_svg, render_topic_distribution_svg


def _items():
    return [
        {"title": "Neural network potential for electronic structure", "classify_taxonomy": "AI×物理", "journal": "arXiv"},
        {"title": "Ferroelectric polarization", "topic_bucket": "physics", "journal": "PRL"},
        # 标题带 AI 词 + 化学词 → 落在「AI × 物理/材料/化学」组（原来的 "General catalyst"
        # 没有 AI 词，三个分组里有一组恒为 0，断言 >1< 出现 3 次是碰巧成立的）
        {"title": "Machine learning for catalyst screening", "classify_taxonomy": "AI×化学·材料", "journal": "Nature"},
    ]


def test_topic_distribution_svg_is_accessible_and_counts_categories():
    svg = render_topic_distribution_svg(_items())
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "<title>" in svg and "aria-label=" in svg and "viewBox=" in svg
    assert "AI×物理" in svg and "AI×化学·材料" in svg
    assert ">1<" in svg


def test_source_split_svg_counts_arxiv_and_journals():
    svg = render_source_split_svg(_items())
    assert "arXiv" in svg and "期刊" in svg
    assert ">1<" in svg and ">2<" in svg


def test_priority_svg_matches_the_daily_grouping():
    """图表的分层标签必须与日报正文的分组一致。

    正文分组已从 priority_tier(P1/P2/P3) 换成「AI×科学交叉」口径；
    图表若还画 P2 铁电·铁磁·多铁，读者会以为正文漏了一个分组。
    """
    svg = render_priority_svg(_items())
    assert "神经网络势·电子结构" in svg
    assert "AI × 物理/材料/化学" in svg
    assert "其他物理/材料" in svg
    assert "P1" not in svg and "P2" not in svg
    assert svg.count(">1<") >= 3


def test_all_daily_svgs_handle_empty_input():
    for render in (render_topic_distribution_svg, render_source_split_svg, render_priority_svg):
        svg = render([])
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert "暂无数据" in svg
