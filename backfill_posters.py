"""补全缺图海报：扫 arxiv_core/aps JSON，对「有 deep_analysis 但 image 为空」的条目补生成海报图。

关键省成本点：这些条目通常已存好 `poster.elements`(中文 5 要素),直接复用它生图即可,
**不重跑昂贵的深读/要素抽取**,每篇仅 1 次图 API。仅当没有现成 elements 时才回退整套
generate_poster(需 provider)。幂等：已有 image 的跳过;可 --max 限量,配额用完即停。

用法：python backfill_posters.py [--glob data/arxiv_core_*.json] [--max 50]
"""
import os, glob as globmod, json, argparse, hashlib
from poster_generator import build_infographic_prompt, generate_poster
from image_provider import generate_and_save

POSTER_DIR = "docs/images/posters"


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


def process_file(path, provider=None, budget=None, out_dir=POSTER_DIR):
    """补一个 JSON 文件里的缺图条目;返回 (filled, missing_total)。保持原写盘格式(无缩进)。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data if isinstance(data, list) else (data.get("items") or data.get("papers") or [])
    missing = find_missing(items)
    filled = 0
    for _i, it in missing:
        if budget is not None and filled >= budget:
            break
        img = backfill_item(it, provider=provider, out_dir=out_dir)
        print(("  ✅" if img else "  ⚠️"), os.path.basename(path),
              (it.get("title") or "")[:48], "->", img)
        if img:
            filled += 1
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="data/arxiv_core_*.json", help="待扫描 JSON 通配")
    ap.add_argument("--max", type=int,
                    default=int(os.environ.get("POSTER_BACKFILL_MAX", "50")),
                    help="本次最多补几张(控成本);<=0 表示不限")
    args = ap.parse_args()
    paths = sorted(globmod.glob(args.glob), reverse=True)  # 新日期优先
    if not paths:
        print(f"没有匹配文件: {args.glob}"); return
    provider = None  # 懒构建：仅在遇到无 elements 的条目时才需要
    budget = args.max if args.max and args.max > 0 else None
    total_filled = total_missing = 0
    for p in paths:
        if budget is not None and budget <= 0:
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
        f, m = process_file(p, provider=provider, budget=budget)
        total_filled += f; total_missing += m
        if budget is not None:
            budget -= f
    print(f"🖼️ backfill posters: filled {total_filled} (scanned missing {total_missing})")


if __name__ == "__main__":
    main()
