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


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] test_fix_update_focus_profile")
