#!/usr/bin/env python3
"""死掉的 RSS 源必须可见。

feedparser 对 HTTP 403/404/500 和畸形 XML 都**不抛异常** —— 只在返回对象上置
bozo=1 / status，entries 为空。此前全仓库没有一处检查这两个字段，于是「源被封了」
和「今天没有新论文」打印出来一模一样。实测 config.RSS_FEEDS 的 65 个源里 17 个(26%)
在 data/index.json 中零产出：全部 5 个 ACS、3 个 aip.scitation(域名已停用)、
ChemRxiv、RSC Digital Discovery、Annual Reviews、Oxford Academic、4 个 feedburner。
"""

import types
from unittest import mock

import rss_fetcher
from rss_fetcher import RSSFetcher


def _feed(entries=(), status=200, bozo=0, exc=None):
    f = types.SimpleNamespace()
    f.entries = list(entries)
    f.status = status
    f.bozo = bozo
    f.bozo_exception = exc
    f.feed = {}
    return f


def _fetcher():
    return RSSFetcher(["machine"])


def test_http_error_is_recorded_not_silently_empty():
    f = _fetcher()
    with mock.patch.object(rss_fetcher.feedparser, "parse", return_value=_feed(status=403)):
        out = f.fetch_feed("https://pubs.acs.org/jctc.rss")
    assert out == []
    assert len(f.feed_health) == 1
    assert f.feed_health[0]["problem"] == "HTTP 403", f.feed_health[0]
    assert f.feed_health[0]["status"] == 403


def test_malformed_xml_with_no_entries_is_recorded():
    f = _fetcher()
    bad = _feed(status=200, bozo=1, exc=ValueError("not well-formed"))
    with mock.patch.object(rss_fetcher.feedparser, "parse", return_value=bad):
        f.fetch_feed("https://chemrxiv.org/feed")
    assert "解析失败" in (f.feed_health[0]["problem"] or ""), f.feed_health[0]


def test_healthy_but_genuinely_empty_feed_is_not_flagged_as_broken():
    """真的没有新论文 != 源坏了；不能误报。"""
    f = _fetcher()
    with mock.patch.object(rss_fetcher.feedparser, "parse", return_value=_feed(status=200)):
        f.fetch_feed("https://ok.example/feed")
    assert f.feed_health[0]["problem"] is None
    stats = f.report_feed_health()
    assert stats == {"ok": 0, "empty": 1, "broken": 0, "total": 1}


def test_bozo_with_entries_is_tolerated():
    """很多正常源也会置 bozo=1(字符集告警等)，只要有条目就不算故障。"""
    f = _fetcher()
    feed = _feed(entries=[{}], status=200, bozo=1, exc=ValueError("charset"))
    with mock.patch.object(rss_fetcher.feedparser, "parse", return_value=feed), \
         mock.patch.object(RSSFetcher, "_parse_entry", return_value=None), \
         mock.patch.object(RSSFetcher, "_get_journal_name", return_value="X"):
        f.fetch_feed("https://ok.example/feed")
    assert f.feed_health[0]["problem"] is None


def test_report_counts_ok_empty_and_broken():
    f = _fetcher()
    f.feed_health = [
        {"url": "a", "count": 3, "status": 200, "problem": None},
        {"url": "b", "count": 0, "status": 200, "problem": None},
        {"url": "c", "count": 0, "status": 403, "problem": "HTTP 403"},
    ]
    assert f.report_feed_health() == {"ok": 1, "empty": 1, "broken": 1, "total": 3}


def test_fetch_feed_works_without_fetch_all():
    """feed_health 必须在 __init__ 就绪，单独调用 fetch_feed 不能 AttributeError。"""
    f = _fetcher()
    assert f.feed_health == []
    with mock.patch.object(rss_fetcher.feedparser, "parse", return_value=_feed(status=500)):
        f.fetch_feed("https://x/feed")
    assert f.feed_health[0]["problem"] == "HTTP 500"


def test_timeout_is_applied_and_restored():
    """抓取期间设默认 socket 超时，结束后必须还原，不能污染其它模块。"""
    import socket
    seen = []
    before = socket.getdefaulttimeout()

    def fake_parse(url):
        seen.append(socket.getdefaulttimeout())
        return _feed(status=200)

    f = _fetcher()
    with mock.patch.object(rss_fetcher.feedparser, "parse", side_effect=fake_parse):
        f.fetch_feed("https://x/feed")
    assert seen and seen[0] == 30.0, seen
    assert socket.getdefaulttimeout() == before


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] rss feed health sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
