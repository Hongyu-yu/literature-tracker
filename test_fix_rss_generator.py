#!/usr/bin/env python3
"""rss_generator 回归测试：内容没变就不写盘 + 写盘失败不砸掉旧 feed。

run_tests.py 只会执行「模块顶层、无必填参数的 test_*」，所以这里全部写成
def test_xxx()，并且只用 unittest.mock + tempfile（不用 pytest 的 fixture）。
"""

import builtins
import datetime as _dt
import os
import re
import tempfile
from unittest import mock

import rss_generator
from rss_generator import generate_daily_rss_feed, generate_rss_feed


ARTICLES = [
    {
        'title_zh': '测试中文标题',
        'title': 'Test English Title',
        'link': 'https://example.com/paper',
        'journal': 'arXiv',
        'authors': ['Alice', 'Bob'],
        'summary': '一段用于 RSS 的中文摘要。',
        'pub_date': '2026-03-27',
    },
]

MORE_ARTICLES = ARTICLES + [
    {
        'title': 'Second Paper',
        'link': 'https://example.com/paper2',
        'journal': 'Nature Materials',
        'abstract': 'English abstract for RSS testing.',
        'pub_date': '2026-03-26',
    },
]


def _build_dates(xml_text):
    return re.findall(r'<lastBuildDate>(.*?)</lastBuildDate>', xml_text)


def _stamp(offset_seconds):
    """伪造一个「此刻」，让两次生成必然拿到不同的 lastBuildDate。"""
    return _dt.datetime(2026, 3, 27, 8, 0, 0) + _dt.timedelta(seconds=offset_seconds)


class _FrozenClock:
    """替换 rss_generator.datetime，只改 utcnow()，其余行为原样透传。"""

    def __init__(self, seconds):
        self._seconds = seconds

    def utcnow(self):
        return _stamp(self._seconds)

    def __getattr__(self, name):
        return getattr(_dt.datetime, name)


def test_unchanged_feed_is_not_rewritten():
    """条目一字未改时不该重写文件：修复前每次运行都会刷新 lastBuildDate 产生假 diff。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'docs', 'feed.xml')

        with mock.patch.object(rss_generator, 'datetime', _FrozenClock(0)):
            assert generate_rss_feed(ARTICLES, output_path=path) is True
        first = open(path, encoding='utf-8').read()
        first_mtime = os.stat(path).st_mtime_ns

        # 第二次生成：内容完全一样，只有时间戳会不同
        with mock.patch.object(rss_generator, 'datetime', _FrozenClock(3600)):
            result = generate_rss_feed(ARTICLES, output_path=path)

        second = open(path, encoding='utf-8').read()
        assert second == first, '内容未变却重写了文件（时间戳假 diff 又回来了）'
        assert os.stat(path).st_mtime_ns == first_mtime, '文件被无谓地重写了'
        assert result is False, '未写盘时应返回 False，否则调用方的“同步了 N 个日期”永远等于总数'
        # 保留的是第一次的时间戳，不是第二次的
        assert _build_dates(second) == ['Fri, 27 Mar 2026 08:00:00 GMT']


def test_changed_feed_is_rewritten():
    """内容真的变了必须照常写盘并返回 True（成功路径行为不能变）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'docs', 'feed.xml')

        with mock.patch.object(rss_generator, 'datetime', _FrozenClock(0)):
            assert generate_rss_feed(ARTICLES, output_path=path) is True
        with mock.patch.object(rss_generator, 'datetime', _FrozenClock(3600)):
            assert generate_rss_feed(MORE_ARTICLES, output_path=path) is True

        xml = open(path, encoding='utf-8').read()
        assert 'https://example.com/paper2' in xml, '新增条目没写进去'
        assert _build_dates(xml) == ['Fri, 27 Mar 2026 09:00:00 GMT'], '内容变了就该刷新时间戳'
        assert not os.path.exists(path + '.tmp'), '成功落盘后不该留下临时文件'


def test_daily_feed_unchanged_is_not_rewritten():
    """日报 feed 走的是同一条路径：sync_daily_rss_feeds 的计数要能反映真实改动。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'docs', 'daily', '2026-03-27.xml')

        with mock.patch.object(rss_generator, 'datetime', _FrozenClock(0)):
            assert generate_daily_rss_feed('2026-03-27', ARTICLES, path) is True
        first = open(path, encoding='utf-8').read()

        with mock.patch.object(rss_generator, 'datetime', _FrozenClock(86400)):
            assert generate_daily_rss_feed('2026-03-27', ARTICLES, path) is False
        assert open(path, encoding='utf-8').read() == first


def test_unreadable_existing_feed_still_writes():
    """读不出旧文件时必须照常写（省写优化绝不能吞掉一次真正的更新）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'docs', 'feed.xml')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 非法 UTF-8：读旧文件会抛 UnicodeDecodeError
        with open(path, 'wb') as f:
            f.write(b'\xff\xfe\x00broken')

        assert generate_rss_feed(ARTICLES, output_path=path) is True
        xml = open(path, encoding='utf-8').read()
        assert 'https://example.com/paper' in xml


def test_write_failure_keeps_previous_feed_intact():
    """写盘写到一半失败时，磁盘上那份好 feed 必须一个字节都不变。

    修复前 open(filepath,'w') 会先把正式文件截成 0 字节，写失败就留下空 XML 并
    随 `git add -A` 推上线；修复后先写 *.tmp 再 os.replace，正式文件毫发无伤。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'docs', 'feed.xml')
        with mock.patch.object(rss_generator, 'datetime', _FrozenClock(0)):
            assert generate_rss_feed(ARTICLES, output_path=path) is True
        good = open(path, encoding='utf-8').read()
        assert good.strip()

        real_open = builtins.open

        class _BoomFile:
            """open() 照常执行（含截断），write() 才炸 —— 模拟写到一半失败。"""

            def __init__(self, fh):
                self._fh = fh

            def write(self, *_a, **_kw):
                raise OSError('模拟磁盘写入失败')

            def __getattr__(self, name):
                return getattr(self._fh, name)

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                self._fh.close()
                return False

        def flaky_open(file, mode='r', *args, **kwargs):
            fh = real_open(file, mode, *args, **kwargs)
            if 'w' in mode or 'a' in mode:
                return _BoomFile(fh)
            return fh

        with mock.patch.object(builtins, 'open', flaky_open):
            with mock.patch.object(rss_generator, 'datetime', _FrozenClock(3600)):
                result = generate_rss_feed(MORE_ARTICLES, output_path=path)

        assert result is False, '写盘失败应返回 False'
        assert open(path, encoding='utf-8').read() == good, '写盘失败把已有的好 feed 砸了'
        assert not os.path.exists(path + '.tmp'), '失败后留下了 .tmp 半成品'


def test_feed_body_ignores_only_the_build_date():
    """_feed_body 只能剥掉 lastBuildDate，条目内容一律保留。"""
    xml = ('<channel><lastBuildDate>Fri, 27 Mar 2026 08:00:00 GMT</lastBuildDate>'
           '<item><title>x</title></item></channel>')
    body = rss_generator._feed_body(xml)
    assert 'lastBuildDate' not in body
    assert '<item><title>x</title></item>' in body
    assert rss_generator._feed_body('') == ''


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'✓ {name}')
    print('[OK] rss_generator 回归测试通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
