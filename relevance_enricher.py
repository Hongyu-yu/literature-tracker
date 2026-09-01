"""
Batch relevance analyser for AI×Physics/Chemistry/Materials interdisciplinary focus.

Design goals:
- High recall: prefer including borderline cases rather than missing relevant papers.
- Batch API calls to keep GitHub Actions runtime reasonable.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ai_summarizer import build_provider
from focus_filter import is_daily_focus


def _extract_json(text: str) -> Any:
    import re

    raw = (text or "").strip()
    # 去掉 ```json ... ``` 围栏后先整体解析：模型返回纯数组时，下面的 {…} 正则会
    # 贪婪匹配到 "},{"，反而把一个完好的响应解析成 JSONDecodeError。
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass

    # 模型在 JSON 前后附带解释文字时，退而求其次截取首个对象 / 数组
    for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        m = re.search(pattern, raw)
        if not m:
            continue
        try:
            return json.loads(m.group())
        except Exception:
            continue
    raise ValueError("No JSON object found")


def batch_analyze_relevance(
    articles: List[Dict[str, Any]],
    *,
    provider_name: str,
    api_key: str,
    model: Optional[str] = None,
    batch_size: int = 16,
) -> List[Dict[str, Any]]:
    """
    Returns a list of analysis dicts aligned with the input order.

    Each result:
      {
        "is_relevant": bool,
        "score": int (0-10),
        "explanation": str (zh),
        "detailed_summary": str (zh),
        "source": "model" | "fallback"
      }

    "source" 标明结论来自模型还是本地关键词回退。调用方据此决定是否把该文献
    写进「已分析」名单——回退结论绝不能被当成 AI 的真实判定永久落盘。
    """

    provider_name = (provider_name or "").strip().lower() or "gemini"
    api_key = (api_key or "").strip()
    model = (model or "").strip() or None

    if not api_key:
        return [
            {
                "is_relevant": False,
                "score": 0,
                "explanation": "未配置 AI_API_KEY，跳过相关性分析",
                "detailed_summary": "",
                "source": "fallback",
            }
            for _ in articles
        ]

    provider = build_provider(provider_name, api_key, model=model)

    results: List[Dict[str, Any]] = []
    total_batches = (len(articles) + batch_size - 1) // batch_size
    fallback_total = 0
    for start in range(0, len(articles), batch_size):
        batch = articles[start : start + batch_size]
        batch_no = start // batch_size + 1
        prompt = _build_prompt(batch)
        batch_failed = False
        try:
            text = provider.call_api(prompt)
            data = _extract_json(text)
            mapping = _parse_items(data)
        except Exception as e:
            batch_failed = True
            # 整批失败 → 该批每篇都会退回本地关键词判定；必须打日志，否则整晚
            # 降级为子串匹配也看不出来（fail-soft 不等于失败无声）。
            print(f"⚠️ 相关性批量分析失败(批次 {batch_no}/{total_batches}, {len(batch)} 篇): {e}")
            mapping = {}

        missing = 0
        for i in range(1, len(batch) + 1):
            item = mapping.get(i)
            if not item:
                missing += 1
                article = batch[i - 1]
                fallback_rel = is_daily_focus(article)
                results.append(
                    {
                        "is_relevant": fallback_rel,
                        "score": 6 if fallback_rel else 0,
                        "explanation": "AI 返回不完整，已按本地 AI×物理/化学/材料规则回退判定。",
                        "detailed_summary": "",
                        "source": "fallback",
                    }
                )
                continue
            results.append(item)

        # 批次没报错但只回了一部分序号，同样是静默降级，一并记账
        if missing and not batch_failed:
            print(f"⚠️ 相关性分析批次 {batch_no}/{total_batches} 仅返回 {len(batch) - missing}/{len(batch)} 条，其余按本地规则回退")
        fallback_total += missing

    if fallback_total:
        print(f"⚠️ 本次相关性分析回退 {fallback_total}/{len(articles)} 篇（AI 未给出结论）")

    return results


def _build_prompt(batch: List[Dict[str, Any]]) -> str:
    lines = []
    for i, a in enumerate(batch, 1):
        title = (a.get("title") or "").strip()
        journal = (a.get("journal") or "").strip()
        authors = a.get("authors") or []
        if isinstance(authors, list):
            authors_str = ", ".join([str(x) for x in authors[:6]]) + (" 等" if len(authors) > 6 else "")
        else:
            authors_str = str(authors or "")
        abstract = (a.get("abstract") or "").strip()
        abstract = abstract[:600]
        lines.append(f"[{i}] Title: {title}\nJournal: {journal}\nAuthors: {authors_str}\nAbstract: {abstract}\n")

    joined = "\n".join(lines)

    return f"""你是一位研究助理，专注于 AI 与物理/化学/材料科学交叉学科（AI4Science）。\n\n请逐条判断以下论文是否与“AI×物理/化学/材料/计算科学”相关。\n\n高召回要求：\n- 宁可多收录，也不要漏掉潜在相关论文。\n- 只要论文可能涉及：机器学习/深度学习/生成模型/图网络/大模型在物理、化学、材料、计算模拟、自动化发现中的应用；或 AI 用于实验/计算数据驱动；或与材料/凝聚态/化学计算强相关且可能与 AI 方法结合，都应判为相关。\n- 纯临床医学、生物医学治疗/诊断、公共卫生、教育、社会科学等，即使使用 AI，也判为不相关；除非论文核心问题明确属于物理/化学/材料/计算模拟方法本身。\n\n输入列表：\n{joined}\n\n请严格输出 JSON（不要 markdown，不要多余解释），并且 items 必须覆盖全部输入序号：\n{{\n  \"items\": [\n    {{\n      \"index\": 1,\n      \"is_relevant\": true,\n      \"score\": 0,\n      \"explanation\": \"中文1-2句，说明为何相关/不相关\",\n      \"detailed_summary\": \"中文3-4句，总结研究对象、方法（AI或物理/化学/材料方法）、主要发现与启发\"\n    }}\n  ]\n}}\n\n注意：除 is_relevant/score 外，其余字段必须是简体中文。"""


def _parse_items(data: Any) -> Dict[int, Dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        # 模型省掉外层 {"items": …} 直接返回数组，不该让整批白跑（仿 focus_interest）
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
        # 单条 score 写成 "8/10" 之类不该连累同批其余文献一起回退
        try:
            score = int(item.get("score", 0) or 0)
        except Exception:
            score = 0
        mapping[idx] = {
            "is_relevant": bool(item.get("is_relevant", False)),
            "score": max(0, min(10, score)),
            "explanation": str(item.get("explanation", "") or ""),
            "detailed_summary": str(item.get("detailed_summary", "") or ""),
            "source": "model",
        }
    return mapping
