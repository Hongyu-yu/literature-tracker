from daily_viz import render_priority_svg, render_source_split_svg, render_topic_distribution_svg


def _items():
    return [
        {"title": "Neural network potential for electronic structure", "classify_taxonomy": "AI×物理", "journal": "arXiv"},
        {"title": "Ferroelectric polarization", "topic_bucket": "physics", "journal": "PRL"},
        {"title": "General catalyst", "classify_taxonomy": "AI×化学·材料", "journal": "Nature"},
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


def test_priority_svg_uses_shared_priority_tiers():
    svg = render_priority_svg(_items())
    assert "P1" in svg and "P2" in svg and "P3" in svg
    assert svg.count(">1<") >= 3


def test_all_daily_svgs_handle_empty_input():
    for render in (render_topic_distribution_svg, render_source_split_svg, render_priority_svg):
        svg = render([])
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert "暂无数据" in svg
