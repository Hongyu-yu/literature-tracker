#!/usr/bin/env python3
"""weekly_summary.py 回归测试（无网络）：

1. feed 标题里的 'Table of Contents' 不再把 Adv. Mater./Science Advances 整批误杀；
2. AI 相关性判断调用失败时不再静默把文章判成「不相关」丢掉；
3. 🎯 与你方向相关 区块不受顶刊闸门约束（与日报口径一致）；
4. 降级周报按实际命中集合分桶，不再用宽松词表把所有文章判成「交叉研究」；
5. 周报 prompt 只索要页面真正会渲染的三段，不再生成后整块丢掉（会截断 trends/outlook）；
6. 默认周窗口 = 上一个已完成周，不再把「运行当天」算进窗口导致当周尾巴永久缺席；
7. 回填历史周时 index.json 已被截断 → 回退长期库 ai_relevant.json；两个源都没有则说清楚；
8. 页脚/索引时间戳用北京时间，不再显示成 8 小时前。
"""

import io
import json
import os
import re
import sys
import types
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


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


class _CapturingProvider:
    """记录 prompt 并返回固定 JSON 的假 provider。"""

    def __init__(self, response):
        self.response = response
        self.prompts = []

    def call_api(self, prompt):
        self.prompts.append(prompt)
        return self.response


def test_weekly_prompt_only_asks_for_rendered_fields():
    """周报 prompt 不再索要 article_summaries/highlights/by_topic。

    这三块以前每周都真金白银地生成（实测一周 366 条一句话总结），解析完直接丢掉：
    没有任何渲染路径读它们，却排在 JSON 前面挤占 AI_MAX_TOKENS，
    把排在末尾、真正上页面的 trends/outlook 截断。
    """
    provider = _CapturingProvider(
        json.dumps({"overview": "概览句", "trends": "趋势句", "outlook": "展望句"},
                   ensure_ascii=False)
    )
    s = _summarizer(provider=provider)
    arts = [_article("Nature", 1)]

    buf = io.StringIO()
    with redirect_stdout(buf):
        out = s._generate_ai_summary(arts, arts, [], WEEK_START, WEEK_END, {"Nature": arts})

    prompt = provider.prompts[0]
    for dead in ("article_summaries", "highlights", "by_topic", "one_sentence", "significance"):
        assert dead not in prompt, f"prompt 仍在索要被丢弃的字段: {dead}"
    for kept in ('"overview"', '"trends"', '"outlook"'):
        assert kept in prompt, f"prompt 缺少页面真正渲染的字段: {kept}"

    assert out["overview"] == "概览句"
    assert out["trends"] == "趋势句"
    assert out["outlook"] == "展望句"
    for dead in ("article_summaries", "highlights", "by_topic"):
        assert dead not in out, f"summary 里仍留着没人读的 {dead}"


def test_dead_weekly_renderers_are_gone():
    """两个从未被调用过的 HTML 渲染方法已删除（250 行死代码，且其中一个不做任何转义）。"""
    for name in ("_generate_all_articles_section", "_generate_overview_article_list"):
        assert not hasattr(WeeklySummarizer, name), f"{name} 是死代码，不应再存在"


class _FakeNow(datetime):
    """固定"现在"：UTC 2026-08-30 16:30 == 北京 2026-08-31 00:30（周一）。"""

    _INSTANT = datetime(2026, 8, 30, 16, 30)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._INSTANT  # 裸 now()：runner 上就是 UTC
        return cls._INSTANT.replace(tzinfo=timezone.utc).astimezone(tz)


def test_default_week_start_is_previous_completed_week():
    """默认周窗口必须是上一个已完成周，且按北京日历日算。

    旧实现取「本周一」，窗口最后一天就是任务运行当天——那天的文献 fetch 还没写进
    index.json，周报此后也不会重新生成，当周尾巴永久缺席。
    """
    ds = weekly_summary.default_week_start

    # 周日 09:00 北京 = 现行 cron 的实际触发时刻：应生成 08-17~08-23（已抓完），
    # 而不是还差最后一天的 08-24~08-30
    assert ds(datetime(2026, 8, 30, 9, 0, tzinfo=weekly_summary.BEIJING_TZ)) == "2026-08-17"

    # 任何一天跑，窗口都必须是周一开始、且整段严格早于「今天」
    for day in range(0, 7):
        now = datetime(2026, 8, 24, 12, 0, tzinfo=weekly_summary.BEIJING_TZ) + timedelta(days=day)
        start = datetime.strptime(ds(now), "%Y-%m-%d")
        assert start.weekday() == 0, (ds(now), now)
        assert (start + timedelta(days=6)).date() < now.date(), (ds(now), now)

    # 时区敏感点：北京周一 00:30 = UTC 周日 16:30，两者会选中不同的周
    with mock.patch.object(weekly_summary, "datetime", _FakeNow):
        assert weekly_summary.default_week_start() == "2026-08-24", "默认必须用北京时间"
    assert ds(datetime(2026, 8, 30, 16, 30)) == "2026-08-17", "裸 UTC 会差一周（对照组）"


def _minimal_summary():
    article = {
        "id": "p1", "title": "Paper", "title_zh": "论文", "journal": "Nature",
        "pub_date": "2026-08-25", "link": "https://example.com/p1", "abstract": "abs",
    }
    return {
        "week_start": WEEK_START, "week_end": WEEK_END, "overview": "概览",
        "generated_by": "test", "both_articles": [], "ferro_articles": [article],
        "ai_articles": [], "all_articles": [article], "by_journal": {"Nature": [article]},
    }


def test_footer_timestamp_is_beijing_time():
    """页脚「更新时间」必须是带标注的北京时间：runner 是 UTC，裸 now() 会显示成 8 小时前。"""
    buf = io.StringIO()
    with TemporaryDirectory() as tmpdir:
        with redirect_stdout(buf):
            path = _summarizer().save_summary_html(_minimal_summary(), tmpdir)
        page = Path(path).read_text(encoding="utf-8")

    assert "（北京时间）" in page, "时间戳没有标注时区"
    m = re.search(r"更新时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", page)
    assert m, "页脚缺少更新时间"
    shown = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
    now_bj = datetime.now(weekly_summary.BEIJING_TZ).replace(tzinfo=None)
    assert abs((now_bj - shown).total_seconds()) < 300, f"显示 {shown}，北京时间 {now_bj}"


def test_weekly_index_updated_timestamp_is_offset_aware():
    """docs/weekly/index.json 的 updated 必须带 +08:00，不再是无时区的裸 UTC。"""
    with TemporaryDirectory() as tmpdir:
        Path(tmpdir, "2026-08-24.html").write_text("<html></html>", encoding="utf-8")
        n = weekly_summary._write_weekly_index_file(tmpdir)
        data = json.loads(Path(tmpdir, "index.json").read_text(encoding="utf-8"))

    assert n == 1
    updated = data["updated"]
    assert datetime.fromisoformat(updated).utcoffset() == timedelta(hours=8), updated


class _FakeSummarizer:
    """替身 WeeklySummarizer：不建 provider、不发请求，只记录拿到了哪些当周文献。"""

    instances = []

    def __init__(self, api_key=None):
        self.received = []
        _FakeSummarizer.instances.append(self)

    def generate_weekly_summary(self, articles, week_start):
        week_end = (datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
        self.received = [a for a in articles if week_start <= (a.get("pub_date") or "") <= week_end]
        return {"week_start": week_start, "week_end": week_end, "total": len(self.received)}

    def save_summary_html(self, summary, output_dir="docs/weekly"):
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"{summary['week_start']}.html")
        Path(path).write_text("<html></html>", encoding="utf-8")
        return path


def _write_data(tmpdir, index_articles, relevant_articles):
    os.makedirs(os.path.join(tmpdir, "data"), exist_ok=True)
    with open(os.path.join(tmpdir, "data", "index.json"), "w", encoding="utf-8") as f:
        json.dump({"articles": index_articles}, f, ensure_ascii=False)
    with open(os.path.join(tmpdir, "data", "ai_relevant.json"), "w", encoding="utf-8") as f:
        json.dump(relevant_articles, f, ensure_ascii=False)


def test_old_week_falls_back_to_long_term_archive():
    """index.json 被截断到最近 30 天后，回填历史周要回退到长期库 ai_relevant.json。

    修复前：窗口内 0 篇 → total 0 → 打一句 📭 → 不写页面、退出码 0，
    整轮 backfill 全绿却一个页面都没生成。
    """
    recent = _article("Nature", 1)                       # pub_date 2026-08-25
    old = _article("Nature", 9)
    old["pub_date"] = "2026-01-07"                       # 落在被请求的那一周内
    _FakeSummarizer.instances = []

    buf = io.StringIO()
    cwd = os.getcwd()
    with TemporaryDirectory() as tmpdir:
        _write_data(tmpdir, [recent], [old])
        try:
            os.chdir(tmpdir)
            with mock.patch.object(weekly_summary, "WeeklySummarizer", _FakeSummarizer):
                with redirect_stdout(buf):
                    out = weekly_summary.generate_weekly_summary("2026-01-05")
        finally:
            os.chdir(cwd)

    assert out is not None, "回退到长期库后应当生成页面"
    assert [a["id"] for a in _FakeSummarizer.instances[-1].received] == ["a9"]
    assert "ai_relevant.json" in buf.getvalue(), buf.getvalue()


def test_week_outside_data_window_is_loud_and_exits_3():
    """两个数据源都不含这一周时：说清楚原因，回填用退出码 3，定时任务仍然 0。"""
    ancient = _article("Nature", 1)
    ancient["pub_date"] = "2020-01-01"                   # 任何默认周窗口都覆盖不到
    buf = io.StringIO()
    cwd = os.getcwd()
    with TemporaryDirectory() as tmpdir:
        _write_data(tmpdir, [ancient], [])
        try:
            os.chdir(tmpdir)
            with redirect_stdout(buf):
                # 默认 fail-soft：返回 None，不抛
                assert weekly_summary.generate_weekly_summary("2026-01-05") is None
                # 回填模式：抛可区分的异常
                try:
                    weekly_summary.generate_weekly_summary("2026-01-05", strict_window=True)
                    raised = False
                except weekly_summary.WeekOutsideDataWindow:
                    raised = True
                # CLI：显式指定周 → 3；定时任务（无参数）→ 0，绝不能连带跳过 commit/deploy
                rc_backfill = weekly_summary.main(["2026-01-05"])
                rc_scheduled = weekly_summary.main([])
        finally:
            os.chdir(cwd)

    assert raised, "strict_window 下应抛 WeekOutsideDataWindow"
    assert rc_backfill == 3, rc_backfill
    assert rc_scheduled == 0, rc_scheduled
    output = buf.getvalue()
    assert "留存数据窗口之外" in output, output
    assert "📭" not in output, "不能再谎称「本周没有符合条件的文献」"


def main() -> int:
    tests = [
        test_toc_feed_title_no_longer_kills_top_journals,
        test_canonical_journal_keeps_real_names,
        test_ai_judge_failure_keeps_keyword_matched_article,
        test_focus_articles_ignore_journal_whitelist,
        test_weekly_page_renders_gate_exempt_focus_articles,
        test_fallback_summary_buckets_by_actual_hits,
        test_weekly_prompt_only_asks_for_rendered_fields,
        test_dead_weekly_renderers_are_gone,
        test_default_week_start_is_previous_completed_week,
        test_footer_timestamp_is_beijing_time,
        test_weekly_index_updated_timestamp_is_offset_aware,
        test_old_week_falls_back_to_long_term_archive,
        test_week_outside_data_window_is_loud_and_exits_3,
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


# ---------------------------------------------------------------- 交叉优先

def test_weekly_orders_crossover_before_single_side_hits():
    """周报的 category='all' 此前是 ferro OR ai：纯物理与纯 AI 靠单侧就能排前面。

    改为交叉优先——两侧都命中的排前面，单侧命中的保留但靠后（不丢内容）。
    """
    s = _summarizer()
    # 用 _loose_matches_ferro_keywords 确实命中的纯物理条目（kagome/superconduct
    # 都不在那份宽松词表里，用它当 fixture 会连候选都进不去 —— 那样断言就落空了）
    pure_physics = _article("arXiv", 101, text="antiferromagnetic spin order in a kagome metal")
    pure_physics["title"] = "Antiferromagnetic spin order in a kagome metal"
    crossover = _article("arXiv", 102, text="ferroelectric domain wall switching in BaTiO3")
    crossover["title"] = "Machine learning interatomic potential for ferroelectric perovskites"

    buf = io.StringIO()
    with redirect_stdout(buf):
        kept = s.filter_articles([pure_physics, crossover], WEEK_START, WEEK_END, "all")

    links = [a["link"] for a in kept]
    assert crossover["link"] in links and pure_physics["link"] in links, links
    assert links.index(crossover["link"]) < links.index(pure_physics["link"]), links
    assert "AI×科学交叉 1 篇" in buf.getvalue()


def test_weekly_skips_ai_judge_for_title_level_crossover():
    """标题里明写 machine learning 的论文不必再花一次往返问"是不是 AI 相关"。"""
    class _CountingProvider:
        def __init__(self):
            self.calls = 0

        def call_api(self, prompt):
            self.calls += 1
            return "是"

    provider = _CountingProvider()
    s = _summarizer(provider)
    obvious = _article("arXiv", 103, text="We train an equivariant graph neural network on "
                                          "density functional theory data for ferroelectric "
                                          "perovskites and report force errors below 20 meV/A. " * 2)
    obvious["title"] = "Machine learning interatomic potential for ferroelectric perovskites"

    buf = io.StringIO()
    with redirect_stdout(buf):
        kept = s.filter_articles([obvious], WEEK_START, WEEK_END, "ai")

    assert [a["link"] for a in kept] == [obvious["link"]]
    assert provider.calls == 0, f"标题级交叉不该再问 AI 判定，实际调用 {provider.calls} 次"
    assert "跳过 AI 判定" in buf.getvalue()
