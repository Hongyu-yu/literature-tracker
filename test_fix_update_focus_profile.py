"""update_focus_profile 蒸馏失败不得摧毁既有画像的回归测试。

覆盖两类事故：
1. 分批蒸馏全部成功、最后一次"合并"调用失败 → 曾经直接返回空串，N 批结果全丢；
2. --distill-only 在无 key / 网关全挂 / 局部失败时无条件写盘 →
   曾经把 directions_zh、our_work_zh 清空、把精选关键词换成标题词频词。

无网络：provider 为假对象；_request_sleep 全部 monkeypatch 掉。
"""

import json
import os
import sys
import tempfile
from unittest import mock

import update_focus_profile as ufp


def _no_sleep(*a, **k):
    return None


class _FakeProvider:
    """按 prompt 内容决定成功/失败的假 provider（call_api 返回 JSON 文本）。"""

    def __init__(self, fail_when=None, directions="新蒸馏方向", keywords=None):
        self.fail_when = fail_when or (lambda prompt: False)
        self.directions = directions
        self.keywords = keywords if keywords is not None else ["new kw"]
        self.prompts = []

    def call_api(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if self.fail_when(prompt):
            raise RuntimeError("gateway 502")
        return json.dumps(
            {"directions_zh": self.directions, "keywords": list(self.keywords)},
            ensure_ascii=False,
        )


def _works(prefix, n):
    return [
        {"title": f"{prefix} paper {i}", "year": 2023, "venue": "PRB", "abstract": None}
        for i in range(n)
    ]


def _old_profile():
    return {
        "generated_at": "2026-07-29",
        "scholars": [
            {
                "scholar_id": "A1",
                "name": "Alpha Scholar",
                "works": _works("alpha ferroelectric", 30),  # 30 篇 → 2 批
                "directions_zh": "既有方向 A：第一性原理与铁电畴壁。",
            },
            {
                "scholar_id": "B1",
                "name": "Beta Scholar",
                "works": _works("beta magnon", 30),
                "directions_zh": "既有方向 B：自旋波与机器学习势。",
            },
        ],
        "our_work_zh": "既有团队画像：铁电、机器学习势、凝聚态计算。",
        "keywords": ["sliding ferroelectricity", "machine learning potential", "spin gnn"],
    }


def _write_old(profile=None):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "focus_interests.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile or _old_profile(), f, ensure_ascii=False, indent=2)
    return path


# ---------- 1. 合并调用失败不得丢弃分批结果 ----------

def test_merge_failure_keeps_batch_partials():
    """6 批成功 + 合并调用失败 → 降级拼接分批结果，而不是返回空串。"""
    provider = _FakeProvider(
        fail_when=lambda p: "分批归纳结果" in p,  # 只让最后的合并调用挂掉
        directions="分批小结文本",
    )
    with mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        out = ufp.distill_scholar_directions("Hongjun Xiang", _works("x", 150), provider)

    # 修复前：merged["directions_zh"] 为 ""，此处直接返回空串（本断言失败）
    assert out["directions_zh"], "合并失败时不得返回空 directions_zh"
    assert "分批小结文本" in out["directions_zh"]
    assert len(out["directions_zh"]) <= ufp.MERGE_FALLBACK_MAX_CHARS  # 降级文本必须截断
    assert out["keywords"] == ["new kw"]  # 分批关键词照常保留


def test_merge_success_path_unchanged():
    """成功路径必须与修复前完全一致：用合并结果，不掺分批拼接。"""
    provider = _FakeProvider(directions="合并后的总方向")
    with mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        out = ufp.distill_scholar_directions("Someone", _works("y", 60), provider)
    assert out["directions_zh"] == "合并后的总方向"
    assert out["keywords"] == ["new kw"]


# ---------- 2. --distill-only 不得覆盖既有画像 ----------

def test_distill_only_refuses_without_provider():
    """无 AI key（provider=None）时 --distill-only 无事可做，必须拒绝写盘。"""
    path = _write_old()
    with mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        out = ufp._distill_only(None, path)
    # 修复前：返回 directions_zh/our_work_zh 全空、关键词退化成标题词频的画像
    assert out is None, "无 provider 时必须返回 None（调用方 exit 1），不得产出空画像"


def test_distill_only_returns_none_when_all_llm_calls_fail():
    """网关全挂（每次调用都抛异常）→ 返回 None，既有画像文件原样不动。"""
    path = _write_old()
    before = open(path, encoding="utf-8").read()
    provider = _FakeProvider(fail_when=lambda p: True)
    with mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        out = ufp._distill_only(provider, path)
    assert out is None, "全部蒸馏失败时必须返回 None 让本次运行判失败"
    assert open(path, encoding="utf-8").read() == before  # _distill_only 本身不写盘


def test_distill_only_partial_failure_keeps_old_fields():
    """一位学者蒸馏失败 → 沿用其既有 directions_zh，且关键词不丢。"""
    path = _write_old()
    old = _old_profile()
    provider = _FakeProvider(
        # Beta 的每一次调用（分批 + 合并）都失败；Alpha 与团队汇总正常
        fail_when=lambda p: "beta magnon" in p or "既有方向 B" in p or "方向 B" in p,
        directions="新蒸馏方向",
        keywords=["fresh kw"],
    )
    with mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        out = ufp._distill_only(provider, path)

    assert out is not None
    by_name = {s["name"]: s for s in out["scholars"]}
    assert by_name["Alpha Scholar"]["directions_zh"] == "新蒸馏方向"
    # 修复前：Beta 的 directions_zh 被写成 ""，好数据被空串覆盖
    assert by_name["Beta Scholar"]["directions_zh"] == old["scholars"][1]["directions_zh"]
    assert by_name["Beta Scholar"]["works"] == old["scholars"][1]["works"]  # works 不受影响
    # 局部失败时关键词与旧画像取并集，不得把精选短语丢掉
    for kw in old["keywords"]:
        assert kw in out["keywords"]
    assert "fresh kw" in out["keywords"]
    assert out["our_work_zh"]


def test_distill_only_keeps_old_our_work_when_aggregate_fails():
    """学者蒸馏成功但团队汇总调用失败 → 沿用既有 our_work_zh，绝不写空串。"""
    path = _write_old()
    old = _old_profile()
    provider = _FakeProvider(
        fail_when=lambda p: "各位学者的研究方向" in p,  # 只让汇总调用挂掉
        directions="新蒸馏方向",
    )
    with mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        out = ufp._distill_only(provider, path)
    assert out is not None
    # 修复前：our_work_zh 被写成 ""，下游 focus prompt 的"我们的工作"整段消失
    assert out["our_work_zh"] == old["our_work_zh"]


def test_distill_only_keeps_curated_keywords_over_title_frequency():
    """蒸馏成功但模型没给关键词 → 沿用既有精选关键词，而不是退化成标题词频词。"""
    path = _write_old()
    old = _old_profile()
    provider = _FakeProvider(directions="新蒸馏方向", keywords=[])
    with mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        out = ufp._distill_only(provider, path)
    assert out is not None
    # 修复前：keywords 被 extract_keywords_from_works 的标题单词（如 "paper"）替换
    assert out["keywords"] == [k.lower() for k in old["keywords"]]
    assert "paper" not in out["keywords"]


def test_distill_only_success_path_replaces_fields():
    """全部成功时行为不变：新方向/新汇总/新关键词整体替换旧值。"""
    path = _write_old()
    provider = _FakeProvider(directions="新蒸馏方向", keywords=["fresh kw"])
    with mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        out = ufp._distill_only(provider, path)
    assert out is not None
    assert all(s["directions_zh"] == "新蒸馏方向" for s in out["scholars"])
    assert out["our_work_zh"] == "新蒸馏方向"
    assert out["keywords"] == ["fresh kw"]  # 成功路径不并入旧关键词
    assert set(out.keys()) == {"generated_at", "scholars", "our_work_zh", "keywords"}


# ---------- 3. main() 端到端：失败时不落盘、退出码非 0 ----------

def test_main_distill_only_without_key_does_not_touch_file():
    """无 AI_API_KEY 跑 --distill-only：exit 1，画像文件逐字节不变。"""
    path = _write_old()
    before = open(path, encoding="utf-8").read()
    argv = ["update_focus_profile.py", "--distill-only", "--output", path]
    env = {k: v for k, v in os.environ.items() if k != "AI_API_KEY"}
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.dict(os.environ, env, clear=True), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep), \
         mock.patch.object(ufp, "save_profile",
                           side_effect=AssertionError("失败时绝不能写盘")):
        rc = ufp.main()
    # 修复前：rc == 0，且 save_profile 被调用（用空画像覆盖并被 workflow 推上 main）
    assert rc == 1
    assert open(path, encoding="utf-8").read() == before


# ---------- 4. 全量重建(build_profile/main)同样不得摧毁既有画像 ----------
#
# --distill-only 之外还有本地全量重建这条路：Scholar 封 IP 时 scrape_scholar_works
# 返回 []、OpenAlex/S2/arXiv 同时抽风时 backfill_abstract 全返回 None，
# 曾经会把 364 篇论文清单 + 272 条摘要 + 5 段方向 + 205 个精选关键词一次性写空。


def _full_old_profile():
    """与 ufp.SCHOLARS 对齐的既有画像（每位学者 3 篇论文，其中 2 篇有摘要）。"""
    scholars = []
    for i, s in enumerate(ufp.SCHOLARS):
        works = _works(f"ferroelectric topic {i}", 3)
        works[0]["abstract"] = f"既有摘要 {i}-0"
        works[1]["abstract"] = f"既有摘要 {i}-1"
        scholars.append(
            {
                "scholar_id": s["scholar_id"],
                "name": s["name"],
                "works": works,
                "directions_zh": f"既有方向 {i}：滑移铁电与机器学习势。",
            }
        )
    return {
        "generated_at": "2026-07-30",
        "scholars": scholars,
        "our_work_zh": "既有团队画像：第一性原理 + 机器学习势 + 铁电/磁性材料。",
        "keywords": ["sliding ferroelectricity", "spin gnn", "machine learning potential"],
    }


def test_build_profile_keeps_works_when_scrape_blocked():
    """Scholar 全部抓空（封 IP）→ 沿用既有论文清单，不得写出 works: []。"""
    path = _write_old(_full_old_profile())
    preserved = []
    with mock.patch.object(ufp, "scrape_scholar_works", return_value=[]), \
         mock.patch.object(ufp, "backfill_abstract",
                           side_effect=AssertionError("沿用既有清单时不该重抓摘要")), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        profile = ufp.build_profile(provider=None, old_path=path, preserved=preserved)

    # 修复前：每位学者 works=[]、directions_zh=""、our_work_zh=""，整份画像被清空
    assert [len(s["works"]) for s in profile["scholars"]] == [3] * len(ufp.SCHOLARS)
    assert profile["scholars"][0]["works"][0]["abstract"] == "既有摘要 0-0"
    assert profile["scholars"][0]["directions_zh"] == "既有方向 0：滑移铁电与机器学习势。"
    assert profile["our_work_zh"] == _full_old_profile()["our_work_zh"]
    assert preserved, "降级内容必须记录在 preserved 里（调用方据此判失败）"


def test_build_profile_restores_abstracts_when_backfill_fails():
    """抓取正常但摘要回填全失败 → 按标题沿用既有摘要，不写 null 覆盖。"""
    old = _full_old_profile()
    path = _write_old(old)
    fresh = [dict(w, abstract=None) for w in old["scholars"][0]["works"]]
    with mock.patch.object(ufp, "scrape_scholar_works",
                           side_effect=lambda *a, **k: [dict(w) for w in fresh]), \
         mock.patch.object(ufp, "backfill_abstract", return_value=None), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        profile = ufp.build_profile(provider=None, old_path=path)

    got = profile["scholars"][0]["works"]
    # 修复前：三篇 abstract 全被写成 None，既有的 2 条摘要永久丢失
    assert got[0]["abstract"] == "既有摘要 0-0"
    assert got[1]["abstract"] == "既有摘要 0-1"
    assert got[2]["abstract"] is None  # 旧画像里本就没有的，仍如实记 null


def test_build_profile_keeps_curated_keywords_and_our_work():
    """无 provider 的重建 → 既有 our_work_zh / 精选关键词照旧，不被标题词频词替换。"""
    old = _full_old_profile()
    path = _write_old(old)
    with mock.patch.object(ufp, "scrape_scholar_works",
                           return_value=_works("ferroelectric domain", 2)), \
         mock.patch.object(ufp, "backfill_abstract", return_value="新摘要"), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        profile = ufp.build_profile(provider=None, old_path=path)

    # 修复前：our_work_zh 被写成 ""、keywords 变成 ["ferroelectric", "domain", "paper"...]
    assert profile["our_work_zh"] == old["our_work_zh"]
    for kw in old["keywords"]:
        assert kw in profile["keywords"]
    assert "paper" not in profile["keywords"]
    assert all(s["directions_zh"] for s in profile["scholars"])


def test_build_profile_success_path_replaces_old_values():
    """抓取+蒸馏全部成功 → 一律用新值，既不掺旧方向也不并旧关键词，preserved 为空。"""
    path = _write_old(_full_old_profile())
    provider = _FakeProvider(directions="新蒸馏方向", keywords=["fresh kw"])
    preserved = []
    with mock.patch.object(ufp, "scrape_scholar_works",
                           return_value=_works("new topic", 2)), \
         mock.patch.object(ufp, "backfill_abstract", return_value="新摘要"), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        profile = ufp.build_profile(provider=provider, old_path=path, preserved=preserved)

    assert all(s["directions_zh"] == "新蒸馏方向" for s in profile["scholars"])
    assert all(w["abstract"] == "新摘要" for s in profile["scholars"] for w in s["works"])
    assert profile["our_work_zh"] == "新蒸馏方向"
    assert profile["keywords"] == ["fresh kw"]  # 成功路径不并入旧关键词
    assert preserved == []
    assert set(profile.keys()) == {"generated_at", "scholars", "our_work_zh", "keywords"}


def test_build_profile_without_old_path_behaves_as_before():
    """不传 old_path（旧调用签名）时行为与修复前完全一致：不去读任何既有画像。"""
    with mock.patch.object(ufp, "scrape_scholar_works", return_value=[]), \
         mock.patch.object(ufp, "backfill_abstract", return_value=None), \
         mock.patch.object(ufp, "_load_existing_profile",
                           side_effect=AssertionError("未给 old_path 时不得读画像文件")), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        profile = ufp.build_profile(provider=None)
    assert profile["our_work_zh"] == ""
    assert profile["keywords"] == []
    assert all(s["works"] == [] for s in profile["scholars"])


def test_main_rebuild_preserves_file_and_exits_nonzero():
    """端到端：无 key + Scholar 抓空 → 文件里的论文/方向/关键词一个不少，且 exit 1。"""
    old = _full_old_profile()
    path = _write_old(old)
    argv = ["update_focus_profile.py", "--output", path]
    env = {k: v for k, v in os.environ.items() if k != "AI_API_KEY"}
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.dict(os.environ, env, clear=True), \
         mock.patch.object(ufp, "scrape_scholar_works", return_value=[]), \
         mock.patch.object(ufp, "backfill_abstract", return_value=None), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        rc = ufp.main()

    written = json.load(open(path, encoding="utf-8"))
    # 修复前：文件被写成 5 位学者全空（works=[]、directions_zh=""、our_work_zh=""、
    # keywords 退化成标题词频词），且 rc == 0 顶着 ✅ 绿勾
    assert sum(len(s["works"]) for s in written["scholars"]) == 3 * len(ufp.SCHOLARS)
    assert all(s["directions_zh"] for s in written["scholars"])
    assert written["our_work_zh"] == old["our_work_zh"]
    for kw in old["keywords"]:
        assert kw in written["keywords"]
    assert rc == 1, "降级重建必须以非 0 退出码暴露，不能顶着 ✅ 绿勾"


def test_main_rebuild_refuses_to_write_all_empty_profile():
    """既无既有画像又一篇都没抓到 → 拒绝落盘，exit 1（不留下空壳画像）。"""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "focus_interests.json")  # 文件不存在
    argv = ["update_focus_profile.py", "--output", path]
    env = {k: v for k, v in os.environ.items() if k != "AI_API_KEY"}
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.dict(os.environ, env, clear=True), \
         mock.patch.object(ufp, "scrape_scholar_works", return_value=[]), \
         mock.patch.object(ufp, "backfill_abstract", return_value=None), \
         mock.patch.object(ufp, "save_profile",
                           side_effect=AssertionError("0 篇时绝不能写盘")), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        rc = ufp.main()
    assert rc == 1
    assert not os.path.exists(path)


def test_save_profile_is_atomic_on_serialization_failure():
    """序列化中途抛错 → 既有画像逐字节不变，也不留 .tmp 垃圾。"""
    path = _write_old(_full_old_profile())
    before = open(path, encoding="utf-8").read()
    bad = {"generated_at": "2026-09-01", "scholars": [], "our_work_zh": "x",
           "keywords": [set()]}  # set 不可 JSON 序列化 → dump 写到一半抛 TypeError
    try:
        ufp.save_profile(bad, path)
        raise AssertionError("不可序列化的画像必须抛出，不能静默吞掉")
    except TypeError:
        pass
    # 修复前：open(path,"w") 已截断并写入半个 JSON,既有画像当场损坏
    assert open(path, encoding="utf-8").read() == before
    assert not os.path.exists(path + ".tmp")


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] test_fix_update_focus_profile")
