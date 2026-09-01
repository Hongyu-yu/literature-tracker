import os
import sys
import json
from datetime import datetime, timezone, timedelta
import time

# Add current dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from config import RSS_FEEDS, KEYWORDS, DEDUP_CONFIG, AI_CONFIG
from rss_fetcher import RSSFetcher
from deduplicator import Deduplicator
from notion_tg_notifier import NotionTGNotifier
from ai_summarizer import AISummarizer, build_provider
from zh_enricher import enrich_articles_zh
from relevance_enricher import batch_analyze_relevance
from focus_filter import analyze_focus, filter_daily_focus_items, filter_focus_items, is_daily_focus, is_target_domain
from rss_generator import generate_rss_feed
from text_normalizer import normalize_articles_inplace

def get_beijing_time():
    beijing_tz = timezone(timedelta(hours=8))
    return datetime.now(beijing_tz)

def get_beijing_today():
    return get_beijing_time().strftime('%Y-%m-%d')


def _atomic_write_json(path: str, payload) -> None:
    """先写临时文件、fsync，再 os.replace 覆盖目标。

    直接 open(path, "w") 会先把原文件截成 0 字节再慢慢 dump；index.json 13MB、
    ai_relevant.json 37MB，而 fetch.yml 有 240 分钟超时 —— 一旦在 dump 途中被杀，
    磁盘上留下的就是半截 JSON，下次运行读不出任何历史数据。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = f"{path}.tmp"   # *.tmp 已在 .gitignore 里，残留也不会被 git add -A 带走
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # 序列化/磁盘写失败时原文件一个字节都没动，把半成品收掉再往上抛
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _user_keywords() -> dict:
    """docs/app.js 用 index.json 里的 user_keywords 填「关键词用户」下拉框。"""
    value = getattr(config, "USER_KEYWORDS", {})
    return value if isinstance(value, dict) else {}


def _is_ai4science_relevant(article_dict: dict) -> bool:
    """AI × 物理/化学/材料 交叉判定。

    等价于原来的 `is_daily_focus(item) or signals['ai_science']`（在 5000 条真实
    数据上逐条比对过，0 处不一致），但先用 analyze_focus 的结果短路：
    is_daily_focus 内部第一步就是 target_domain 检查，所以 ai_science 为真、或者
    target_domain 为假时都不必再算一遍 analyze_focus（原来是雷打不动两遍）。
    """
    signals = analyze_focus(article_dict)
    if signals['ai_science']:
        return True
    if not signals['target_domain']:
        return False
    return is_daily_focus(article_dict)


def _article_is_ai4science_relevant(a) -> bool:
    """同上，但接受 Article 对象（未配置 AI_API_KEY 时也用同一条判定）。

    原 _heuristic_ai4s_relevant 会先调一次 is_hard_offtopic()：那是多余的，
    hard_offtopic 为真时 analyze_focus 会同时把 target_domain 与 ai_science
    置 False（focus_filter.analyze_focus），结果完全一致。
    """
    article_dict = a.to_dict() if hasattr(a, "to_dict") else dict(a or {})
    return _is_ai4science_relevant(article_dict)


# relevance_enricher 在整批 API 调用失败 / JSON 解析失败时，会为该批每篇文献
# 合成一条「本地关键词规则」结论；它和模型的真实判定长得一模一样。
_FALLBACK_EXPLANATION_MARKERS = (
    "AI 返回不完整",
    "未配置 AI_API_KEY",
)


def _is_fallback_explanation(text) -> bool:
    """这段 ai_explanation 是否来自本地回退（而非模型真实判定）。"""
    return any(marker in str(text or "") for marker in _FALLBACK_EXPLANATION_MARKERS)


def _is_fallback_analysis(analysis) -> bool:
    """相关性结果是否为回退产物（AI 并未真正给出结论）。

    一次 429/超时/JSON 解析失败会让整批文献拿到本地规则的合成结论；若把它们
    当作「已分析」写进 deep_history.json，这批文献以后再也不会被 AI 评分。
    这里保守判定：拿不准就当回退，宁可下次重跑一遍，也不要永久丢弃。
    """
    if not isinstance(analysis, dict):
        return True
    if str(analysis.get("source") or "").strip().lower() in ("fallback", "local", "heuristic"):
        return True
    return _is_fallback_explanation(analysis.get("explanation"))

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
    # 本轮要写入永久黑名单的 id；索引成功落盘后才真正写文件（见下方注释）
    processed_pending = None
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

    # 本地 AI4S 判定在下面是「必要条件」：AI 给再高的分也翻不过去（见 is_rel）。
    # 先在本地筛一遍，别把注定要丢掉的候选送去 LLM 白花 token 和 wall time。
    # 注意：这里刷掉的 link 不写进 processed_ids —— 本地判定会随 focus_filter 词表
    # 变化，留着下次重判；processed_ids 的语义仍然是「真的送给模型看过」。
    pre_filter_before = len(recent_candidates)
    recent_candidates = [a for a in recent_candidates if _article_is_ai4science_relevant(a)]
    if pre_filter_before != len(recent_candidates):
        print(f"⏭️ 本地 AI4S 预筛去掉 {pre_filter_before - len(recent_candidates)} 篇，"
              f"实际送相关性分析: {len(recent_candidates)} 篇")

    ai_relevant_path = "data/ai_relevant.json"
    ai_relevant_list = []
    # ai_relevant.json 是唯一的长期库（还带着昂贵的 ai_detailed_summary），而且是就地
    # 覆盖写。读不出来不等于里面没有好数据（写到一半被杀 / 合并冲突 / 磁盘错误），
    # 这时候宁可这一轮什么都不落盘，也不能拿一个空表把它抹掉。
    ai_relevant_readable = True
    if os.path.exists(ai_relevant_path):
        loaded = []
        try:
            with open(ai_relevant_path, "r", encoding="utf-8") as f:
                loaded = json.load(f) or []
        except Exception as e:
            print(f"❌ 读取 {ai_relevant_path} 失败({type(e).__name__}: {e})，本次跳过写回，保留磁盘原文件，下次运行重试")
            ai_relevant_readable = False
            loaded = []
        if isinstance(loaded, list):
            ai_relevant_list = loaded
        elif loaded:
            print(f"❌ {ai_relevant_path} 顶层不是数组({type(loaded).__name__})，本次跳过写回，保留磁盘原文件")
            ai_relevant_readable = False

    normalize_articles_inplace(ai_relevant_list)
    ai_relevant_list = [item for item in ai_relevant_list if isinstance(item, dict)]

    # 存量重过滤默认关闭：存量在写入时就过了同一条 _is_ai4science_relevant 门槛，
    # 实测 16783/16783 一条都删不掉，却要烧掉 ~100s CPU（每天两次、随存量线性增长）。
    # 更要紧的是它没有任何保险：focus_filter 的词表一旦收紧，这里会把几千条已经
    # 花过 AI token 的记录直接从唯一的长期库里永久删掉。需要重新裁剪时用
    # AI_RELEVANT_REFILTER=1 显式跑一轮，并看日志确认删了多少。
    if (os.environ.get("AI_RELEVANT_REFILTER", "0") or "").strip().lower() in ("1", "true", "yes"):
        refilter_before = len(ai_relevant_list)
        ai_relevant_list = [item for item in ai_relevant_list if _is_ai4science_relevant(item)]
        print(f"🧹 AI_RELEVANT_REFILTER=1：存量重过滤 {refilter_before} → {len(ai_relevant_list)} 篇"
              f"（永久删除 {refilter_before - len(ai_relevant_list)} 篇）")

    newly_relevant_items = []
    existing_relevant_by_link = {
        a.get("link"): a for a in ai_relevant_list if isinstance(a, dict) and a.get("link")
    }
    existing_relevant_links = set(existing_relevant_by_link)

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

            fallback_count = 0
            upgraded_count = 0

            for article, analysis in zip(recent_candidates, analyses):
                is_fallback = _is_fallback_analysis(analysis)
                if not isinstance(analysis, dict):
                    analysis = {}
                if is_fallback:
                    # AI 没有真正给出结论：不写入已处理集合，留给下次运行重试
                    fallback_count += 1
                else:
                    processed_ids.add(article.link)

                score = int(analysis.get("score", 0) or 0)
                article_dict = article.to_dict()
                # 本地 AI4S 门槛已在 recent_candidates 预筛时统一施加，这里只看 AI 结论
                is_rel = bool(analysis.get("is_relevant")) or score >= relevance_threshold

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
                    newly_relevant_items.append(item)
                    existing_relevant_by_link[article.link] = item
                    existing_relevant_links.add(article.link)
                elif article.link and not is_fallback:
                    # 此前占位的是回退结论：AI 恢复后用真实结论覆盖（其它字段不动）
                    prev = existing_relevant_by_link.get(article.link)
                    if isinstance(prev, dict) and _is_fallback_explanation(prev.get("ai_explanation")):
                        prev.update(
                            {
                                "ai_score": score,
                                "ai_explanation": analysis.get("explanation"),
                                "ai_detailed_summary": analysis.get("detailed_summary"),
                            }
                        )
                        upgraded_count += 1

                if score >= notify_score_min:
                    notify_queue.append((score, article, analysis))

            if fallback_count:
                print(f"⚠️ 相关性分析回退 {fallback_count} 篇 (AI 未返回有效结果)，本次不标记为已处理，下次运行重试")
            if upgraded_count:
                print(f"♻️ 已用 AI 真实结论覆盖此前的回退判定: {upgraded_count} 篇")

            if ai_relevant_readable:
                # 注意：这里**只登记、不落盘**。deep_history.json 是永久黑名单
                # （第 187 行按 processed_ids 过滤候选），一旦写进去，这批文献以后
                # 再也不会被 AI 评分。而后面还有两处提前 return（索引读不出来 /
                # 条目数腰斩），若在那之前就把黑名单写死，这批文献会被标记成「已处理」
                # 却从未进入 data/index.json —— 站点索引、搜索和 docs/feed.xml 永久丢失它们。
                # 因此推迟到索引真正写盘成功之后再落盘。
                processed_pending = sorted(processed_ids)
            else:
                # ai_relevant.json 这轮不写回；此时若把这批文献记成「已处理」，
                # 它们会被永久排除在 AI 评分之外，却哪里都没落盘 —— 直接丢数据。
                print(f"⏭️ 因 {ai_relevant_path} 不可读，本次不更新 {processed_file}，下次运行重跑这批候选")
        else:
            # Heuristic fallback: do NOT mark as processed so that once AI key is added
            # the same items can be re-analysed with LLM.
            # AI4S 判定已在 recent_candidates 预筛时施加，这里不再重复计算。
            for article in recent_candidates:
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
                    newly_relevant_items.append(item)
                    existing_relevant_by_link[article.link] = item
                    existing_relevant_links.add(article.link)

    # Persist ai_relevant.json even if empty (stable downstream daily generation)
    # 只归一化本轮新增的条目：存量在上面已经过了一遍，全表再扫一次是纯浪费。
    normalize_articles_inplace(newly_relevant_items)
    if ai_relevant_readable:
        _atomic_write_json(ai_relevant_path, ai_relevant_list)
    else:
        print(f"⏭️ 已跳过 {ai_relevant_path} 写回（本轮新增 {len(newly_relevant_items)} 篇未落盘，下次运行重试）")

    # Optional notifications (top-N, high score only)
    if notify_max > 0:
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
    elif notify_queue:
        # AI_NOTIFY_MAX=0（fetch.yml 里就是 0）会让整条 TG/Notion 推送静默失效；
        # 至少让日志说清楚「不是没东西可推，是开关关着」。
        print(f"⏭️ AI_NOTIFY_MAX=0，已跳过 TG/Notion 推送（本次有 {len(notify_queue)} 篇达到通知阈值 {notify_score_min}）")

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
    previous_total = 0
    if os.path.exists(full_data_path):
        try:
            with open(full_data_path, "r", encoding="utf-8") as f:
                index_payload = json.load(f)
            articles_field = (index_payload or {}).get("articles", [])
            if not isinstance(articles_field, list):
                raise ValueError(f"articles 字段不是数组: {type(articles_field).__name__}")
            existing_articles = articles_field
            previous_total = len(existing_articles)
        except Exception as e:
            # 文件存在却读不出来（写到一半被杀 / 合并冲突 / 顶层结构不对）：
            # 继续往下跑就会在第 4 步末尾把 5000 篇的索引覆盖成本次抓到的百来篇，
            # 而且 CI 全绿、日志无异常。宁可这轮不更新索引，也不能抹掉磁盘上的好数据。
            print(f"❌ 读取 {full_data_path} 失败({type(e).__name__}: {e})，"
                  f"本次跳过索引更新与全站 RSS 生成，保留磁盘原文件，下次运行重试")
            if processed_pending:
                print(f"⏭️ 同时跳过 {processed_file} 的更新，这批候选下次运行会重跑（否则会被永久拉黑却没入索引）")
            print(f"\n⚠️ 同步部分完成！本次新识别相关文献: {newly_relevant_count} 篇（data/index.json 未更新）")
            return

    normalize_articles_inplace(existing_articles)
    normalized = _normalize_existing_articles(existing_articles)
    if normalized:
        print(f"🧹 已规范化历史字段: {normalized} 处 (e.g., arXiv journal/category)")

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
    existing_articles.sort(key=lambda x: x.get("pub_date", ""), reverse=True)

    # 索引是就地覆盖写：条目数腰斩几乎一定是上游出了问题（过滤规则误伤、抓取
    # 全挂、历史被截断），而不是正常的一天。宁可这轮不写，也不要把站点archive
    # 抹成一天的量 —— 反正下次运行还会重试。
    if previous_total >= 100 and len(existing_articles) < previous_total * 0.5:
        print(f"❌ 索引条目从 {previous_total} 篇骤降到 {len(existing_articles)} 篇（不足一半），"
              f"疑似数据异常，本次拒绝写回 {full_data_path}（磁盘原文件保持不变）")
        if processed_pending:
            print(f"⏭️ 同时跳过 {processed_file} 的更新，这批候选下次运行会重跑（否则会被永久拉黑却没入索引）")
        print(f"\n⚠️ 同步部分完成！本次新识别相关文献: {newly_relevant_count} 篇（data/index.json 未更新）")
        return

    published = existing_articles[:5000]
    # docs/app.js 读 data.total / data.last_update / data.user_keywords；
    # 之前只写 articles，页头因此永远是「共 0 篇文献 / 最后更新: -」。
    _atomic_write_json(full_data_path, {
        "total": len(published),
        "last_update": get_beijing_time().isoformat(timespec="seconds"),
        "user_keywords": _user_keywords(),
        "articles": published,
    })
    print(f"📊 索引文件已更新 (Total: {len(published)})")

    # 索引确实落盘之后，才把这批候选记入永久黑名单：顺序反过来的话，任何一条提前 return
    # 都会让文献「被拉黑但没入索引」，之后再也不会被重新评分。
    if processed_pending:
        _atomic_write_json(processed_file, processed_pending)

    try:
        generate_rss_feed(published, output_path='docs/feed.xml')
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
