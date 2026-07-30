"""研究兴趣画像匹配：规则预筛 + LLM 批量分析。

- 画像文件 data/focus_interests.json 由 update_focus_profile.py 离线生成（手动/dispatch）。
- 预筛仿 focus_filter 规则表风格（纯函数、无外部依赖）；
  LLM 批量分析仿 relevance_enricher.batch_analyze_relevance。
- fail-soft 铁律：无画像文件 / 无 provider / 批次失败 → 跳过，绝不阻塞流水线。
"""

from __future__ import annotations

import json
import os
import time
from string import Template
from typing import Any, Dict, List, Optional

DEFAULT_PROFILE_PATH = "data/focus_interests.json"
_FOCUS_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "ai_prompts", "focus_interest.txt")

# LLM 批量分析的每批文章数（批量省请求，单批过大易截断）
FOCUS_BATCH_SIZE = 8


def _extract_json(text: str) -> Any:
    import re

    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise ValueError("No JSON object found")
    return json.loads(m.group())


def load_interest_profile(path: str = DEFAULT_PROFILE_PATH) -> Dict[str, Any]:
    """读取兴趣画像；文件缺失/损坏 → {}（fail-soft）。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").replace("\n", " ").split()).lower()


def _profile_keywords(profile: Dict[str, Any]) -> List[str]:
    raw = profile.get("keywords") or []
    if not isinstance(raw, list):
        raw = [raw]
    return [_normalize_text(k) for k in raw if _normalize_text(k)]


def _keyword_hits(text: str, keywords: List[str]) -> int:
    return sum(1 for kw in keywords if kw and kw in text)


def prefilter_candidates(
    articles: List[Dict[str, Any]],
    profile: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """关键词打分预筛（标题/摘要/期刊），仿 focus_filter 规则表风格。

    命中数 >0 的文章按分数降序返回；无关键词画像 → []。
    纯函数，不修改输入。
    """
    keywords = _profile_keywords(profile or {})
    if not keywords:
        return []

    scored = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        title_text = _normalize_text(" ".join([a.get("title") or "", a.get("title_zh") or ""]))
        body_text = _normalize_text(
            " ".join([a.get("abstract") or "", a.get("abstract_zh") or "", a.get("journal") or ""])
        )
        # 标题命中权重更高
        hits = _keyword_hits(title_text, keywords) * 2 + _keyword_hits(body_text, keywords)
        if hits > 0:
            scored.append((hits, a))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [a for _, a in scored]


def _load_prompt_template() -> Template:
    with open(_FOCUS_PROMPT_PATH, encoding="utf-8") as f:
        return Template(f.read())


def _build_batch_prompt(batch: List[Dict[str, Any]], profile: Dict[str, Any]) -> str:
    lines = []
    for i, a in enumerate(batch, 1):
        title = (a.get("title") or "").strip()
        journal = (a.get("journal") or "").strip()
        abstract = (a.get("abstract") or "").strip()[:1200]
        lines.append(f"[{i}] Title: {title}\nJournal: {journal}\nAbstract: {abstract}\n")
    joined = "\n".join(lines)

    our_work = (profile.get("our_work_zh") or "").strip()
    if not our_work:
        # 无汇总描述时退化为学者方向拼接（仍为画像内容，不是占位文案）
        parts = []
        for s in profile.get("scholars") or []:
            d = (s.get("directions_zh") or "").strip()
            if d:
                parts.append(f"{s.get('name', '')}: {d}")
        our_work = "\n".join(parts)
    keywords = ", ".join(_profile_keywords(profile))

    return _load_prompt_template().safe_substitute(
        our_work=our_work,
        keywords=keywords,
        articles=joined,
    )


def _parse_items(data: Any) -> Dict[int, Dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("Unexpected JSON schema")

    mapping: Dict[int, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except Exception:
            continue
        try:
            score = int(item.get("focus_score", 0) or 0)
        except Exception:
            score = 0
        mapping[idx] = {
            "focus_score": max(0, min(10, score)),
            "focus_summary": str(item.get("focus_summary", "") or ""),
            "focus_relation": str(item.get("focus_relation", "") or ""),
            "focus_suggestion": str(item.get("focus_suggestion", "") or ""),
        }
    return mapping


def analyze_focus_batch(
    articles: List[Dict[str, Any]],
    profile: Dict[str, Any],
    provider: Any,
    batch_size: int = FOCUS_BATCH_SIZE,
) -> Dict[int, Dict[str, Any]]:
    """批量 LLM 分析 → {输入列表下标(0基): 分析 dict}。

    批次失败只打印 ⚠️，对应文章不出现在返回值中（留待其再次被预筛命中时重试）。
    """
    results: Dict[int, Dict[str, Any]] = {}
    for start in range(0, len(articles), batch_size):
        batch = articles[start : start + batch_size]
        prompt = _build_batch_prompt(batch, profile)
        try:
            text = provider.call_api(prompt)
            data = _extract_json(text)
            mapping = _parse_items(data)
        except Exception as e:
            print(f"⚠️ focus 批量分析失败(批次 {start // batch_size + 1}): {e}")
            mapping = {}

        for i in range(1, len(batch) + 1):
            item = mapping.get(i)
            if item is not None:
                results[start + i - 1] = item

        time.sleep(0.2)
    return results


def enrich_focus_interest(
    articles: List[Dict[str, Any]],
    provider: Any = None,
    max_items: Optional[int] = None,
) -> int:
    """编排入口：预筛 + LLM 批量分析，原地写入 focus_* 四个字段。

    幂等：已有 focus_score 的文章跳过。返回本次富化的文章数。
    provider 为 None 或无画像 → 打印 ⚠️ 返回 0（fail-soft）。
    """
    profile = load_interest_profile()
    if not profile or not _profile_keywords(profile):
        print("⚠️ 未找到研究兴趣画像(data/focus_interests.json)或画像无关键词，跳过 focus 匹配")
        return 0
    if provider is None:
        print("⚠️ 未配置 AI provider，跳过 focus 画像匹配")
        return 0

    if max_items is None:
        try:
            max_items = int(os.environ.get("AI_FOCUS_MAX_ITEMS", "20"))
        except Exception:
            max_items = 20

    pending = [a for a in articles if isinstance(a, dict) and "focus_score" not in a]
    candidates = prefilter_candidates(pending, profile)
    if max_items and max_items > 0:
        candidates = candidates[:max_items]
    if not candidates:
        return 0

    results = analyze_focus_batch(candidates, profile, provider)
    enriched = 0
    for idx, item in results.items():
        a = candidates[idx]
        a["focus_score"] = item["focus_score"]
        a["focus_summary"] = item["focus_summary"]
        a["focus_relation"] = item["focus_relation"]
        a["focus_suggestion"] = item["focus_suggestion"]
        enriched += 1
    return enriched
