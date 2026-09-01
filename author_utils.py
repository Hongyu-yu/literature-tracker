#!/usr/bin/env python3
"""Helpers for normalizing inconsistent author metadata."""

from __future__ import annotations

import re
from typing import Any, List

from text_normalizer import normalize_text


def _compact_text(value: Any) -> str:
    return " ".join(normalize_text(value).replace("\u00a0", " ").split())


def _looks_like_character_stream(items: List[str]) -> bool:
    meaningful = [item for item in items if str(item)]
    if len(meaningful) < 8:
        return False
    short_count = sum(1 for item in meaningful if len(str(item).strip()) <= 1)
    return short_count / max(1, len(meaningful)) >= 0.85


# 期刊 RSS 常把整串作者塞进一个字段(dc_creator),整串会被当成"一位作者",
# authors_label 的 max_names 截断因此永远不触发。下面这组正则负责拆成单个作者名。
_AFFILIATION_RE = re.compile(r"\([^)]*\)")  # 括号里多是单位/备注,arXiv 尤其常见
_TRAILING_URL_RE = re.compile(r"https?://.*$", re.IGNORECASE | re.DOTALL)  # PNAS 把 ROR 链接和单位粘在名字后面
_NAME_SPLIT_RE = re.compile(r"\s*[;,]\s*|\s+and\s+|\s*&\s*")
_LEADING_CONJUNCTION_RE = re.compile(r"^(?:and|&)\s+", re.IGNORECASE)  # "A, B, and C" 拆完会残留 "and C"
_NAME_SUFFIX_RE = re.compile(r"^(?:Jr|Sr|II|III|IV|Ph\.?\s?D|M\.?\s?D)\.?$", re.IGNORECASE)
_WORD_RE = re.compile(r"\w")


def _split_names(text: str) -> List[str]:
    """把 "A, B, and C" 这类合并字符串拆成单个作者名。

    拆不出任何东西时原样返回整串 —— 宁可显示得长一点,也绝不把已有的作者信息弄丢。
    """
    stripped = _AFFILIATION_RE.sub(" ", _TRAILING_URL_RE.sub(" ", text))
    names: List[str] = []
    for part in _NAME_SPLIT_RE.split(stripped):
        part = " ".join(_LEADING_CONJUNCTION_RE.sub("", part.strip()).strip(" ,;").split())
        if not part or not _WORD_RE.search(part):
            continue
        if _NAME_SUFFIX_RE.match(part) and names:
            names[-1] = f"{names[-1]}, {part}"  # Jr./III 之类后缀接回上一位,别凭空多出一位"作者"
            continue
        names.append(part)
    return names or ([text] if text else [])


def normalize_author_names(authors: Any) -> List[str]:
    if not authors:
        return []

    if isinstance(authors, list):
        raw_items = [str(item) for item in authors if str(item)]
        if _looks_like_character_stream(raw_items):
            return _split_names(_compact_text("".join(raw_items)))

        names: List[str] = []
        for item in raw_items:
            names.extend(_split_names(_compact_text(item)))
        return names

    return _split_names(_compact_text(authors))


def authors_label(authors: Any, *, max_names: int = 6) -> str:
    cleaned = normalize_author_names(authors)
    if not cleaned:
        return ""
    if len(cleaned) > max_names:
        return ", ".join(cleaned[:max_names]) + f" 等{len(cleaned)}位作者"
    return ", ".join(cleaned)
