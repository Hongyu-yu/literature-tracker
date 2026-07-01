import json, os, tempfile
from unittest import mock
import backfill_posters
from backfill_posters import find_missing, backfill_item, process_file

def _item(**kw):
    base = {"title": "T", "deep_analysis": "…含创新…" * 500}
    base.update(kw); return base

def _with_elements(doc_id="axABC123", image=None):
    return _item(poster={"elements": {"研究问题": "Q", "创新方法": "M"},
                         "doc_id": doc_id, "image": image}, image=image)


def test_find_missing_selects_deep_but_imageless():
    items = [
        _with_elements(image=None),                                   # 缺图 → 选中
        _with_elements(doc_id="axHAS", image="images/posters/axHAS.webp"),  # 有图 → 跳过
        _item(deep_analysis="", poster={"elements": {}, "doc_id": "axX"}),   # 无深读 → 跳过
        {"not": "a real item with deep"},                            # 无深读 → 跳过
    ]
    miss = find_missing(items)
    assert len(miss) == 1
    assert miss[0][0] == 0


def test_backfill_item_reuses_elements_no_provider_needed():
    it = _with_elements(doc_id="axDEF456")
    called = {}
    def fake_gen(prompt, out_path, **k):
        called["prompt"] = prompt; called["out_path"] = out_path
        return out_path
    with mock.patch.object(backfill_posters, "generate_and_save", side_effect=fake_gen), \
         mock.patch.object(backfill_posters, "generate_poster",
                           side_effect=AssertionError("不应重跑 generate_poster")):
        img = backfill_item(it, provider=None)
    assert img == "images/posters/axDEF456.webp"          # docs/ 前缀被去掉
    assert called["out_path"] == os.path.join("docs/images/posters", "axDEF456.webp")
    assert "M" in called["prompt"]                          # 复用了中文要素(创新方法=M)


def test_backfill_item_sets_image_on_item_and_poster():
    it = _with_elements(doc_id="axG")
    with mock.patch.object(backfill_posters, "generate_and_save",
                           side_effect=lambda prompt, out_path, **k: out_path):
        backfill_item(it, provider=None)
    assert it["image"] == "images/posters/axG.webp"
    assert it["poster"]["image"] == "images/posters/axG.webp"


def test_backfill_item_none_on_image_failure():
    it = _with_elements(doc_id="axH")
    with mock.patch.object(backfill_posters, "generate_and_save",
                           side_effect=lambda prompt, out_path, **k: None):
        img = backfill_item(it, provider=None)
    assert img is None
    assert it.get("image") in (None, "")                   # 失败不写脏值
    assert it["poster"]["image"] is None


def test_backfill_item_skips_when_no_elements_and_no_provider():
    it = _item(poster={"doc_id": "axNOEL"}, deep_analysis="创新" * 100)  # 无 elements
    with mock.patch.object(backfill_posters, "generate_and_save",
                           side_effect=AssertionError("无要素且无 provider 时不应生图")):
        img = backfill_item(it, provider=None)
    assert img is None


def test_backfill_item_fallback_to_generate_poster_with_provider():
    it = _item(link="https://arxiv.org/abs/1", deep_analysis="创新" * 100)  # 无 poster/elements
    fake_res = {"elements": {"研究问题": "q"}, "title_zh": "标题",
                "image": "images/posters/axFB.webp"}
    with mock.patch.object(backfill_posters, "generate_poster", return_value=fake_res), \
         mock.patch.object(backfill_posters, "generate_and_save",
                           side_effect=AssertionError("回退路径应走 generate_poster")):
        img = backfill_item(it, provider=object())
    assert img == "images/posters/axFB.webp"
    assert it["image"] == "images/posters/axFB.webp"
    assert it["poster_elements"] == {"研究问题": "q"}
    assert it["title_zh"] == "标题"


def test_process_file_writes_compact_and_counts():
    items = [_with_elements(doc_id="axP1"), _with_elements(doc_id="axP2"),
             _with_elements(doc_id="axHASIMG", image="images/posters/axHASIMG.webp")]
    d = tempfile.mkdtemp(); path = os.path.join(d, "arxiv_core_2026-06-30.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    with mock.patch.object(backfill_posters, "generate_and_save",
                           side_effect=lambda prompt, out_path, **k: out_path):
        filled, missing = process_file(path, provider=None)
    assert (filled, missing) == (2, 2)
    raw = open(path, encoding="utf-8").read()
    assert "\n" not in raw                                   # 无缩进/单行,匹配 run_deep 写盘
    reloaded = json.loads(raw)
    assert reloaded[0]["image"] == "images/posters/axP1.webp"
    assert reloaded[2]["image"] == "images/posters/axHASIMG.webp"  # 原有图不动


def test_process_file_respects_budget():
    items = [_with_elements(doc_id=f"axB{i}") for i in range(5)]
    d = tempfile.mkdtemp(); path = os.path.join(d, "arxiv_core_2026-06-29.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    with mock.patch.object(backfill_posters, "generate_and_save",
                           side_effect=lambda prompt, out_path, **k: out_path):
        filled, missing = process_file(path, provider=None, budget=2)
    assert filled == 2 and missing == 5                      # 配额封顶


def test_process_file_parallel_fills_all_correctly():
    import threading
    items = [_with_elements(doc_id=f"axC{i}") for i in range(10)]
    d = tempfile.mkdtemp(); path = os.path.join(d, "arxiv_core_2026-06-28.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    seen = set(); lock = threading.Lock(); peak = {"n": 0, "cur": 0}
    def gen(prompt, out_path, **k):
        with lock:
            peak["cur"] += 1; peak["n"] = max(peak["n"], peak["cur"])
            seen.add(out_path)
        try:
            return out_path
        finally:
            with lock: peak["cur"] -= 1
    with mock.patch.object(backfill_posters, "generate_and_save", side_effect=gen):
        filled, missing = process_file(path, provider=None, max_workers=6)
    assert (filled, missing) == (10, 10)                     # 并发下全部补齐、无丢失/重复
    assert len(seen) == 10                                   # 10 个不同 doc_id 各生一次
    reloaded = json.loads(open(path, encoding="utf-8").read())
    assert all(it["image"] == f"images/posters/axC{i}.webp" for i, it in enumerate(reloaded))
