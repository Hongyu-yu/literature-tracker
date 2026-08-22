"""Fail-soft Chinese highlight completion for papers shown in daily reports."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Dict, List


PROMPT_PATH = Path(__file__).with_name("ai_prompts") / "highlight_from_abstract.txt"
FORBIDDEN = ("摘要信息不足", "需查阅原文确认具体方法与结论")


def translate_text(text: str) -> str:
    """Lazy import keeps the pure render path usable without deep-translator."""
    from translator import translate_text as _translate_text
    return _translate_text(text)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _clean_highlight(value: Any) -> str:
    text = str(value or "").strip()
    if not text or any(term in text for term in FORBIDDEN):
        return ""
    return text[:120].rstrip() + ("…" if len(text) > 120 else "")


def _parse_response(raw: str) -> Dict[int, str]:
    match = re.search(r"\{[\s\S]*\}", raw or "")
    if not match:
        return {}
    data = json.loads(match.group())
    result: Dict[int, str] = {}
    for entry in data.get("items", []) if isinstance(data, dict) else []:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        value = _clean_highlight(entry.get("highlight") or entry.get("one_sentence_summary"))
        if value and _has_cjk(value):
            result[index] = value
    return result


def _build_prompt(items: List[Dict[str, Any]]) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    payload = [
        {
            "index": index,
            "title": str(item.get("title") or item.get("title_en") or "").strip(),
            "abstract": str(item.get("abstract") or "").strip(),
        }
        for index, item in enumerate(items, 1)
    ]
    return template.replace("${items}", json.dumps(payload, ensure_ascii=False))


def ensure_highlights(
    items: List[Dict[str, Any]], *, provider: Any, max_items: int,
    translate_fallback: bool = True,
) -> int:
    """Mutate missing highlights in place and return the number completed."""
    if max_items <= 0:
        return 0
    candidates = [
        item for item in items
        if not _clean_highlight(item.get("one_sentence_summary"))
        and not str(item.get("abstract_zh") or "").strip()
        and str(item.get("abstract") or "").strip()
    ][:max_items]
    if not candidates:
        return 0

    generated: Dict[int, str] = {}
    if provider is not None:
        try:
            generated = _parse_response(provider.call_api(_build_prompt(candidates)))
        except Exception as exc:
            print(f"⚠️ 亮点保障 AI 跳过，改用翻译兜底: {exc}")

    updated = 0
    for index, item in enumerate(candidates, 1):
        highlight = generated.get(index, "")
        if not highlight and translate_fallback:
            abstract = str(item.get("abstract") or "").strip()[:200]
            try:
                translated = _clean_highlight(translate_text(abstract))
                if translated and translated != abstract and _has_cjk(translated):
                    highlight = translated
            except Exception as exc:
                print(f"⚠️ 亮点翻译兜底跳过: {exc}")
        if highlight:
            item["one_sentence_summary"] = highlight
            updated += 1
    return updated
