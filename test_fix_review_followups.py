#!/usr/bin/env python3
"""对抗式评审在 pass-2 里抓到的三个问题的回归测试。

1. run_optimized_sync 把永久黑名单 deep_history.json 写在两处提前 return **之前**，
   于是索引写失败时这批文献「被拉黑却没入索引」，此后再也不会被重新评分。
2. config.AI_CONFIG 用 os.environ.get(key, default)：GitHub 对**已声明但为空**的 secret
   注入空字符串，key 存在，默认值永不生效，provider 变成 "" → build_provider 走到
   完全不同的客户端上。
3. weekly_summary 的 ai_relevant.json 降级回退会用更薄的页面覆盖掉已有的好页面。
"""

import importlib
import json
import os
import shutil
import tempfile
from unittest import mock


# ---------------------------------------------------------------- 1. 黑名单顺序

def test_blacklist_is_written_only_after_index_write():
    """deep_history.json 的写入必须排在 data/index.json 之后。

    这两个文件的先后顺序就是「文献会不会被永久丢弃」的分界线，所以直接对源码顺序
    下断言 —— 端到端跑一次 run_optimized_sync 需要联网抓 RSS，不适合放进单测。
    """
    src = open("run_optimized_sync.py", encoding="utf-8").read()
    idx_write = src.index('_atomic_write_json(full_data_path')
    blacklist_write = src.index('_atomic_write_json(processed_file, processed_pending)')
    assert blacklist_write > idx_write, (
        "deep_history.json（永久黑名单）必须在 index.json 落盘成功之后再写，"
        "否则提前 return 时文献会被拉黑却没进索引")


def test_blacklist_write_is_deferred_not_immediate():
    """相关性分析那一段只能登记 processed_pending，不能就地写盘。"""
    src = open("run_optimized_sync.py", encoding="utf-8").read()
    head = src[: src.index('_atomic_write_json(full_data_path')]
    assert 'processed_pending = sorted(processed_ids)' in head
    assert '_atomic_write_json(processed_file, sorted(processed_ids))' not in head, \
        "分析阶段不得直接写黑名单"


# ---------------------------------------------------------- 2. 空 secret 兜底

def _reload_config(env):
    keep = {k: v for k, v in os.environ.items()
            if k not in ("AI_PROVIDER", "AI_MODEL", "AI_BASE_URL", "OPENROUTER_BASE_URL")}
    keep.update(env)
    with mock.patch.dict(os.environ, keep, clear=True):
        import config
        return importlib.reload(config)


def test_empty_ai_secrets_fall_back_to_defaults():
    """已声明但为空的 secret 会被注入空字符串，此时必须回落到默认值。"""
    try:
        cfg = _reload_config({"AI_PROVIDER": "", "AI_MODEL": "", "AI_BASE_URL": ""})
        assert cfg.AI_CONFIG["provider"] == "aigw", cfg.AI_CONFIG["provider"]
        assert cfg.AI_CONFIG["model"] == "gpt-5.5", cfg.AI_CONFIG["model"]
        assert cfg.AI_CONFIG["base_url"].startswith("http"), cfg.AI_CONFIG["base_url"]
    finally:
        import config
        importlib.reload(config)


def test_real_ai_secret_values_still_win():
    try:
        cfg = _reload_config({"AI_PROVIDER": "kimi", "AI_MODEL": "kimi-k2"})
        assert cfg.AI_CONFIG["provider"] == "kimi"
        assert cfg.AI_CONFIG["model"] == "kimi-k2"
    finally:
        import config
        importlib.reload(config)


# ------------------------------------------- 3. 降级回退不覆盖已有周报页面

def test_fallback_regeneration_does_not_overwrite_existing_weekly_page():
    """回退到 ai_relevant.json 时，若该周页面已存在则必须原样保留。

    ai_relevant.json 是更小的池子（只收通过 AI4S 与相关性闸门的条目，且写在 zh/focus
    富化之前），拿它重生成会得到更薄、且 🎯 区块为空的页面；而 backfill-weekly.yml
    默认区间覆盖 15 个已存在的周页并会强推 main。
    """
    import weekly_summary as w
    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        os.makedirs("docs/weekly", exist_ok=True)
        os.makedirs("data", exist_ok=True)
        page = os.path.join("docs/weekly", "2026-03-16.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write("<html>完整的历史周报，含 🎯 与你方向相关</html>")
        # index.json 该窗口内 0 篇；ai_relevant.json 有 → 触发降级回退
        with open("data/index.json", "w", encoding="utf-8") as f:
            json.dump({"articles": []}, f)
        with open("data/ai_relevant.json", "w", encoding="utf-8") as f:
            json.dump([{"title": "Ferroelectric ML", "abstract": "machine learning",
                        "journal": "Nature", "pub_date": "2026-03-17",
                        "link": "https://x/1"}], f, ensure_ascii=False)
        called = []
        with mock.patch.object(w.WeeklySummarizer, "save_summary_html",
                               side_effect=lambda *a, **k: called.append(1)):
            out = w.generate_weekly_summary("2026-03-16", api_key="")
        after = open(page, encoding="utf-8").read()
        assert not called, "已有页面时不得调用 save_summary_html"
        assert out is None
        assert "完整的历史周报" in after, "已有的好页面被降级内容覆盖了"
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] pass-2 review follow-up checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
