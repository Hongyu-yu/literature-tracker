"""补全缺图海报：扫 arxiv_core/aps JSON，对「有 deep_analysis 但 image 为空」的条目补生成海报图。

关键省成本点：这些条目通常已存好 `poster.elements`(中文 5 要素),直接复用它生图即可,
**不重跑昂贵的深读/要素抽取**,每篇仅 1 次图 API。仅当没有现成 elements 时才回退整套
generate_poster(需 provider)。幂等：已有 image 的跳过;--max 按「尝试数」限量(失败也扣配额),
配额用完即停;连续多次生图失败会熔断,不把整轮时间烧在挂掉的图 API 上。

用法：python backfill_posters.py [--glob "data/aps_*.json,data/arxiv_core_*.json"] [--max 50]
      --glob 支持逗号分隔多组,按给出的顺序优先(APS 是当日精读头卡,先补)。
"""
import os, glob as globmod, json, argparse, hashlib, threading
from poster_generator import build_infographic_prompt, generate_poster
from image_provider import generate_and_save

POSTER_DIR = "docs/images/posters"
# 默认两族都扫：APS 深读条目在 run_deep 里永远不会重跑(有 poster 字典即视为已完成),
# 只能靠这里补图;放在前面是因为它是每日页的「今日精读」头卡,优先级最高。
DEFAULT_GLOB = "data/aps_*.json,data/arxiv_core_*.json"


class _FailBreaker:
    """跨文件共享的连续失败熔断器：图 API 系统性故障时,继续尝试只是白烧配额和时间。

    线程安全(process_file 是并发生图的)。limit<=0 表示不熔断。"""

    def __init__(self, limit=6):
        self.limit = limit
        self.streak = 0
        self._lock = threading.Lock()

    def tripped(self):
        with self._lock:
            return self.limit > 0 and self.streak >= self.limit

    def record(self, ok):
        with self._lock:
            self.streak = 0 if ok else self.streak + 1


def _poster_of(it):
    p = it.get("poster")
    return p if isinstance(p, dict) else {}


def find_missing(items):
    """返回 [(index, item), ...]：有 deep_analysis 但缺海报图的条目。"""
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not it.get("deep_analysis"):
            continue
        image = it.get("image") or _poster_of(it).get("image")
        if not image:
            out.append((i, it))
    return out


def _elements_of(it):
    return _poster_of(it).get("elements") or it.get("poster_elements") or None


def _doc_id_of(it):
    did = _poster_of(it).get("doc_id") or it.get("doc_id")
    if did:
        return did
    key = it.get("link") or it.get("title") or ""
    return ("ax" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:14]) if key else None


def _apply_image(it, image_rel):
    it["image"] = image_rel
    if isinstance(it.get("poster"), dict):
        it["poster"]["image"] = image_rel


def backfill_item(it, provider=None, out_dir=POSTER_DIR):
    """给单条补海报图。返回 image 相对路径(images/...)或 None(失败/无法处理)。"""
    doc_id = _doc_id_of(it)
    if not doc_id:
        return None
    elements = _elements_of(it)
    title = it.get("title", "")
    if elements:
        # 快路径：复用已抽取的中文要素,只调一次图 API,不重跑深读/抽取
        prompt = build_infographic_prompt(elements, title)
        out_path = os.path.join(out_dir, f"{doc_id}.webp")
        saved = generate_and_save(prompt, out_path, max_edge=1536)
        image = (saved or "").replace("docs/", "") or None
    else:
        # 回退：没有现成要素,用 deep_analysis 走整套(抽取+生图),需要 provider
        src = it.get("deep_analysis") or it.get("abstract") or it.get("summary") or ""
        if not (src and provider):
            return None
        meta = {"title": title, "doc_id": doc_id}
        res = generate_poster(meta, src, provider=provider, out_dir=out_dir)
        if res:
            it["poster"] = res
            it["poster_elements"] = res.get("elements")
            if res.get("title_zh") and not it.get("title_zh"):
                it["title_zh"] = res["title_zh"]
        image = (res or {}).get("image")
    if image:
        _apply_image(it, image)
    return image


def process_file(path, provider=None, budget=None, out_dir=POSTER_DIR, max_workers=None,
                 breaker=None, stats=None):
    """补一个 JSON 文件里的缺图条目;返回 (filled, missing_total)。保持原写盘格式(无缩进)。

    并发生图(每条目/每 doc_id 互不相干,线程安全):先按 budget 切片,再并发调 backfill_item。
    本次实际尝试数写入可选的 stats 字典(stats["attempted"]),调用方据此按尝试数扣配额;
    breaker 是可选的跨文件熔断器,连续失败到阈值后不再发起新的图 API 调用。"""
    if max_workers is None:
        try:
            max_workers = int(os.environ.get("POSTER_BACKFILL_WORKERS", "6"))
        except (TypeError, ValueError):
            max_workers = 6
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data if isinstance(data, list) else (data.get("items") or data.get("papers") or [])
    missing = find_missing(items)
    todo = missing if budget is None else missing[:max(0, budget)]
    if stats is not None:
        stats["attempted"] = len(todo)   # 尝试数(不管成败),供调用方扣配额
    filled = 0
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        def _work(pair):
            if breaker is not None and breaker.tripped():
                return None              # 已熔断:不再发起新的图 API 调用
            img = backfill_item(pair[1], provider=provider, out_dir=out_dir)
            if breaker is not None:
                breaker.record(bool(img))
            return img
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(todo)))) as ex:
            for (idx, it), img in zip(todo, ex.map(_work, todo)):
                print(("  ✅" if img else "  ⚠️"), os.path.basename(path),
                      (it.get("title") or "")[:48], "->", img)
                if img:
                    filled += 1
    if breaker is not None and breaker.tripped():
        print(f"🛑 连续 {breaker.limit} 次生图失败(疑似图 API 故障),提前收手;"
              f"本文件已补 {filled} 张,其余留待下轮重试")
    if filled:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)   # 匹配 run_deep 写盘(无缩进)
    return filled, len(missing)


def _build_provider():
    try:
        from ai_summarizer import build_provider
        return build_provider(os.environ.get("AI_PROVIDER", "aigw"),
                              os.environ.get("AI_API_KEY", ""),
                              os.environ.get("AI_MODEL", "gpt-5.5"))
    except Exception as e:
        print(f"⚠️ provider 构建失败(仅影响无 elements 的回退路径): {e}")
        return None


def collect_paths(pattern):
    """把 --glob 展开成待扫文件列表：逗号分隔的多组通配按给出顺序排(先扫的先花配额),
    每组内部按文件名倒序(新日期优先),跨组去重。"""
    seen, out = set(), []
    for pat in str(pattern or "").split(","):
        pat = pat.strip()
        if not pat:
            continue
        for p in sorted(globmod.glob(pat), reverse=True):  # 新日期优先
            if p not in seen:
                seen.add(p); out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default=DEFAULT_GLOB,
                    help="待扫描 JSON 通配;可逗号分隔多组,按给出顺序优先")
    ap.add_argument("--max", type=int,
                    default=int(os.environ.get("POSTER_BACKFILL_MAX", "50")),
                    help="本次最多尝试补几张(失败也扣配额,控成本);<=0 表示不限")
    args = ap.parse_args()
    paths = collect_paths(args.glob)
    if not paths:
        print(f"没有匹配文件: {args.glob}"); return
    try:
        fail_limit = int(os.environ.get("POSTER_BACKFILL_FAIL_LIMIT", "6"))
    except (TypeError, ValueError):
        fail_limit = 6
    breaker = _FailBreaker(fail_limit)
    provider = None  # 懒构建：仅在遇到无 elements 的条目时才需要
    budget = args.max if args.max and args.max > 0 else None
    total_filled = total_missing = total_attempted = 0
    for p in paths:
        if budget is not None and budget <= 0:
            break
        if breaker.tripped():
            break
        # 只有存在「无 elements」的缺图条目时才需要 provider
        if provider is None:
            try:
                with open(p, encoding="utf-8") as f:
                    _d = json.load(f)
                _items = _d if isinstance(_d, list) else (_d.get("items") or [])
                if any(not _elements_of(it) for _i, it in find_missing(_items)):
                    provider = _build_provider()
            except Exception:
                pass
        st = {}
        f, m = process_file(p, provider=provider, budget=budget, breaker=breaker, stats=st)
        attempted = st.get("attempted", m if budget is None else min(m, budget))
        total_filled += f; total_missing += m; total_attempted += attempted
        if budget is not None:
            # ⚠️ 按尝试数扣配额,不是成功数:图 API 全挂时 f 恒为 0,配额永远不减,
            # 于是每个文件都会把 --max 重花一遍(80 个文件 → 数十倍图调用,把任务拖到超时)。
            budget -= attempted
    print(f"🖼️ backfill posters: filled {total_filled}/{total_attempted} attempted "
          f"(scanned missing {total_missing})" + ("  🛑 已熔断提前结束" if breaker.tripped() else ""))


if __name__ == "__main__":
    main()
