#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Enhance generated daily HTML pages with navigation, hyperlinks, and a single-page TOC."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from text_normalizer import is_suspicious_text, normalize_articles_inplace, normalize_text

STYLE_ID = "daily-enhancement-style"
TOP_NAV_ID = "daily-enhancement-top-nav"
BOTTOM_NAV_ID = "daily-enhancement-bottom-nav"
SIDEBAR_NAV_ID = "daily-enhancement-sidebar-nav"
OUTLINE_ID = "daily-enhancement-outline"

ENHANCEMENT_CSS = """
#daily-enhancement-top-nav, #daily-enhancement-bottom-nav {
    margin-top: 18px;
}

.daily-page-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
}

.daily-page-nav-link,
.daily-page-nav-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 9px 14px;
    border-radius: 999px;
    border: 1px solid var(--border-color);
    background: rgba(255,255,255,0.92);
    color: var(--text-secondary);
    text-decoration: none;
    font-size: 0.92rem;
    transition: all var(--transition-fast);
}

.daily-page-nav-link:hover,
.daily-page-nav-pill:hover {
    color: var(--accent-primary);
    border-color: rgba(99,102,241,0.35);
    background: rgba(99,102,241,0.08);
}

.daily-page-nav-disabled {
    opacity: 0.45;
    pointer-events: none;
}

.daily-title-link {
    color: inherit;
    text-decoration: none;
}

.daily-title-link:hover {
    color: var(--accent-primary);
}

.daily-permalink-link {
    margin-left: 8px;
    color: var(--text-muted);
    text-decoration: none;
    font-size: 0.9rem;
}

.daily-permalink-link:hover {
    color: var(--accent-primary);
}

.daily-inline-links {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 12px;
}

.daily-inline-link {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--accent-primary);
    text-decoration: none;
    font-size: 0.9rem;
    font-weight: 600;
}

.daily-inline-link:hover {
    color: var(--accent-hover);
}

.daily-toc-card {
    max-height: min(74vh, calc(100vh - 40px));
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable both-edges;
    padding-right: 12px;
}

.daily-toc-card::-webkit-scrollbar {
    width: 10px;
}

.daily-toc-card::-webkit-scrollbar-thumb {
    background: rgba(99, 102, 241, 0.28);
    border-radius: 999px;
    border: 2px solid transparent;
    background-clip: padding-box;
}

.daily-toc-card::-webkit-scrollbar-track {
    background: rgba(148, 163, 184, 0.10);
    border-radius: 999px;
}

.daily-outline-scroll {
    max-height: min(40vh, calc(100vh - 320px));
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable both-edges;
    padding-right: 6px;
}

.daily-outline-scroll::-webkit-scrollbar {
    width: 10px;
}

.daily-outline-scroll::-webkit-scrollbar-thumb {
    background: rgba(99, 102, 241, 0.28);
    border-radius: 999px;
    border: 2px solid transparent;
    background-clip: padding-box;
}

.daily-outline-scroll::-webkit-scrollbar-track {
    background: rgba(148, 163, 184, 0.10);
    border-radius: 999px;
}

.daily-outline-group + .daily-outline-group {
    margin-top: 14px;
}

.daily-outline-group-title {
    display: block;
    margin-bottom: 8px;
    color: var(--text-primary);
    font-weight: 700;
    text-decoration: none;
}

.daily-outline-group-title:hover {
    color: var(--accent-primary);
}

.daily-outline-list {
    display: grid;
    gap: 6px;
}

.daily-outline-link {
    display: block;
    padding: 8px 10px;
    border-radius: 12px;
    color: var(--text-secondary);
    text-decoration: none;
    font-size: 0.9rem;
    line-height: 1.45;
    background: rgba(255,255,255,0.6);
    border: 1px solid rgba(148,163,184,0.18);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.daily-outline-link:hover {
    color: var(--accent-primary);
    background: rgba(99,102,241,0.08);
}

.daily-outline-link-highlight {
    border-color: rgba(99,102,241,0.24);
}

.daily-outline-meta {
    display: block;
    margin-top: 2px;
    color: var(--text-muted);
    font-size: 0.8rem;
}

.daily-topic-anchor {
    margin-left: 8px;
}

@media (max-width: 980px) {
    .daily-toc-card {
        max-height: none;
    }
    .daily-outline-scroll {
        max-height: none;
    }
}
"""


def _safe_text(text: str) -> str:
    return " ".join(normalize_text(text or "").split())


def _slugify(text: str, fallback: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", _safe_text(text)).strip("-").lower()
    return base or fallback


def _relative_daily_href(entry: Dict[str, str]) -> str:
    return entry.get("file") or f"{entry.get('date')}.html"


def load_summary_entries(index_path: str | Path = "docs/daily/summaries.json") -> List[Dict]:
    path = Path(index_path)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [item for item in (data.get("summaries") or []) if isinstance(item, dict) and item.get("date")]


def load_article_lookup(paths: Iterable[str | Path] = ("data/index.json", "docs/data/index.json")) -> Dict[str, Dict]:
    lookup: Dict[str, Dict] = {}
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        articles = data.get("articles", []) if isinstance(data, dict) else []
        if not isinstance(articles, list):
            continue
        normalize_articles_inplace(articles)
        for article in articles:
            if not isinstance(article, dict):
                continue
            link = str(article.get("link") or "").strip()
            if link and link not in lookup:
                lookup[link] = article
    return lookup


def build_nav_context(entries: List[Dict]) -> Dict[str, Dict[str, Optional[Dict]]]:
    by_date: Dict[str, Dict[str, Optional[Dict]]] = {}
    for idx, entry in enumerate(entries):
        by_date[entry["date"]] = {
            "newer": entries[idx - 1] if idx > 0 else None,
            "older": entries[idx + 1] if idx + 1 < len(entries) else None,
            "latest": entries[0] if entries else None,
            "archive": {"file": "index.html", "label": "日报归档"},
        }
    return by_date


def _day_gap_label(current_date: str, target_date: Optional[str], *, older: bool) -> str:
    if not target_date:
        return ""
    try:
        current_dt = datetime.strptime(current_date, "%Y-%m-%d")
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        days = abs((current_dt - target_dt).days)
    except Exception:
        days = None
    if days == 1:
        return "前一天" if older else "后一天"
    return "上一期" if older else "下一期"


def _ensure_style(soup: BeautifulSoup) -> None:
    old = soup.find(id=STYLE_ID)
    if old is not None:
        old.decompose()
    style = soup.new_tag("style", id=STYLE_ID)
    style.string = ENHANCEMENT_CSS
    if soup.head is not None:
        soup.head.append(style)


def _ensure_rss_head_link(soup: BeautifulSoup, date_str: str) -> None:
    if soup.head is None:
        return
    for node in soup.head.select('link[type="application/rss+xml"]'):
        href = (node.get("href") or "").strip()
        if href.endswith(f"{date_str}.xml"):
            return
    rss_link = soup.new_tag(
        "link",
        rel="alternate",
        type="application/rss+xml",
        title=f"{date_str} 日报 RSS",
        href=f"{date_str}.xml",
    )
    soup.head.append(rss_link)


def _title_plain_text(node: Optional[Tag]) -> str:
    if node is None:
        return ""
    linked = node.select_one("a.daily-title-link")
    if linked is not None:
        return _safe_text(linked.get_text(" ", strip=True))
    texts = []
    for child in node.children:
        if getattr(child, "get", lambda *_: None)("class") and "daily-permalink-link" in (child.get("class") or []):
            continue
        texts.append(child.get_text(" ", strip=True) if hasattr(child, "get_text") else str(child))
    return _safe_text(" ".join(texts))


def _strip_trailing_anchor_hashes(text: str) -> str:
    return re.sub(r"(?:\s*#\s*)+$", "", text or "").strip()


def _replace_title_with_link(soup: BeautifulSoup, title_node: Optional[Tag], href: str, anchor_id: str) -> None:
    if title_node is None:
        return
    text = _title_plain_text(title_node) or _safe_text(title_node.get_text(" ", strip=True))
    text = _strip_trailing_anchor_hashes(text)
    if not text:
        return
    title_node.clear()
    if href and href != "#":
        link = soup.new_tag("a", href=href, target="_blank", rel="noopener noreferrer", **{"class": "daily-title-link"})
        link.string = text
        title_node.append(link)
    else:
        title_node.string = text
    permalink = soup.new_tag("a", href=f"#{anchor_id}", **{"class": "daily-permalink-link", "title": "复制本页定位"})
    permalink.string = "#"
    title_node.append(permalink)


def _ensure_inline_links(soup: BeautifulSoup, item_node: Tag, anchor_id: str) -> None:
    source_link = item_node.select_one(".daily-news-link")
    actions = item_node.select_one(".daily-paper-actions")
    if actions is None:
        actions = item_node.select_one(".daily-news-body") or item_node.select_one(".daily-paper-body") or item_node
    existing = item_node.select_one(".daily-inline-links")
    if existing is not None:
        existing.decompose()

    inline = soup.new_tag("div", **{"class": "daily-inline-links"})
    # 新版卡片的操作行里已经有「阅读原文 ↗」，再加一个「原文链接」只是重复
    has_read_link = actions is not None and actions.select_one(".daily-news-link") is not None \
        and "daily-paper-actions" in (actions.get("class") or [])
    if source_link is not None and source_link.get("href") and not has_read_link:
        raw = soup.new_tag(
            "a",
            href=source_link.get("href"),
            target="_blank",
            rel="noopener noreferrer",
            **{"class": "daily-inline-link"},
        )
        raw.string = "原文链接"
        inline.append(raw)
    anchor = soup.new_tag("a", href=f"#{anchor_id}", **{"class": "daily-inline-link"})
    anchor.string = "页内定位"
    inline.append(anchor)
    actions.append(inline)


def _build_nav_block(soup: BeautifulSoup, date_str: str, nav: Dict[str, Optional[Dict]], block_id: str) -> Tag:
    wrapper = soup.new_tag("div", id=block_id, **{"class": "daily-page-nav"})

    def append_link(label: str, href: Optional[str], extra_text: str = "", disabled: bool = False):
        cls = "daily-page-nav-link"
        if disabled:
            cls += " daily-page-nav-disabled"
            tag = soup.new_tag("span", **{"class": cls})
        else:
            tag = soup.new_tag("a", href=href or "#", **{"class": cls})
        tag.string = label + (f" · {extra_text}" if extra_text else "")
        wrapper.append(tag)

    older = nav.get("older")
    newer = nav.get("newer")
    latest = nav.get("latest")

    append_link(_day_gap_label(date_str, older.get("date") if older else None, older=True) or "前一天",
                _relative_daily_href(older) if older else None,
                older.get("date") if older else "暂无",
                disabled=older is None)
    append_link(_day_gap_label(date_str, newer.get("date") if newer else None, older=False) or "后一天",
                _relative_daily_href(newer) if newer else None,
                newer.get("date") if newer else "暂无",
                disabled=newer is None)
    if latest is not None:
        append_link("最新一期", _relative_daily_href(latest), latest.get("date") or "")
    append_link("日报归档", "index.html")
    append_link("当日RSS", f"{date_str}.xml")
    append_link("站点RSS", "../feed.xml")
    append_link("主页", "../index.html")
    append_link("今日摘要", "#summary")
    append_link("今日文献", "#papers")
    return wrapper


def _remove_existing_injected_blocks(soup: BeautifulSoup) -> None:
    for block_id in (TOP_NAV_ID, BOTTOM_NAV_ID, SIDEBAR_NAV_ID, OUTLINE_ID):
        node = soup.find(id=block_id)
        if node is not None:
            node.decompose()

    for node in soup.select(".daily-inline-links"):
        node.decompose()
    for node in soup.select("a.daily-permalink-link"):
        node.decompose()


def _sanitize_soup_text(soup: BeautifulSoup) -> None:
    for node in list(soup.find_all(string=True)):
        if isinstance(node, Comment):
            continue
        if node.parent is not None and node.parent.name in {"script", "style"}:
            continue
        old = str(node)
        new = normalize_text(old)
        if new != old:
            node.replace_with(NavigableString(new))

    for tag in soup.find_all(True):
        for attr in ("title", "aria-label", "alt", "content"):
            if tag.has_attr(attr):
                tag[attr] = normalize_text(tag.get(attr))


def _replace_plain_text(node: Optional[Tag], text: str) -> None:
    if node is None:
        return
    node.clear()
    node.append(NavigableString(text))


def _apply_article_title_fallback(item_node: Tag, article_lookup: Dict[str, Dict], *, is_paper: bool) -> None:
    source_link = item_node.select_one(".daily-news-link")
    href = str(source_link.get("href") or "").strip() if source_link is not None else ""
    if not href:
        return
    article = article_lookup.get(href)
    if not article:
        return

    title_zh = normalize_text(article.get("title_zh") or "")
    title_en = normalize_text(article.get("title") or article.get("title_en") or "")
    zh_selector = ".daily-paper-title-zh" if is_paper else ".daily-news-title-zh"
    en_selector = ".daily-paper-title-en" if is_paper else ".daily-news-title-en"

    # 如果渲染层已经输出了真正的中文标题（由 Kimi 生成，不在 data/index.json 里），
    # 不要用英文原标题覆盖它。判定依据：当前 zh 节点文本包含 CJK 且不与 en 完全相同。
    def _has_cjk(s: str) -> bool:
        return any('\u4e00' <= ch <= '\u9fff' for ch in (s or ""))

    zh_node = item_node.select_one(zh_selector)
    current_zh = _title_plain_text(zh_node) if zh_node is not None else ""
    current_already_translated = (
        _has_cjk(current_zh)
        and current_zh.casefold() != title_en.casefold()
    )

    if not current_already_translated:
        display_zh = title_zh if title_zh and not is_suspicious_text(title_zh) else title_en
        if display_zh and not is_suspicious_text(display_zh):
            _replace_plain_text(zh_node, display_zh)

    if title_en and not is_suspicious_text(title_en):
        _replace_plain_text(item_node.select_one(en_selector), title_en)


def enhance_daily_html_file(
    file_path: str | Path,
    summaries: List[Dict],
    *,
    date_str: Optional[str] = None,
    article_lookup: Optional[Dict[str, Dict]] = None,
) -> bool:
    path = Path(file_path)
    if not path.exists() or path.name == "index.html":
        return False

    if date_str is None:
        date_str = path.stem

    nav_map = build_nav_context(summaries)
    nav = nav_map.get(date_str, {"newer": None, "older": None, "latest": summaries[0] if summaries else None})

    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    _sanitize_soup_text(soup)
    article = soup.select_one(".daily-article")
    hero = soup.select_one(".daily-hero")
    toc_card = soup.select_one(".daily-toc-card")
    footer = soup.select_one(".daily-footer")
    if article is None or hero is None or toc_card is None or footer is None:
        return False

    _ensure_style(soup)
    _ensure_rss_head_link(soup, date_str)
    _remove_existing_injected_blocks(soup)

    top_nav = _build_nav_block(soup, date_str, nav, TOP_NAV_ID)
    hero.append(top_nav)

    outline_sections = []

    for idx, item in enumerate(soup.select(".daily-news-item"), 1):
        item_id = f"highlight-{idx}"
        item["id"] = item_id
        if article_lookup:
            _apply_article_title_fallback(item, article_lookup, is_paper=False)
        href = "#"
        source_link = item.select_one(".daily-news-link")
        if source_link is not None and source_link.get("href"):
            href = source_link.get("href")
        _replace_title_with_link(soup, item.select_one(".daily-news-title-zh"), href, item_id)
        _replace_title_with_link(soup, item.select_one(".daily-news-title-en"), href, item_id)
        _ensure_inline_links(soup, item, item_id)
        title = _title_plain_text(item.select_one(".daily-news-title-zh")) or _title_plain_text(item.select_one(".daily-news-title-en")) or _safe_text(item.get_text(" ", strip=True))
        outline_sections.append({"kind": "highlight", "title": title, "href": f"#{item_id}", "meta": "交叉重点"})

    topic_sections = []
    for idx, group in enumerate(soup.select(".daily-topic-group"), 1):
        title_node = group.select_one(".daily-topic-title")
        group_title = _safe_text(title_node.get_text(" ", strip=True) if title_node else f"专题 {idx}")
        group_id = f"topic-{_slugify(group_title, f'group-{idx}') }"
        group["id"] = group_id
        if title_node is not None:
            old_anchor = title_node.find_next_sibling("a", class_="daily-topic-anchor")
            if old_anchor is not None:
                old_anchor.decompose()
            anchor = soup.new_tag("a", href=f"#{group_id}", **{"class": "daily-permalink-link daily-topic-anchor", "title": "复制专题定位"})
            anchor.string = "#"
            title_node.insert_after(anchor)
        links = []
        for card in group.select(".daily-paper-card"):
            card_id = card.get("id") or ""
            if article_lookup:
                _apply_article_title_fallback(card, article_lookup, is_paper=True)
            source_link = card.select_one(".daily-news-link")
            href = source_link.get("href") if source_link is not None and source_link.get("href") else "#"
            _replace_title_with_link(soup, card.select_one(".daily-paper-title-zh"), href, card_id)
            _replace_title_with_link(soup, card.select_one(".daily-paper-title-en"), href, card_id)
            _ensure_inline_links(soup, card, card_id)
            title = _title_plain_text(card.select_one(".daily-paper-title-zh")) or _title_plain_text(card.select_one(".daily-paper-title-en")) or _safe_text(card.get_text(" ", strip=True))
            links.append({"title": title, "href": f"#{card_id}", "meta": (card.select_one(".daily-paper-number") or card).get_text(" ", strip=True)})
        topic_sections.append({"title": group_title, "href": f"#{group_id}", "links": links})

    # Unified single-list layout (current): paper cards live directly under #papers,
    # NOT inside a .daily-topic-group. Process them into one "今日文献" outline group.
    # (Old archived pages still use .daily-topic-group above and are handled there.)
    unified_links = []
    papers_section = soup.select_one("#papers")
    if papers_section is not None:
        for card in papers_section.select(".daily-paper-card"):
            if card.find_parent(class_="daily-topic-group") is not None:
                continue  # already handled by the topic loop (legacy layout)
            card_id = card.get("id") or ""
            if article_lookup:
                _apply_article_title_fallback(card, article_lookup, is_paper=True)
            source_link = card.select_one(".daily-news-link")
            href = source_link.get("href") if source_link is not None and source_link.get("href") else "#"
            _replace_title_with_link(soup, card.select_one(".daily-paper-title-zh"), href, card_id)
            _replace_title_with_link(soup, card.select_one(".daily-paper-title-en"), href, card_id)
            _ensure_inline_links(soup, card, card_id)
            title = _title_plain_text(card.select_one(".daily-paper-title-zh")) or _title_plain_text(card.select_one(".daily-paper-title-en")) or _safe_text(card.get_text(" ", strip=True))
            unified_links.append({"title": title, "href": f"#{card_id}",
                                  "meta": (card.select_one(".daily-paper-number") or card).get_text(" ", strip=True)})
    if unified_links:
        topic_sections.append({"title": "今日文献", "href": "#papers", "links": unified_links})

    outline_block = soup.new_tag("div", id=OUTLINE_ID, **{"class": "daily-sidebar-block"})
    outline_title = soup.new_tag("div", **{"class": "daily-sidebar-title"})
    outline_title.string = "单页目录"
    outline_block.append(outline_title)
    scroll = soup.new_tag("div", **{"class": "daily-outline-scroll"})

    if outline_sections:
        group = soup.new_tag("div", **{"class": "daily-outline-group"})
        group_title = soup.new_tag("a", href="#highlights", **{"class": "daily-outline-group-title"})
        group_title.string = "交叉重点"
        group.append(group_title)
        listing = soup.new_tag("div", **{"class": "daily-outline-list"})
        for item in outline_sections:
            link = soup.new_tag("a", href=item["href"], **{"class": "daily-outline-link daily-outline-link-highlight"})
            link.string = item["title"]
            meta = soup.new_tag("span", **{"class": "daily-outline-meta"})
            meta.string = item["meta"]
            link.append(meta)
            listing.append(link)
        group.append(listing)
        scroll.append(group)

    for topic in topic_sections:
        group = soup.new_tag("div", **{"class": "daily-outline-group"})
        group_title = soup.new_tag("a", href=topic["href"], **{"class": "daily-outline-group-title"})
        group_title.string = topic["title"]
        group.append(group_title)
        listing = soup.new_tag("div", **{"class": "daily-outline-list"})
        for item in topic["links"]:
            link = soup.new_tag("a", href=item["href"], **{"class": "daily-outline-link"})
            link.string = item["title"]
            meta = soup.new_tag("span", **{"class": "daily-outline-meta"})
            meta.string = item["meta"]
            link.append(meta)
            listing.append(link)
        group.append(listing)
        scroll.append(group)

    outline_block.append(scroll)

    sidebar_nav = soup.new_tag("div", id=SIDEBAR_NAV_ID, **{"class": "daily-sidebar-block"})
    sidebar_nav_title = soup.new_tag("div", **{"class": "daily-sidebar-title"})
    sidebar_nav_title.string = "日期跳转"
    sidebar_nav.append(sidebar_nav_title)
    sidebar_nav.append(_build_nav_block(soup, date_str, nav, "daily-sidebar-nav-inner"))

    reading_block = toc_card.select(".daily-sidebar-block")[-1] if toc_card.select(".daily-sidebar-block") else None
    if reading_block is not None:
        reading_block.insert_before(outline_block)
        reading_block.insert_before(sidebar_nav)
    else:
        toc_card.append(sidebar_nav)
        toc_card.append(outline_block)

    bottom_nav = _build_nav_block(soup, date_str, nav, BOTTOM_NAV_ID)
    footer.insert_before(bottom_nav)

    path.write_text(str(soup), encoding="utf-8")
    return True


def enhance_daily_archive(index_path: str | Path = "docs/daily/summaries.json", files: Optional[Iterable[str]] = None) -> int:
    summaries = load_summary_entries(index_path)
    article_lookup = load_article_lookup()
    selected = set(files or [])
    changed = 0
    for entry in summaries:
        file_name = entry.get("file") or f"{entry.get('date')}.html"
        if selected and file_name not in selected and entry.get("date") not in selected:
            continue
        path = Path(index_path).parent / file_name
        if enhance_daily_html_file(path, summaries, date_str=entry.get("date"), article_lookup=article_lookup):
            changed += 1
    return changed


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default="docs/daily/summaries.json")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()

    changed = enhance_daily_archive(args.index, args.files or None)
    print(f"enhanced {changed} daily page(s)")


if __name__ == "__main__":
    main()
