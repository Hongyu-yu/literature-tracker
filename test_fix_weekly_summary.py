#!/usr/bin/env python3
"""weekly_summary.py 回归测试（无网络）：

1. feed 标题里的 'Table of Contents' 不再把 Adv. Mater./Science Advances 整批误杀；
2. AI 相关性判断调用失败时不再静默把文章判成「不相关」丢掉；
3. 🎯 与你方向相关 区块不受顶刊闸门约束（与日报口径一致）；
4. 降级周报按实际命中集合分桶，不再用宽松词表把所有文章判成「交叉研究」。
"""

import io
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory


def _stub_third_party(name, attrs):
    """本机缺 bs4/deep_translator 时桩进 sys.modules（本测试路径用不到它们）;
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
    """translator.py 在 import 时就实例化 GoogleTranslator，桩必须可构造。"""

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
    # 本机无 bs4：归档导航增强（BeautifulSoup 解析，与本测试断言无关）打桩跳过
    weekly_summary.enhance_weekly_archive = lambda *a, **k: 0


WEEK_START = "2026-08-24"
WEEK_END = "2026-08-30"


def _summarizer(provider=None):
    """绕开 __init__：不建真 provider / AbstractScraper，只装配测试需要的字段。"""
    s = WeeklySummarizer.__new__(WeeklySummarizer)
    s.provider = provider
    s.provider_name = None
    s._judge_failures = 0
    return s


def _article(journal, idx, text="ferroelectric domain wall switching in BaTiO3 thin films"):
    return {
        "id": f"a{idx}",
        "title": "Test paper",
        "abstract": text,
        "journal": journal,
        "pub_date": "2026-08-25",
        "link": f"https://example.com/{idx}",
    }


class _BoomProvider:
    """模拟网关 429/504：call_api 永远抛异常。"""

    def call_api(self, prompt):
        raise RuntimeError("gateway 429")


def test_toc_feed_title_no_longer_kills_top_journals():
    """'Wiley: Advanced Materials: Table of Contents' 等 feed 标题必须能进顶刊白名单。"""
    s = _summarizer()
    articles = [
        _article("Wiley: Advanced Materials: Table of Contents", 1),
        _article("AAAS: Science Advances: Table of Contents", 2),
        _article("AAAS: Science: Table of Contents", 3),
        _article("arXiv", 4),
        # 以下仍必须被拦住
        _article("ScienceDirect", 5),
        _article("ScienceDirect Publication: Materials Today", 6),
        _article("Physics News", 7),
        _article("Editors' Suggestions", 8),
        _article("Phys. Rev. B", 9),
        _article("Machine Learning: Science and Technology", 10),
    ]

    buf = io.StringIO()
    with redirect_stdout(buf):
        kept = s.filter_articles(articles, WEEK_START, WEEK_END, "ferro")

    journals = {a["journal"] for a in kept}
    assert "Wiley: Advanced Materials: Table of Contents" in journals, journals
    assert "AAAS: Science Advances: Table of Contents" in journals, journals
    assert "AAAS: Science: Table of Contents" in journals, journals
    assert "arXiv" in journals, journals
    # 非期刊来源 / 非顶刊仍然过滤，且过滤有日志
    assert "ScienceDirect" not in journals, journals
    assert "ScienceDirect Publication: Materials Today" not in journals, journals
    assert "Physics News" not in journals, journals
    assert "Editors' Suggestions" not in journals, journals
    assert "Phys. Rev. B" not in journals, journals
    assert "Machine Learning: Science and Technology" not in journals, journals
    assert "期刊闸门过滤" in buf.getvalue()


def test_canonical_journal_keeps_real_names():
    """归一化只剥厂商前缀/栏目后缀，不能把正常期刊名洗坏或洗空。"""
    cj = WeeklySummarizer._canonical_journal
    assert cj("Wiley: Advanced Materials: Table of Contents") == "Advanced Materials"
    assert cj("AAAS: Science: Table of Contents") == "Science"
    assert cj("AAAS: Science Advances: Table of Contents") == "Science Advances"
    assert cj("The Journal of Chemical Physics Current Issue") == "The Journal of Chemical Physics"
    # 期刊名自带冒号（不是厂商前缀）不能被截断
    assert cj("Machine Learning: Science and Technology") == "Machine Learning: Science and Technology"
    assert cj("Nature") == "Nature"
    assert cj("Phys. Rev. B") == "Phys. Rev. B"
    assert cj("") == ""
    assert cj(None) == ""


def test_ai_judge_failure_keeps_keyword_matched_article():
    """AI 判断调用失败时，明确写了 machine learning 的文章不能被静默丢掉。"""
    s = _summarizer(provider=_BoomProvider())
    strict = _article(
        "arXiv", 1,
        text=("We train a machine learning interatomic potential for perovskite oxides "
              "and benchmark it against DFT reference data across many compositions."),
    )
    loose = _article(
        "arXiv", 2,
        text=("Quantum transport measurements in twisted bilayer graphene reveal an "
              "unconventional correlated insulating state at low temperature."),
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        kept = s.filter_articles([strict, loose], WEEK_START, WEEK_END, "ai")

    kept_ids = {a["id"] for a in kept}
    # 修复前：except -> return False，两篇都消失且没有任何日志
    assert "a1" in kept_ids, kept_ids
    # 仅靠宽松词（quantum）命中的边缘候选仍然落选，避免故障时 AI 版块被灌水
    assert "a2" not in kept_ids, kept_ids
    output = buf.getvalue()
    assert "AI判断失败" in output, output
    assert s._judge_failures >= 2, s._judge_failures


def test_focus_articles_ignore_journal_whitelist():
    """focus 命中的文章按周窗口直接取自原始池，不经顶刊闸门，并按相关度降序。"""
    s = _summarizer()
    pool = [
        {"id": "f1", "journal": "Phys. Rev. B", "pub_date": "2026-08-25",
         "focus_score": 0.72, "title": "PRB focus paper", "link": "https://example.com/f1"},
        {"id": "f2", "journal": "npj Computational Materials", "pub_date": "2026-08-26",
         "focus_score": 0.91, "title": "npj focus paper", "link": "https://example.com/f2"},
        {"id": "f3", "journal": "arXiv", "pub_date": "2026-09-10",
         "focus_score": 0.99, "title": "下一周的文章", "link": "https://example.com/f3"},
        {"id": "n1", "journal": "arXiv", "pub_date": "2026-08-25",
         "title": "没有 focus_score", "link": "https://example.com/n1"},
    ]

    got = s._collect_focus_articles(pool, WEEK_START, WEEK_END)

    assert [a["id"] for a in got] == ["f2", "f1"], [a["id"] for a in got]


def test_weekly_page_renders_gate_exempt_focus_articles():
    """周报页面的 🎯 区块用 summary['focus_articles'] 渲染，即使它不在 all_articles 里。"""
    plain = {
        "id": "plain-1",
        "title": "Plain arXiv paper",
        "title_zh": "普通 arXiv 文章",
        "journal": "arXiv",
        "pub_date": "2026-08-25",
        "link": "https://example.com/plain",
        "abstract": "Some abstract.",
        "is_ai": True,
    }
    focus_only = {
        "id": "focus-1",
        "title": "PRB focus paper",
        "title_zh": "PRB 里的方向相关论文",
        "journal": "Phys. Rev. B",
        "pub_date": "2026-08-26",
        "link": "https://example.com/focus",
        "focus_score": 0.88,
        "focus_summary": "用机器学习势模拟畴壁运动。",
        "focus_relation": "与我们的铁电畴壁工作直接相关。",
        "focus_suggestion": "可复用其训练集做迁移学习。",
    }
    summary = {
        "week_start": WEEK_START,
        "week_end": WEEK_END,
        "overview": "测试周报。",
        "generated_by": "test",
        "both_articles": [],
        "ferro_articles": [],
        "ai_articles": [plain],
        "all_articles": [plain],
        "by_journal": {"arXiv": [plain]},
        "focus_articles": [focus_only],
    }

    with TemporaryDirectory() as tmpdir:
        path = _summarizer().save_summary_html(summary, tmpdir)
        html = Path(path).read_text(encoding="utf-8")

    # 修复前：focus 列表只从 all_articles 推导，这篇 PRB 文章整块消失
    assert "与你方向相关" in html
    assert "PRB 里的方向相关论文" in html
    assert "与我们的铁电畴壁工作直接相关。" in html


def test_fallback_summary_buckets_by_actual_hits():
    """降级周报按 ferro/ai 命中集合分桶，'quantum' 不再让所有文章变成交叉研究。"""
    s = _summarizer()
    ferro = {
        "id": "x1",
        "title": "Ferroelectric switching in a quantum paraelectric",
        "abstract": "We study polarization reversal and domain wall motion in BaTiO3.",
        "journal": "Nature",
        "pub_date": "2026-08-25",
        "link": "https://example.com/x1",
    }
    ai = {
        "id": "x2",
        "title": "A machine learning interatomic potential",
        "abstract": "We train a neural network model on DFT data.",
        "journal": "arXiv",
        "pub_date": "2026-08-26",
        "link": "https://example.com/x2",
    }
    by_journal = {"Nature": [ferro], "arXiv": [ai]}

    out = s._fallback_summary([ferro, ai], [ferro], [ai], WEEK_START, WEEK_END, by_journal)

    # 修复前：ferro 篇因为标题里的 'quantum' 命中宽松 AI 词表 -> 被算成 both
    assert out["both_count"] == 0, out["both_count"]
    assert [a["id"] for a in out["ferro_articles"]] == ["x1"], out["ferro_articles"]
    assert [a["id"] for a in out["ai_articles"]] == ["x2"], out["ai_articles"]
    assert ferro["is_ferro"] is True and ferro["is_ai"] is False
    assert ai["is_ai"] is True and ai["is_ferro"] is False
    # 统计口径自洽：ferro_only + both == ferro_count
    assert len(out["ferro_articles"]) + out["both_count"] == out["ferro_count"]


def main() -> int:
    tests = [
        test_toc_feed_title_no_longer_kills_top_journals,
        test_canonical_journal_keeps_real_names,
        test_ai_judge_failure_keeps_keyword_matched_article,
        test_focus_articles_ignore_journal_whitelist,
        test_weekly_page_renders_gate_exempt_focus_articles,
        test_fallback_summary_buckets_by_actual_hits,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"✓ {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"✗ {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
