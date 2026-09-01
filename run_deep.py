"""编排：拉 APS → 精读 → 海报 → 分类 → 写 arxiv_core/aps 富化。所有步骤失败静默降级。

幂等：已在 data/aps_<date>.json 里带 deep_analysis 的论文直接复用，不重复调用 gpt-5.5。
默认只处理最近 1 天（DEEP_WINDOW_DAYS=1），手动 dispatch 可传更大窗口做增量回填。
"""
import os, json, glob, datetime, hashlib
from concurrent.futures import ThreadPoolExecutor

from aps_client import ApsClient
from ai_summarizer import build_provider
from deep_reader import deep_read, abstract_read
from poster_generator import generate_poster
from auto_classifier import classify
from image_provider import generate_and_save
from link_utils import normalize_link


def _deep_complete(text):
    """全文深读是否完整：苏格拉底 prompt 第五部分为「创新评估」，截断会缺它。"""
    return bool(text) and ("创新" in text) and len(text) >= 5000


def _deep_complete_abstract(text):
    """摘要级解析完整性：abstract 解析是精炼的(远短于全文)，含创新性判断且达基本篇幅即视为完成。
    用 5000 字门槛会让短摘要解析永远判为未完成→每轮无限重处理耗尽预算；120 字足以区分真解析与空/截断。"""
    return bool(text) and ("创新" in text) and len(text) >= 120


def _tier2_complete(rec):
    """tier-2 富化完成判定（支持全文升级 + attempts 封顶防无限重处理）。
    - 全文模式(html/pdf) 且含"创新" 且 ≥3000 字 → 完成（拿到真正全文苏格拉底）。
    - 否则继续尝试升级全文；attempts≥3 → **无条件**定稿（HTML-less / provider 持续失败的论文
      在 3 次后停手，避免空/无关键词的记录每轮重处理、耗尽预算）。
    - 旧缓存(无 mode/attempts, attempts=0) → 未完成(待升级)。"""
    if not rec:
        return False
    text = rec.get("deep_analysis") or ""
    attempts = int(rec.get("ft_attempts") or 0)
    mode = rec.get("analysis_mode") or "abstract"
    if mode in ("html", "pdf") and ("创新" in text) and len(text) >= 3000:
        return True
    # 硬封顶：尝试 3 次后接受现状（即便分析为空/缺关键词），杜绝无限重处理拖垮预算
    if attempts >= 3:
        return True
    return False


def _aps_complete(rec):
    """APS 记录是否定稿（完整深读 或 重试封顶），与 tier-2 的 _tier2_complete 同一套封顶思路。
    - 深读完整(含第五部分「创新评估」且 ≥5000 字) → 直接复用缓存，零成本；
    - 否则继续重试升级；deep_attempts≥3 → **无条件**定稿。
      不封顶的话，OSS 对象 404 / 全文抓不到 / provider 持续报错的论文，只要日期还在
      DEEP_WINDOW_DAYS 窗口里就每轮都被判成 fresh：既每次重传最多 4 万字全文白烧 token，
      又占满 DEEP_MAX_NEW_PER_RUN 预算，把真正的新论文挤到下一轮（手动 dispatch 传大窗口
      回填时尤其致命——30 天窗口就是 30 轮空转）。
    - 旧缓存(无 deep_attempts 字段)按 0 次算，与改动前行为一致。"""
    if not rec:
        return False
    if _deep_complete(rec.get("deep_analysis")):
        return True
    try:
        attempts = int(rec.get("deep_attempts") or 0)
    except (TypeError, ValueError):  # 缓存被手改坏也不该炸掉整轮 APS
        attempts = 0
    return attempts >= 3


def _core_key(rec):
    """arxiv_core 行的主键：link 归一化(与 backfill_top_posters/generate_daily_pages 的 join 口径一致)，
    无 link 才退回标题。缓存命中与合并写盘共用同一口径，避免同一篇论文写成两行。"""
    link = str((rec or {}).get("link") or "").strip()
    if link:
        return normalize_link(link)
    return "title:" + str((rec or {}).get("title") or "").strip()


def _enrich_one(meta, client, provider, out_dir, cached=None):
    # 幂等复用：已生成完整深读(含第五部分创新评估)、或重试已封顶的论文算完成、直接复用
    if _aps_complete(cached):
        return cached
    md = client.fetch_markdown(meta)
    rec = dict(meta)
    rec["source"] = "APS"
    rec["category"] = (cached or {}).get("category") or classify(meta, provider=provider)
    # 抓不到全文 / provider 报错时 deep_read 返回空串：绝不能用空串覆盖缓存里已有的(哪怕截断的)深读，
    # 否则一次网关抖动就把好数据抹掉。保留旧文本，下轮再重试升级。
    new_deep = deep_read(meta, md, provider=provider) if md else ""
    if not new_deep and (cached or {}).get("deep_analysis"):
        print(f"⚠️ 深读失败，保留缓存深读: {meta.get('doc_id')}")
    rec["deep_analysis"] = new_deep or (cached or {}).get("deep_analysis") or ""
    # 尝试次数必须显式从缓存搬过来(rec 是 meta 的副本，metadata.jsonl 里没有这个字段)，
    # 否则计数永远停在 1，_aps_complete 的封顶形同虚设。
    try:
        rec["deep_attempts"] = int((cached or {}).get("deep_attempts") or 0) + 1
    except (TypeError, ValueError):
        rec["deep_attempts"] = 1
    # 海报要素复用深读产出(更聚焦、省 input token)；深读为空才退回原文
    poster_src = rec["deep_analysis"] or md
    # 复用已有海报，避免重复图像生成；缺失才生成
    rec["poster"] = (cached or {}).get("poster") or (
        generate_poster(meta, poster_src, provider=provider, out_dir=out_dir) if poster_src else None)
    if rec.get("poster") and rec["poster"].get("title_zh"):
        rec["title_zh"] = rec["poster"]["title_zh"]
    return rec


def process_date(date, client, provider, out_dir="docs/images/posters", max_workers=5,
                 cache=None, max_new=None):
    """处理某天的全文论文。cache 命中(已带深读)直接复用；max_new 限制本轮新生成的论文数
    （超出预算的新论文本轮跳过，下轮再处理，靠幂等累积回填）。返回 (records, new_used)。"""
    metas = client.fetch_metadata(date)
    full = [m for m in metas if m.get("has_full_text")]
    if not full:
        return [], 0
    cache = cache or {}
    cached, fresh = [], []
    for m in full:
        c = cache.get(m.get("doc_id") or m.get("paper_id"))
        # 只有带完整深读、或重试已封顶的才算完成；缺深读或被截断(缺第五部分)的要重试(走 fresh，复用海报)。
        # 判定口径必须与 _enrich_one 里的一致：否则封顶后的论文仍被算进 fresh，白占一格
        # max_new 预算再原样返回，等于每轮凭空漏掉一篇新论文。
        (cached if _aps_complete(c) else fresh).append((m, c))
    if max_new is not None:
        fresh = fresh[:max(0, max_new)]
    results = [c for (_m, c) in cached]  # 复用缓存，零成本
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_enrich_one, m, client, provider, out_dir, c) for (m, c) in fresh]
        for f in futs:
            try: results.append(f.result())
            except Exception as e: print(f"⚠️ enrich failed: {e}")
    return results, len(fresh)


def _enrich_arxiv_tier2_one(cand, provider, out_dir, cached=None):
    if cached and _tier2_complete(cached):
        return cached
    import hashlib
    from arxiv_fulltext import fetch_fulltext
    rec = dict(cand)
    rec["source"] = "arxiv"
    rec["category"] = cand.get("category") or classify(cand, provider=provider)
    doc_id = "ax" + hashlib.sha1((cand.get("link") or cand.get("title", "")).encode("utf-8")).hexdigest()[:14]
    meta = {"title": cand.get("title", ""), "authors": cand.get("authors"),
            "year": cand.get("year"), "doc_id": doc_id}
    # 抓全文(HTML 优先/PDF 兜底) → 苏格拉底深读；拿不到 → 退回摘要解析
    fulltext, mode = fetch_fulltext(cand.get("link") or "")
    prev = cached or {}
    prev_deep = prev.get("deep_analysis") or ""
    prev_mode = prev.get("analysis_mode") or "abstract"
    prev_attempts = int(prev.get("ft_attempts") or 0)
    new_deep, new_mode = "", ""
    if fulltext:
        new_deep, new_mode = deep_read(meta, fulltext, provider=provider), mode
    elif not _deep_complete_abstract(prev_deep):
        # 已有完整摘要解析就不再重复计费(全文升级失败前每轮都会重跑到这里)；缺失/截断才(重)跑
        abs_txt = cand.get("abstract") or cand.get("summary") or ""
        if abs_txt:
            new_deep, new_mode = abstract_read(cand, abs_txt, provider=provider), "abstract"
    # 深读/摘要解析失败(返回空)时保留缓存旧解析，绝不用空串覆盖好数据——尤其 ft_attempts 封顶后
    # 会永久定稿。analysis_mode 必须与文本同进退：否则旧摘要文本被标成 html，_tier2_complete 误判完成。
    if not new_deep and prev_deep:
        print(f"⚠️ 精读失败，保留缓存解析({prev_mode}): {cand.get('link') or cand.get('title')}")
    rec["deep_analysis"] = new_deep or prev_deep
    rec["analysis_mode"] = new_mode if new_deep else prev_mode
    rec["ft_attempts"] = prev_attempts + 1
    # 海报要素优先喂深读产出(更聚焦、省 input token)；深读为空再退回全文/摘要
    poster_src = rec["deep_analysis"] or fulltext or cand.get("abstract") or cand.get("summary") or ""
    poster = prev.get("poster") or (
        generate_poster(meta, poster_src, provider=provider, out_dir=out_dir) if poster_src else None)
    rec["poster"] = poster or prev.get("poster")
    # 海报生成失败(poster=None)同样不许把已有的图/要素抹成 None
    rec["image"] = (poster or {}).get("image") or prev.get("image")
    rec["poster_elements"] = (poster or {}).get("elements") or prev.get("poster_elements")
    if poster and poster.get("title_zh") and not rec.get("title_zh"):
        rec["title_zh"] = poster["title_zh"]
    return rec


def process_arxiv_tier2(date, candidates, provider, out_dir="docs/images/posters",
                        max_workers=5, cache=None, max_new=None):
    cache = cache or {}
    cands = candidates or []
    cached, fresh = [], []
    for c in cands:
        prev = cache.get(_core_key(c))
        (cached if (prev and _tier2_complete(prev)) else fresh).append((c, prev))
    overflow = []
    if max_new is not None and len(fresh) > max_new:
        overflow = fresh[max_new:]
        fresh = fresh[:max(0, max_new)]
    results = [p for (_c, p) in cached]
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_enrich_arxiv_tier2_one, c, provider, out_dir, prev) for (c, prev) in fresh]
        for f in futs:
            try: results.append(f.result())
            except Exception as e: print(f"⚠️ tier2 enrich failed: {e}")
    # over-budget candidates: keep them in the feed as plain text cards (no deep/image yet);
    # they get enriched in a later run thanks to the idempotent cache.
    for (c, prev) in overflow:
        results.append(prev if prev else {**c, "source": "arxiv"})
    return results, len(fresh)


def prune_images(window_days=60, today=None, dirs=("docs/images/posters", "docs/images/cards")):
    today = today or datetime.date.today()
    if isinstance(today, str):
        today = datetime.date.fromisoformat(today)
    cutoff = today - datetime.timedelta(days=window_days)
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*.webp")):
            try:
                mtime = datetime.date.fromtimestamp(os.path.getmtime(p))
                if mtime < cutoff:
                    os.remove(p)
            except Exception:
                pass


def _enrich_arxiv_one(a, provider, out_dir):
    rec = dict(a)
    rec["source"] = "arxiv"
    rec["category"] = classify(a, provider=provider)
    # 幂等：已有 image 的不重复生成
    if a.get("image"):
        return rec
    h = hashlib.sha1((a.get("link") or a.get("title", "")).encode("utf-8")).hexdigest()[:16]
    prompt = ("Flat vector minimalist scientific illustration, single clear concept, "
              "clean lines, off-white background, deep blue + teal accents, no text. "
              f"Concept: {a.get('title','')[:120]}")
    saved = generate_and_save(prompt, os.path.join(out_dir, f"{h}.webp"), max_edge=768)
    rec["image"] = (saved or "").replace("docs/", "") or None
    return rec


def enrich_arxiv_core(items, provider=None, out_dir="docs/images/cards", max_workers=5):
    items = items or []
    out = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_enrich_arxiv_one, a, provider, out_dir) for a in items]
        for f in futs:
            try: out.append(f.result())
            except Exception as e: print(f"⚠️ arxiv enrich failed: {e}")
    return out


def _load_core_cache(date):
    """读 data/arxiv_core_<date>.json（tier-2 幂等缓存的唯一读入口）。
    注：曾有一个逐字节相同的 _load_arxiv_core 副本，无任何调用方，已删——两份实现只会各自漂移。"""
    path = f"data/arxiv_core_{date}.json"
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _merge_core_records(existing, results):
    """把本轮 tier-2 结果**逐字段合并**进既有 arxiv_core 行，而不是整表替换。
    - 候选之外的行(backfill_top_posters 写的 source=top_poster、历史行)原样保留；
    - 空值(None/""/{}/[])不覆盖已有好数据：某轮海报或深读失败不会抹掉上一轮的成果；
    - 保持既有行序，新行追加 → 每轮 diff 最小、无无谓 commit。"""
    merged = [r for r in (existing or []) if isinstance(r, dict)]
    index = {}
    for i, r in enumerate(merged):
        index.setdefault(_core_key(r), i)
    for rec in (results or []):
        if not isinstance(rec, dict):
            continue
        key = _core_key(rec)
        i = index.get(key)
        if i is None:
            index[key] = len(merged)
            merged.append(dict(rec))
        else:
            merged[i].update({f: v for f, v in rec.items() if v not in (None, "", {}, [])})
    return merged


def _save_core_merged(date, results, path=None):
    """♻️ 合并写 data/arxiv_core_<date>.json（不是整表覆盖）。返回是否真的落盘。"""
    path = path or f"data/arxiv_core_{date}.json"
    existing = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except (ValueError, UnicodeDecodeError) as e:
            print(f"⚠️ arxiv_core {date} 解析失败({e})，按空表重建")  # 坏文件本就读不出内容，重建无损失
            existing = []
        except OSError as e:  # 读盘故障：宁可不写，也不要用残缺结果覆盖既有数据
            print(f"⚠️ arxiv_core {date} 读取失败({e})，跳过写盘")
            return False
    if not isinstance(existing, list):
        existing = existing.get("items", []) if isinstance(existing, dict) else []
    before = json.dumps(existing, ensure_ascii=False, sort_keys=True)
    merged = _merge_core_records(existing, results)
    if json.dumps(merged, ensure_ascii=False, sort_keys=True) == before:
        return False  # 无变化不落盘（回填老日期时常见），避免每轮空提交
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
    return True


def _load_aps_cache(date):
    """已生成的 aps_<date>.json → {doc_id: rec} 供幂等复用。"""
    path = f"data/aps_{date}.json"
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            recs = json.load(f)
    except Exception:
        return {}
    return {(r.get("doc_id") or r.get("paper_id")): r for r in recs if isinstance(r, dict)}


def _save_aps_index(date, aps):
    os.makedirs("data", exist_ok=True)
    with open(f"data/aps_{date}.json", "w", encoding="utf-8") as f:
        json.dump(aps, f, ensure_ascii=False)


def main():
    provider = build_provider(os.environ.get("AI_PROVIDER", "aigw"),
                              os.environ.get("AI_API_KEY", ""),
                              os.environ.get("AI_MODEL", "gpt-5.5"))
    client = ApsClient()
    # APS data lags ~1-2 days, so window must cover it (default 4 days).
    window = int(os.environ.get("DEEP_WINDOW_DAYS", "4"))
    workers = int(os.environ.get("DEEP_WORKERS", "5"))
    # Per-run budget of NEW papers to deep-read (prevents 90-min timeout on first backfill;
    # idempotent cache lets repeated/scheduled runs finish the rest).
    budget = int(os.environ.get("DEEP_MAX_NEW_PER_RUN", "14"))
    # ---- APS full-text deep-read (T1) ----
    dates = client.list_dates(window_days=window)
    print(f"📚 APS dates to process (window={window}, new-budget={budget}): {dates}")
    for d in sorted(dates, reverse=True):  # newest first → freshest within budget
        cache = _load_aps_cache(d)
        aps, used = process_date(d, client, provider, max_workers=workers,
                                 cache=cache, max_new=budget)
        budget -= used
        enriched = sum(1 for a in aps if a.get("deep_analysis"))
        print(f"  APS {d}: {len(aps)} papers ({enriched} with deep_analysis), {used} new this run")
        if aps:
            _save_aps_index(d, aps)

    # ---- arXiv tier-2 abstract-level deep-read + infographic (T2) ----
    # DECOUPLED from APS dates: iterate the arxiv_tier2_<date>.json files the daily generator
    # wrote (arXiv has data even when APS is unavailable/empty). Shared budget, newest-first.
    # run_deep is the SOLE writer of arxiv_core_<date>.json (with deep_analysis/image/poster_elements).
    t2dates = sorted({os.path.basename(p)[len("arxiv_tier2_"):-len(".json")]
                      for p in glob.glob("data/arxiv_tier2_*.json")}, reverse=True)
    print(f"📰 arXiv tier-2 dates: {t2dates} (remaining budget {budget})")
    for d in t2dates:
        if budget <= 0:
            break
        try:
            with open(f"data/arxiv_tier2_{d}.json", encoding="utf-8") as f:
                cands = json.load(f)
            # 缓存键必须与 process_arxiv_tier2 的查找口径(_core_key)一致，
            # 否则缓存永远查不中，每轮都会把所有论文重新深读一遍。
            t2cache = {_core_key(x): x for x in _load_core_cache(d)}
            t2, t2used = process_arxiv_tier2(d, cands, provider, max_workers=workers,
                                             cache=t2cache, max_new=budget)
            budget -= t2used
            ndeep = sum(1 for x in t2 if x.get("deep_analysis"))
            print(f"  tier2 {d}: {len(t2)} items ({ndeep} with deep_analysis), {t2used} new this run")
            if t2:
                # 合并写：本轮只处理 tier2 候选，整表覆盖会删掉 backfill_top_posters 写的
                # source=top_poster 行以及本轮预算外的历史行。
                if _save_core_merged(d, t2):
                    print(f"  ♻️ arxiv_core {d} 已合并更新")
        except Exception as e:
            print(f"⚠️ tier2 processing failed for {d}: {e}")

    prune_images(window_days=60)
    print("✅ run_deep done (feed.json no longer written; enrichment lives in arxiv_core/aps)")


if __name__ == "__main__":
    main()
