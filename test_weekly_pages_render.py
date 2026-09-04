#!/usr/bin/env python3
"""Deterministic sanity test for weekly page rendering (no network)."""

import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory


def _stub_third_party(name, attrs):
    """本机缺 bs4/deep_translator 时桩进 sys.modules(渲染路径用不到它们);
    CI 装了真依赖则不动。返回 True 表示用的是桩。"""
    try:
        __import__(name)
        return False
    except ModuleNotFoundError:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return True


class _UnavailableStub:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("stubbed optional dep: not usable in tests")


class _GoogleTranslatorStub:
    """translator.py 在 import 时就实例化 GoogleTranslator,桩必须可构造;
    translate 真被调用才报错(本测试路径不会触发)。"""
    def __init__(self, *args, **kwargs):
        pass

    def translate(self, text):
        raise RuntimeError("stubbed deep_translator: translate not usable in tests")


_BS4_STUBBED = _stub_third_party("bs4", {"BeautifulSoup": _UnavailableStub,
                                         "Comment": str, "NavigableString": str,
                                         "Tag": object})
_stub_third_party("deep_translator", {"GoogleTranslator": _GoogleTranslatorStub})

import weekly_summary
from weekly_summary import WeeklySummarizer

if _BS4_STUBBED:
    # 本机无 bs4:索引导航增强(BeautifulSoup 解析,与渲染断言无关)全局打桩跳过
    weekly_summary.enhance_weekly_archive = lambda *a, **k: 0


def main() -> int:
    summary = {
        "week_start": "2026-03-16",
        "week_end": "2026-03-22",
        "overview": "本周围绕 AI × 材料交叉、磁性与分子设计展开。",
        "trends": "交叉研究继续集中在 AI 驱动材料发现与自旋体系建模。",
        "outlook": "后续值得关注预印本向顶刊转化与实验验证闭环。",
        "generated_by": "test",
        "both_articles": [
            {
                "id": "cross-1",
                "title_zh": "测试交叉文献中文标题",
                "title": "Test Cross Paper",
                "journal": "arXiv",
                "authors": ["Alice", "Bob", "Carol"],
                "ai_analysis": "利用机器学习建模磁性材料相变。",
                "abstract_zh": "中文摘要内容。",
                "abstract": "English abstract content.",
                "pub_date": "2026-03-18",
                "link": "https://example.com/cross",
                "is_ferro": True,
                "is_ai": True,
            }
        ],
        "ferro_articles": [
            {
                "id": "ferro-1",
                "title_zh": "测试磁性文献中文标题",
                "title": "Test Ferro Paper",
                "journal": "Nature Materials",
                "authors": ["Dave"],
                "abstract_zh": "磁性文献中文摘要。",
                "abstract": "Ferro abstract.",
                "pub_date": "2026-03-17",
                "link": "https://example.com/ferro",
                "is_ferro": True,
                "is_ai": False,
            }
        ],
        "ai_articles": [
            {
                "id": "ai-1",
                "title_zh": "测试 AI 文献中文标题",
                "title": "Test AI Paper",
                "journal": "Science",
                "authors": ["Eve", "Frank"],
                "abstract_zh": "AI 文献中文摘要。",
                "abstract": "AI abstract.",
                "pub_date": "2026-03-19",
                "link": "https://example.com/ai",
                "is_ferro": False,
                "is_ai": True,
            }
        ],
    }
    summary["all_articles"] = summary["both_articles"] + summary["ferro_articles"] + summary["ai_articles"]
    summary["by_journal"] = {
        "arXiv": [summary["both_articles"][0]],
        "Nature Materials": [summary["ferro_articles"][0]],
        "Science": [summary["ai_articles"][0]],
    }

    with TemporaryDirectory() as tmpdir:
        path = WeeklySummarizer().save_summary_html(summary, tmpdir)
        html = Path(path).read_text(encoding="utf-8")

    assert "AI × Science 周报" in html
    assert "本周总览" in html
    assert "交叉研究" in html
    assert "磁性 / 铁电专题" in html
    assert "AI / 机器学习专题" in html
    assert "期刊分布" in html
    assert "测试交叉文献中文标题" in html
    assert "Test Cross Paper" in html
    assert "Alice" in html
    assert "https://example.com/cross" in html
    assert "toggleTheme" in html
    assert "toggleAbstract" in html
    assert "查看完整摘要" in html

    # ----- Weekly core-focus section -----
    from weekly_summary import render_core_weekly_section as _rcw
    wk = {
        'core_items':[{'title':'等变神经网络势','title_en':'Equivariant NNP','link':'https://ex/1','journal':'Nature','abstract_zh':'为 BaTiO3 训练 MACE。','method_point':'MACE 等变势','related_work':'与 NequIP 同族','implication':'可迁移反铁磁'}],
        'core_weekly_note':'本周 MACE 用于 BaTiO3；CrI3 中 GNN 学习自旋哈密顿量。'
    }
    block = _rcw(wk)
    if 'weekly-core-section' not in block or '本周核心方向' not in block:
        print('FAIL: weekly core section missing heading'); return 1
    if '方法要点' not in block or '启示' not in block:
        print('FAIL: weekly deep labels missing'); return 1
    if _rcw({'core_items':[], 'core_weekly_note':''}).strip() != '':
        print('FAIL: weekly core section should be empty when no items'); return 1

    print("[OK] weekly renderer sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def _weekly_summary(articles, focus=False):
    """构造最小周报 summary;focus=True 时给首篇文章加 focus_* 字段。"""
    if focus and articles:
        articles[0] = dict(articles[0], focus_score=9, focus_summary="周总结",
                           focus_relation="周关系", focus_suggestion="周建议")
    return {
        "week_start": "2026-03-16", "week_end": "2026-03-22",
        "overview": "总览", "trends": "热点", "outlook": "展望",
        "generated_by": "test",
        "both_articles": articles, "ferro_articles": [], "ai_articles": [],
        "all_articles": list(articles),
        "by_journal": {"arXiv": articles} if articles else {},
    }


def _render_weekly(summary):
    summarizer = WeeklySummarizer()
    with TemporaryDirectory() as tmpdir:
        path = summarizer.save_summary_html(summary, tmpdir)
        return Path(path).read_text(encoding="utf-8")


def test_render_focus_weekly_section_cards_sorted_with_three_lines():
    from weekly_summary import render_focus_weekly_section
    items = [
        {"title_zh": "低分文", "focus_score": 4, "focus_summary": "总结A",
         "focus_relation": "关系A", "focus_suggestion": "建议A",
         "link": "https://ex/a", "journal": "arXiv"},
        {"title_zh": "高分文", "focus_score": 10, "focus_summary": "总结B",
         "focus_relation": "关系B", "focus_suggestion": "建议B",
         "link": "https://ex/b", "journal": "Nature"},
    ]
    html = render_focus_weekly_section(items)
    assert 'id="focus-interest"' in html and "与你方向相关" in html
    assert "2 篇" in html
    assert "📝 简单总结" in html and "🔗 与我们工作的关系" in html
    assert "💡 进一步工作建议" in html
    # focus_score 芯片改名为「画像分」：卡片上另有两条进度条叫「AI×科学交叉」和
    # 「方向匹配」，三者含义不同，同名会让人以为是同一个数。
    assert "画像分 10" in html and "画像分 4" in html
    assert html.index("高分文") < html.index("低分文")  # 按分数降序
    # 与日报对齐的新增信息：作者、日期、双相关度条
    assert "weekly-relevance" in html and "AI×科学交叉" in html and "方向匹配" in html


def test_render_focus_weekly_section_hidden_when_no_focus_items():
    from weekly_summary import render_focus_weekly_section
    assert render_focus_weekly_section([]) == ""
    assert render_focus_weekly_section(None) == ""
    # 旧数据(无 focus 字段) → 区块整体隐藏
    assert render_focus_weekly_section([{"title": "旧文章", "link": "http://x"}]) == ""


def test_weekly_collapsed_section_prefers_full_translation():
    art = {"id": "c1", "title_zh": "中文标题", "title": "EN Title",
           "journal": "arXiv", "authors": ["Alice"],
           "ai_analysis": "AI 解读内容。",
           "abstract_zh": "浓缩摘要标记", "abstract_zh_full": "完整中文翻译标记",
           "abstract": "English body.",
           "pub_date": "2026-03-18", "link": "https://example.com/c",
           "is_ferro": True, "is_ai": True}
    html = _render_weekly(_weekly_summary([art]))
    assert "完整中文翻译标记" in html   # 折叠区中文 = 完整翻译
    assert "浓缩摘要标记" not in html  # 浓缩版不再出现
    assert "English body." in html


def test_weekly_collapsed_section_falls_back_to_concise_zh():
    art = {"id": "c2", "title_zh": "中文标题", "title": "EN Title",
           "journal": "arXiv", "authors": ["Alice"],
           "ai_analysis": "AI 解读内容。",
           "abstract_zh": "旧数据浓缩摘要标记", "abstract": "English body.",
           "pub_date": "2026-03-18", "link": "https://example.com/c2",
           "is_ferro": True, "is_ai": True}
    html = _render_weekly(_weekly_summary([art]))
    assert "旧数据浓缩摘要标记" in html  # 无 abstract_zh_full 时回退旧行为


def test_weekly_focus_section_wired_shown_and_hidden():
    art = {"id": "c3", "title_zh": "焦点文", "title": "Focus Paper",
           "journal": "arXiv", "authors": ["Alice"], "ai_analysis": "解读。",
           "abstract_zh": "摘要。", "abstract": "abs.",
           "pub_date": "2026-03-18", "link": "https://example.com/f",
           "is_ferro": True, "is_ai": True}
    html_focus = _render_weekly(_weekly_summary([art], focus=True))
    assert 'id="focus-interest"' in html_focus
    assert "周总结" in html_focus and "周关系" in html_focus and "周建议" in html_focus
    assert 'href="#focus-interest"' in html_focus  # 目录链接同步出现
    # 旧数据(无 focus 字段) → 无区块、无目录链接
    html_plain = _render_weekly(_weekly_summary([art]))
    assert 'id="focus-interest"' not in html_plain
    assert 'href="#focus-interest"' not in html_plain


def test_core_weekly_cards_show_authors_date_and_abstracts_like_the_daily():
    """核心区块此前只有标题/期刊/中文摘要 —— 作者、日期、英文摘要、相关度
    在 core_items 构造时就被丢掉了，渲染层想显示也无从显示。"""
    from weekly_summary import render_core_weekly_section
    summary = {"core_items": [{
        "title": "深度生成模型中哈密顿量的自主涌现",
        "title_zh": "深度生成模型中哈密顿量的自主涌现",
        "title_en": "Autonomous Emergence of Hamiltonian in Deep Generative Models",
        "journal": "arXiv", "link": "https://arxiv.org/abs/1",
        "authors": ["Alice Zhang", "Bob Li"], "pub_date": "2026-08-25",
        "abstract": "arXiv:2604.20821v3 Announce Type: replace Abstract: Deep generative models.",
        "abstract_zh_full": "arXiv:2604.20821v3 发布类型：替换 摘要：深度生成模型的预测成功。",
        "cross_score": 9, "me_score": 8, "me_reason": "与机器学习哈密顿量方向直接相关",
        "method_point": "方法要点内容", "core_score": 0.9,
    }], "core_weekly_note": "本周说明"}
    html = render_core_weekly_section(summary)
    assert "Alice Zhang, Bob Li" in html          # 作者
    assert "2026-08-25" in html                    # 日期
    assert "中文摘要" in html and "English Abstract" in html
    assert "查看完整摘要" in html                  # 长摘要折叠，不再整段直出
    assert "AI×科学交叉" in html and "方向匹配" in html
    assert "与机器学习哈密顿量方向直接相关" in html
    # 中文标题优先；英文作为副标题
    assert html.index("深度生成模型中哈密顿量的自主涌现") < html.index("Autonomous Emergence")
    # arXiv 公告前缀不得出现在正文里
    assert "Announce Type" not in html and "发布类型" not in html


def test_core_weekly_falls_back_to_english_title_without_translation():
    """没有中文翻译时照旧显示英文标题，不能变成空标题。"""
    from weekly_summary import render_core_weekly_section
    html = render_core_weekly_section({"core_items": [{
        "title": "Autonomous Emergence of Hamiltonian",
        "title_en": "Autonomous Emergence of Hamiltonian",
        "journal": "arXiv", "link": "https://arxiv.org/abs/1",
    }]})
    assert "Autonomous Emergence of Hamiltonian" in html
    assert "未命名文献" not in html


def test_focus_weekly_cards_gained_authors_and_abstract():
    from weekly_summary import render_focus_weekly_section
    html = render_focus_weekly_section([{
        "title_zh": "通过滑移连续调控极化", "title": "Continuous polarization tuning",
        "journal": "arXiv", "pub_date": "2026-08-24", "authors": ["Wei Chen"],
        "link": "https://arxiv.org/abs/2", "focus_score": 10,
        "abstract": "arXiv:2608.21162v1 Announce Type: new Abstract: We propose sliding.",
        "abstract_zh": "该论文提出电驱动的长距离超润滑滑移。",
        "focus_summary": "总结", "focus_relation": "关系", "focus_suggestion": "建议",
    }])
    assert "Wei Chen" in html and "2026-08-24" in html
    assert "该论文提出电驱动的长距离超润滑滑移。" in html
    assert "English Abstract" in html and "Announce Type" not in html
    assert "Continuous polarization tuning" in html   # 英文副标题


def test_weekly_chips_show_canonical_journal_not_raw_feed_title():
    """卡片显示规范期刊名，不是原始 feed 标题。

    归一化函数（顶刊闸门一直在用）早就有，只是展示这一侧没接，
    结果卡片上写着 'Wiley: Advanced Science: Table of Contents'。
    """
    from weekly_summary import _weekly_meta_chips
    html = _weekly_meta_chips({"journal": "Wiley: Advanced Science: Table of Contents"})
    assert "Advanced Science" in html
    assert "Table of Contents" not in html
    assert "Wiley:" not in html
    # 本来就规范的名字不能被洗坏
    assert "arXiv" in _weekly_meta_chips({"journal": "arXiv"})
    assert "Phys. Rev. X" in _weekly_meta_chips({"journal": "Phys. Rev. X"})
