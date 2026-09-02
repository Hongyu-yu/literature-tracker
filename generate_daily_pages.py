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
import re
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
from cross_relevance import (
    cross_sort_key, effective_cross_score, is_cross_item, rule_cross_tier, split_cross_sections,
)
from link_utils import normalize_link
from research_context import build_direction_note, ensure_relation_fields, load_research_profile


def _resolve_ai() -> Tuple[str, str, Optional[str]]:
    """解析 AI provider / api_key / model：环境变量 > config.AI_CONFIG > 'aigw'。

    本模块此前完全没读 config.AI_CONFIG，只认环境变量 —— 但 README_CONFIG 让用户把
    provider/api_key 写进 config.local.py，config.py 会把它合进 AI_CONFIG。结果是
    「按文档配好了却拿不到 key」：api_key=None → summarizer=None → 每天都走
    `AI_API_KEY is empty` 异常，日报整段降级。兜底默认也从 'kimi' 改成 config.py
    自己写的 'aigw'，避免 AI_PROVIDER 为空时把请求发去协议不匹配的 Kimi 客户端。

    config 导入失败不能拖垮日报生成，因此整段 fail-soft，退回纯环境变量。
    """
    cfg: Dict = {}
    try:
        from config import AI_CONFIG
        if isinstance(AI_CONFIG, dict):
            cfg = AI_CONFIG
    except Exception as exc:
        print(f"⚠️ 读取 config.AI_CONFIG 失败，仅按环境变量解析 AI 配置: {exc}")
    provider = (os.environ.get("AI_PROVIDER") or cfg.get("provider") or "aigw").strip() or "aigw"
    api_key = (
        os.environ.get("AI_API_KEY")
        or os.environ.get("KIMI_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or cfg.get("api_key")
        or ""
    ).strip()
    model = (os.environ.get("AI_MODEL") or cfg.get("model") or "").strip() or None
    return provider, api_key, model


# 富化环节的 AI 调用计数。ensure_highlights / enrich_focus_interest 把批次失败吞掉后
# 返回 0，光看返回值分不清「没有候选，一次都没调」和「调用发出去了但颗粒无收」。
_AI_CALL_STATS: Dict[str, int] = {"calls": 0, "errors": 0}


def _reset_ai_call_stats() -> None:
    _AI_CALL_STATS["calls"] = 0
    _AI_CALL_STATS["errors"] = 0


class _CountingProvider:
    """透明代理：只统计 call_api 实发次数/失败次数，其余属性原样转发。"""

    def __init__(self, inner):
        self._inner = inner

    def call_api(self, *args, **kwargs):
        _AI_CALL_STATS["calls"] += 1
        try:
            return self._inner.call_api(*args, **kwargs)
        except Exception:
            _AI_CALL_STATS["errors"] += 1
            raise

    def __getattr__(self, name):
        return getattr(self._inner, name)


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
    provider_name, api_key, model = _resolve_ai()
    if not api_key:
        print("⏭️ 亮点保障跳过：未配置 AI key")
        return 0
    try:
        from ai_summarizer import build_provider
        from highlight_guarantee import ensure_highlights
        provider = _CountingProvider(build_provider(provider_name, api_key, model=model))
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
    provider_name, api_key, model = _resolve_ai()
    if not api_key:
        print("⏭️ focus 日报富化跳过：未配置 AI key")
        return 0
    try:
        from ai_summarizer import build_provider
        from focus_interest import enrich_focus_interest
        provider = _CountingProvider(build_provider(provider_name, api_key, model=model))
        updated = enrich_focus_interest(items, provider=provider, max_items=max_items)
        print(f"🎯 focus 日报富化补全 {updated} 篇")
        return updated
    except Exception as exc:
        print(f"⚠️ focus 日报富化跳过: {exc}")
        return 0


def _enrich_daily_cross(items: List[Dict], max_items: Optional[int] = None) -> int:
    """给当日集合打「AI × 物理/材料/化学」交叉强度分（cross_score/cross_reason）。

    只对 cross_relevance.rule_cross_tier <= 1 的条目花钱——纯物理/纯材料本来就归
    「其他」区，不需要一个 LLM 分来确认。规则层已经保证交叉论文不会被 max_keep
    截掉，所以这里不必对全部候选打分（09-01 实测 214 篇候选里只有 31 篇要打分，
    约 4 次调用；对全部打分要 27 次）。

    max_items=None → 读环境变量;传入具体值 → 用它(供跨天全局预算)。"""
    if max_items is None:
        try:
            max_items = max(0, int(os.environ.get("AI_CROSS_DAILY_MAX", "60")))
        except (TypeError, ValueError):
            max_items = 60
    else:
        max_items = max(0, int(max_items))
    if not items or max_items <= 0:
        return 0
    provider_name, api_key, model = _resolve_ai()
    if not api_key:
        print("⏭️ 交叉相关度打分跳过：未配置 AI key（排序退回规则分层）")
        return 0
    try:
        from ai_summarizer import build_provider
        from cross_relevance import enrich_cross_relevance
        provider = _CountingProvider(build_provider(provider_name, api_key, model=model))
        updated = enrich_cross_relevance(items, provider=provider, max_items=max_items)
        print(f"🤖 AI×科学 交叉打分 {updated} 篇")
        return updated
    except Exception as exc:
        print(f"⚠️ 交叉相关度打分跳过: {exc}")
        return 0


def _new_daily_enrich_budget() -> Dict[str, int]:
    """整次 generate 调用的【全局】富化预算(跨天共享),避免 --days N 把 AI 调用放大 N 倍。"""
    def _cap(name: str, default: str) -> int:
        try:
            return max(0, int(os.environ.get(name, default)))
        except (TypeError, ValueError):
            return int(default)
    return {
        "hl": _cap("AI_HIGHLIGHT_MAX_ITEMS", "60"), "fs": _cap("AI_FOCUS_DAILY_MAX", "60"),
        "cross": _cap("AI_CROSS_DAILY_MAX", "60"),
        # 连续「发了 AI 调用却一篇都没补上」的天数，用于熔断（见 _charge_enrich_budget）
        "hl_zero": 0, "fs_zero": 0, "cross_zero": 0,
    }


# 连续多少天「调用发出去但颗粒无收」就把该项预算清零(本次运行内)
_ENRICH_ZERO_YIELD_LIMIT = 2


def _charge_enrich_budget(budget: Dict[str, int], key: str, used: int, label: str) -> None:
    """按【实际发生的 AI 调用】扣预算，而不是只按成功条数扣。

    预算的存在意义是「--days N 不要把 AI 调用放大 N 倍」，但原来只做
    `budget -= used`：provider 故障时 used 恒为 0(批次失败被 analyze_focus_batch /
    ensure_highlights 吞掉)，预算一分不扣 —— 恰恰在花钱最冤的场景下完全不设防，
    --days 14 会把注定失败的调用重复 14 天。

    规则：
      * calls == 0 → 这次根本没发出 AI 调用(没候选/没画像/没 key)，不计费；
      * used  > 0  → 正常按成功条数扣，并清零失败连击；
      * calls > 0 且 used == 0 → 钱花了、一条没补上，记一次失败连击并大声打日志；
        连续 _ENRICH_ZERO_YIELD_LIMIT 天如此就把该项预算清零(仅影响本次运行剩余
        日期，下次运行重新开始)，留一天重试的余地以容忍偶发抖动。
    """
    calls = int(_AI_CALL_STATS.get("calls", 0))
    errors = int(_AI_CALL_STATS.get("errors", 0))
    if used > 0:
        budget[key] = max(0, int(budget.get(key, 0)) - used)
        budget[key + "_zero"] = 0
        return
    if calls <= 0:
        return
    streak = int(budget.get(key + "_zero", 0)) + 1
    budget[key + "_zero"] = streak
    print(f"⚠️ {label}：发出 {calls} 次 AI 调用({errors} 次抛错)却补全 0 篇"
          f"（连续第 {streak} 天）")
    if streak >= _ENRICH_ZERO_YIELD_LIMIT:
        budget[key] = 0
        print(f"🛑 {label}：连续 {streak} 天颗粒无收，本次运行剩余日期不再调用 AI（下次运行自动恢复）")


def _apply_daily_enrichment(items: List[Dict], budget: Dict[str, int]) -> None:
    """对单日 daily 集做一次富化(focus + 亮点),消耗共享全局预算(就地扣减)。"""
    if not items:
        return
    # 交叉打分放最前：分区、排序、邮件选卡全部依赖它，
    # 后面两项即使被熔断清零也不影响"日报仍然聚焦交叉方向"这个主目标。
    if budget.get("cross", 0) > 0:
        _reset_ai_call_stats()
        used = _enrich_daily_cross(items, max_items=budget["cross"]) or 0
        _charge_enrich_budget(budget, "cross", used, "AI×科学 交叉打分")
    if budget.get("fs", 0) > 0:
        _reset_ai_call_stats()
        used = _enrich_daily_focus(items, max_items=budget["fs"]) or 0
        _charge_enrich_budget(budget, "fs", used, "focus 日报富化")
    if budget.get("hl", 0) > 0:
        _reset_ai_call_stats()
        used = _guarantee_daily_highlights(items, max_items=budget["hl"]) or 0
        _charge_enrich_budget(budget, "hl", used, "亮点保障")


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

def group_daily_items(items: List[Dict]) -> List[Dict]:
    """按「AI×科学交叉」分区。

    分组主键从 priority_tier 换成 cross_relevance 的交叉判定：
    priority_tier 只认 focus_core.PRIORITY_TERMS 那 22 个字面词，标题写
    Hamiltonian 而不是 ml hamiltonian 就落到 P3。交叉分既有 LLM 打分，
    也有规则兜底，两条路都不依赖某个词恰好被写进词表。

    交叉组内保留「神经网络势·电子结构」作为置顶子块（priority_tier==0），
    那仍然是主线方向，只是不再充当唯一的分组依据。
    """
    groups = {
        "core": {
            "title": "🔬 神经网络势 · 电子结构（重点）",
            "description": "神经网络势、哈密顿量、密度矩阵、电荷密度与电子结构。",
            "items": [],
        },
        "cross": {
            "title": "🤖 AI × 物理 / 材料 / 化学",
            "description": "机器学习与第一性原理、分子动力学、材料与化学问题的交叉研究。",
            "items": [],
        },
        "other": {
            "title": "🧲 其他物理 / 材料进展",
            "description": "不含机器学习成分的凝聚态、铁电磁性与材料工作。",
            "items": [],
        },
    }

    for item in items:
        if not is_cross_item(item):
            key = "other"
        elif priority_tier(item) == 0:
            key = "core"
        else:
            key = "cross"
        groups[key]["items"].append(item)

    ordered = []
    for key in ("core", "cross", "other"):
        if groups[key]["items"]:
            groups[key]["items"].sort(key=cross_sort_key)
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


_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"'<>?#]+)")

# 合并重复条目时不允许被覆盖/补写的字段：tier0 的富化结果与已归一化的链接。
_DEDUP_MERGE_SKIP = {"_tier", "_enrich", "link", "doi", "poster", "image", "deep_analysis"}


def _dedup_key(link: str) -> str:
    """同一篇文章在不同数据源里链接形态不同：APS 侧只有裸 doi(→ https://doi.org/10.1103/x)，
    RSS 索引侧是 http://link.aps.org/doi/10.1103/x。按原始串比对永远不相等 → 同一篇被渲染两次。
    含 DOI 的链接一律折算成 doi:<小写 doi> 作为去重键，其余原样返回。"""
    s = (link or "").strip()
    if not s:
        return ""
    m = _DOI_RE.search(s)
    if not m:
        return s
    return f"doi:{m.group(1).rstrip('/.').lower()}"


def _fill_missing_fields(target: Dict, source: Dict) -> None:
    """把重复条目里 target 缺的字段补进来（只补空缺，绝不覆盖已有值）。
    APS 行没有 title_zh/abstract_zh/focus_* 等中文富化字段，直接丢掉重复的 full_list 条目
    会连同这些已经生成好的中文内容一起丢掉。"""
    _EMPTY = (None, "", [], {})
    for k, v in (source or {}).items():
        if k in _DEDUP_MERGE_SKIP or v in _EMPTY:
            continue
        if target.get(k) in _EMPTY:
            target[k] = v


def build_unified_items(full_list, enrich_map, aps_items):
    """合并 APS 全文(tier0) + full_list(tier1 富化 / tier2 普通) 成一个扁平列表，
    按 (tier, focus_priority) 排序。每项注入 _tier 与 _enrich(dict|None)。"""
    items: List[Dict] = []
    seen: Dict[str, Dict] = {}
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
        key = _dedup_key(it["link"])
        if key:
            seen.setdefault(key, it)
    for it0 in (full_list or []):
        link = normalize_link((it0.get("link") or "").strip())
        key = _dedup_key(link)
        kept = seen.get(key) if key else None
        if kept is not None:
            # 同一篇（DOI 相同、链接形态不同）已在列表里：把中文/focus 等字段补进已有条目，
            # 而不是把这份数据整条丢掉。
            _fill_missing_fields(kept, it0)
            continue
        it = dict(it0)
        it["link"] = link or it0.get("link") or ""
        en = enrich_map.get(link) if link else None
        it["_tier"] = 1 if en else 2
        it["_enrich"] = en
        items.append(it)
        if key:
            seen.setdefault(key, it)
    # 研究层在前、富化层在后 —— 这里的 _tier 是"有没有富化配图"(1=有/2=无)，
    # 与 daily_email 里的 _tier(0=APS 全文精读) 不是一回事，不能照搬那边的键序：
    # 把它提到首位会让"有图但离题"压过"交叉但暂无图"，正是 2026-08-22 设计里
    # 明确否决过的那种排法（test_unified_sort_prioritizes_research_layer_before_
    # enrichment_tier 守着它）。研究层从 priority_tier 换成交叉分，层内再让有图的上浮。
    items.sort(key=lambda x: (-effective_cross_score(x), x.get("_tier", 2),
                              priority_tier(x), focus_priority(x)))
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
    # 不再要求三段各 ≥180 字。那条长度门是为「模板兜底文本必然很长」量身定的：
    # research_context 过去会把短于 180 字的内容整段替换成模板，于是长度自然达标。
    # 该替换会删掉真实但简短的 AI 分析，已修复 —— 长度门也就随之失去意义，
    # 反而在 AI 正常作答时必然不满足，使 rerender_ok 恒为 False、日报页面再也不刷新。
    # report["relation"] == total 已经保证三段字段全部非空，这才是真正要守的东西。
    return all(report[k] == total for k in ("title_zh", "abstract_zh", "summary", "relation")) and bool(
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

# 注意：这里曾有一个无人调用的 update_index()，它写出的 summaries 条目没有 digest 字段。
# 一旦被重新接线，主循环的 should_skip(依赖 digest) 会永远失效 → 每次运行都全量重跑 AI。
# 单日更新请走 save_summary_index(merged)，digest 由生成路径统一写入。


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


def _daily_max_keep() -> int:
    """单日日报保留篇数上限（env DAILY_MAX_KEEP，默认 72）。

    从 60 提到 72：交叉区与「其他」区现在同处一份列表，60 的老上限是在
    "全部混排"时定的。09-01 实测交叉 31 + 其他 183，72 让交叉区全进、
    其他区仍留 40 篇左右的余量。
    """
    try:
        return max(1, int(os.environ.get("DAILY_MAX_KEEP", "72")))
    except (TypeError, ValueError):
        return 72


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
    # 交叉优先排序 —— 这是「谁会被 max_keep 截掉」的唯一决定因素。
    # 此前用的是 focus_priority + filter_daily_focus_items 的默认键(priority_tier +
    # 标题分档)，2026-09-01 实测 198 篇候选里截掉 138 篇，其中包括
    # 「用于非绝热 TDDFT 动力学的三头哈密顿一致性神经网络」(已有 focus_score=8，
    # 排第 116)、「机器学习揭示高临界温度非常规超导体的共同特征」(第 112)等，
    # 留下的 60 篇里标题含 learning/neural/network/machine 的只有 2 篇。
    focused_articles = sorted(focused_articles, key=cross_sort_key)
    daily_articles, overflow_articles = filter_daily_focus_items(
        focused_articles, min_keep=12, max_keep=_daily_max_keep(), sort_key=cross_sort_key
    )
    # AI 富化(focus/亮点)已移出本热函数:collect_daily_articles 在主循环(每天)+
    # sync_daily_rss_feeds(每条最多 120 次)都会被调用,若在此调 AI 会把调用量放大到 ~124×
    # 导致 generate step 撞 90min 超时。改在 main() 生成路径按【全局预算】每天调一次。
    daily_articles = sorted(daily_articles, key=cross_sort_key)
    return {
        "raw_day_articles": raw_day_articles,
        "focused_articles": focused_articles,
        "dropped_articles": dropped_articles,
        "daily_articles": daily_articles,
        "overflow_articles": overflow_articles,
    }


_CJK_RE = re.compile(r"[一-鿿]")


def _feed_zh_coverage(path: str) -> Optional[Tuple[int, int]]:
    """已落盘 <date>.xml 的 (条目数, 含中文的 title/description 数)。
    文件缺失/解析失败 → None，表示“没有基线”，照旧直接写。"""
    if not os.path.exists(path):
        return None
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(path).getroot()
    except Exception:
        return None
    nodes = root.findall(".//item")
    zh = sum(
        1
        for node in nodes
        for tag in ("title", "description")
        if _CJK_RE.search(node.findtext(tag) or "")
    )
    return len(nodes), zh


def _articles_zh_coverage(articles: List[Dict]) -> Tuple[int, int]:
    """待写入的这批文章能给 RSS 提供的 (条目数, 含中文的 title/description 数)。
    取值顺序与 rss_generator._article_title/_article_description 保持一致，
    这样和 _feed_zh_coverage 是同一把尺子。"""
    total = 0
    zh = 0
    for a in (articles or []):
        if not isinstance(a, dict):
            continue
        total += 1
        title = str(a.get("title_zh") or a.get("title") or "")
        body = str(a.get("summary") or a.get("one_sentence_summary") or "") or str(
            a.get("abstract_zh") or a.get("abstract") or "")
        desc = " ".join([str(a.get("journal") or ""), format_authors(a.get("authors")), body])
        if _CJK_RE.search(title):
            zh += 1
        if _CJK_RE.search(desc):
            zh += 1
    return total, zh


def _rss_downgrade_reason(path: str, articles: List[Dict]) -> str:
    """写 <date>.xml 前的护栏。data/index.json 只保留最近 5000 篇，窗口滑过某天后
    collect_daily_articles 只剩 ai_relevant.json(中文富化之前写入的那份)，重算结果会缺
    title_zh/abstract_zh —— 无条件改写会把历史 feed 里已经生成好的中文标题/摘要冲掉，
    只能从 git 历史里找回。返回非空原因串 → 本次跳过写入，保留磁盘上更完整的那份。"""
    base = _feed_zh_coverage(path)
    if base is None:
        return ""
    old_total, old_zh = base
    if old_total <= 0:
        return ""
    new_total, new_zh = _articles_zh_coverage(articles)
    if new_total < old_total:
        return f"条目 {old_total}→{new_total}"
    if new_zh < old_zh:
        return f"中文字段 {old_zh}→{new_zh}"
    return ""


def sync_daily_rss_feeds(index_articles: List[Dict], relevant_articles: List[Dict],
                         summaries: List[Dict], only_dates=None) -> int:
    """把 summaries 里的日期同步成 docs/daily/<date>.xml，并刷新 latest.xml。

    only_dates=None（默认，backfill_zh 的全量同步走这条）→ 逐日重算全部日期。
    only_dates=集合 → 只重算集合里的日期；其余日期若 .xml 已在磁盘上就原样保留。
    collect_daily_articles 是 analyze_focus 密集型热函数，对 120 天全量重算要 ~7 分钟，
    而每次 generate 只可能改动 --days 指定的那 1-2 天；其余日期重算出来的结果不是更好，
    反而可能因为 index.json 只留最近 5000 篇而更差（见 _rss_downgrade_reason）。
    找不到 .xml 的日期一律照常生成，保证新日期/丢失的 feed 不会被漏掉。
    """
    changed = 0
    skipped_unchanged = 0
    only = set(only_dates) if only_dates is not None else None
    # 降级保护只适用于**历史**日期：它防的是「用只剩 31 天的 index 去重算老 feed，把当时
    # 的中文内容洗掉」。最近几天则相反 —— 新文献刚抓进来还没翻译，中文覆盖率本来就会掉，
    # 这是正常状态；对它们套用保护会让最新 feed 永久冻结（条目数被 max_keep=60 顶住，
    # 中文比例又一直不达标，永远解不开），连带 latest.xml 也一起冻住。
    try:
        fresh_days = int((os.environ.get("RSS_FRESH_WINDOW_DAYS", "7") or "7").strip())
    except Exception:
        fresh_days = 7
    # 以**真实当天**为基准，而不是 summaries[0]：新鲜与否取决于「这些日期的文献是否还在
    # 陆续到达/翻译」，那是现实时间的属性。若拿 summaries[0] 当基准，只回填某个老日期时
    # 它自己就成了「最新」，保护会被误关掉。
    try:
        fresh_cutoff = (datetime.strptime(beijing_today(), "%Y-%m-%d")
                        - timedelta(days=max(0, fresh_days - 1))).strftime("%Y-%m-%d")
    except Exception:
        fresh_cutoff = ""

    for entry in summaries:
        day_str = str(entry.get("date") or "").strip()
        if not day_str:
            continue
        rss_path = daily_rss_path(day_str)
        if only is not None and day_str not in only and os.path.exists(rss_path):
            skipped_unchanged += 1
            continue
        collected = collect_daily_articles(index_articles, relevant_articles, day_str)
        is_fresh = bool(fresh_cutoff) and day_str >= fresh_cutoff
        reason = "" if is_fresh else _rss_downgrade_reason(rss_path, collected["daily_articles"])
        if reason:
            print(f"⏭️ RSS 跳过 {day_str}：重算结果劣于既有 feed（{reason}），保留原文件")
            continue
        if generate_daily_rss_feed(day_str, collected["daily_articles"], rss_path):
            changed += 1

    if skipped_unchanged:
        print(f"⏭️ RSS 增量同步：跳过 {skipped_unchanged} 个本次未重算的历史日期（feed 已在磁盘上）")
    # latest.xml 始终跟着 summaries[0]（整份索引的最新一期），与 only_dates 无关：
    # 只回填某个老日期时，绝不能把 latest.xml 换成那天的 feed。
    latest_date = str((summaries[0] or {}).get("date") or "").strip() if summaries else ""
    latest_source = daily_rss_path(latest_date) if latest_date else ""
    latest_target = os.path.join("docs/daily", "latest.xml")
    if latest_source and os.path.exists(latest_source):
        shutil.copyfile(latest_source, latest_target)
    return changed

def _nav_sequence_changed(prev_entries: List[Dict], new_entries_list: List[Dict]) -> bool:
    """summaries.json 的 (date, file) 序列是否变了。

    daily_page_enhancer.build_nav_context 只按列表【位置】取前一天/后一天/最新一期，
    所以每张归档页的导航只依赖这个序列。序列不变 → 没重新生成的页面导航必然不变，
    可以只 enhance 本次真正重写过的页面；序列变了（新一期上线 / 回填插入老日期 /
    120 条窗口挤掉最老一期）→ 全站的“最新一期”或邻居标签会漂移，必须全量重跑。
    """
    def seq(entries: List[Dict]):
        return [(str(e.get("date") or ""), str(e.get("file") or ""))
                for e in (entries or []) if isinstance(e, dict)][:120]
    return seq(prev_entries) != seq(new_entries_list)


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
            # 邮件先于降级判定取值：内容降级也照常发信，只是不覆盖已有页面
            if ds == date_str:
                email_summary = summ
            # rerender_ok 缺省回退到 quality_ok，再回退到 True(2026-07-31 前的旧 sidecar)
            if not summ.get("rerender_ok", summ.get("quality_ok", True)):
                print(f"⏭️  rerender skip {ds}: 缓存 summary 为降级内容，保留既有页面")
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
    
    # Provider 选择顺序：环境变量 AI_PROVIDER > config.AI_CONFIG（含 config.local.py）> 'aigw'
    # use_local_kimi 必须按【解析后】的 provider 判断：只在 config.local.py 里写
    # provider="localkimi" 时，若仍只看环境变量就会走 AISummarizer('localkimi', key)，
    # 而 build_provider 没有 localkimi 分支，会静默退回 Gemini。
    provider, api_key, _ai_model = _resolve_ai()
    use_local_kimi = provider.lower() in ('localkimi', 'local-kimi')

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
                # AI 兜底时是否保留既有页面（不覆盖），但 summary 仍要落盘供邮件使用
                preserved_page = False
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
                    # 富化刚把 cross_score 写进条目，必须按新分重排：
                    # 这一步的顺序决定了 AI 摘要的分块顺序与 core_items 的取材。
                    daily_articles = sorted(daily_articles, key=cross_sort_key)
                    if summarizer is None:
                        raise ValueError("AI_API_KEY is empty; cannot generate daily summary")
                    summary = summarizer.generate_daily_summary(daily_articles, day_str)
                    if summary.get("generated_by") == "fallback" and os.path.exists(out_path):
                        # AI 失败：不用降级内容覆盖已有的好页面，但**不再直接 continue** ——
                        # fallback_summary 本身带着翻译(title_zh/abstract_zh)、关键词与画像
                        # 筛选(focus_*)、核心判定(is_core_focus/core_score)和规则版三段文本，
                        # 足以撑起一份当天的日报。此前 continue 把它连同 sidecar 一起丢掉，
                        # 导致"AI 挂掉的那天收不到任何日报邮件"。改为：保留页面，但照常落盘。
                        preserved_page = True
                        print(f"⚠️ AI fallback for {day_str}：保留既有页面，但仍落盘 summary 供邮件使用")
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
                        # 只在真拿到深读结果时才覆盖：此前是无条件赋值，深读失败(或某条
                        # link 没返回)时会把 ensure_relation_fields 已填好的规则版三段文本
                        # 抹成空串 —— 而且抹的恰恰是核心文献。core_items 与 full_list 是同
                        # 一批 dict 对象，full_list 也会被一起抹掉。
                        for it in core_items:
                            info = deep_fields.get(it.get("link") or "", {}) or {}
                            for key in ("method_point", "related_work", "implication"):
                                value = str(info.get(key) or "").strip()
                                if value:
                                    it[key] = value
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
                    # quality_ok: 纯质量门结果，仅供诊断
                    # rerender_ok: --rerender-only 是否可以拿这份缓存重画页面。降级内容
                    #              (质量不达标 / AI 兜底且已有好页面)一律不许覆盖页面，
                    #              但两者都不影响发邮件 —— 邮件只要有 summary 就发。
                    summary["quality_ok"] = daily_quality_ok(summary)
                    summary["rerender_ok"] = summary["quality_ok"] and not preserved_page
                    print(f"📋 daily quality {day_str}: {quality} "
                          f"(ok={summary['quality_ok']}, rerender_ok={summary['rerender_ok']})")
                    with open(os.path.join("data", f"daily_summary_{day_str}.json"), "w", encoding="utf-8") as sf:
                        json.dump(summary, sf, ensure_ascii=False)
                except Exception as e:
                    print(f"⚠️ daily summary sidecar skip {day_str}: {e}")

                if preserved_page:
                    # 页面保持原样，索引沿用旧条目；summary 已落盘，邮件照发
                    new_entries.append(preserve_existing_entry(prev, day_str))
                else:
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
    # 导航是否需要全量刷新，必须在覆盖 summaries.json 之前用旧索引判断。
    nav_seq_unchanged = not _nav_sequence_changed(existing_items, merged[:120])

    save_summary_index(merged[:120])
    rss_changed = sync_daily_rss_feeds(index_articles, relevant_articles, merged[:120],
                                       only_dates=updated_dates)
    print(f"📡 Synced daily RSS feeds for {rss_changed} date(s)")
    from daily_page_enhancer import enhance_daily_archive
    # files=空集合会被 enhance_daily_archive 当成“未指定”从而全量处理，故单独短路。
    scoped = {d for d in updated_dates if d} | {f"{d}.html" for d in updated_dates if d}
    if not nav_seq_unchanged:
        enhanced = enhance_daily_archive("docs/daily/summaries.json")
        print(f"🧭 Enhanced daily navigation/TOC for {enhanced} page(s)（索引顺序变化，全量刷新导航）")
    elif scoped:
        enhanced = enhance_daily_archive("docs/daily/summaries.json", files=scoped)
        print(f"🧭 Enhanced daily navigation/TOC for {enhanced} page(s)（索引顺序未变，只处理本次重算日期）")
    else:
        print("🧭 索引顺序与本次生成结果均无变化，跳过导航增强")
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
