"""run_deep 修复回归：APS 侧的重试封顶（预算不再被永久失败的论文吃光）+ 死代码清理。

背景：tier-2 早就有 `_tier2_complete` 的 ft_attempts 封顶，APS 侧却没有。
`_deep_complete` 要求深读 ≥5000 字，所以任何深读失败/被截断的 APS 论文，只要日期还在
DEEP_WINDOW_DAYS 窗口里，就每轮都被判成 fresh：重抓全文、重传最多 4 万字给 gpt-5.5，
还占着 DEEP_MAX_NEW_PER_RUN 的名额，把真正的新论文挤到下一轮。

约定：stdlib only（unittest.mock + tempfile），模块级无参 test_*，run_tests.py 才跑得到。
"""
import os
import tempfile
from unittest import mock

import run_deep


# ---------------------------------------------------------------- 测试替身

class _FakeProvider:
    """分类 prompt 回类别名；其余(深读)回一段完整苏格拉底文本并计数。"""

    def __init__(self):
        self.deep_calls = 0

    def call_api(self, prompt):
        if "归入且仅归入" in prompt:          # auto_classifier.classify 的兜底 prompt
            return "AI×物理"
        self.deep_calls += 1
        return "## 精读\n第五部分：创新评估 " + "x" * 6000


def _meta(doc_id, title):
    return {"title": title, "has_full_text": True, "markdown_oss_key": "k",
            "doc_id": doc_id, "summary": "s"}


def _stuck_cache_rec(doc_id, attempts=3):
    """深读永久失败(OSS 对象 404 / provider 持续报错)、已重试到封顶的缓存行。"""
    return {"doc_id": doc_id, "source": "APS", "deep_analysis": "",
            "category": "AI×物理", "poster": None, "deep_attempts": attempts}


# ---------------------------------------------------------------- 单元：封顶谓词

def test_aps_complete_caps_retries_after_three_attempts():
    """✅ _aps_complete：完整深读直接算完；否则 3 次后无条件定稿。"""
    assert run_deep._aps_complete(None) is False
    assert run_deep._aps_complete({}) is False
    # 完整深读 → 与尝试次数无关，永远算完成
    assert run_deep._aps_complete({"deep_analysis": "第五部分：创新评估 " + "x" * 6000}) is True
    # 深读为空：未到封顶继续重试，到封顶就定稿
    assert run_deep._aps_complete({"deep_analysis": "", "deep_attempts": 2}) is False
    assert run_deep._aps_complete({"deep_analysis": "", "deep_attempts": 3}) is True
    # 截断/缺「创新」的文本同理（这类记录用 5000 字门槛永远判不完成）
    assert run_deep._aps_complete({"deep_analysis": "截断在这里", "deep_attempts": 5}) is True
    # 老缓存没有 deep_attempts 字段 → 按 0 次算，行为与改动前一致(仍会重试)
    assert run_deep._aps_complete({"deep_analysis": "截断在这里"}) is False
    # 缓存被手改坏也不许抛异常炸掉整轮 APS（process_date 的分流循环没有 try 包着）
    assert run_deep._aps_complete({"deep_analysis": "", "deep_attempts": "坏值"}) is False


# ---------------------------------------------------------------- 计数落盘

def test_enrich_one_carries_and_increments_deep_attempts():
    """✅ deep_attempts 必须从缓存搬进新记录再 +1，否则永远停在 1、封顶形同虚设。"""
    class C:
        def fetch_markdown(self, m):
            return "# Paper\nbody"

    prov = _FakeProvider()
    with mock.patch.object(run_deep, "generate_poster", return_value=None):
        first = run_deep._enrich_one(_meta("d1", "P"), C(), prov, "unused")
        assert first["deep_attempts"] == 1
        # rec = dict(meta)，而 metadata.jsonl 里没有 deep_attempts —— 不显式搬就丢了
        again = run_deep._enrich_one(_meta("d1", "P"), C(), prov, "unused",
                                     cached={"deep_analysis": "太短", "deep_attempts": 2})
        assert again["deep_attempts"] == 3
    # 坏值不该炸，退回从 1 重新数
    with mock.patch.object(run_deep, "generate_poster", return_value=None):
        bad = run_deep._enrich_one(_meta("d2", "P"), C(), _FakeProvider(), "unused",
                                   cached={"deep_analysis": "太短", "deep_attempts": None})
        assert bad["deep_attempts"] == 1


# ---------------------------------------------------------------- 集成：不再重处理

def test_process_date_settles_capped_aps_paper_without_reprocessing():
    """✅ 封顶后的论文既不重抓全文也不重调 provider，且原样留在输出里。"""
    metas = [_meta("d1", "永远抓不到全文的论文")]

    class BoomClient:
        def fetch_metadata(self, d):
            return metas

        def fetch_markdown(self, m):
            raise AssertionError("封顶后的论文不该再抓全文")

    class BoomProvider:
        def call_api(self, p):
            raise AssertionError("封顶后的论文不该再调用 provider")

    cache = {"d1": _stuck_cache_rec("d1")}
    out, used = run_deep.process_date("2026-05-28", client=BoomClient(),
                                      provider=BoomProvider(), cache=cache)
    # 改前：_deep_complete("") 为假 → 走 fresh → used=1，且 _enrich_one 抛错被
    # process_date 的 except 吞掉，记录直接从输出里消失(len(out)==0)。
    assert used == 0, f"封顶后仍被当成 fresh 重处理: used={used}"
    assert len(out) == 1
    assert out[0]["doc_id"] == "d1"
    assert out[0]["deep_attempts"] == 3


def test_capped_aps_papers_no_longer_starve_the_new_paper_budget():
    """✅ 核心危害回归：卡死的论文不该再占着 max_new 名额把新论文挤出这一轮。"""
    stuck = [_meta("s%d" % i, "Stuck %d" % i) for i in range(3)]
    brand_new = [_meta("n%d" % i, "New %d" % i) for i in range(2)]
    metas = stuck + brand_new          # 卡住的排在前面 → 预算截断时首当其冲

    class C:
        def fetch_metadata(self, d):
            return metas

        def fetch_markdown(self, m):
            return "# Paper\nbody"

    cache = {("s%d" % i): _stuck_cache_rec("s%d" % i) for i in range(3)}
    prov = _FakeProvider()
    with mock.patch.object(run_deep, "generate_poster", return_value=None):
        out, used = run_deep.process_date("2026-05-28", client=C(), provider=prov,
                                          cache=cache, max_new=2)

    got = {r.get("doc_id"): (r.get("deep_analysis") or "") for r in out}
    # 改前：5 篇全判 fresh → fresh[:2] 只留 s0/s1 → 输出里根本没有 n0/n1，
    # 两格预算全烧在注定失败的 s0/s1 上，且下一轮还会重来一遍。
    assert set(got) == {"s0", "s1", "s2", "n0", "n1"}, f"输出缺条目: {sorted(got)}"
    assert got["n0"] and got["n1"], "新论文被封顶前的僵尸论文挤出了预算"
    assert used == 2
    assert prov.deep_calls == 2, f"只该为两篇新论文各深读一次，实际 {prov.deep_calls} 次"
    # 定稿的记录原样保留，绝不被抹空
    assert all(got[k] == "" for k in ("s0", "s1", "s2"))


def test_incomplete_aps_paper_below_cap_is_still_retried():
    """✅ 反向保证：没到封顶的截断深读仍要重试（封顶不能顺手把重试也关掉）。"""
    metas = [_meta("d1", "截断了的论文")]

    class C:
        def fetch_metadata(self, d):
            return metas

        def fetch_markdown(self, m):
            return "# Paper\nbody"

    cache = {"d1": {"doc_id": "d1", "source": "APS", "deep_analysis": "## 截断在这里",
                    "category": "AI×物理", "poster": None, "deep_attempts": 1}}
    prov = _FakeProvider()
    with mock.patch.object(run_deep, "generate_poster", return_value=None):
        out, used = run_deep.process_date("2026-05-28", client=C(), provider=prov,
                                          cache=cache, max_new=5)
    assert used == 1
    assert "创新" in out[0]["deep_analysis"]
    assert out[0]["deep_attempts"] == 2


def test_deep_attempts_survives_the_aps_index_round_trip():
    """✅ 计数要能落盘再读回来，否则每轮重新从 0 数、封顶永远够不着。"""
    cwd = os.getcwd()
    d = tempfile.mkdtemp()
    try:
        os.chdir(d)
        run_deep._save_aps_index("2026-05-28", [_stuck_cache_rec("d1", attempts=2)])
        back = run_deep._load_aps_cache("2026-05-28")
    finally:
        os.chdir(cwd)
    assert back["d1"]["deep_attempts"] == 2
    assert run_deep._aps_complete(back["d1"]) is False
    back["d1"]["deep_attempts"] = 3
    assert run_deep._aps_complete(back["d1"]) is True


# ---------------------------------------------------------------- 死代码

def test_no_duplicate_arxiv_core_loader():
    """✅ _load_arxiv_core 是 _load_core_cache 的逐字节副本且无调用方，必须只剩一份。"""
    assert not hasattr(run_deep, "_load_arxiv_core"), \
        "重复的 arxiv_core 读入口又回来了：两份实现只会各自漂移"
    assert run_deep._load_core_cache("2099-01-01") == []


if __name__ == "__main__":
    import sys
    fails = 0
    for name in sorted(n for n in dir() if n.startswith("test_")):
        fn = globals()[name]
        try:
            fn()
            print(f"✓ {name}")
        except Exception as e:
            fails += 1
            print(f"✗ {name}: {e}")
            import traceback
            traceback.print_exc()
    print(f"\n{fails} failed")
    sys.exit(1 if fails else 0)
