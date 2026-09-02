"""Rich daily-report email adapter with per-day delivery deduplication."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
import os
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from config import EMAIL_CONFIG
from email_notifier import EmailNotifier
from focus_core import classify_taxonomy, priority_tier
from focus_filter import focus_priority
from link_utils import normalize_link
from research_context import pick_summary


DEFAULT_SITE_BASE = "https://hongyu-yu.github.io/literature-tracker"

_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"'<>?#]+)")

# 信息图五要素里可当邮件亮点用的中文字段（按优先级）
_APS_HIGHLIGHT_KEYS = ("关键结果", "研究问题")


def _doi_key(link: Any) -> str:
    """同一篇论文在不同数据源里链接形态不同：APS 侧只有裸 doi(→ https://doi.org/10.1103/x)，
    RSS 侧是 http://link.aps.org/doi/10.1103/x，按原串比对永远不相等 → 同一篇会出现两张卡片。
    含 DOI 的链接一律折算成 doi:<小写 doi> 作为去重键，其余原样返回。
    规则与 generate_daily_pages._dedup_key 保持一致（那边是页面侧的同一份去重）。"""
    s = str(link or "").strip()
    if not s:
        return ""
    m = _DOI_RE.search(s)
    return f"doi:{m.group(1).rstrip('/.').lower()}" if m else s


def _safe_url(value: Any) -> str:
    url = str(value or "").strip()
    try:
        return escape(url, quote=True) if urlparse(url).scheme in ("http", "https") else "#"
    except Exception:
        return "#"


def _items(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = summary.get("daily_articles") or summary.get("full_list") or summary.get("summaries") or []
    items = [item for item in raw if isinstance(item, dict)]
    # 与页面 build_unified_items 同一把排序尺子：_tier=0 是 APS 全文精读（未标记的普通条目按 2 处理，
    # 该键对它们恒定 → 原有顺序不变）。仅靠 append/prepend 是没用的，sorted 会把顺序重排掉。
    return sorted(items, key=lambda item: (priority_tier(item), item.get("_tier", 2), focus_priority(item)))


def _image_url(item: Dict[str, Any], site_base: str) -> str:
    poster = item.get("poster") if isinstance(item.get("poster"), dict) else {}
    image = str(item.get("image") or poster.get("image") or "").strip()
    if not image:
        return ""
    if image.startswith(("http://", "https://")):
        return _safe_url(image)
    path = image.removeprefix("docs/").lstrip("/")
    return _safe_url(f"{site_base.rstrip('/')}/{path}")


def build_daily_email_html(summary: Dict[str, Any], day_str: str, site_base: str) -> Tuple[str, str]:
    subject = f"📚 每日文献日报 · {day_str}"
    site_base = (site_base or DEFAULT_SITE_BASE).rstrip("/")
    daily_url = _safe_url(f"{site_base}/daily/{day_str}.html")
    items = _items(summary or {})
    try:
        poster_max = max(0, int(os.environ.get("EMAIL_POSTER_MAX", "5")))
    except (TypeError, ValueError):
        poster_max = 5
    try:
        item_max = max(1, int(os.environ.get("EMAIL_MAX_ITEMS", "12")))
    except (TypeError, ValueError):
        item_max = 12
    cards = []
    posters = 0
    for item in items[:item_max]:
        title = escape(str(item.get("title_zh") or item.get("title") or item.get("title_en") or "未命名文献"))
        highlight = escape(pick_summary(item))
        link = _safe_url(item.get("link"))
        tier = priority_tier(item)
        tier_label = "P1" if tier == 0 else "P2" if tier == 2 else "P3"
        category = escape(classify_taxonomy(item))
        image = _image_url(item, site_base)
        image_html = ""
        if image and posters < poster_max:
            posters += 1
            image_html = f'<a href="{link}"><img src="{image}" width="520" alt="{title} 海报" style="max-width:100%;height:auto;border-radius:10px"></a>'
        cards.append(
            '<div style="border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:14px 0">'
            f'<span style="color:#4f46e5;font-weight:700">{tier_label} · {category}</span>'
            f'<h2 style="font-size:17px;margin:8px 0">{title}</h2>{image_html}'
            f'<p style="line-height:1.7"><strong>💡 亮点：</strong>{highlight}</p>'
            f'<a href="{link}" style="color:#4f46e5">阅读原文 →</a></div>'
        )
    content = "".join(cards) if cards else '<p style="padding:20px">今日暂无目标方向文献。</p>'
    overview = escape(str((summary or {}).get("overview") or f"今日收录 {len(items)} 篇重点文献。"))
    html = (
        '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#1f2937;max-width:680px;margin:auto">'
        f'<div style="background:#4f46e5;color:white;padding:22px;border-radius:14px"><h1 style="margin:0">📚 每日文献日报 · {escape(day_str)}</h1>'
        f'<p style="margin-bottom:0">今日概览：{overview}（共 {len(items)} 篇）</p></div>{content}'
        f'<p style="text-align:center;margin:28px"><a href="{daily_url}" style="display:inline-block;background:#f59e0b;color:#111827;padding:13px 22px;border-radius:999px;text-decoration:none;font-weight:700">查看完整图文日报</a></p>'
        '<p style="color:#6b7280;font-size:12px;text-align:center">海报无法显示时，请点击论文链接或完整日报查看。</p></body></html>'
    )
    return subject, html


def _aps_highlight(poster: Dict[str, Any]) -> str:
    """APS 记录没有 one_sentence_summary/abstract_zh，pick_summary 只能退回英文 abstract，
    而 APS 的 abstract 以 'Author(s): …' 作者串开头，截断后基本只剩人名。
    信息图五要素里已经有现成的中文结论，直接拿来当亮点。"""
    elements = poster.get("elements") if isinstance(poster.get("elements"), dict) else {}
    for key in _APS_HIGHLIGHT_KEYS:
        text = " ".join(str(elements.get(key) or "").split())
        if text:
            return text if len(text) <= 160 else text[:160].rstrip() + "…"
    return ""


def _merge_aps_items(items: List[Dict[str, Any]], day_str: str) -> List[Dict[str, Any]]:
    """把 data/aps_<date>.json 的 APS 全文精读并进邮件条目——即页面 build_unified_items 里的 tier0。
    这些是当天分析最深、几乎唯一带信息图的论文，但它们不在 sidecar 的 full_list 里，
    此前邮件一篇都看不到，连标题栏的「共 N 篇」都少算。
    文件缺失/损坏 → 原样返回，邮件照发（只发常规列表）。"""
    path = os.path.join("data", f"aps_{day_str}.json")
    if not os.path.exists(path):
        return items
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        if not isinstance(records, list):
            raise ValueError(f"期望 list，实际是 {type(records).__name__}")
    except Exception as exc:
        print(f"⚠️ APS 精读并入邮件失败({path})，本次只发常规列表: {exc}")
        return items
    indexed: Dict[str, Dict[str, Any]] = {}
    for item in items:
        key = _doi_key(item.get("link"))
        if key:
            indexed.setdefault(key, item)
    added: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        poster = record.get("poster") if isinstance(record.get("poster"), dict) else {}
        image = record.get("image") or poster.get("image")
        if not (record.get("deep_analysis") or image):
            continue  # 无富化 → 跳过（与页面一致；APS 不在 full_list，不会丢可展示内容）
        link = normalize_link(str(record.get("link") or record.get("doi") or "").strip())
        key = _doi_key(link)
        target = indexed.get(key) if key else None
        if target is None:
            # RSS 侧没有这一篇：整条并入，标题/摘要/海报都取自 APS 记录
            target = dict(record)
            target["link"] = link
            added.append(target)
            if key:
                indexed[key] = target
        target["_tier"] = 0
        # 下面一律只补空缺，绝不覆盖 full_list 里已经富化好的中文内容
        if image and not target.get("image"):
            target["image"] = image
        if poster.get("title_zh") and not target.get("title_zh"):
            target["title_zh"] = poster["title_zh"]
        if not any(str(target.get(k) or "").strip()
                   for k in ("one_sentence_summary", "abstract_zh", "abstract_zh_full")):
            highlight = _aps_highlight(poster)
            if highlight:
                target["one_sentence_summary"] = highlight
    if added:
        print(f"📖 APS 全文精读并入日报邮件: {len(added)} 篇")
    return added + items


def _with_enrichment(summary: Dict[str, Any], day_str: str) -> Dict[str, Any]:
    copied = dict(summary or {})
    key = "daily_articles" if copied.get("daily_articles") else "full_list" if copied.get("full_list") else "summaries"
    items = [dict(item) for item in copied.get(key, []) if isinstance(item, dict)]
    try:
        with open(os.path.join("data", f"arxiv_core_{day_str}.json"), encoding="utf-8") as f:
            records = json.load(f)
        if isinstance(records, dict):
            records = records.get("items", [])
        enrich = {normalize_link(str(record.get("link") or "")): record for record in records if isinstance(record, dict)}
        for item in items:
            record = enrich.get(normalize_link(str(item.get("link") or "")), {})
            poster = record.get("poster") if isinstance(record.get("poster"), dict) else {}
            image = record.get("image") or poster.get("image")
            if image and not item.get("image"):
                item["image"] = image
    except Exception:
        pass
    copied[key] = _merge_aps_items(items, day_str)
    return copied


def send_daily_email(summary: Dict[str, Any], day_str: str, *, sent_path: str = "data/email_sent.json",
                     site_base: str | None = None) -> bool:
    if str(os.environ.get("EMAIL_ENABLED", "1")).strip().lower() in ("0", "false", "no", "off"):
        print("⏭️ 每日邮件已关闭(EMAIL_ENABLED=0)")
        return False
    config = EMAIL_CONFIG or {}
    if not str(config.get("sender_email") or "").strip() or not str(config.get("sender_password") or "").strip():
        print("⏭️ 每日邮件跳过：未配置 EMAIL_SENDER/EMAIL_PASSWORD")
        return False
    recipients = config.get("recipients") or ([config["recipient"]] if config.get("recipient") else [])
    recipients = [str(x).strip() for x in recipients if str(x or "").strip()]
    if not recipients:
        print("⏭️ 每日邮件跳过：未配置收件人(EMAIL_RECIPIENTS)")
        return False
    try:
        with open(sent_path, encoding="utf-8") as f:
            sent = json.load(f)
        if not isinstance(sent, dict):
            sent = {}
    except Exception:
        sent = {}
    # 防重标记按收件人粒度记录：{day: {recipient: iso}}。
    # 兼容旧的扁平格式 {day: iso}——那时只有单个收件人，视为该收件人已发。
    day_marker = sent.get(day_str)
    if isinstance(day_marker, dict):
        already = set(day_marker)
    elif day_marker:
        already = set(recipients[:1])
    else:
        already = set()
    pending = [addr for addr in recipients if addr not in already]
    if not pending:
        print(f"⏭️ 每日邮件已发送过: {day_str} ({len(already)} 个收件人)")
        return True
    summary = _with_enrichment(summary, day_str)
    subject, html = build_daily_email_html(summary, day_str, site_base or os.environ.get("SITE_BASE_URL") or DEFAULT_SITE_BASE)
    notifier = EmailNotifier(
        smtp_server=config.get("smtp_server") or "smtp.qq.com",
        smtp_port=int(config.get("smtp_port") or 465),
        sender_email=config.get("sender_email") or "",
        sender_password=config.get("sender_password") or "",
    )
    delivered = notifier.send_html_multi(pending, subject, html)
    if not delivered:
        return False
    failed = [addr for addr in pending if addr not in delivered]
    if failed:
        # 未标记的地址下次运行会被重新纳入 pending，实现按收件人补发
        print(f"⚠️ {len(failed)} 个收件人未送达，下次运行将补发: {', '.join(failed)}")
    try:
        os.makedirs(os.path.dirname(sent_path) or ".", exist_ok=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        marker = dict(day_marker) if isinstance(day_marker, dict) else {}
        if day_marker and not isinstance(day_marker, dict):
            marker[recipients[0]] = day_marker  # 迁移旧的扁平格式
        for addr in delivered:
            marker[addr] = now_iso
        sent[day_str] = marker
        with open(sent_path, "w", encoding="utf-8") as f:
            json.dump(sent, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"⚠️ 邮件已发送但防重标记写入失败: {exc}")
    return True
