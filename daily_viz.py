"""Dependency-free inline SVG charts for daily report pages."""

from __future__ import annotations

from collections import Counter
from html import escape
from typing import Any, Dict, Iterable, List, Tuple

from focus_core import classify_taxonomy, priority_tier
from cross_relevance import is_cross_item


def _svg(title: str, rows: List[Tuple[str, int]], total: int) -> str:
    height = 76 if not rows else 42 + len(rows) * 28
    # .vl/.vn/.vb/.vt 的样式在 docs/daily-common.css（限定在 .daily-viz-svg 下）。
    # 不要内联 <style>：内联 SVG 的 <style> 在 HTML 里不是 scoped 的，会泄漏到整页，
    # 且每页重复 3 份（见 test_daily_pages_render.py 的“恒定 CSS 不应再内联”守卫）。
    chunks = [
        f'<svg class="daily-viz-svg" viewBox="0 0 360 {height}" style="max-width:100%;height:auto" '
        f'role="img" aria-label="{escape(title, quote=True)}"><title>{escape(title)}</title>',
        f'<text class="vt" x="8" y="20">{escape(title)}</text>',
    ]
    if not rows:
        chunks.append('<text class="vl" x="8" y="52">暂无数据</text>')
    else:
        maximum = max((value for _, value in rows), default=1) or 1
        for index, (label, value) in enumerate(rows):
            y = 36 + index * 28
            width = round(190 * value / maximum, 1)
            chunks.extend([
                f'<text class="vl" x="8" y="{y + 14}">{escape(label)}</text>',
                f'<rect class="vb" x="142" y="{y}" width="{width}" height="18" rx="5"/>',
                f'<text class="vn" x="{min(338, 148 + width)}" y="{y + 14}">{value}</text>',
            ])
    chunks.append('</svg>')
    return "".join(chunks)


def _topic(item: Dict[str, Any]) -> str:
    explicit = str(item.get("classify_taxonomy") or item.get("category") or "").strip()
    if explicit:
        return explicit
    bucket = str(item.get("topic_bucket") or "").strip()
    labels = {"physics": "物理", "chemistry": "化学", "materials": "材料", "methods": "方法"}
    return labels.get(bucket, bucket) if bucket else classify_taxonomy(item)


def render_topic_distribution_svg(items: Iterable[Dict[str, Any]]) -> str:
    counts = Counter(_topic(item) for item in items if isinstance(item, dict))
    rows = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return _svg("主题分布", rows, sum(counts.values()))


def render_source_split_svg(items: Iterable[Dict[str, Any]]) -> str:
    counts = Counter()
    for item in items:
        if not isinstance(item, dict):
            continue
        journal = str(item.get("journal") or "").strip().lower()
        link = str(item.get("link") or "").lower()
        counts["arXiv" if journal == "arxiv" or "arxiv.org" in link else "期刊"] += 1
    rows = [(name, counts[name]) for name in ("arXiv", "期刊") if counts[name]]
    return _svg("来源构成", rows, sum(counts.values()))


def render_priority_svg(items: Iterable[Dict[str, Any]]) -> str:
    """与日报正文同一套分区口径（见 generate_daily_pages.group_daily_items）。

    这里必须跟着分组走：图表说"P2 铁电·铁磁·多铁 17 篇"而正文根本没有那个分组，
    读者只会以为漏了内容。
    """
    core = cross = other = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if not is_cross_item(item):
            other += 1
        elif priority_tier(item) == 0:
            core += 1
        else:
            cross += 1
    total = core + cross + other
    rows = [
        ("神经网络势·电子结构", core),
        ("AI × 物理/材料/化学", cross),
        ("其他物理/材料", other),
    ] if total else []
    return _svg("方向分层", rows, total)
