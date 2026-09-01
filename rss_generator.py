#!/usr/bin/env python3
"""RSS feed generation helpers for the literature tracker site."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from xml.sax.saxutils import escape

from author_utils import authors_label
from text_normalizer import normalize_text

SITE_URL = (os.environ.get('LITERATURE_TRACKER_SITE_URL') or 'https://hongyu-yu.github.io/literature-tracker').rstrip('/')
SITE_TITLE = '文献追踪系统'
SITE_DESCRIPTION = '自动追踪 AI × 物理 / 化学 / 材料交叉领域最新文献'


def _format_rfc822(pub_date: str) -> str:
    text = str(pub_date or '').strip()
    if not text:
        return ''
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime('%a, %d %b %Y 00:00:00 GMT')
        except Exception:
            continue
    return ''


def _trim_text(value: str, limit: int) -> str:
    compact = ' '.join(normalize_text(value).split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + '…'


def _authors_label(article: Dict) -> str:
    return authors_label(article.get('authors'), max_names=6)


def _article_title(article: Dict) -> str:
    return normalize_text(article.get('title_zh') or article.get('title') or '无标题').strip() or '无标题'


def _article_description(article: Dict, *, max_chars: int = 800) -> str:
    journal = normalize_text(article.get('journal') or '').strip()
    authors = _authors_label(article)
    summary = normalize_text(article.get('summary') or article.get('one_sentence_summary') or '').strip()
    abstract = normalize_text(article.get('abstract_zh') or article.get('abstract') or '').strip()

    parts = []
    if journal:
        parts.append(f'📚 {journal}')
    if authors:
        parts.append(f'👤 {authors}')
    body = summary or abstract
    if body:
        parts.append(_trim_text(body, max_chars))
    return '\n\n'.join(parts)


_BUILD_DATE_RE = re.compile(r'<lastBuildDate>.*?</lastBuildDate>', re.S)


def _feed_body(xml_text: str) -> str:
    """剥掉 <lastBuildDate> 之后的 feed 正文，用来判断内容是不是真的变了。"""
    return _BUILD_DATE_RE.sub('', xml_text or '')


def _feed_content_unchanged(filepath: str, xml_content: str) -> bool:
    """磁盘上已有的 feed 除时间戳外是否与新内容逐字相同。

    读不到（不存在/权限/编码坏了）一律当作「变了」，照常写盘 —— 这里只做省写优化，
    绝不能因为读旧文件失败就漏掉一次真正的更新。
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            existing = f.read()
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"⚠️ 无法读取既有RSS feed({filepath}): {e}，按内容已变化处理")
        return False
    return _feed_body(existing) == _feed_body(xml_content)


def _output_url(site_url: str, output_path: str) -> str:
    path = Path(output_path)
    parts = list(path.parts)
    if 'docs' in parts:
        parts = parts[parts.index('docs') + 1:]
    relative = '/'.join(parts)
    return f"{site_url}/{relative}" if relative else site_url


class RSSGenerator:
    """RSS 2.0 feed generator."""

    def __init__(
        self,
        site_url: str = SITE_URL,
        title: str = SITE_TITLE,
        description: str = SITE_DESCRIPTION,
        *,
        self_url: Optional[str] = None,
        channel_link: Optional[str] = None,
    ):
        self.site_url = str(site_url or SITE_URL).rstrip('/')
        self.title = title
        self.description = description
        self.self_url = self_url
        self.channel_link = channel_link or self.site_url

    def generate_feed(self, articles: List[Dict], max_items: int = 100) -> str:
        sorted_articles = sorted(
            [item for item in (articles or []) if isinstance(item, dict)],
            key=lambda x: str(x.get('pub_date') or ''),
            reverse=True,
        )[:max_items]

        items_xml = '\n'.join(self._create_item(article) for article in sorted_articles)
        now = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
        atom_href = escape(self.self_url or f'{self.site_url}/feed.xml')
        channel_link = escape(self.channel_link or self.site_url)

        return f'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
    <title>{escape(self.title)}</title>
    <link>{channel_link}</link>
    <description>{escape(self.description)}</description>
    <language>zh-CN</language>
    <lastBuildDate>{now}</lastBuildDate>
    <atom:link href="{atom_href}" rel="self" type="application/rss+xml"/>
{items_xml}
</channel>
</rss>'''

    def _create_item(self, article: Dict) -> str:
        title = _article_title(article)
        link = str(article.get('link') or '').strip()
        description = _article_description(article)
        pub_date_rfc = _format_rfc822(str(article.get('pub_date') or article.get('date') or '').strip())
        guid = link or f"{title}-{article.get('pub_date') or article.get('date') or ''}"

        return f'''    <item>
        <title>{escape(title)}</title>
        <link>{escape(link)}</link>
        <description><![CDATA[{description}]]></description>
        <pubDate>{pub_date_rfc}</pubDate>
        <guid isPermaLink="{'true' if link else 'false'}">{escape(guid)}</guid>
    </item>'''

    def save_feed(self, articles: List[Dict], filepath: str, max_items: int = 100) -> bool:
        """写出 feed，返回「文件是否真的变了」。

        <lastBuildDate> 每次都取「此刻」，所以无条件写盘的话，即便条目一个字都没改，
        120 个日报 feed + docs/feed.xml 每次运行都会产生一行时间戳 diff：
        git 历史被噪音撑大(docs/feed.xml 已有 408 个版本)、各 workflow 的
        `git diff --staged --quiet` 永远不会命中「nothing to commit」、
        deploy-on-push 于是每次都重新部署整个 docs/，
        而 sync_daily_rss_feeds 的「本次同步了 N 个日期」也永远等于总数、毫无意义。
        按 RSS 2.0 规范 lastBuildDate 本就表示「频道内容最后一次变化的时间」，
        内容没变时保留旧时间戳反而更贴合语义。

        内容没变 → 跳过写入返回 False；生成/写盘失败 → 同样返回 False(与之前一致)。
        现有调用方要么忽略返回值(main / run_optimized_sync / backfill_zh)，
        要么只用它累加计数(generate_daily_pages.sync_daily_rss_feeds)，都不受影响。
        """
        try:
            xml_content = self.generate_feed(articles, max_items)
            if _feed_content_unchanged(filepath, xml_content):
                print(f"⏭️ RSS内容未变化，跳过写入: {filepath}")
                return False
            directory = os.path.dirname(filepath)
            if directory:
                os.makedirs(directory, exist_ok=True)
            # 原子落盘(同 backfill_zh.save_index / run_optimized_sync._atomic_write_json)：
            # 直接 open(filepath,'w') 会先把线上那份好 feed 截成 0 字节再慢慢写，
            # 写到一半遇到磁盘满 / 文本里混进孤立代理字符(UnicodeEncodeError)，
            # 留在 docs/ 里的就是一份被截断的 XML，随后 `git add -A` 直接推上线。
            # *.tmp 已在 .gitignore 里，被 kill 留下的残件不会被 git 带走。
            tmp_path = f'{filepath}.tmp'
            try:
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    f.write(xml_content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, filepath)
            except Exception:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise
            print(f"✅ RSS feed已保存: {filepath}")
            return True
        except Exception as e:
            print(f"❌ RSS生成失败: {e}")
            return False


def generate_rss_feed(
    articles: List[Dict],
    output_path: str = 'docs/feed.xml',
    *,
    site_url: str = SITE_URL,
    title: str = SITE_TITLE,
    description: str = SITE_DESCRIPTION,
    channel_link: Optional[str] = None,
    max_items: int = 100,
):
    generator = RSSGenerator(
        site_url=site_url,
        title=title,
        description=description,
        self_url=_output_url(site_url, output_path),
        channel_link=channel_link,
    )
    return generator.save_feed(articles, output_path, max_items=max_items)


def generate_daily_rss_feed(
    date_str: str,
    articles: List[Dict],
    output_path: Optional[str] = None,
    *,
    site_url: str = SITE_URL,
) -> bool:
    output_path = output_path or f'docs/daily/{date_str}.xml'
    title = f'{date_str} AI × Science 文献日报 RSS'
    description = f'{date_str} 当天筛选出的 AI × 物理 / 化学 / 材料交叉文献 RSS。'
    channel_link = f'{site_url}/daily/{date_str}.html'
    return generate_rss_feed(
        articles,
        output_path=output_path,
        site_url=site_url,
        title=title,
        description=description,
        channel_link=channel_link,
        max_items=max(1, len(articles or [])),
    )


if __name__ == '__main__':
    import json

    try:
        with open('data/index.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        articles = data.get('articles', [])
        generate_rss_feed(articles)
    except FileNotFoundError:
        print('未找到数据文件，请先运行抓取脚本')
