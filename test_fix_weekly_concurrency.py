#!/usr/bin/env python3
"""周报的 AI 相关性判断必须并发执行，且行为与串行版完全一致。

背景：filter_articles 原来在 for 循环里逐篇同步调 AI，实测单周 1315 次串行往返 ≈ 110 分钟
纯等待，是 weekly workflow 逼近 240 分钟 timeout（六次里被取消过一次）的主因。
改成 ThreadPoolExecutor(max_workers=3) 后，判定结果、保留顺序和失败降级语义都不能变。
"""

import sys
import threading
import time
import types

# weekly_summary 依赖 bs4/deep_translator，测试环境未必装；按既有测试的做法打桩
for name, attrs in (("bs4", {"BeautifulSoup": object, "Comment": object,
                             "NavigableString": object, "Tag": object}),
                    ("deep_translator", {"GoogleTranslator": object})):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            m = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[name] = m

from weekly_summary import WeeklySummarizer


LONG = "machine learning neural network potential for ferroelectric perovskite materials " * 4


def _articles(n):
    return [{"id": f"a{i}", "title": f"Paper {i}", "abstract": LONG,
             "journal": "Nature", "pub_date": "2026-08-25"} for i in range(n)]


def _summarizer(call):
    s = WeeklySummarizer.__new__(WeeklySummarizer)
    s.provider = types.SimpleNamespace(call_api=call)
    s._judge_failures = 0
    s._judge_lock = threading.Lock()
    return s


def test_judge_calls_run_concurrently_not_serially():
    """3 个并发 worker：12 篇 × 0.15s 串行要 1.8s，并发应显著更快。"""
    inflight, peak, lock = [0], [0], threading.Lock()

    def call(prompt):
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        time.sleep(0.15)
        with lock:
            inflight[0] -= 1
        return "是"

    s = _summarizer(call)
    t0 = time.monotonic()
    out = s.filter_articles(_articles(12), "2026-08-24", "2026-08-30", "ai")
    elapsed = time.monotonic() - t0
    assert len(out) == 12
    assert peak[0] > 1, f"判断仍是串行的（并发峰值 {peak[0]}）"
    assert elapsed < 1.5, f"耗时 {elapsed:.2f}s，看起来仍在串行（串行需 ~1.8s）"


def test_result_order_matches_input_order():
    """并发只改变执行顺序，保留下来的文章顺序必须与输入一致。"""
    s = _summarizer(lambda p: "是")
    arts = _articles(8)
    out = s.filter_articles(arts, "2026-08-24", "2026-08-30", "ai")
    assert [a["id"] for a in out] == [a["id"] for a in arts]


def test_rejected_articles_are_dropped():
    """AI 判否的文章要被剔除（并发不能让判定失效）。"""
    def call(prompt):
        return "否" if "Paper 3" in prompt else "是"
    s = _summarizer(call)
    out = s.filter_articles(_articles(6), "2026-08-24", "2026-08-30", "ai")
    ids = [a["id"] for a in out]
    assert "a3" not in ids and len(ids) == 5, ids


def test_judge_exception_keeps_article_and_counts_failure():
    """判断抛异常时保留文献并计数——绝不能把「调用失败」当成「判为否」。"""
    def call(prompt):
        raise RuntimeError("gateway 502")
    s = _summarizer(call)
    out = s.filter_articles(_articles(5), "2026-08-24", "2026-08-30", "ai")
    assert len(out) == 5, "网关故障时不得静默清空当周文献"
    assert s._judge_failures == 5, f"失败次数应被计满，实得 {s._judge_failures}"


def test_failure_counter_is_thread_safe():
    """并发下计数不能丢失（+= 非原子，必须加锁）。"""
    def call(prompt):
        raise RuntimeError("boom")
    s = _summarizer(call)
    s.filter_articles(_articles(30), "2026-08-24", "2026-08-30", "ai")
    assert s._judge_failures == 30, f"计数丢失: {s._judge_failures}/30"


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] weekly AI-judge concurrency sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
