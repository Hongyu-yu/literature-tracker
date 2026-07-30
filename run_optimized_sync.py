import os
import sys
import json
from datetime import datetime, timezone, timedelta
import time

# Add current dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import RSS_FEEDS, KEYWORDS, DEDUP_CONFIG, AI_CONFIG
from rss_fetcher import RSSFetcher
from deduplicator import Deduplicator
from notion_tg_notifier import NotionTGNotifier
from ai_summarizer import AISummarizer, build_provider
from zh_enricher import enrich_articles_zh
from relevance_enricher import batch_analyze_relevance
from focus_filter import analyze_focus, filter_daily_focus_items, filter_focus_items, is_daily_focus, is_target_domain, is_hard_offtopic
from rss_generator import generate_rss_feed
from text_normalizer import normalize_articles_inplace

def get_beijing_time():
    beijing_tz = timezone(timedelta(hours=8))
    return datetime.now(beijing_tz)

def get_beijing_today():
    return get_beijing_time().strftime('%Y-%m-%d')

def _heuristic_ai4s_relevant(a) -> bool:
    """Heuristic fallback when AI_API_KEY is not configured."""
    article_dict = a.to_dict() if hasattr(a, "to_dict") else dict(a or {})
    if is_hard_offtopic(article_dict):
        return False
    signals = analyze_focus(article_dict)
    return is_daily_focus(article_dict) or signals['ai_science']


def _is_ai4science_relevant(article_dict: dict) -> bool:
    signals = analyze_focus(article_dict)
    return is_daily_focus(article_dict) or signals['ai_science']

def _normalize_existing_articles(articles: list[dict]) -> int:
    """In-place normalize historical schema quirks (e.g., arXiv journal naming). Returns number of changed items."""
    changed = 0
    for a in articles:
        if not isinstance(a, dict):
            continue
        src = (a.get("source_url") or "").strip()
        link = (a.get("link") or "").strip()
        if ("rss.arxiv.org/rss/" in src) or ("arxiv.org" in link):
            if (a.get("journal") or "") != "arXiv":
                a["journal"] = "arXiv"
                changed += 1
            if not (a.get("arxiv_category") or "").strip():
                marker = "/rss/"
                if marker in src:
                    a["arxiv_category"] = src.split(marker, 1)[1].strip()
                    changed += 1
    return changed

def run_optimized_sync():
    print(f"\n{'='*60}")
    print(f"开始优化同步 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")
    
    # 1) Fetch all RSS entries
    fetcher = RSSFetcher(KEYWORDS)
    print("📡 正在抓取所有RSS源...")
    all_articles = fetcher.fetch_all(RSS_FEEDS)
    print(f"获取 {len(all_articles)} 篇原始文献")

    # Deduplicate by link (high recall; keep the first occurrence)
    seen_links = set()
    unique_all = []
    for a in all_articles:
        if not getattr(a, "link", None):
            continue
        if a.link in seen_links:
            continue
        seen_links.add(a.link)
        unique_all.append(a)
    all_articles = unique_all
    print(f"按 link 去重后剩余 {len(all_articles)} 篇")

    # AI config (shared across relevance/zh/daily)
    ai_key = (os.environ.get("AI_API_KEY") or AI_CONFIG.get("api_key") or "").strip()
    ai_provider = (os.environ.get("AI_PROVIDER") or AI_CONFIG.get("provider") or "gemini").strip()
    ai_model = (os.environ.get("AI_MODEL") or AI_CONFIG.get("model") or "").strip() or None

    # 2) High-recall relevance scan for recent papers (default: yesterday only, ALL feeds incl arXiv)
    today = get_beijing_today()
    yesterday = (get_beijing_time() - timedelta(days=1)).strftime("%Y-%m-%d")

    processed_file = "data/deep_history.json"
    processed_ids = set()
    if os.path.exists(processed_file):
        try:
            with open(processed_file, "r", encoding="utf-8") as f:
                processed_ids = set(json.load(f))
        except Exception:
            processed_ids = set()

    # 默认回看最近几天，避免 RSS 晚到导致「相关文献漏收/日报不完整」
    try:
        days_back = max(1, int((os.environ.get("AI_RELEVANCE_DAYS_BACK", "3") or "3").strip()))
    except Exception:
        days_back = 3

    analysis_dates = [
        (get_beijing_time() - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(1, days_back + 1)
    ]
    if (os.environ.get("AI_RELEVANCE_INCLUDE_TODAY", "0") or "").strip().lower() in ("1", "true", "yes"):
        analysis_dates.insert(0, today)

    recent_candidates = [a for a in all_articles if a.pub_date in analysis_dates and a.link not in processed_ids]
    print(f"近期(全量)待相关性分析: {len(recent_candidates)} 篇 (日期: {', '.join(analysis_dates)})")

    ai_relevant_path = "data/ai_relevant.json"
    ai_relevant_list = []
    if os.path.exists(ai_relevant_path):
        try:
            with open(ai_relevant_path, "r", encoding="utf-8") as f:
                ai_relevant_list = json.load(f) or []
        except Exception:
            ai_relevant_list = []
    normalize_articles_inplace(ai_relevant_list)
    ai_relevant_list = [item for item in ai_relevant_list if isinstance(item, dict) and _is_ai4science_relevant(item)]
    existing_relevant_links = {a.get("link") for a in ai_relevant_list if isinstance(a, dict)}

    relevance_threshold = int(os.environ.get("AI_RELEVANCE_THRESHOLD", "6"))
    relevance_batch_size = int(os.environ.get("AI_RELEVANCE_BATCH_SIZE", "16"))
    notify_score_min = int(os.environ.get("AI_NOTIFY_SCORE_MIN", "8"))
    notify_max = int(os.environ.get("AI_NOTIFY_MAX", "5"))

    newly_relevant_count = 0
    relevant_recent = []
    notify_queue = []

    if recent_candidates:
        if ai_key:
            analyses = batch_analyze_relevance(
                [a.to_dict() for a in recent_candidates],
                provider_name=ai_provider,
                api_key=ai_key,
                model=ai_model,
                batch_size=relevance_batch_size,
            )

            for article, analysis in zip(recent_candidates, analyses):
                processed_ids.add(article.link)

                score = int(analysis.get("score", 0) or 0)
                article_dict = article.to_dict()
                is_rel = (bool(analysis.get("is_relevant")) or score >= relevance_threshold) and _is_ai4science_relevant(article_dict)

                if not is_rel:
                    continue

                relevant_recent.append(article)
                newly_relevant_count += 1

                if article.link and article.link not in existing_relevant_links:
                    item = article_dict
                    item.update(
                        {
                            "ai_score": score,
                            "ai_explanation": analysis.get("explanation"),
                            "ai_detailed_summary": analysis.get("detailed_summary"),
                        }
                    )
                    ai_relevant_list.append(item)
                    existing_relevant_links.add(article.link)

                if score >= notify_score_min:
                    notify_queue.append((score, article, analysis))

            with open(processed_file, "w", encoding="utf-8") as f:
                json.dump(sorted(list(processed_ids)), f, ensure_ascii=False, indent=2)
        else:
            # Heuristic fallback: do NOT mark as processed so that once AI key is added
            # the same items can be re-analysed with LLM.
            for article in recent_candidates:
                if not _heuristic_ai4s_relevant(article):
                    continue
                relevant_recent.append(article)
                newly_relevant_count += 1
                if article.link and article.link not in existing_relevant_links:
                    item = article.to_dict()
                    item.update(
                        {
                            "ai_score": relevance_threshold,
                            "ai_explanation": "未配置 AI_API_KEY，使用关键词/分类启发式纳入（高召回）",
                            "ai_detailed_summary": "",
                        }
                    )
                    ai_relevant_list.append(item)
                    existing_relevant_links.add(article.link)

    # Persist ai_relevant.json even if empty (stable downstream daily generation)
    os.makedirs("data", exist_ok=True)
    with open(ai_relevant_path, "w", encoding="utf-8") as f:
        json.dump(ai_relevant_list, f, ensure_ascii=False, indent=2)

    # Optional notifications (top-N, high score only)
    notifier = NotionTGNotifier()
    for score, article, analysis in sorted(notify_queue, key=lambda x: x[0], reverse=True)[:notify_max]:
        msg = f"<b>🆕 发现高度相关文献 (Score: {score})</b>\n\n"
        msg += f"<b>{article.title_zh or article.title}</b>\n"
        msg += f"<i>{article.journal}</i>\n\n"
        msg += f"🤖 <b>AI推荐理由：</b>\n{analysis.get('explanation','')}\n\n"
        msg += f"📝 <b>深度解析：</b>\n{analysis.get('detailed_summary','')}\n\n"
        msg += f"<a href='{article.link}'>🔗 查看原文</a>"
        notifier.send_tg_message(msg)
        notifier.sync_article(article.to_dict(), analysis.get("detailed_summary", ""))

    # 3) Build index candidates: keyword-filtered + AI-relevant recent (do not omit relevant)
    keyword_filtered = fetcher.filter_by_keywords(all_articles)
    print(f"关键词筛选后剩余 {len(keyword_filtered)} 篇")

    merged = keyword_filtered + relevant_recent
    merged_seen = set()
    merged_unique = []
    for a in merged:
        if not getattr(a, "link", None):
            continue
        if a.link in merged_seen:
            continue
        merged_seen.add(a.link)
        merged_unique.append(a)

    if DEDUP_CONFIG.get("enabled", True):
        deduper = Deduplicator(similarity_threshold=DEDUP_CONFIG.get("similarity_threshold", 0.98))
        merged_unique, dup_count = deduper.deduplicate(merged_unique)
        print(f"去重后剩余 {len(merged_unique)} 篇 (去除 {dup_count} 篇)")

    filtered = merged_unique

    # 4) Update global index (data/index.json), then enrich zh fields incrementally
    full_data_path = "data/index.json"
    os.makedirs("data", exist_ok=True)
    existing_articles = []
    if os.path.exists(full_data_path):
        try:
            with open(full_data_path, "r", encoding="utf-8") as f:
                existing_articles = json.load(f).get("articles", [])
        except Exception:
            existing_articles = []

    normalize_articles_inplace(existing_articles)
    normalized = _normalize_existing_articles(existing_articles)
    if normalized:
        print(f"🧹 已规范化历史字段: {normalized} 处 (e.g., arXiv journal/category)")

    existing_links = {a.get("link") for a in existing_articles if a.get("link")}
    existing_articles = [a for a in existing_articles if isinstance(a, dict) and is_target_domain(a)]
    existing_links = {a.get("link") for a in existing_articles if a.get("link")}
    new_count = 0
    new_articles = []
    for a in filtered:
        if a.link and a.link not in existing_links:
            d = a.to_dict()
            existing_articles.append(d)
            new_articles.append(d)
            new_count += 1

    zh_max_items = int(os.environ.get("AI_ZH_MAX_ITEMS", "120"))
    zh_updated = enrich_articles_zh(
        existing_articles,
        provider_name=ai_provider,
        api_key=ai_key,
        model=ai_model,
        max_items=zh_max_items,
    )
    if zh_updated:
        print(f"🌐 已补全中文标题/摘要: {zh_updated} 篇 (本次新增: {new_count})")
    elif new_count:
        print(f"🌐 本次新增: {new_count} 篇 (中文字段补全: 0)")

    # 4.5) 研究兴趣画像匹配：只对本次新文章（fail-soft，失败不影响其他阶段与退出码）
    try:
        focus_enabled = (os.environ.get("FOCUS_ENABLED", "1") or "1").strip().lower() not in ("0", "false", "no")
        if focus_enabled and new_articles:
            from focus_interest import enrich_focus_interest
            try:
                focus_max_items = int(os.environ.get("AI_FOCUS_MAX_ITEMS", "20"))
            except Exception:
                focus_max_items = 20
            focus_provider = build_provider(ai_provider, ai_key, model=ai_model) if ai_key else None
            focus_updated = enrich_focus_interest(new_articles, provider=focus_provider, max_items=focus_max_items)
            if focus_updated:
                print(f"🎯 兴趣画像匹配: {focus_updated} 篇当日新文章")
    except Exception as e:
        print(f"⚠️ 兴趣画像匹配失败(已跳过): {e}")

    normalize_articles_inplace(existing_articles)
    normalize_articles_inplace(ai_relevant_list)
    existing_articles.sort(key=lambda x: x.get("pub_date", ""), reverse=True)
    with open(full_data_path, "w", encoding="utf-8") as f:
        json.dump({"articles": existing_articles[:5000]}, f, ensure_ascii=False, indent=2)
    print(f"📊 索引文件已更新 (Total: {len(existing_articles[:5000])})")

    try:
        generate_rss_feed(existing_articles[:5000], output_path='docs/feed.xml')
    except Exception as e:
        print(f"⚠️ 全站RSS生成失败: {e}")

    print(f"\n✅ 同步完成！本次新识别相关文献: {newly_relevant_count} 篇")

def send_daily_summary():
    print(f"[{datetime.now()}] 正在生成每日汇总报告...")
    offset_days = int((os.environ.get('AI_DAILY_SUMMARY_OFFSET_DAYS', '1') or '1').strip())
    day_str = (get_beijing_time() - timedelta(days=max(0, offset_days))).strftime('%Y-%m-%d')

    index_path = "data/index.json"
    if not os.path.exists(index_path):
        print("未发现文献数据，跳过报告")
        return

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError) as e:
        print(f"⚠️ 读取 {index_path} 失败({type(e).__name__}: {e}),跳过报告")
        return

    articles = data.get("articles", [])
    normalize_articles_inplace(articles)
    day_articles = [a for a in articles if (a.get('pub_date') or '').startswith(day_str)]
    focused_articles, _ = filter_focus_items(day_articles)
    daily_articles, _ = filter_daily_focus_items(focused_articles, min_keep=12, max_keep=60)

    if not daily_articles:
        print(f"目标日期 ({day_str}) 无适合日报推送的交叉文献，跳过报告")
        return

    api_key = os.environ.get('AI_API_KEY') or os.environ.get('GEMINI_API_KEY')
    provider = os.environ.get('AI_PROVIDER') or 'gemini'

    summarizer = AISummarizer(provider, api_key)
    summary = summarizer.generate_daily_summary(daily_articles, day_str)
    summary['focused_total'] = len(focused_articles)
    summary['raw_total'] = len(day_articles)

    notifier = NotionTGNotifier()
    notifier.send_daily_report(summary)
    print("✅ 每日报告已推送至 TG 和 Notion")

if __name__ == "__main__":
    if "--summary" in sys.argv:
        send_daily_summary()
    else:
        run_optimized_sync()
