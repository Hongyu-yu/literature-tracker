#!/usr/bin/env python3
"""arxiv_core_<date>.json 必须合并写，不能整表覆盖。

原问题(3 个独立审计各自发现)：run_deep 每轮只处理 tier2 候选，却用整表覆盖写回
data/arxiv_core_<date>.json，于是 backfill_top_posters 写进去的 source=top_poster 行、
以及本轮预算(DEEP_MAX_NEW_PER_RUN)之外的历史行，全部被删掉 —— 海报白生成了。
"""

import json
import os
import shutil
import tempfile

import run_deep


def _rows():
    return [
        {"link": "https://arxiv.org/abs/1111.1111", "title": "tier2 paper",
         "deep_analysis": "旧的完整深读" * 20, "image": "images/posters/a.webp", "source": "arxiv"},
        {"link": "https://arxiv.org/abs/2222.2222", "title": "top poster paper",
         "image": "images/posters/b.webp", "poster_elements": {"k": "v"}, "source": "top_poster"},
    ]


def _write(tmp, rows, date="2026-08-30"):
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
    p = os.path.join(tmp, "data", f"arxiv_core_{date}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    return p


def test_merge_keeps_rows_that_are_not_in_this_round():
    """本轮只产出 tier2 那一行，另一行(top_poster)必须原样留下。"""
    tmp = tempfile.mkdtemp()
    try:
        p = _write(tmp, _rows())
        run_deep._save_core_merged("2026-08-30", [
            {"link": "https://arxiv.org/abs/1111.1111", "title": "tier2 paper",
             "deep_analysis": "新的深读" * 30, "source": "arxiv"},
        ], path=p)
        out = json.load(open(p, encoding="utf-8"))
        keys = {r.get("link") for r in out}
        assert "https://arxiv.org/abs/2222.2222" in keys, "top_poster 行被删掉了"
        assert len(out) == 2
        tp = [r for r in out if r["link"].endswith("2222.2222")][0]
        assert tp["image"] == "images/posters/b.webp"
        assert tp["poster_elements"] == {"k": "v"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_merge_never_blanks_existing_fields_with_empty_values():
    """本轮某字段失败返回空(None/""/{})时，不得覆盖上一轮的好数据。"""
    tmp = tempfile.mkdtemp()
    try:
        p = _write(tmp, _rows())
        run_deep._save_core_merged("2026-08-30", [
            {"link": "https://arxiv.org/abs/1111.1111", "title": "tier2 paper",
             "deep_analysis": "", "image": None, "poster_elements": {}},
        ], path=p)
        r = [x for x in json.load(open(p, encoding="utf-8")) if x["link"].endswith("1111.1111")][0]
        assert r["deep_analysis"].startswith("旧的完整深读"), "空串抹掉了已有深读"
        assert r["image"] == "images/posters/a.webp", "None 抹掉了已有配图"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_merge_appends_new_rows_and_updates_real_values():
    tmp = tempfile.mkdtemp()
    try:
        p = _write(tmp, _rows())
        run_deep._save_core_merged("2026-08-30", [
            {"link": "https://arxiv.org/abs/3333.3333", "title": "new paper", "deep_analysis": "新"},
            {"link": "https://arxiv.org/abs/1111.1111", "deep_analysis": "升级后的深读"},
        ], path=p)
        out = json.load(open(p, encoding="utf-8"))
        assert len(out) == 3
        assert out[0]["deep_analysis"] == "升级后的深读", "真实新值应当覆盖"
        assert out[0]["link"].endswith("1111.1111"), "既有行序应保持"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_merge_returns_false_and_skips_write_when_nothing_changed():
    """无变化不落盘，避免每轮产生空提交。"""
    tmp = tempfile.mkdtemp()
    try:
        p = _write(tmp, _rows())
        before = os.path.getmtime(p)
        changed = run_deep._save_core_merged("2026-08-30", [dict(_rows()[0])], path=p)
        assert changed is False
        assert os.path.getmtime(p) == before
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_core_key_matches_normalized_link_so_cache_lookups_hit():
    """写盘口径与 process_arxiv_tier2 的查找口径必须一致，否则缓存永远查不中。"""
    rec = {"link": "10.1103/PhysRevB.1.1", "title": "t"}
    assert run_deep._core_key(rec) == "https://doi.org/10.1103/PhysRevB.1.1"
    assert run_deep._core_key({"link": "https://arxiv.org/abs/1.1"}) == "https://arxiv.org/abs/1.1"
    assert run_deep._core_key({"title": "no link"}) == "title:no link"


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] run_deep arxiv_core merge sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
