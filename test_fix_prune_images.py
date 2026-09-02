#!/usr/bin/env python3
"""海报清理必须按引用关系，而不是按 mtime。

旧实现：`if datetime.date.fromtimestamp(os.path.getmtime(p)) < cutoff: os.remove(p)`。
git 不记录 mtime，actions/checkout 每次都重写全部文件，所以 CI 里所有海报的 mtime
都等于本次运行开始时间，永远不早于 cutoff —— 这是个永久空转的空操作。
实测本地 1145 张 .webp 的 mtime 完全一致（clone 那天），docs/images/posters 已 122MB。
"""

import json
import os
import shutil
import tempfile

import run_deep


def _make(tmp, names):
    d = os.path.join(tmp, "docs/images/posters")
    os.makedirs(d, exist_ok=True)
    for n in names:
        with open(os.path.join(d, n), "wb") as f:
            f.write(b"x" * 100)
    return d


def _run(tmp, **kw):
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        return run_deep.prune_images(dirs=("docs/images/posters",), **kw)
    finally:
        os.chdir(cwd)


def test_orphans_are_deleted_and_referenced_files_survive():
    tmp = tempfile.mkdtemp()
    try:
        d = _make(tmp, ["keep_html.webp", "keep_json.webp", "orphan.webp"])
        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "docs/daily"), exist_ok=True)
        with open(os.path.join(tmp, "docs/daily/x.html"), "w", encoding="utf-8") as f:
            f.write('<img src="../images/posters/keep_html.webp">')
        with open(os.path.join(tmp, "data/arxiv_core_2026-08-30.json"), "w", encoding="utf-8") as f:
            json.dump([{"image": "images/posters/keep_json.webp"}], f)
        removed = _run(tmp, max_delete_ratio=1.0)
        left = sorted(os.listdir(d))
        assert removed == 1, removed
        assert left == ["keep_html.webp", "keep_json.webp"], left
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_json_only_references_are_not_treated_as_orphans():
    """只看 HTML 会误删 JSON 仍引用的图 —— 实测那是 1145 张里的 127 张。"""
    tmp = tempfile.mkdtemp()
    try:
        d = _make(tmp, ["pending.webp"])
        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
        with open(os.path.join(tmp, "data/daily_summary_2026-08-30.json"), "w", encoding="utf-8") as f:
            json.dump({"full_list": [{"image": "images/posters/pending.webp"}]}, f)
        assert _run(tmp) == 0
        assert os.listdir(d) == ["pending.webp"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_refuses_to_delete_when_orphan_ratio_is_implausible():
    """孤儿比例过高 = 引用集收集出了问题，不是真有那么多垃圾。删除不可逆，宁可不删。"""
    tmp = tempfile.mkdtemp()
    try:
        d = _make(tmp, [f"a{i}.webp" for i in range(10)])
        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
        with open(os.path.join(tmp, "data/arxiv_core_2026-08-30.json"), "w", encoding="utf-8") as f:
            json.dump([{"image": "images/posters/a0.webp"}], f)   # 只引用 1/10
        assert _run(tmp) == 0, "9/10 判成孤儿时必须拒绝删除"
        assert len(os.listdir(d)) == 10
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_empty_reference_set_aborts_instead_of_wiping_everything():
    """一个引用都收不到（glob 没匹配上/JSON 全坏）时绝不能把整个目录清空。"""
    tmp = tempfile.mkdtemp()
    try:
        d = _make(tmp, ["a.webp", "b.webp"])
        assert _run(tmp) == 0
        assert len(os.listdir(d)) == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_dry_run_reports_without_deleting():
    tmp = tempfile.mkdtemp()
    try:
        d = _make(tmp, ["keep.webp", "orphan.webp"])
        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
        with open(os.path.join(tmp, "data/arxiv_core_2026-08-30.json"), "w", encoding="utf-8") as f:
            json.dump([{"image": "images/posters/keep.webp"}], f)
        assert _run(tmp, dry_run=True, max_delete_ratio=1.0) == 1
        assert len(os.listdir(d)) == 2, "dry-run 不得真的删除"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune_no_longer_depends_on_mtime():
    """守住根因：实现里不能再**调用** getmtime。

    只查调用形式 `getmtime(`——docstring 里为了解释这个历史 bug 会提到这个名字，
    查裸名字会把说明文字也算成违规。
    """
    src = open("run_deep.py", encoding="utf-8").read()
    start = src.index("def prune_images(")
    body = src[start: src.index("\ndef ", start + 10)]
    assert "getmtime(" not in body, "prune_images 不得再按 mtime 判断（CI 里 mtime 恒为 checkout 时间）"


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] prune_images sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
