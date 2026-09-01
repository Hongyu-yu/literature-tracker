#!/usr/bin/env python3
"""一次性回填：修复此前被 LaTeX 归一化写坏的文献文本。

背景
----
旧的 text_normalizer._decode_latex 有两个缺陷（已在代码层修复，但**已入库的数据不会自愈**）：

1. LATEX_SPECIAL_MAP 用裸 str.replace 逐条替换，而 \\o/\\l/\\i/\\O/\\L 这些单字母键没有
   词边界，会先把更长命令的前缀吃掉：\\omega -> ømega、\\lambda -> łambda、\\leq -> łeq。
2. ACCENT_RE 的命令字符类含 r/c/u/v/H 且允许裸接字母，把命令名拆成「重音+字母」：
   \\rho -> h̊o、\\chi -> ḩi、\\right -> i̊ght。

这些坏文本被写进 data/index.json 等文件，并作为论文标题/摘要喂给 LLM 做相关性打分、
翻译和深读 —— 模型在对着乱码做物理判断。

做法
----
不靠手写替换表（容易漏），而是**用新旧两版实现自动推导**：对每个 LaTeX 命令 token 分别跑
旧实现和新实现，凡结果不同且旧结果确实是坏形态的，就构成一条 {坏形态: 正确值} 修复规则。
按长度降序替换，避免短规则先吃掉长形态。

用法
----
    python3 backfill_latex_repair.py            # 预演，只统计不写盘
    python3 backfill_latex_repair.py --apply    # 实际写盘
    python3 backfill_latex_repair.py --apply --paths data/index.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import types

import text_normalizer as new_tn

# 修复前的实现所在的提交（本文件引入修复之前的最后一版）
OLD_REF = os.environ.get("LATEX_REPAIR_OLD_REF", "ae249183c")

DEFAULT_PATHS = [
    "data/index.json",
    "data/ai_relevant.json",
    "data/daily_summary_*.json",
    "data/arxiv_core_*.json",
    "data/arxiv_tier2_*.json",
    "data/aps_*.json",
]

# 需要修复的文本字段（只碰文本，绝不动 link/id/date 等结构性字段）
TEXT_FIELDS = (
    "title", "title_en", "title_zh", "abstract", "abstract_zh", "abstract_zh_full",
    "summary", "one_sentence_summary", "journal", "overview", "trends",
    "method_point", "related_work", "implication", "focus_summary", "focus_relation",
    "focus_suggestion", "ai_explanation", "ai_detailed_summary", "deep_analysis",
)


def _load_old_module():
    """把修复前的 text_normalizer 源码加载成一个独立模块。"""
    src = subprocess.run(["git", "show", f"{OLD_REF}:text_normalizer.py"],
                         capture_output=True, text=True, check=True).stdout
    mod = types.ModuleType("old_text_normalizer")
    mod.__dict__["__name__"] = "old_text_normalizer"
    exec(compile(src, "<old text_normalizer>", "exec"), mod.__dict__)
    return mod


# 不在 LATEX_*_MAP 里、但同样会被旧实现咬坏的常见 LaTeX 命令。
# 只需要覆盖以「会被吃掉的字母」开头的命令：单字母转义 o/l/i/j/O/L，以及被误判成重音的
# r/c/u/v/H。新实现对这些命令不做替换，最终只会去掉反斜杠（\left -> left），
# 而旧实现会产出 łeft / i̊ght 这类坏形态，因此仍需回填。
EXTRA_COMMANDS = [
    r"\left", r"\right", r"\langle", r"\rangle", r"\lceil", r"\lfloor", r"\ldots",
    r"\log", r"\lim", r"\ln", r"\leftrightarrow", r"\longrightarrow",
    r"\int", r"\infty", r"\in", r"\imath", r"\jmath", r"\iint",
    r"\operatorname", r"\overline", r"\oplus", r"\otimes", r"\odot", r"\oint",
    r"\cdot", r"\cdots", r"\cos", r"\cosh", r"\cup", r"\cap", r"\circ", r"\colon",
    r"\rm", r"\rangle", r"\Re", r"\rightarrow", r"\rightleftharpoons",
    r"\varsigma", r"\varrho", r"\varkappa", r"\vec", r"\vert",
    r"\varDelta", r"\varGamma", r"\varOmega", r"\varSigma", r"\varTheta",
    r"\varLambda", r"\varPhi", r"\varPsi", r"\varUpsilon", r"\varPi", r"\varvec",
    r"\underline", r"\uparrow", r"\updownarrow",
]


def build_repair_map(old_tn) -> dict:
    """跑新旧两版实现，自动推导 {坏形态: 正确值}。"""
    commands = set(new_tn.LATEX_COMMAND_TEXT_MAP) | set(new_tn.LATEX_SPECIAL_MAP)
    commands |= set(EXTRA_COMMANDS)
    repair = {}
    for cmd in commands:
        try:
            old = old_tn.normalize_text(cmd)
            new = new_tn.normalize_text(cmd)
        except Exception:
            continue
        if not old or old == new:
            continue
        # 坏形态必须含非 ASCII 字符（ø/ł/Ø/Ł 或组合重音），那是被吃掉前缀/误判重音留下的
        # 指纹，正常英文里不可能出现，替换是安全的。
        # 纯 ASCII 的坏形态一律跳过：例如 \iota 旧实现产出 'iota'，而 'iota' 本身就是英文
        # 单词，全局替换会把正常文本改坏 —— 那比留着 'iota' 不还原糟得多。
        if old.isascii():
            continue
        repair[old] = new
    # 长的先替换，避免 'łeq' 被 'ł' 之类的短规则提前吃掉
    return dict(sorted(repair.items(), key=lambda kv: -len(kv[0])))


def _detect_indent(raw: str):
    """从原文件推断缩进宽度，回写时保持一致。

    不这么做的话，一个 34MB 的美化 JSON 会被压成单行：内容没变，diff 却是 40 多万行，
    review 无从下手，git 历史也被污染。
    """
    for line in raw.split("\n", 40)[1:40]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
        if stripped:          # 第二行就顶格 → 紧凑格式
            return None
    return None


def repair_text(value, repair: dict):
    if not isinstance(value, str) or not value:
        return value, 0
    out, hits = value, 0
    for bad, good in repair.items():
        if bad in out:
            hits += out.count(bad)
            out = out.replace(bad, good)
    return out, hits


def walk(obj, repair: dict, stats: dict):
    """递归修复 dict/list 里的文本字段。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and k in TEXT_FIELDS:
                fixed, n = repair_text(v, repair)
                if n:
                    obj[k] = fixed
                    stats["fields"] += 1
                    stats["hits"] += n
                    stats["by_field"][k] = stats["by_field"].get(k, 0) + n
            else:
                walk(v, repair, stats)
    elif isinstance(obj, list):
        for item in obj:
            walk(item, repair, stats)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写盘（默认只预演）")
    ap.add_argument("--paths", nargs="*", default=None, help="要处理的文件/通配符")
    args = ap.parse_args()

    old_tn = _load_old_module()
    repair = build_repair_map(old_tn)
    print(f"自动推导出 {len(repair)} 条修复规则（新旧实现对比得出）：")
    for bad, good in list(repair.items())[:14]:
        print(f"    {bad!r:16} -> {good!r}")
    if len(repair) > 14:
        print(f"    …… 另有 {len(repair) - 14} 条")
    if not repair:
        print("没有可修复的规则，退出。")
        return 0

    patterns = args.paths or DEFAULT_PATHS
    files = []
    for pat in patterns:
        files.extend(sorted(glob.glob(pat)))
    print(f"\n扫描 {len(files)} 个文件（{'写盘' if args.apply else '预演'}）\n")

    total = {"files": 0, "fields": 0, "hits": 0}
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
            data = json.loads(raw)
        except Exception as e:
            print(f"  ⏭️ 跳过 {path}: {e}")
            continue
        indent = _detect_indent(raw)   # 保留原有缩进，否则 diff 会变成几十万行的重排
        stats = {"fields": 0, "hits": 0, "by_field": {}}
        walk(data, repair, stats)
        if not stats["hits"]:
            continue
        total["files"] += 1
        total["fields"] += stats["fields"]
        total["hits"] += stats["hits"]
        top = ", ".join(f"{k}×{v}" for k, v in sorted(stats["by_field"].items(),
                                                      key=lambda kv: -kv[1])[:4])
        print(f"  {'✅' if args.apply else '🔍'} {path}: {stats['fields']} 个字段 / "
              f"{stats['hits']} 处 ({top})")
        if args.apply:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=indent)
            os.replace(tmp, path)   # 原子替换，避免写一半损坏 13MB 的 index.json

    print(f"\n合计：{total['files']} 个文件 / {total['fields']} 个字段 / {total['hits']} 处")
    if not args.apply:
        print("这是预演。确认无误后加 --apply 实际写盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
