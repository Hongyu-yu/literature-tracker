"""Generate posters for the highest-priority imageless papers in one daily report."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from typing import Any, Dict, List

from focus_core import priority_tier
from focus_filter import focus_priority
from link_utils import normalize_link
from poster_generator import generate_poster


POSTER_DIR = "docs/images/posters"


def _image(item: Dict[str, Any]) -> str:
    poster = item.get("poster") if isinstance(item.get("poster"), dict) else {}
    enrich = item.get("_enrich") if isinstance(item.get("_enrich"), dict) else {}
    return str(item.get("image") or poster.get("image") or enrich.get("image") or "").strip()


def _doc_id(item: Dict[str, Any]) -> str:
    key = str(item.get("link") or item.get("title") or item.get("title_en") or "")
    return "ax" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:14]


def select_top_candidates(items: List[Dict[str, Any]], existing: List[Dict[str, Any]], max_items: int) -> List[Dict[str, Any]]:
    if max_items <= 0:
        return []
    existing_handled = {
        normalize_link(str(item.get("link") or ""))
        for item in existing if isinstance(item, dict) and item.get("link")
        and (_image(item) or str(item.get("deep_analysis") or "").strip())
    }
    candidates = [
        item for item in items if isinstance(item, dict)
        and str(item.get("abstract") or item.get("summary") or item.get("abstract_zh") or "").strip()
        and not _image(item)
        and normalize_link(str(item.get("link") or "")) not in existing_handled
    ]
    candidates.sort(key=lambda item: (priority_tier(item), focus_priority(item)))
    return candidates[:max_items]


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _daily_items(summary_path: str, index_path: str) -> List[Dict[str, Any]]:
    summary = _load_json(summary_path, {})
    items = summary.get("full_list") or summary.get("summaries") or [] if isinstance(summary, dict) else []
    index = _load_json(index_path, {})
    index_items = index.get("articles", []) if isinstance(index, dict) else []
    by_link = {normalize_link(str(item.get("link") or "")): item for item in index_items if isinstance(item, dict)}
    merged = []
    for source in items:
        item = dict(source)
        full = by_link.get(normalize_link(str(item.get("link") or "")), {})
        for key in ("abstract", "abstract_zh", "focus_score", "core_score", "title", "title_en"):
            if not item.get(key) and full.get(key):
                item[key] = full[key]
        merged.append(item)
    return merged


def process_day(day_str: str, *, provider: Any, max_items: int = 12,
                summary_path: str | None = None, index_path: str = "data/index.json",
                output_path: str | None = None, out_dir: str = POSTER_DIR,
                max_workers: int = 6) -> int:
    summary_path = summary_path or os.path.join("data", f"daily_summary_{day_str}.json")
    output_path = output_path or os.path.join("data", f"arxiv_core_{day_str}.json")
    existing = _load_json(output_path, [])
    if not isinstance(existing, list):
        existing = existing.get("items", []) if isinstance(existing, dict) else []
    candidates = select_top_candidates(_daily_items(summary_path, index_path), existing, max_items)
    if not candidates or provider is None:
        if provider is None and candidates:
            print("⏭️ Top 海报跳过：未配置 AI provider")
        return 0

    def work(item: Dict[str, Any]):
        doc_id = _doc_id(item)
        meta = {"title": item.get("title") or item.get("title_en") or "", "doc_id": doc_id}
        src = str(item.get("abstract") or item.get("summary") or item.get("abstract_zh") or "")
        try:
            poster = generate_poster(meta, src, provider=provider, out_dir=out_dir)
            if not poster or not poster.get("image"):
                return None
            return {
                "link": item.get("link") or "", "title": meta["title"],
                "title_zh": poster.get("title_zh") or item.get("title_zh") or "",
                "image": poster["image"], "poster": poster,
                "poster_elements": poster.get("elements") or {}, "source": "top_poster",
            }
        except Exception as exc:
            print(f"⚠️ Top 海报生成失败: {exc}")
            return None

    workers = max(1, min(max_workers, len(candidates)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        generated = [record for record in executor.map(work, candidates) if record]
    if not generated:
        return 0
    existing_by_link = {normalize_link(str(item.get("link") or "")): index for index, item in enumerate(existing)}
    for record in generated:
        key = normalize_link(str(record.get("link") or ""))
        if key in existing_by_link:
            existing[existing_by_link[key]].update(record)
        else:
            existing.append(record)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False)
    return len(generated)


def _provider():
    api_key = (os.environ.get("AI_API_KEY") or "").strip()
    if not api_key:
        print("⏭️ Top 海报跳过：未配置 AI_API_KEY")
        return None
    try:
        from ai_summarizer import build_provider
        return build_provider(os.environ.get("AI_PROVIDER") or "aigw", api_key,
                              model=(os.environ.get("AI_MODEL") or "gpt-5.5"))
    except Exception as exc:
        print(f"⚠️ Top 海报 provider 构建失败: {exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--max", type=int, default=int(os.environ.get("TOP_POSTER_MAX", "12")))
    args = parser.parse_args()
    try:
        if "TOP_POSTER_MAX" in os.environ and int(os.environ["TOP_POSTER_MAX"]) == 0:
            args.max = 0
    except (TypeError, ValueError):
        pass
    if args.max <= 0:
        print("⏭️ Top 海报已关闭(TOP_POSTER_MAX=0)")
        return 0
    day = args.date or (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=1)).strftime("%Y-%m-%d")
    filled = process_day(day, provider=_provider(), max_items=args.max)
    print(f"🖼️ Top 海报补全 {filled} 张（上限 {args.max}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
