"""Rich daily-report email adapter with per-day delivery deduplication."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
import os
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from config import EMAIL_CONFIG
from email_notifier import EmailNotifier
from focus_core import classify_taxonomy, priority_tier
from focus_filter import focus_priority
from link_utils import normalize_link
from research_context import pick_summary


DEFAULT_SITE_BASE = "https://hongyu-yu.github.io/literature-tracker"


def _safe_url(value: Any) -> str:
    url = str(value or "").strip()
    try:
        return escape(url, quote=True) if urlparse(url).scheme in ("http", "https") else "#"
    except Exception:
        return "#"


def _items(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = summary.get("daily_articles") or summary.get("full_list") or summary.get("summaries") or []
    items = [item for item in raw if isinstance(item, dict)]
    return sorted(items, key=lambda item: (priority_tier(item), focus_priority(item)))


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
    copied[key] = items
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
    try:
        with open(sent_path, encoding="utf-8") as f:
            sent = json.load(f)
        if not isinstance(sent, dict):
            sent = {}
    except Exception:
        sent = {}
    if day_str in sent:
        print(f"⏭️ 每日邮件已发送过: {day_str}")
        return True
    summary = _with_enrichment(summary, day_str)
    subject, html = build_daily_email_html(summary, day_str, site_base or os.environ.get("SITE_BASE_URL") or DEFAULT_SITE_BASE)
    notifier = EmailNotifier(
        smtp_server=config.get("smtp_server") or "smtp.qq.com",
        smtp_port=int(config.get("smtp_port") or 465),
        sender_email=config.get("sender_email") or "",
        sender_password=config.get("sender_password") or "",
        mode=config.get("mode") or "digest",
    )
    if not notifier.send_html(config.get("recipient") or "", subject, html):
        return False
    try:
        os.makedirs(os.path.dirname(sent_path) or ".", exist_ok=True)
        sent[day_str] = datetime.now(timezone.utc).isoformat()
        with open(sent_path, "w", encoding="utf-8") as f:
            json.dump(sent, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"⚠️ 邮件已发送但防重标记写入失败: {exc}")
    return True
