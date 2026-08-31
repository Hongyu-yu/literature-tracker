#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate daily summary pages for GitHub Pages.

- Input (preferred): data/index.json (keyword-filtered global index)
  - Fallback: data/ai_relevant.json (created by run_optimized_sync)
- Output:
  - docs/daily/YYYY-MM-DD.html
  - docs/daily/summaries.json
"""
import os
import json
import html
import shutil
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple
import hashlib

from ai_summarizer import AISummarizer
from local_kimi_provider import build_provider_extended
from author_utils import authors_label
from focus_filter import analyze_focus, filter_daily_focus_items, filter_focus_items, focus_priority, topic_bucket
from rss_generator import generate_daily_rss_feed
from text_normalizer import normalize_articles_inplace, normalize_text
from focus_core import classify_taxonomy, core_score, is_core_focus, priority_tier
from link_utils import normalize_link
from research_context import build_direction_note, ensure_relation_fields, load_research_profile


def _guarantee_daily_highlights(items: List[Dict], max_items: Optional[int] = None) -> int:
    """Best-effort generation-time highlight completion; never breaks a report.

    max_items=None → 读环境变量(单日默认预算);传入具体值 → 用它(供跨天全局预算)。"""
    if max_items is None:
        try:
            max_items = max(0, int(os.environ.get("AI_HIGHLIGHT_MAX_ITEMS", "60")))
        except (TypeError, ValueError):
            max_items = 60
    else:
        max_items = max(0, int(max_items))
    if not items or max_items <= 0:
        return 0
    api_key = (os.environ.get("AI_API_KEY") or os.environ.get("KIMI_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        print("⏭️ 亮点保障跳过：未配置 AI key")
        return 0
    try:
        from ai_summarizer import build_provider
        from highlight_guarantee import ensure_highlights
        provider = build_provider(os.environ.get("AI_PROVIDER") or "kimi", api_key,
                                  model=(os.environ.get("AI_MODEL") or "").strip() or None)
        updated = ensure_highlights(items, provider=provider, max_items=max_items)
        print(f"✨ 亮点保障补全 {updated} 篇")
        return updated
    except Exception as exc:
        print(f"⚠️ 亮点保障跳过: {exc}")
        return 0


def _enrich_daily_focus(items: List[Dict], max_items: Optional[int] = None) -> int:
    """Best-effort focus-score coverage for the final daily set.

    max_items=None → 读环境变量;传入具体值 → 用它(供跨天全局预算)。"""
    if max_items is None:
        try:
            max_items = max(0, int(os.environ.get("AI_FOCUS_DAILY_MAX", "60")))
        except (TypeError, ValueError):
            max_items = 60
    else:
        max_items = max(0, int(max_items))
    if not items or max_items <= 0:
        return 0
    api_key = (os.environ.get("AI_API_KEY") or os.environ.get("KIMI_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        print("⏭️ focus 日报富化跳过：未配置 AI key")
        return 0
    try:
        from ai_summarizer import build_provider
        from focus_interest import enrich_focus_interest
        provider = build_provider(os.environ.get("AI_PROVIDER") or "kimi", api_key,
                                  model=(os.environ.get("AI_MODEL") or "").strip() or None)
        updated = enrich_focus_interest(items, provider=provider, max_items=max_items)
        print(f"🎯 focus 日报富化补全 {updated} 篇")
        return updated
    except Exception as exc:
        print(f"⚠️ focus 日报富化跳过: {exc}")
        return 0


def _new_daily_enrich_budget() -> Dict[str, int]:
    """整次 generate 调用的【全局】富化预算(跨天共享),避免 --days N 把 AI 调用放大 N 倍。"""
    def _cap(name: str, default: str) -> int:
        try:
            return max(0, int(os.environ.get(name, default)))
        except (TypeError, ValueError):
            return int(default)
    return {"hl": _cap("AI_HIGHLIGHT_MAX_ITEMS", "60"), "fs": _cap("AI_FOCUS_DAILY_MAX", "60")}


def _apply_daily_enrichment(items: List[Dict], budget: Dict[str, int]) -> None:
    """对单日 daily 集做一次富化(focus + 亮点),消耗共享全局预算(就地扣减)。"""
    if not items:
        return
    if budget.get("fs", 0) > 0:
        used = _enrich_daily_focus(items, max_items=budget["fs"]) or 0
        budget["fs"] = max(0, budget["fs"] - used)
    if budget.get("hl", 0) > 0:
        used = _guarantee_daily_highlights(items, max_items=budget["hl"]) or 0
        budget["hl"] = max(0, budget["hl"] - used)


def beijing_today() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime('%Y-%m-%d')

def beijing_yesterday() -> str:
    tz = timezone(timedelta(hours=8))
    return (datetime.now(tz) - timedelta(days=1)).strftime('%Y-%m-%d')


def safe_text(value: str) -> str:
    if value is None:
        return ""
    return html.escape(normalize_text(value), quote=True)


def safe_url(value: str) -> str:
    url = (value or "").strip()
    if not url:
        return "#"
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return "#"
    except Exception:
        return "#"
    return html.escape(url, quote=True)

def format_authors(authors) -> str:
    return authors_label(authors, max_names=6)

def arxiv_badge(item: Dict) -> str:
    """Return a readable arXiv category badge from item fields."""
    journal = (item.get("journal") or "").strip()
    if journal.lower() != "arxiv":
        return ""
    cat = (item.get("arxiv_category") or "").strip()
    if not cat:
        # fallback: infer from source_url
        src = (item.get("source_url") or "").strip()
        marker = "/rss/"
        if marker in src:
            cat = src.split(marker, 1)[1].strip()
    if not cat:
        return ""
    return cat

def load_relevant(path: str) -> List[Dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            normalize_articles_inplace(data)
        return data
    except Exception:
        return []

def load_index_articles(path: str = "data/index.json") -> List[Dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        articles = data.get("articles", []) or []
        if isinstance(articles, list):
            normalize_articles_inplace(articles)
        return articles
    except Exception:
        return []


def ensure_dirs():
    os.makedirs('docs/daily', exist_ok=True)


def daily_rss_filename(date_str: str) -> str:
    return f"{date_str}.xml"


def daily_rss_path(date_str: str) -> str:
    return os.path.join("docs/daily", daily_rss_filename(date_str))

def digest_links(articles: List[Dict]) -> str:
    links = sorted({(a.get("link") or "").strip() for a in articles if (a.get("link") or "").strip()})
    raw = "\n".join(links).encode("utf-8")
    return hashlib.md5(raw).hexdigest()

def format_date_display(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%Y/%m/%d")
    except Exception:
        return date_str

def count_unique_journals(items: List[Dict]) -> int:
    journals = {
        (item.get("journal") or "").strip().lower()
        for item in items
        if (item.get("journal") or "").strip()
    }
    return len(journals)

def count_arxiv_items(items: List[Dict]) -> int:
    return sum(1 for item in items if (item.get("journal") or "").strip().lower() == "arxiv")

def build_daily_tags(items: List[Dict]) -> List[str]:
    tags = ["AI", "物理", "化学", "材料", "交叉学科"]
    if any((item.get("journal") or "").strip().lower() == "arxiv" for item in items):
        tags.append("arXiv")
    if any(arxiv_badge(item) for item in items):
        tags.append("预印本追踪")
    return tags[:7]

def build_highlight_reason(item: Dict) -> str:
    reason = str(item.get("reason") or "").strip()
    if reason:
        return reason

    ai_score = item.get("ai_score")
    if ai_score is not None and str(ai_score).strip() != "":
        return f"AI相关度 {ai_score}"

    arxiv_cat = arxiv_badge(item)
    if arxiv_cat:
        return f"arXiv / {arxiv_cat}"

    journal = str(item.get("journal") or "").strip()
    if journal:
        return journal

    return "交叉重点"

def collect_focus_highlights(summary: Dict, items: List[Dict], limit: int = 8) -> List[Dict]:
    selected: List[Dict] = []
    seen = set()

    def add(item: Dict):
        if not isinstance(item, dict):
            return
        key = (item.get("link") or item.get("title_en") or item.get("title_zh") or "").strip()
        if not key or key in seen:
            return
        selected.append(item)
        seen.add(key)

    for group_name in ("ml_highlights", "ferro_highlights"):
        for item in summary.get(group_name, []) or []:
            add(item)
            if len(selected) >= limit:
                return selected[:limit]

    def score_key(item: Dict):
        try:
            return float(item.get("ai_score"))
        except Exception:
            return -1.0

    ranked = sorted(items, key=score_key, reverse=True)
    for item in ranked:
        add(item)
        if len(selected) >= limit:
            return selected[:limit]

    for item in items:
        add(item)
        if len(selected) >= limit:
            return selected[:limit]

    return selected[:limit]


def group_daily_items(items: List[Dict]) -> List[Dict]:
    groups = {
        "p1": {
            "title": "🔬 神经网络势 · 电子结构（重点）",
            "description": "神经网络势、哈密顿量、密度矩阵、电荷密度与电子结构。",
            "items": [],
        },
        "p2": {
            "title": "🧲 铁电 · 铁磁 · 多铁（物理）",
            "description": "铁电、铁磁、反铁磁、多铁及其耦合物理。",
            "items": [],
        },
        "p3": {
            "title": "🧩 其他交叉 / 方法",
            "description": "其他 AI × Science 交叉研究、材料与计算方法。",
            "items": [],
        },
    }

    for item in items:
        tier = priority_tier(item)
        key = "p1" if tier == 0 else "p2" if tier == 2 else "p3"
        groups[key]["items"].append(item)

    ordered = []
    for key in ("p1", "p2", "p3"):
        if groups[key]["items"]:
            groups[key]["items"].sort(key=focus_priority)
            ordered.append(groups[key])
    return ordered


def _adjacent_dates(date_str: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (prev_date, next_date) that actually have generated daily pages.

    Reads summaries.json (older→newer relative to date_str) and scans docs/daily/*.html
    as a fallback so newly-generated days without an index entry still link correctly.
    """
    dates: set = set()
    idx = load_summary_index()
    for s in idx.get("summaries", []) or []:
        d = s.get("date")
        if isinstance(d, str) and len(d) == 10:
            dates.add(d)
    daily_dir = "docs/daily"
    if os.path.isdir(daily_dir):
        for name in os.listdir(daily_dir):
            if name.endswith(".html") and len(name) == 15:  # YYYY-MM-DD.html
                dates.add(name[:-5])
    sorted_dates = sorted(dates)
    prev_d = None
    next_d = None
    for d in sorted_dates:
        if d < date_str:
            prev_d = d  # keep updating; last one < date_str wins
        elif d > date_str and next_d is None:
            next_d = d
            break
    return prev_d, next_d


def _render_date_nav(date_str: str, position: str = "top") -> str:
    prev_d, next_d = _adjacent_dates(date_str)
    prev_html = (
        f'<a class="daily-nav-link daily-nav-prev" href="{safe_text(prev_d)}.html">← 前一天 · {safe_text(prev_d)}</a>'
        if prev_d else
        '<span class="daily-nav-link daily-nav-disabled">← 前一天</span>'
    )
    next_html = (
        f'<a class="daily-nav-link daily-nav-next" href="{safe_text(next_d)}.html">后一天 · {safe_text(next_d)} →</a>'
        if next_d else
        '<span class="daily-nav-link daily-nav-disabled">后一天 →</span>'
    )
    return (
        f'<nav class="daily-nav daily-nav-{position}" aria-label="日报日期导航">'
        f'{prev_html}'
        f'<a class="daily-nav-home" href="../index.html#daily">📅 日报索引</a>'
        f'{next_html}'
        f'</nav>'
    )


def _en_title(it):
    """English title for classification/display. full_list often stores English under
    `title_en` with `title` empty/Chinese, while raw articles use `title`."""
    return (it.get("title_en") or it.get("title") or "").strip()


def _best_abstract(it):
    """Non-empty abstract text for analysis: English preferred, then Chinese, then summary.
    full_list drops the English abstract, so we must fall back to abstract_zh/summary."""
    return (it.get("abstract") or it.get("abstract_en") or it.get("abstract_zh")
            or it.get("summary") or "").strip()


def _classify(it):
    """Classify on resolved English title + best abstract (not the raw `it`, whose
    `title` may be empty/Chinese → would mis-bucket AI×交叉 papers as 其他)."""
    return classify_taxonomy({
        "title": _en_title(it),
        "summary": it.get("summary") or "",
        "abstract": _best_abstract(it),
    })


def build_core_export(core_items):
    """Pure helper: build the core-export list with category and abstract fields."""
    out = []
    for it in (core_items or []):
        out.append({
            "title": _en_title(it),
            "title_zh": it.get("title_zh") or "",
            "summary": it.get("summary") or it.get("abstract_zh") or "",
            "abstract": _best_abstract(it),
            "category": _classify(it),
            "link": (it.get("link") or "").strip(),
            "journal": it.get("journal") or "",
        })
    return out


def build_tier2_candidates(full_list, max_n=20):
    """Pure helper: select AI-intersection / core-focus candidates for deep analysis."""
    cand = []
    for it in (full_list or []):
        cat = _classify(it)
        if cat in ("AI×物理", "AI×化学·材料") or it.get("is_core_focus"):
            cand.append({
                "title": _en_title(it),
                "title_zh": it.get("title_zh") or "",
                "summary": it.get("summary") or it.get("abstract_zh") or "",
                "abstract": _best_abstract(it),
                "category": cat,
                "link": (it.get("link") or "").strip(),
                "journal": it.get("journal") or "",
            })
    return cand[:max_n]


def load_enrichment(date_str: str) -> Dict[str, Dict]:
    """读 data/arxiv_core_<date>.json → {normalize_link(link): enrich}。
    enrich = {deep_analysis, image, elements, category, title_zh}。
    仅返回真正带 deep_analysis 或 image 的行；缺文件/坏文件 → {}（安全降级）。"""
    out: Dict[str, Dict] = {}
    path = os.path.join("data", f"arxiv_core_{date_str}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        return out
    if not isinstance(rows, list):
        return out
    for r in rows:
        link = normalize_link((r.get("link") or "").strip())
        if not link:
            continue
        poster = r.get("poster") or {}
        image = r.get("image") or poster.get("image")
        deep = r.get("deep_analysis") or ""
        if not (deep or image):
            continue
        out[link] = {
            "deep_analysis": deep,
            "image": image,
            "elements": r.get("poster_elements") or poster.get("elements") or {},
            "category": r.get("category") or "",
            "title_zh": r.get("title_zh") or poster.get("title_zh") or "",
        }
    return out


def build_unified_items(full_list, enrich_map, aps_items):
    """合并 APS 全文(tier0) + full_list(tier1 富化 / tier2 普通) 成一个扁平列表，
    按 (tier, focus_priority) 排序。每项注入 _tier 与 _enrich(dict|None)。"""
    items: List[Dict] = []
    seen = set()
    for a in (aps_items or []):
        link = normalize_link((a.get("link") or a.get("doi") or "").strip())
        poster = a.get("poster") or {}
        image = poster.get("image") or a.get("image")
        deep = a.get("deep_analysis") or ""
        if not (deep or image):
            continue  # APS 无富化 → 跳过（APS 不在 full_list，不会丢可展示内容）
        it = dict(a)
        it["link"] = link or it.get("link") or ""
        it["_tier"] = 0
        it["_enrich"] = {
            "deep_analysis": deep, "image": image,
            "elements": poster.get("elements") or {},
            "category": a.get("category") or "",
            "title_zh": a.get("title_zh") or poster.get("title_zh") or "",
        }
        items.append(it)
        if link:
            seen.add(link)
    for it0 in (full_list or []):
        link = normalize_link((it0.get("link") or "").strip())
        if link and link in seen:
            continue
        it = dict(it0)
        it["link"] = link or it0.get("link") or ""
        en = enrich_map.get(link) if link else None
        it["_tier"] = 1 if en else 2
        it["_enrich"] = en
        items.append(it)
        if link:
            seen.add(link)
    items.sort(key=lambda x: (priority_tier(x), x.get("_tier", 2), focus_priority(x)))
    return items


def daily_quality_report(summary: Dict) -> Dict[str, int]:
    """Return non-empty required-field counts for a daily summary."""
    items = summary.get("full_list") or summary.get("summaries") or []
    return {
        "total": len(items),
        "title_zh": sum(bool(str(x.get("title_zh") or "").strip()) for x in items),
        "abstract_zh": sum(bool(str(x.get("abstract_zh") or "").strip()) for x in items),
        "abstract_zh_full": sum(bool(str(x.get("abstract_zh_full") or "").strip()) for x in items),
        "summary": sum(bool(str(x.get("summary") or "").strip()) for x in items),
        "relation": sum(all(bool(str(x.get(k) or "").strip()) for k in ("method_point", "related_work", "implication")) for x in items),
    }


def daily_quality_ok(summary: Dict) -> bool:
    report = daily_quality_report(summary)
    total = report["total"]
    if total <= 0:
        return bool(summary.get("overview"))
    # 与 daily_quality_report 取同一份 items（此前漏了这行，total>0 时必抛 NameError，
    # 被调用处的宽 except 吞掉 → sidecar 自 2026-07-31 起从未落盘）
    items = summary.get("full_list") or summary.get("summaries") or []
    detailed_ok = all(
        all(len(str(x.get(k) or "").strip()) >= 180 for k in ("method_point", "related_work", "implication"))
        for x in items
    )
    return all(report[k] == total for k in ("title_zh", "abstract_zh", "summary", "relation")) and detailed_ok and bool(
        summary.get("overview") and summary.get("trends")
    )


TOPIC_LABELS = {
    "physics": "物理 / 凝聚态",
    "chemistry": "化学 / 分子",
    "materials": "材料 / 器件",
    "methods": "方法 / 工具",
    "other": "其他",
}


def render_meta_chips(item: Dict) -> str:
    journal = safe_text(item.get("journal", ""))
    arxiv_cat = safe_text(arxiv_badge(item))
    authors = safe_text(format_authors(item.get("authors")))
    ai_score = item.get("ai_score")
    bucket = topic_bucket(item)
    topic_name = safe_text(TOPIC_LABELS.get(bucket, "相关"))
    category = safe_text(classify_taxonomy(item))
    tier = priority_tier(item)
    priority_label = "P1" if tier == 0 else "P2" if tier == 2 else "P3"
    meta_parts = [
        f"<span class='daily-chip daily-chip-topic'>🧭 {topic_name}</span>",
        f"<span class='daily-chip daily-chip-category daily-chip-priority-{priority_label.lower()}'>{priority_label} · {category}</span>",
    ]
    if journal:
        if arxiv_cat:
            meta_parts.append(f"<span class='daily-chip daily-chip-journal'>📖 {journal} / {arxiv_cat}</span>")
        else:
            meta_parts.append(f"<span class='daily-chip daily-chip-journal'>📖 {journal}</span>")
    if authors:
        meta_parts.append(f"<span class='daily-chip daily-chip-authors'>👤 {authors}</span>")
    if ai_score is not None and str(ai_score).strip() != "":
        meta_parts.append(f"<span class='daily-chip daily-chip-score'>🔥 AI {safe_text(ai_score)}</span>")
    return "".join(meta_parts)


def md_to_html(text):
    """把苏格拉底深析的轻量 markdown 渲染成安全 HTML。
    支持 #/##/### 标题、**粗体**、- / 数字. 列表、空行分段；
    先对每行 safe_text 转义，再套白名单标签，杜绝注入。"""
    if not text:
        return ""
    import re as _re
    lines = str(text).split("\n")
    out = []
    in_list = [False]

    def close_list():
        if in_list[0]:
            out.append("</ul>")
            in_list[0] = False

    def inline(s):
        s = safe_text(s)  # 先转义
        s = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)  # 在转义后的文本上加粗
        return s

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        m = _re.match(r'^(#{1,3})\s+(.*)$', line)
        if m:
            close_list()
            level = min(len(m.group(1)) + 1, 4)  # # → h2, ## → h3, ### → h4
            out.append(f"<h{level} class='deep-h'>{inline(m.group(2))}</h{level}>")
            continue
        m = _re.match(r'^\s*(?:[-*]|\d+\.)\s+(.*)$', line)
        if m:
            if not in_list[0]:
                out.append("<ul class='deep-ul'>")
                in_list[0] = True
            out.append(f"<li>{inline(m.group(1))}</li>")
            continue
        close_list()
        out.append(f"<p>{inline(line)}</p>")
    close_list()
    return "".join(out)


def render_unified_item(item: Dict, index: int) -> str:
    """单列表条目：列表态 = 中文标题 + 一句话亮点 + 标签(+含图深析徽标)；
    富化时 <details> 展开 = 信息图 + 中文5要素 + 深析正文。"""
    en = item.get("_enrich")
    ensure_relation_fields(item, load_research_profile())
    title_en = (item.get("title_en") or item.get("title") or "").strip()
    title_zh = (item.get("title_zh") or (en or {}).get("title_zh") or "").strip()
    show_zh = bool(title_zh) and title_zh.casefold() != title_en.casefold()
    disp_zh = safe_text(title_zh if show_zh else title_en)
    title_en_block = (f'<div class="daily-paper-title-en">{safe_text(title_en)}</div>'
                      if show_zh and title_en else "")
    meta_html = render_meta_chips(item)
    try:
        relevance = float(item.get("focus_score")) if item.get("focus_score") is not None else core_score(item) * 10
    except (TypeError, ValueError):
        relevance = core_score(item) * 10
    relevance = max(0.0, min(10.0, relevance))
    relevance_label = f"{relevance:.1f}".rstrip("0").rstrip(".")
    relevance_html = (
        f'<div class="daily-relevance" aria-label="相关度 {relevance_label} / 10">'
        f'<span>相关度</span><div class="daily-relevance-track"><i class="daily-relevance-bar" style="width:{relevance * 10:.1f}%"></i></div>'
        f'<strong>{relevance_label}</strong></div>'
    )
    abstract_zh = (item.get("abstract_zh_full") or item.get("abstract_zh") or "").strip()
    abs_html = (f'<p class="daily-paper-abstract"><strong>📄 摘要：</strong>{safe_text(abstract_zh)}</p>'
                if abstract_zh else "")
    abstract_en = (item.get("abstract") or "").strip()
    abs_en_html = (f'<details class="daily-abstract-en"><summary>📖 英文原文</summary>'
                   f'<p class="daily-abstract-en-body">{safe_text(abstract_en)}</p></details>'
                   if abstract_en else "")
    highlight = (item.get("summary") or item.get("one_sentence_summary") or "").strip()
    hl_html = (f'<p class="daily-paper-highlight"><strong>💡 亮点：</strong>{safe_text(highlight)}</p>'
               if highlight else "")
    relation_html = (
        '<details class="daily-research-relation"><summary>🔬 与我们研究方向的关系</summary>'
        f'<p><strong>📐 方法要点：</strong>{safe_text(item.get("method_point") or "")}</p>'
        f'<p><strong>🔗 相关工作关联：</strong>{safe_text(item.get("related_work") or "")}</p>'
        f'<p><strong>💡 对你方向的启示：</strong>{safe_text(item.get("implication") or "")}</p>'
        '</details>'
    )
    link = safe_url(item.get("link") or "")
    badge = '<span class="enrich-badge">📊 含图深析</span>' if en else ""
    details = ""
    if en:
        img = en.get("image")
        img_src = img if (not img or str(img).startswith(("http", "/", "../"))) else f"../{img}"
        figure = (f'<div class="poster-figure"><img loading="lazy" src="{safe_text(img_src)}" '
                  f'onerror="this.style.display=\'none\'"></div>') if img else ""
        el = en.get("elements") or {}
        rows = "".join(
            f'<div class="poster-row"><b>{safe_text(k)}</b>{safe_text(el.get(k, ""))}</div>'
            for k in ["研究问题", "创新方法", "工作流程", "关键结果", "应用价值"] if el.get(k))
        elems = f'<div class="daily-deep-elements">{rows}</div>' if rows else ""
        deep = en.get("deep_analysis") or ""
        deep_html = f'<div class="deep-body">{md_to_html(deep)}</div>' if deep else ""
        details = (f'<details class="enrich-details"><summary>📖 展开分析 + 配图</summary>'
                   f'{figure}{elems}{deep_html}</details>')
    return f"""
    <li class="daily-paper-card" id="paper-{index}" data-bookmark-key="{link}">
        <span class="daily-paper-number">{index:02d}</span>
        <div class="daily-paper-body">
            <div class="daily-paper-head"><div class="daily-paper-titles">
                <div class="daily-paper-title-zh">{disp_zh}</div>
                {title_en_block}
            </div>{badge}</div>
            <div class="daily-paper-meta">{meta_html}</div>
            {relevance_html}
            {abs_html}
            {abs_en_html}
            {hl_html}
            {relation_html}
            {details}
            <div class="daily-paper-actions"><a class="daily-news-link" href="{link}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a></div>
        </div>
    </li>
    """


def render_focus_section(focus_items: List[Dict]) -> str:
    """「🎯 与你方向相关」区块：当日 focus_score 命中的文章按分数降序排列，
    卡片含 简单总结 / 与我们工作的关系 / 进一步工作建议 三行（空字段跳过）。
    无匹配文章（旧数据无 focus 字段）时返回空串，区块整体隐藏。"""
    items = [it for it in (focus_items or []) if isinstance(it, dict) and it.get("focus_score")]
    if not items:
        return ""

    def _score(it) -> float:
        try:
            return float(it.get("focus_score") or 0)
        except (TypeError, ValueError):
            return 0.0

    items = sorted(items, key=_score, reverse=True)
    cards = []
    for i, it in enumerate(items, 1):
        title = (it.get("title_zh") or it.get("title_en") or it.get("title") or "").strip()
        journal = (it.get("journal") or "").strip()
        pub_date = (it.get("pub_date") or it.get("date") or "").strip()
        summary_zh = (it.get("focus_summary") or "").strip()
        relation = (it.get("focus_relation") or "").strip()
        suggestion = (it.get("focus_suggestion") or "").strip()
        link = safe_url(it.get("link") or "")
        meta_parts = []
        if journal:
            meta_parts.append(f"<span class='daily-chip daily-chip-journal'>📖 {safe_text(journal)}</span>")
        if pub_date:
            meta_parts.append(f"<span class='daily-chip'>📅 {safe_text(pub_date)}</span>")
        meta_parts.append(f"<span class='daily-chip daily-chip-focus'>🎯 相关度 {safe_text(it.get('focus_score'))}</span>")
        analysis_parts = []
        if summary_zh:
            analysis_parts.append(f"<p><strong>📝 简单总结：</strong>{safe_text(summary_zh)}</p>")
        if relation:
            analysis_parts.append(f"<p><strong>🔗 与我们工作的关系：</strong>{safe_text(relation)}</p>")
        if suggestion:
            analysis_parts.append(f"<p><strong>💡 进一步工作建议：</strong>{safe_text(suggestion)}</p>")
        analysis = f"<div class='daily-focus-deep'>{''.join(analysis_parts)}</div>" if analysis_parts else ""
        cards.append(f"""
        <li class="daily-focus-card" data-bookmark-key="{link}">
            <div class="daily-focus-number">{i:02d}</div>
            <div class="daily-focus-body">
                <div class="daily-focus-title"><a class="daily-focus-link" href="{link}" target="_blank" rel="noopener noreferrer">{safe_text(title)}</a></div>
                <div class="daily-focus-meta">{''.join(meta_parts)}</div>
                {analysis}
            </div>
        </li>
        """)
    return f"""
    <section id="focus-interest" class="daily-section daily-focus-section">
      <div class="daily-section-head">
        <span class="daily-section-index">🎯</span>
        <h2 class="daily-section-title">与你方向相关</h2>
        <span class="daily-focus-count">{len(items)} 篇</span>
      </div>
      <ol class="daily-focus-list">{''.join(cards)}</ol>
    </section>
    """


def render_daily_html(date_str: str, summary: Dict) -> str:
    # 「今日精读」(deep-read) section, populated from APS full-text papers.
    # Absent/broken file → empty list → page identical to before.
    aps_items: List[Dict] = []
    try:
        aps_path = os.path.join("data", f"aps_{date_str}.json")
        if os.path.exists(aps_path):
            with open(aps_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                aps_items = loaded
    except Exception:
        aps_items = []
    enrich_map = load_enrichment(date_str)

    items = summary.get("full_list") or summary.get("summaries") or []
    unified = build_unified_items(items, enrich_map, aps_items)
    focus_items = [it for it in unified if it.get("focus_score")]
    enriched_count = sum(1 for it in unified if it.get("_enrich"))
    tag_list = build_daily_tags(items)
    display_date = format_date_display(date_str)
    journal_count = count_unique_journals(items)
    arxiv_count = count_arxiv_items(items)
    excluded_count = int(summary.get("excluded_count") or 0)
    raw_total = int(summary.get("raw_total") or (len(items) + excluded_count))
    focused_total = int(summary.get("focused_total") or len(items))

    grouped_html = []
    item_index = 1
    for group in group_daily_items(unified):
        cards = []
        for item in group["items"]:
            cards.append(render_unified_item(item, item_index))
            item_index += 1
        grouped_html.append(
            f'<section class="daily-priority-group"><h3>{safe_text(group["title"])}</h3>'
            f'<p>{safe_text(group["description"])}</p><ol class="daily-paper-list">{"".join(cards)}</ol></section>'
        )
    unified_html = "".join(grouped_html) or '<ol class="daily-paper-list"><li class="daily-summary-card"><p>今日无目标方向文献。</p></li></ol>'

    profile = load_research_profile()
    for it in items:
        ensure_relation_fields(it, profile)
    overview = safe_text(summary.get('overview', '') or f"今日共收录{len(items)}篇文献。")
    trends = safe_text(summary.get('trends', '') or "今日热点围绕机器学习、计算物理和功能材料展开，具体结论以原文摘要为准。")
    direction_note = safe_text(summary.get('research_direction_note', '') or build_direction_note(items, profile))
    tags_html = "".join(f"<span class='daily-tag'>{safe_text(tag)}</span>" for tag in tag_list)
    tagline = " | ".join(safe_text(tag) for tag in tag_list)

    sidebar_stats = (
        f"<div class='daily-sidebar-fact'><span>文献总数</span><strong>{len(unified)}</strong></div>"
        f"<div class='daily-sidebar-fact'><span>含图深析</span><strong>{enriched_count}</strong></div>"
        f"<div class='daily-sidebar-fact'><span>期刊数</span><strong>{journal_count}</strong></div>"
    )
    from daily_viz import render_priority_svg, render_source_split_svg, render_topic_distribution_svg
    daily_viz_html = (
        '<section class="daily-viz" aria-label="今日概览"><h2>📊 今日概览</h2><div class="daily-viz-grid">'
        + render_topic_distribution_svg(unified)
        + render_source_split_svg(unified)
        + render_priority_svg(unified)
        + '</div></section>'
    )

    filtered_note = ''
    if excluded_count > 0 or focused_total > len(items):
        filtered_note = f"<p class='daily-filter-note'>原始候选 {raw_total} 篇中，已剔除 {excluded_count} 篇明显偏离主线的内容，并从剩余 {focused_total} 篇主线相关文献中精选 {len(items)} 篇进入日报页，优先保留 AI × 物理 / 化学 / 材料交叉与关键计算方法工作。</p>"

    def render_core_section(core_items: List[Dict], note: str) -> str:
        if not core_items:
            return ""
        note_html = f"<p class='daily-core-note'>{safe_text(note)}</p>" if note else ""
        cards = []
        for i, it in enumerate(core_items, 1):
            ensure_relation_fields(it, load_research_profile())
            title_zh = safe_text((it.get('title_zh') or '').strip())
            title_en = safe_text((it.get('title_en') or it.get('title') or '').strip())
            show_zh_block = bool(title_zh) and title_zh.casefold() != title_en.casefold()
            journal = safe_text(it.get('journal') or '')
            abstract_zh = safe_text((it.get('abstract_zh_full') or it.get('abstract_zh') or '').strip())
            one_sentence = safe_text((it.get('summary') or '').strip())
            mp = safe_text((it.get('method_point') or '').strip())
            rw = safe_text((it.get('related_work') or '').strip())
            im = safe_text((it.get('implication') or '').strip())
            link = safe_url(it.get('link') or '')
            title_en_block = f"<div class='daily-core-title-en'>{title_en}</div>" if show_zh_block else ""
            display_title = title_zh if show_zh_block else title_en
            deep_block = ""
            if mp or rw or im:
                deep_parts = []
                if mp: deep_parts.append(f"<p><strong>📐 方法要点：</strong>{mp}</p>")
                if rw: deep_parts.append(f"<p><strong>🔗 相关工作关联：</strong>{rw}</p>")
                if im: deep_parts.append(f"<p><strong>💡 对你方向的启示：</strong>{im}</p>")
                deep_block = f"<div class='daily-core-deep'>{''.join(deep_parts)}</div>"
            abstract_html = f"<p class='daily-paper-abstract'><strong>📄 摘要：</strong>{abstract_zh}</p>" if abstract_zh else ""
            highlight_html = f"<p class='daily-paper-highlight'><strong>💡 亮点：</strong>{one_sentence}</p>" if one_sentence else ""
            cards.append(f"""
            <li class="daily-core-card" data-bookmark-key="{safe_url(it.get('link') or '')}">
                <div class="daily-core-number">{i:02d}</div>
                <div class="daily-core-body">
                    <div class="daily-core-title-zh">{display_title}</div>
                    {title_en_block}
                    <div class="daily-core-meta"><span class="daily-chip daily-chip-core">🎯 核心关注</span><span class="daily-chip daily-chip-journal">📖 {journal}</span></div>
                    {abstract_html}
                    {highlight_html}
                    {deep_block}
                    <div class="daily-paper-actions"><a class="daily-news-link" href="{link}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a></div>
                </div>
            </li>
            """)
        return f"""
        <section id="core-focus" class="daily-section daily-core-section">
          <div class="daily-section-head">
            <span class="daily-section-index">🎯</span>
            <h2 class="daily-section-title">核心关注（ML × ferro / 凝聚态）</h2>
            <span class="daily-core-count">{len(core_items)} 篇</span>
          </div>
          {note_html}
          <ol class="daily-core-list">{''.join(cards)}</ol>
        </section>
        """

    date_nav_top = _render_date_nav(date_str, position="top")
    date_nav_bottom = _render_date_nav(date_str, position="bottom")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{date_str} 文献日报 - 文献追踪系统</title>
  <link rel="stylesheet" href="../style.css" />
  <link rel="stylesheet" href="../bookmarks.css" />
  <script defer src="../exports.js"></script>
  <script defer src="../bookmarks.js"></script>
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="default" />
  <meta name="apple-mobile-web-app-title" content="文献追踪" />
  <meta name="theme-color" content="#f59e0b" />
  <link rel="alternate" type="application/rss+xml" title="{safe_text(date_str)} 日报 RSS" href="{daily_rss_filename(date_str)}" />
  <link rel="stylesheet" href="../daily-common.css" />
</head>
<body>
  <div class="daily-shell">
    <div class="daily-topbar">
      <div class="daily-topbar-left">
        <a href="../index.html" class="back-link">← 返回主页</a>
        <span class="daily-mini-chip">AI × Science Daily</span>
      </div>
      <div class="daily-topbar-right">
        <a href="{daily_rss_filename(date_str)}" class="daily-mini-chip">📡 当日 RSS</a>
        <a href="../feed.xml" class="daily-mini-chip">📰 全站 RSS</a>
        <span class="daily-mini-chip">{safe_text(date_str)}</span>
        <button class="theme-toggle" id="themeToggle" onclick="toggleTheme()" title="切换主题">🌙</button>
      </div>
    </div>
    {date_nav_top}
    <nav class="daily-toc-sticky" aria-label="移动目录">
      <a href="#summary">摘要</a>
      {('<a href="#core-focus">核心关注</a>' if summary.get('core_items') else '')}
      {('<a href="#focus-interest">与你相关</a>' if focus_items else '')}
      <a href="#papers">今日文献</a>
    </nav>
    <div class="daily-layout">
      <article class="daily-article">
        <div class="daily-hero">
          <div class="daily-kicker">AI 文献日报</div>
          <h1 class="daily-title">AI × Science 文献日报 {display_date}</h1>
          <p class="daily-subtitle">聚焦 AI × 物理 / 化学 / 材料交叉方向，自动过滤明显偏题的医学、教育与社会科学内容，并按主题重新排版，便于快速深读。</p>
          <blockquote class="daily-quote">{tagline}</blockquote>
          <div class="daily-tags">{tags_html}</div>
          <div class="daily-stats">
            <div class="daily-stat">
              <div class="daily-stat-label">日报精选</div>
              <div class="daily-stat-value">{len(items)}</div>
            </div>
            <div class="daily-stat">
              <div class="daily-stat-label">主线候选</div>
              <div class="daily-stat-value">{focused_total}</div>
            </div>
            <div class="daily-stat">
              <div class="daily-stat-label">期刊 / 来源</div>
              <div class="daily-stat-value">{journal_count}</div>
            </div>
            <div class="daily-stat">
              <div class="daily-stat-label">arXiv 相关</div>
              <div class="daily-stat-value">{arxiv_count}</div>
            </div>
          </div>
          {daily_viz_html}
          {filtered_note}
        </div>

        {render_core_section(summary.get('core_items', []) or [], summary.get('core_direction_note') or '')}
        {render_focus_section(focus_items)}
        <section id="summary" class="daily-section">
          <div class="daily-section-head">
            <span class="daily-section-index">01</span>
            <h2 class="daily-section-title">今日摘要</h2>
          </div>
          <div class="daily-summary-card">
            <p><strong>总览：</strong>{overview}</p>
            <p><strong>热点：</strong>{trends}</p>
            <p><strong>与我们研究方向的总体关系：</strong>{direction_note}</p>
          </div>
        </section>

        <section id="papers" class="daily-section">
          <div class="daily-section-head">
            <span class="daily-section-index">📚</span>
            <h2 class="daily-section-title">今日文献</h2>
            <span class="daily-core-count">{len(unified)} 篇 · {enriched_count} 含图深析</span>
          </div>
          {unified_html}
        </section>

        {date_nav_bottom}

        <div class="daily-footer">
          本页由文献追踪系统自动生成，仅保留 AI × 物理 / 化学 / 材料主线相关文献，并按专题重新整理，方便快速筛选与深度阅读。
        </div>
      </article>

      <aside class="daily-toc">
        <div class="daily-toc-card">
          <div class="daily-toc-title">目录</div>
          {'<a href="#core-focus"><span>🎯 核心关注</span><span>00</span></a>' if summary.get('core_items') else ''}
          {'<a href="#focus-interest"><span>🎯 与你相关</span><span>🔍</span></a>' if focus_items else ''}
          <a href="#summary"><span>今日摘要</span><span>01</span></a>
          <a href="#papers"><span>今日文献</span><span>📚</span></a>

          <div class="daily-sidebar-block">
            <div class="daily-sidebar-title">专题分布</div>
            <div class="daily-sidebar-stats">{sidebar_stats}</div>
          </div>

          <div class="daily-sidebar-block">
            <div class="daily-sidebar-title">阅读建议</div>
            <ul class="insight-note-list">
              <li class="insight-note-item">含「📊 含图深析」徽标的条目可点开看信息图、中文要素与深度分析。</li>
              <li class="insight-note-item">明显偏离主线的医学、教育等内容已自动剔除。</li>
              <li class="insight-note-item">若需回看历史日报，可从日报索引页按日期倒序进入。</li>
            </ul>
          </div>
        </div>
      </aside>
    </div>
  </div>

  <script>
    const THEME_KEY = 'literature_theme';

    function initTheme() {{
      const theme = localStorage.getItem(THEME_KEY) || 'light';
      document.documentElement.setAttribute('data-theme', theme);
      updateThemeButton();
    }}

    function toggleTheme() {{
      const current = document.documentElement.getAttribute('data-theme') || 'light';
      const next = current === 'light' ? 'dark' : 'light';
      localStorage.setItem(THEME_KEY, next);
      document.documentElement.setAttribute('data-theme', next);
      updateThemeButton();
    }}

    function updateThemeButton() {{
      const btn = document.getElementById('themeToggle');
      const theme = document.documentElement.getAttribute('data-theme') || 'light';
      if (btn) btn.textContent = theme === 'light' ? '🌙' : '☀️';
    }}

    initTheme();
  </script>
</body>
</html>
"""

def update_index(date_str: str, total: int):
    index_path = os.path.join('docs/daily', 'summaries.json')
    data = {"summaries": []}
    if os.path.exists(index_path):
        try:
            with open(index_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = {"summaries": []}
    summaries = [s for s in data.get('summaries', []) if s.get('date') != date_str]
    summaries.insert(0, {"date": date_str, "file": f"{date_str}.html", "total": total})
    data["summaries"] = summaries[:120]
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_summary_index() -> Dict:
    index_path = os.path.join("docs/daily", "summaries.json")
    if not os.path.exists(index_path):
        return {"summaries": []}
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f) or {"summaries": []}
    except Exception:
        return {"summaries": []}

def save_summary_index(summaries: List[Dict]):
    index_path = os.path.join("docs/daily", "summaries.json")
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({"summaries": summaries}, f, ensure_ascii=False, indent=2)

def preserve_existing_entry(prev: Dict, date_str: str) -> Dict:
    preserved = dict(prev or {})
    preserved["date"] = date_str
    preserved["file"] = preserved.get("file") or f"{date_str}.html"
    if "total" not in preserved:
        preserved["total"] = 0
    return preserved


def collect_daily_articles(index_articles: List[Dict], relevant_articles: List[Dict], day_str: str) -> Dict:
    relevant_day = [a for a in relevant_articles if (a.get("pub_date") or "").startswith(day_str)]

    # ai_relevant.json 在中文/focus 富化之前写入，条目缺 abstract_zh*/focus_* 字段；
    # index.json 中的同 link 条目才是富化后的完整版，按 link 补齐缺失字段
    index_by_link = {a.get("link"): a for a in index_articles if a.get("link")}
    _MERGE_KEYS = (
        "title_zh", "abstract_zh", "abstract_zh_full",
        "focus_score", "focus_summary", "focus_relation", "focus_suggestion",
    )

    def _merge_from_index(a: Dict) -> Dict:
        idx = index_by_link.get(a.get("link"))
        if not idx:
            return a
        merged = a
        for k in _MERGE_KEYS:
            if not merged.get(k) and idx.get(k):
                if merged is a:
                    merged = dict(a)
                merged[k] = idx[k]
        return merged

    relevant_day = [_merge_from_index(a) for a in relevant_day]
    relevant_links = {a.get("link") for a in relevant_day if a.get("link")}

    index_day = [
        a for a in index_articles
        if (a.get("pub_date") or "").startswith(day_str) and (a.get("link") not in relevant_links)
    ]

    raw_day_articles = relevant_day + index_day
    focused_articles, dropped_articles = filter_focus_items(raw_day_articles)
    focused_articles = sorted(focused_articles, key=focus_priority)
    daily_articles, overflow_articles = filter_daily_focus_items(focused_articles, min_keep=12, max_keep=60)
    # AI 富化(focus/亮点)已移出本热函数:collect_daily_articles 在主循环(每天)+
    # sync_daily_rss_feeds(每条最多 120 次)都会被调用,若在此调 AI 会把调用量放大到 ~124×
    # 导致 generate step 撞 90min 超时。改在 main() 生成路径按【全局预算】每天调一次。
    daily_articles = sorted(daily_articles, key=lambda item: (priority_tier(item), focus_priority(item)))
    return {
        "raw_day_articles": raw_day_articles,
        "focused_articles": focused_articles,
        "dropped_articles": dropped_articles,
        "daily_articles": daily_articles,
        "overflow_articles": overflow_articles,
    }


def sync_daily_rss_feeds(index_articles: List[Dict], relevant_articles: List[Dict], summaries: List[Dict]) -> int:
    changed = 0
    for entry in summaries:
        day_str = str(entry.get("date") or "").strip()
        if not day_str:
            continue
        collected = collect_daily_articles(index_articles, relevant_articles, day_str)
        if generate_daily_rss_feed(day_str, collected["daily_articles"], daily_rss_path(day_str)):
            changed += 1

    latest_date = str((summaries[0] or {}).get("date") or "").strip() if summaries else ""
    latest_source = daily_rss_path(latest_date) if latest_date else ""
    latest_target = os.path.join("docs/daily", "latest.xml")
    if latest_source and os.path.exists(latest_source):
        shutil.copyfile(latest_source, latest_target)
    return changed

def _load_cached_summary(date_str: str):
    """读 data/daily_summary_<date>.json（正常生成时写入的完整 summary）。缺失/坏 → None。
    供 --rerender-only 复用 overview/trends/full_list，避免重新调用 AI。"""
    try:
        with open(os.path.join("data", f"daily_summary_{date_str}.json"), "r", encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else None
    except Exception:
        return None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=None, help='YYYY-MM-DD (Beijing). 默认使用北京时间昨天以保证日报完整。')
    parser.add_argument('--days', default="1", help='生成最近 N 天（包含 --date 指定的日期）。用于补回漏抓/晚到数据。')
    parser.add_argument('--force', action='store_true', help='强制重新生成（忽略 summaries.json 中的 digest/total 缓存）。')
    parser.add_argument('--rerender-only', action='store_true',
                        help='只重渲染最近 N 天的 HTML（复用 data/daily_summary_*.json 缓存 + 新鲜 arxiv_core/aps 富化），不调用 AI、不抓取。')
    parser.add_argument('--send-email', action='store_true', help='为目标日期发送一次富 HTML 日报邮件（带防重）。')
    args = parser.parse_args()

    # 默认生成“北京时间昨天”的日报：与 Actions 的抓取频率 (08:00/20:00) 匹配，避免当天数据不全导致“摘要缺失/为0”。
    date_str = args.date or beijing_yesterday()
    try:
        days = max(1, int(str(args.days).strip()))
    except Exception:
        days = 1

    ensure_dirs()

    if args.rerender_only:
        # 仅重渲染：复用已落盘的 summary（含 overview/trends/full_list/core_items），
        # render_daily_html 会读最新 arxiv_core/aps 富化。绝不调用 AI / 不抓取。
        base_dt = datetime.strptime(date_str, "%Y-%m-%d")
        wanted = sorted({(base_dt - timedelta(days=k)).strftime("%Y-%m-%d") for k in range(days)},
                        reverse=True)
        n = 0
        rerendered_files = []
        email_summary = None
        for ds in wanted:
            summ = _load_cached_summary(ds)
            if not summ:
                print(f"⏭️  rerender skip {ds}: 无 daily_summary sidecar")
                continue
            # 邮件先于质量门取值：质量不达标也照常发信，只是不覆盖已有页面
            if ds == date_str:
                email_summary = summ
            if not summ.get("quality_ok", True):
                print(f"⏭️  rerender skip {ds}: 缓存 summary 未过质量门，保留既有页面")
                continue
            html = render_daily_html(ds, summ)
            with open(os.path.join("docs/daily", f"{ds}.html"), "w", encoding="utf-8") as f:
                f.write(html)
            rerendered_files.append(f"{ds}.html")
            n += 1
            print(f"♻️  re-rendered {ds} with fresh enrichment")
        # Re-apply the enhancer post-processing (day-nav / single-page outline / title links):
        # rerender overwrote the raw render_daily_html output, dropping the prior enhancement.
        if rerendered_files:
            try:
                from daily_page_enhancer import enhance_daily_archive
                enhanced = enhance_daily_archive("docs/daily/summaries.json", files=rerendered_files)
                print(f"🧭 Re-enhanced {enhanced} re-rendered page(s)")
            except Exception as e:
                print(f"⚠️ rerender enhancer skipped: {e}")
        print(f"♻️  re-rendered {n} daily page(s) (no AI)")
        if args.send_email:
            try:
                if email_summary:
                    from daily_email import send_daily_email
                    send_daily_email(email_summary, date_str)
                else:
                    print(f"⏭️ 每日邮件跳过：无 {date_str} summary")
            except Exception as exc:
                print(f"⚠️ 每日邮件跳过，日报流程继续: {exc}")
        return

    # Prefer full daily list from index.json, but always union with ai_relevant.json
    # to avoid omitting focus-relevant papers.
    index_articles = load_index_articles("data/index.json")
    relevant_articles = load_relevant("data/ai_relevant.json")

    existing_index = load_summary_index()
    existing_items = existing_index.get("summaries", []) or []
    existing_by_date = {s.get("date"): s for s in existing_items if isinstance(s, dict) and s.get("date")}

    new_entries: List[Dict] = []
    
    # Provider 选择顺序：环境变量 AI_PROVIDER > config.py 默认值 > 'kimi'
    use_local_kimi = os.environ.get('AI_PROVIDER', '').lower() == 'localkimi'
    api_key = (
        os.environ.get('AI_API_KEY')
        or os.environ.get('KIMI_API_KEY')
        or os.environ.get('GEMINI_API_KEY')
    )
    provider = os.environ.get('AI_PROVIDER') or 'kimi'
    
    if use_local_kimi:
        # 本地模式：不初始化远程API，使用LocalKimiProvider
        print("🤖 使用本地Kimi模式（通过OpenClaw AI助手）")
        summarizer = AISummarizer('localkimi', 'dummy_key')
        # 替换provider
        summarizer.provider = build_provider_extended('localkimi', 'dummy_key')
    elif api_key:
        summarizer = AISummarizer(provider, api_key)
    else:
        summarizer = None

    base_dt = datetime.strptime(date_str, "%Y-%m-%d")
    # 跨天共享的【全局】富化预算:只在真正生成非空页时消耗,--days N 不再放大 AI 调用。
    enrich_budget = _new_daily_enrich_budget()
    # Generate newest -> oldest to keep logs intuitive.
    for i in range(days):
        day_dt = base_dt - timedelta(days=i)
        day_str = day_dt.strftime("%Y-%m-%d")

        collected = collect_daily_articles(index_articles, relevant_articles, day_str)
        raw_day_articles = collected["raw_day_articles"]
        focused_articles = collected["focused_articles"]
        dropped_articles = collected["dropped_articles"]
        daily_articles = collected["daily_articles"]

        total = len(daily_articles)
        digest = digest_links(daily_articles) if daily_articles else ""

        out_path = os.path.join("docs/daily", f"{day_str}.html")
        prev = existing_by_date.get(day_str) or {}
        prev_digest = str(prev.get("digest") or "")
        prev_total = prev.get("total")

        should_skip = (
            (not args.force)
            and os.path.exists(out_path)
            and prev_digest
            and prev_digest == digest
            and isinstance(prev_total, int)
            and prev_total == total
        )

        if should_skip:
            print(f"⏭️  Skip daily page (unchanged): {out_path}")
            entry = {"date": day_str, "file": f"{day_str}.html", "total": total, "digest": digest}
            if prev.get("generated_by"):
                entry["generated_by"] = prev["generated_by"]
            new_entries.append(entry)
        else:
            try:
                if not daily_articles:
                    # still generate empty page so index shows date
                    summary = {
                        "date": day_str,
                        "total": 0,
                        "overview": "今日无符合 AI × 物理 / 化学 / 材料主线的文献。",
                        "trends": "",
                        "summaries": [],
                        "excluded_count": len(dropped_articles),
                        "raw_total": len(raw_day_articles),
                        "focused_total": len(focused_articles),
                    }
                else:
                    # 富化(focus 覆盖 + 亮点保障)只在真正生成非空页时进行,按【全局预算】
                    # 每天调一次(跳过的天不浪费 AI),然后按优先级重排。
                    _apply_daily_enrichment(daily_articles, enrich_budget)
                    daily_articles = sorted(daily_articles, key=lambda item: (priority_tier(item), focus_priority(item)))
                    if summarizer is None:
                        raise ValueError("AI_API_KEY is empty; cannot generate daily summary")
                    summary = summarizer.generate_daily_summary(daily_articles, day_str)
                    if summary.get("generated_by") == "fallback" and os.path.exists(out_path):
                        # AI failed: don't overwrite a previously good page with the
                        # degraded fallback (no abstracts/core/focus). Keep the old page.
                        print(f"⚠️ AI fallback for {day_str}, preserving existing page")
                        new_entries.append(preserve_existing_entry(prev, day_str))
                        continue
                    summary["excluded_count"] = len(dropped_articles)
                    summary["raw_total"] = len(raw_day_articles)
                    summary["focused_total"] = len(focused_articles)

                # ---- Core-focus deep fields (ML × ferro/凝聚态) ----
                try:
                    from config import CORE_FOCUS_CONFIG
                except Exception:
                    CORE_FOCUS_CONFIG = {"enabled": True, "daily_max_items": 8, "min_score": 0.60}
                if CORE_FOCUS_CONFIG.get("enabled", True) and summarizer is not None:
                    full = summary.get("full_list", []) or []
                    min_score = float(CORE_FOCUS_CONFIG.get("min_score", 0.60))
                    max_n = int(CORE_FOCUS_CONFIG.get("daily_max_items", 8))
                    core_items = [
                        it for it in full
                        if it.get("is_core_focus") and float(it.get("core_score") or 0.0) >= min_score
                    ]
                    core_items.sort(key=lambda x: -float(x.get("core_score") or 0.0))
                    core_items = core_items[:max_n]
                    # Export ONLY the tier-2 candidate list (core-focus ∪ AI×交叉) for run_deep
                    # to enrich. run_deep is the SOLE writer of arxiv_core_<date>.json (with
                    # deep_analysis/image) — daily must NOT write arxiv_core, or it would clobber
                    # run_deep's enrichment (daily runs after run_deep in the workflow) and break the
                    # idempotent cache, causing tier-2 to be regenerated every run. Never break generation.
                    try:
                        os.makedirs("data", exist_ok=True)
                        tier2 = build_tier2_candidates(summary.get("full_list", []))
                        with open(os.path.join("data", f"arxiv_tier2_{day_str}.json"), "w", encoding="utf-8") as tf:
                            json.dump(tier2, tf, ensure_ascii=False, indent=2)
                    except Exception as e:
                        print(f"⚠️ arxiv tier2 export skipped: {e}")
                    if core_items:
                        try:
                            deep_fields, direction_note = summarizer.generate_core_deep_fields(core_items, day_str)
                        except Exception as e:
                            print(f"⚠️ core deep-fields skipped: {e}")
                            deep_fields, direction_note = {}, ""
                        for it in core_items:
                            link = it.get("link") or ""
                            info = deep_fields.get(link, {})
                            it["method_point"] = info.get("method_point", "")
                            it["related_work"] = info.get("related_work", "")
                            it["implication"] = info.get("implication", "")
                        summary["core_items"] = core_items
                        summary["core_direction_note"] = direction_note
                    else:
                        summary["core_items"] = []
                        summary["core_direction_note"] = ""

                # Persist the full summary (overview/trends/full_list/core_items) so
                # --rerender-only can re-render HTML with FRESH enrichment (arxiv_core/aps)
                # WITHOUT calling AI again. Never break generation on sidecar failure.
                # sidecar 无条件落盘，质量判定作为字段随行：质量门只用于挡住"降级内容覆盖
                # 已有好页面"(见 --rerender-only)，不再连带阻断每日邮件 —— 否则质量门一失败
                # 邮件就整天发不出去。
                try:
                    quality = daily_quality_report(summary)
                    summary["quality_ok"] = daily_quality_ok(summary)
                    print(f"📋 daily quality {day_str}: {quality} (ok={summary['quality_ok']})")
                    with open(os.path.join("data", f"daily_summary_{day_str}.json"), "w", encoding="utf-8") as sf:
                        json.dump(summary, sf, ensure_ascii=False)
                except Exception as e:
                    print(f"⚠️ daily summary sidecar skip {day_str}: {e}")

                page_html = render_daily_html(day_str, summary)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(page_html)
                print(f"✅ Daily page generated: {out_path} (daily {len(daily_articles)} / focus {len(focused_articles)} / raw {len(raw_day_articles)})")
                generated_by = summary.get("generated_by") or ("fallback" if summarizer is None else "kimi")
                new_entries.append({"date": day_str, "file": f"{day_str}.html", "total": total, "digest": digest, "generated_by": generated_by})
            except Exception as exc:
                has_existing_page = os.path.exists(out_path)
                if has_existing_page:
                    print(f"⚠️ Daily page generation failed for {day_str}, preserving existing page: {exc}")
                    new_entries.append(preserve_existing_entry(prev, day_str))
                else:
                    print(f"⚠️ Daily page generation failed for {day_str}, skipping this date for now: {exc}")

    # Merge index entries: update our generated dates, keep others, then sort by date desc.
    updated_dates = {e.get("date") for e in new_entries if e.get("date")}
    merged = [e for e in existing_items if e.get("date") not in updated_dates]
    merged.extend(new_entries)
    merged = [e for e in merged if isinstance(e, dict) and e.get("date")]
    merged.sort(key=lambda x: x.get("date") or "", reverse=True)
    save_summary_index(merged[:120])
    rss_changed = sync_daily_rss_feeds(index_articles, relevant_articles, merged[:120])
    print(f"📡 Synced daily RSS feeds for {rss_changed} date(s)")
    from daily_page_enhancer import enhance_daily_archive
    enhanced = enhance_daily_archive("docs/daily/summaries.json")
    print(f"🧭 Enhanced daily navigation/TOC for {enhanced} page(s)")
    if args.send_email:
        try:
            summary = _load_cached_summary(date_str)
            if summary:
                from daily_email import send_daily_email
                send_daily_email(summary, date_str)
            else:
                print(f"⏭️ 每日邮件跳过：无 {date_str} summary")
        except Exception as exc:
            print(f"⚠️ 每日邮件跳过，日报流程继续: {exc}")


if __name__ == '__main__':
    main()
