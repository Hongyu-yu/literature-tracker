#!/usr/bin/env python3
"""
One-shot full backfill for Chinese fields:
- Fill `title_zh` and `abstract_zh` for ALL articles in `data/index.json`.
- `--full` mode: backfill `abstract_zh_full` (完整忠实中文翻译) for existing articles.
- Write the updated file to `data/index.json`(docs/data 由 deploy job 部署期复制,
  如需额外副本可设 BACKFILL_DOCS_PATH)。

Run in GitHub Actions (recommended) with secrets:
  AI_PROVIDER=openrouter
  AI_MODEL=stepfun/step-3.5-flash:free
  AI_API_KEY=...
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

from bs4 import BeautifulSoup

from text_normalizer import is_suspicious_text, normalize_articles_inplace, normalize_text
from zh_enricher import enrich_articles_zh


def load_index(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f) or {}


def save_index(path: str, articles: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"articles": articles}, f, ensure_ascii=False, indent=2)


def count_missing(articles: List[Dict[str, Any]]) -> int:
    n = 0
    for a in articles:
        if not (a.get("title_zh") or "").strip() or is_suspicious_text(a.get("title_zh")):
            n += 1
            continue
        if not (a.get("abstract_zh") or "").strip() or is_suspicious_text(a.get("abstract_zh")):
            n += 1
            continue
    return n


def _target_needs_full(a: Dict[str, Any]) -> bool:
    """--full 模式目标：有英文摘要但缺 abstract_zh_full（或可疑/未翻译）。
    与 zh_enricher._full_needs_translation 保持一致：full 原样等于英文摘要
    且无中文视为未翻译；源摘要本身是中文(full==abstract 且含 CJK)不算缺失。"""
    abstract = normalize_text(a.get("abstract") or "").strip()
    if not abstract:
        return False
    full = normalize_text(a.get("abstract_zh_full") or "").strip()
    return (not full) or is_suspicious_text(full) or (full == abstract and not _has_cjk(full))


def count_missing_full(articles: List[Dict[str, Any]]) -> int:
    return sum(1 for a in articles if _target_needs_full(a))


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _target_needs_title(a: Dict[str, Any]) -> bool:
    title = normalize_text(a.get("title") or "").strip()
    title_zh = normalize_text(a.get("title_zh") or "").strip()
    if not title:
        return False
    return (not title_zh) or is_suspicious_text(title_zh) or title_zh == title


def _target_needs_abstract(a: Dict[str, Any]) -> bool:
    abstract = normalize_text(a.get("abstract") or "").strip()
    abstract_zh = normalize_text(a.get("abstract_zh") or "").strip()
    if not abstract:
        return False
    return (not abstract_zh) or is_suspicious_text(abstract_zh) or abstract_zh == abstract


def _is_exact_english_fallback_title(a: Dict[str, Any]) -> bool:
    title = normalize_text(a.get("title") or "").strip()
    title_zh = normalize_text(a.get("title_zh") or "").strip()
    return bool(title and title_zh and title_zh == title)


def _is_exact_english_fallback_abstract(a: Dict[str, Any]) -> bool:
    abstract = normalize_text(a.get("abstract") or "").strip()
    abstract_zh = normalize_text(a.get("abstract_zh") or "").strip()
    return bool(abstract and abstract_zh and abstract_zh == abstract)


def _load_json_list(path: str | Path, key: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get(key, []) if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _collect_visible_english_links_from_html(file_path: str | Path) -> Set[str]:
    path = Path(file_path)
    if not path.exists():
        return set()

    try:
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    except Exception:
        return set()

    links: Set[str] = set()
    for item in soup.select(".daily-news-item, .daily-paper-card, .weekly-paper-card"):
        title = item.select_one(".daily-news-title-zh, .daily-paper-title-zh, .weekly-paper-title-zh")
        href = ""
        for selector in (".daily-news-link", ".weekly-paper-link", "a[href]"):
            node = item.select_one(selector)
            if node is not None and node.get("href"):
                href = str(node.get("href") or "").strip()
                if href:
                    break
        title_text = normalize_text(title.get_text(" ", strip=True) if title else "").replace("#", "").strip()
        if href and title_text and not _has_cjk(title_text):
            links.add(href)
    return links


def _collect_site_latest_english_links() -> Set[str]:
    links: Set[str] = set()

    daily_entries = _load_json_list("docs/daily/summaries.json", "summaries")
    daily_entries.sort(key=lambda x: x.get("date") or "", reverse=True)
    latest_daily_count = max(1, int(os.environ.get("BACKFILL_LATEST_DAILY_COUNT", "2") or "2"))
    for entry in daily_entries[:latest_daily_count]:
        file_name = entry.get("file") or f"{entry.get('date')}.html"
        links.update(_collect_visible_english_links_from_html(Path("docs/daily") / file_name))

    weekly_entries = _load_json_list("docs/weekly/index.json", "weeklies")
    weekly_entries.sort(key=lambda x: x.get("week_start") or "", reverse=True)
    latest_weekly_count = max(0, int(os.environ.get("BACKFILL_LATEST_WEEKLY_COUNT", "1") or "1"))
    for entry in weekly_entries[:latest_weekly_count]:
        file_name = entry.get("file") or f"{entry.get('week_start')}.html"
        links.update(_collect_visible_english_links_from_html(Path("docs/weekly") / file_name))

    return links


def _select_targets(articles: List[Dict[str, Any]], scope: str) -> List[Dict[str, Any]]:
    fallback_targets = [a for a in articles if _is_exact_english_fallback_title(a) or _is_exact_english_fallback_abstract(a)]
    if scope == "all_missing":
        return [a for a in articles if _target_needs_title(a) or _target_needs_abstract(a)]
    if scope == "fallback_english_only":
        return fallback_targets
    if scope == "site_latest_and_fallback":
        visible_links = _collect_site_latest_english_links()
        return [
            a for a in articles
            if _is_exact_english_fallback_title(a)
            or _is_exact_english_fallback_abstract(a)
            or ((a.get("link") or "").strip() in visible_links and not _has_cjk(normalize_text(a.get("title_zh") or "").strip()))
        ]
    raise ValueError(f"Unsupported BACKFILL_SCOPE: {scope}")


def _prepare_targets(targets: Iterable[Dict[str, Any]]) -> None:
    for a in targets:
        if _target_needs_title(a) or not _has_cjk(normalize_text(a.get("title_zh") or "").strip()):
            a["title_zh"] = ""
        if _target_needs_abstract(a):
            a["abstract_zh"] = ""


def _sync_site_outputs(articles: List[Dict[str, Any]]) -> None:
    from daily_page_enhancer import enhance_daily_archive
    from generate_daily_pages import load_index_articles, load_relevant, load_summary_index, sync_daily_rss_feeds
    from rss_generator import generate_rss_feed
    from weekly_page_enhancer import enhance_weekly_archive

    generate_rss_feed(articles, output_path="docs/feed.xml")
    summaries = (load_summary_index().get("summaries") or [])
    rss_changed = sync_daily_rss_feeds(load_index_articles("data/index.json"), load_relevant("data/ai_relevant.json"), summaries)
    daily_changed = enhance_daily_archive("docs/daily/summaries.json")
    weekly_changed = enhance_weekly_archive("docs/weekly/index.json")
    print(
        f"[backfill] synced site outputs: feed=docs/feed.xml daily_rss={rss_changed} "
        f"daily_pages={daily_changed} weekly_pages={weekly_changed}"
    )


def main() -> int:
    full_mode = "--full" in sys.argv
    index_path = os.environ.get("BACKFILL_INDEX_PATH") or "data/index.json"
    # docs/data 为部署期产物(deploy job 从 data/ 复制),默认不再写副本;需要时设 BACKFILL_DOCS_PATH
    out_docs_path = os.environ.get("BACKFILL_DOCS_PATH") or ""
    scope = (os.environ.get("BACKFILL_SCOPE") or "all_missing").strip()

    data = load_index(index_path)
    articles = data.get("articles", []) or []
    normalize_articles_inplace(articles)
    if not articles:
        print("No articles found; abort.")
        return 1

    ai_key = (os.environ.get("AI_API_KEY") or "").strip()
    ai_provider = (os.environ.get("AI_PROVIDER") or "openrouter").strip()
    ai_model = (os.environ.get("AI_MODEL") or "").strip() or None

    batch_size = int(os.environ.get("AI_ZH_BATCH_SIZE", "16"))
    max_passes = int(os.environ.get("AI_ZH_MAX_PASSES", "20"))
    sleep_s = float(os.environ.get("AI_ZH_PASS_SLEEP_SECONDS", "1.0"))

    if full_mode:
        # --full: 只回填 abstract_zh_full（完整翻译），不动已有 title_zh/abstract_zh
        targets = [a for a in articles if _target_needs_full(a)]
        missing_fn = count_missing_full
        mode_label = "full"
    else:
        targets = _select_targets(articles, scope)
        _prepare_targets(targets)
        missing_fn = count_missing
        mode_label = f"scope={scope}"

    missing_before = missing_fn(targets)
    print(f"[backfill] mode={mode_label} total={len(articles)} targets={len(targets)} missing_before={missing_before}")
    if missing_before == 0:
        print("[backfill] already complete; nothing to do.")
        return 0

    for p in range(1, max_passes + 1):
        missing = missing_fn(targets)
        if missing == 0:
            break

        updated = enrich_articles_zh(
            targets,
            provider_name=ai_provider,
            api_key=ai_key,
            model=ai_model,
            max_items=len(targets),
            batch_size=batch_size,
        )
        missing_after = missing_fn(targets)
        print(f"[backfill] pass={p} updated={updated} missing_after={missing_after}")

        if updated == 0:
            # Avoid infinite loop: either API is failing or remaining items have no usable inputs.
            time.sleep(sleep_s * 5)
        else:
            time.sleep(sleep_s)

    missing_final = missing_fn(targets)
    print(f"[backfill] missing_final={missing_final}")

    # Persist
    save_index(index_path, articles)
    if out_docs_path:
        save_index(out_docs_path, articles)
    print(f"[backfill] wrote {index_path}" + (f" and {out_docs_path}" if out_docs_path else ""))
    _sync_site_outputs(articles)

    # Non-zero exit if still missing (so Actions can alert)
    return 0 if missing_final == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
