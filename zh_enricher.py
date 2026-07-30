"""
Chinese enrichment utilities:
- Ensure every article has `title_zh`, `abstract_zh` (2-4 句浓缩版) and
  `abstract_zh_full` (完整忠实中文翻译, 同一次 LLM 调用产出).

Strategy:
- Prefer LLM batch translation/summarization via the configured AI provider (OpenRouter recommended).
- Fallback to GoogleTranslator (deep-translator) for single-item translation when AI is unavailable.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from ai_summarizer import build_provider
from text_normalizer import is_suspicious_text, normalize_articles_inplace, normalize_text


def _extract_json(text: str) -> Any:
    import re

    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise ValueError("No JSON object found")
    return json.loads(m.group())


def _default_ai_model() -> Optional[str]:
    return (os.environ.get("AI_MODEL") or "").strip() or None


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _full_needs_translation(a: Dict[str, Any]) -> bool:
    """abstract_zh_full 缺失/可疑,或原样等于英文摘要(LLM 未翻译) → 需重填。
    已含中文且与原文一致(源摘要本身是中文)视为正常,保持幂等。"""
    full = (a.get("abstract_zh_full") or "").strip()
    if not full or is_suspicious_text(full):
        return True
    abstract = (a.get("abstract") or "").strip()
    return bool(abstract) and full == abstract and not _has_cjk(full)


def enrich_articles_zh(
    articles: List[Dict[str, Any]],
    *,
    provider_name: str,
    api_key: str,
    model: Optional[str] = None,
    max_items: int = 120,
    batch_size: int = 12,
    abstract_char_limit: int = 3000,
    on_progress: Optional[Any] = None,
) -> int:
    """
    Mutates `articles` in-place. Returns number of articles updated.

    `abstract_zh` is allowed to be a concise Chinese abstract/summary (2-4 sentences).
    `abstract_zh_full` is a complete faithful Chinese translation of the full abstract.
    `on_progress` (optional) is invoked after each processed batch so callers can
    checkpoint-persist partial progress (protects long backfills from job timeouts).
    """

    provider_name = (provider_name or "").strip().lower()
    api_key = (api_key or "").strip()
    model = (model or "").strip() or _default_ai_model()

    normalize_articles_inplace(articles)

    # Candidates: missing or corrupted zh fields
    candidates = [
        a
        for a in articles
        if (
            not (a.get("title_zh") or "").strip()
            or not (a.get("abstract_zh") or "").strip()
            or _full_needs_translation(a)
            or is_suspicious_text(a.get("title_zh"))
            or is_suspicious_text(a.get("abstract_zh"))
        )
        and (a.get("title") or "").strip()
        and (a.get("link") or "").strip()
    ]
    if not candidates:
        return 0

    # Keep the newest first (pub_date is YYYY-MM-DD)
    candidates.sort(key=lambda x: (x.get("pub_date") or ""), reverse=True)
    candidates = candidates[: max_items if max_items > 0 else len(candidates)]

    updated = 0

    if api_key:
        provider = build_provider(provider_name, api_key, model=model)
        for start in range(0, len(candidates), batch_size):
            batch = candidates[start : start + batch_size]
            batch_payload = []
            for i, a in enumerate(batch, 1):
                title = (a.get("title") or "").strip()
                journal = (a.get("journal") or "").strip()
                authors = a.get("authors") or []
                if isinstance(authors, list):
                    authors_str = ", ".join([str(x) for x in authors[:6]]) + (" 等" if len(authors) > 6 else "")
                else:
                    authors_str = str(authors or "")
                abstract = (a.get("abstract") or "").strip()
                abstract = abstract[:abstract_char_limit]
                batch_payload.append(
                    {
                        "index": i,
                        "title": title,
                        "journal": journal,
                        "authors": authors_str,
                        "abstract": abstract,
                    }
                )

            prompt = _build_batch_prompt(batch_payload)
            try:
                resp = provider.call_api(prompt)
                data = _extract_json(resp)
                mapping = _parse_batch_result(data)
            except Exception:
                # Small delay + skip batch (will retry in later runs)
                time.sleep(1)
                continue

            for i, a in enumerate(batch, 1):
                item = mapping.get(i)
                if not item:
                    continue
                title_zh = normalize_text((item.get("title_zh") or "").strip())
                abstract_zh = normalize_text((item.get("abstract_zh") or "").strip())
                abstract_zh_full = normalize_text((item.get("abstract_zh_full") or "").strip())
                if title_zh:
                    if not (a.get("title_zh") or "").strip() or is_suspicious_text(a.get("title_zh")):
                        a["title_zh"] = title_zh
                if abstract_zh:
                    if not (a.get("abstract_zh") or "").strip() or is_suspicious_text(a.get("abstract_zh")):
                        a["abstract_zh"] = abstract_zh
                if abstract_zh_full:
                    if _full_needs_translation(a):
                        a["abstract_zh_full"] = abstract_zh_full
                if title_zh or abstract_zh or abstract_zh_full:
                    updated += 1

            if on_progress:
                try:
                    on_progress()
                except Exception:
                    pass
            time.sleep(0.2)

        return updated

    # Fallback: Google translate (slower, but avoids empty zh fields)
    try:
        from translator import translate_text
    except Exception:
        return 0

    for a in candidates:
        try:
            if not (a.get("title_zh") or "").strip() or is_suspicious_text(a.get("title_zh")):
                a["title_zh"] = normalize_text(translate_text(a.get("title") or ""))
            if not (a.get("abstract_zh") or "").strip() or is_suspicious_text(a.get("abstract_zh")):
                a["abstract_zh"] = normalize_text(translate_text((a.get("abstract") or "")[:2000]))
            if _full_needs_translation(a):
                a["abstract_zh_full"] = normalize_text(translate_text(a.get("abstract") or ""))
            updated += 1
        except Exception:
            continue

    return updated


def _build_batch_prompt(items: List[Dict[str, str]]) -> str:
    lines = []
    for item in items:
        lines.append(
            f"[{item['index']}] Title: {item['title']}\nJournal: {item.get('journal','')}\nAuthors: {item.get('authors','')}\nAbstract: {item['abstract']}\n"
        )
    joined = "\n".join(lines)

    return f"""你是专业的学术翻译与摘要助手。请对以下每条文献生成中文标题、中文摘要与完整中文翻译。\n\n输入列表:\n{joined}\n\n请严格输出 JSON（不要 markdown，不要多余解释）：\n{{\n  \"items\": [\n    {{\"index\": 1, \"title_zh\": \"中文标题\", \"abstract_zh\": \"中文摘要(2-4句,忠实且简洁)\", \"abstract_zh_full\": \"摘要的完整忠实中文翻译(逐句对应原文,不删减不浓缩)\"}},\n    ...\n  ]\n}}\n\n要求:\n1. items 必须包含全部输入条目，index 与输入的 [序号] 严格一致。\n2. 如果原摘要为空/过短/仅为元数据（如 EarlyView、Published online 等），abstract_zh 与 abstract_zh_full 仍应给出基于标题与期刊信息的谨慎概述（不要编造具体数值/结论，允许以“该工作围绕...展开，详情需查阅原文”表述）。\n3. 不要输出任何链接。\n"""


def _parse_batch_result(data: Any) -> Dict[int, Dict[str, str]]:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("Unexpected JSON schema")

    mapping: Dict[int, Dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        try:
            idx_int = int(idx)
        except Exception:
            continue
        mapping[idx_int] = {
            "title_zh": str(item.get("title_zh", "") or ""),
            "abstract_zh": str(item.get("abstract_zh", "") or ""),
            "abstract_zh_full": str(item.get("abstract_zh_full", "") or ""),
        }
    return mapping
