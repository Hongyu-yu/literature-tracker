#!/usr/bin/env python3
"""
周报生成器 - 每周总结Nature/Science系列的磁性/铁电工作
"""

import os
import sys
import json
import requests
import html
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
from ai_summarizer import build_provider
from author_utils import authors_label as format_authors_label
from abstract_scraper import AbstractScraper
from text_normalizer import normalize_articles_inplace, normalize_text
from translator import translate_text
from concurrent.futures import ThreadPoolExecutor, as_completed
from weekly_page_enhancer import enhance_weekly_archive
from weekly_prompts import _build_analyze_prompt
import time
import threading

try:
    from config import AI_CONFIG as DEFAULT_AI_CONFIG
except ImportError:
    DEFAULT_AI_CONFIG = {}


# 全站的 pub_date 都是北京日历日（rss_fetcher 按 UTC+8 落库），而 GitHub runner 的
# 本地时间是 UTC。周报页脚、index.json 时间戳、默认周窗口统一用北京时间，否则
# 页面上的「更新时间」会比正文里的日期整整早 8 小时（看起来像跑失败了），
# 跨零点时甚至和自己的 week_end 差一天。
BEIJING_TZ = timezone(timedelta(hours=8))


class WeekOutsideDataWindow(RuntimeError):
    """请求的这一周完全不在留存数据窗口内。

    和「本周没有符合条件的文献」是两回事：前者再跑多少次也不可能生成，
    后者只是这周确实没有值得收录的论文。
    """


def _safe_text(value) -> str:
    return html.escape(normalize_text(value or ""), quote=True)


def _safe_multiline(value) -> str:
    return _safe_text(value).replace("\n", "<br/>")


def _safe_url(value) -> str:
    url = str(value or "").strip()
    if not url:
        return "#"
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return "#"
    except Exception:
        return "#"
    return html.escape(url, quote=True)


def _safe_id(value) -> str:
    raw = str(value or "")
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "_" for c in raw)
    return cleaned or "x"


def render_core_weekly_section(summary: Dict) -> str:
    import html as _html
    def _t(s: str) -> str:
        return _html.escape(str(s or ""), quote=True)
    def _u(url: str) -> str:
        url = (url or "").strip()
        if not url:
            return "#"
        if not (url.startswith("http://") or url.startswith("https://")):
            return "#"
        return _html.escape(url, quote=True)

    items = summary.get('core_items') or []
    if not items:
        return ""
    note = summary.get('core_weekly_note') or ""
    cards = []
    for i, it in enumerate(items, 1):
        title = (it.get('title') or '').strip()
        title_en = (it.get('title_en') or '').strip()
        journal = (it.get('journal') or '').strip()
        link = (it.get('link') or '').strip() or '#'
        abstract_zh = (it.get('abstract_zh_full') or it.get('abstract_zh') or '').strip()
        mp = (it.get('method_point') or '').strip()
        rw = (it.get('related_work') or '').strip()
        im = (it.get('implication') or '').strip()
        show_en = bool(title) and title.casefold() != title_en.casefold()
        deep = ""
        if mp or rw or im:
            parts = []
            if mp: parts.append(f"<p><strong>📐 方法要点：</strong>{_t(mp)}</p>")
            if rw: parts.append(f"<p><strong>🔗 相关工作关联：</strong>{_t(rw)}</p>")
            if im: parts.append(f"<p><strong>💡 对你方向的启示：</strong>{_t(im)}</p>")
            deep = f"<div class='weekly-core-deep'>{''.join(parts)}</div>"
        title_en_html = f"<div class='weekly-core-title-en'>{_t(title_en)}</div>" if show_en else ""
        abs_block = f"<p class='weekly-core-abs'><strong>📄 摘要：</strong>{_t(abstract_zh)}</p>" if abstract_zh else ""
        display_title = _t(title or title_en)
        cards.append(f"""
        <li class="weekly-core-card" data-bookmark-key="{_t(link)}">
          <div class="weekly-core-number">{i:02d}</div>
          <div class="weekly-core-body">
            <div class="weekly-core-title-zh">{display_title}</div>
            {title_en_html}
            <div class="weekly-core-meta"><span class="weekly-chip weekly-chip-core">🎯 核心</span><span class="weekly-chip">📖 {_t(journal)}</span></div>
            {abs_block}
            {deep}
            <div class="weekly-core-actions"><a href="{_u(link)}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a></div>
          </div>
        </li>
        """)
    note_html = f"<p class='weekly-core-note'>{_t(note)}</p>" if note else ""
    return f"""
    <section id="core-focus" class="weekly-section weekly-core-section">
      <div class="weekly-section-head"><span class="weekly-section-index">🎯</span><h2 class="weekly-section-title">本周核心方向（ML × ferro / 凝聚态）</h2><span class="weekly-core-count">{len(items)} 篇</span></div>
      {note_html}
      <ol class="weekly-core-list">{''.join(cards)}</ol>
    </section>
    """


def render_focus_weekly_section(articles: List[Dict]) -> str:
    """「🎯 与你方向相关」周报区块：汇总当周 focus_score 命中的文章，按分数降序，
    卡片含 简单总结 / 与我们工作的关系 / 进一步工作建议 三行（空字段跳过）。
    无匹配文章（旧数据无 focus 字段）时返回空串，区块整体隐藏。"""
    import html as _html
    def _t(s: str) -> str:
        return _html.escape(str(s or ""), quote=True)
    def _u(url: str) -> str:
        url = (url or "").strip()
        if not url:
            return "#"
        if not (url.startswith("http://") or url.startswith("https://")):
            return "#"
        return _html.escape(url, quote=True)

    items = [a for a in (articles or []) if isinstance(a, dict) and a.get('focus_score')]
    if not items:
        return ""

    def _score(a) -> float:
        try:
            return float(a.get('focus_score') or 0)
        except (TypeError, ValueError):
            return 0.0

    items = sorted(items, key=_score, reverse=True)
    cards = []
    for i, it in enumerate(items, 1):
        title = (it.get('title_zh') or it.get('title') or it.get('title_en') or '').strip()
        journal = (it.get('journal') or '').strip()
        pub_date = (it.get('pub_date') or it.get('date') or '').strip()
        link = (it.get('link') or '').strip() or '#'
        fs = (it.get('focus_summary') or '').strip()
        fr = (it.get('focus_relation') or '').strip()
        fg = (it.get('focus_suggestion') or '').strip()
        deep = ""
        parts = []
        if fs: parts.append(f"<p><strong>📝 简单总结：</strong>{_t(fs)}</p>")
        if fr: parts.append(f"<p><strong>🔗 与我们工作的关系：</strong>{_t(fr)}</p>")
        if fg: parts.append(f"<p><strong>💡 进一步工作建议：</strong>{_t(fg)}</p>")
        if parts:
            deep = f"<div class='weekly-focus-deep'>{''.join(parts)}</div>"
        meta_parts = []
        if journal:
            meta_parts.append(f"<span class='weekly-chip'>📖 {_t(journal)}</span>")
        if pub_date:
            meta_parts.append(f"<span class='weekly-chip'>📅 {_t(pub_date)}</span>")
        meta_parts.append(f"<span class='weekly-chip weekly-chip-focus'>🎯 相关度 {_t(it.get('focus_score'))}</span>")
        cards.append(f"""
        <li class="weekly-focus-card" data-bookmark-key="{_t(link)}">
          <div class="weekly-focus-number">{i:02d}</div>
          <div class="weekly-focus-body">
            <div class="weekly-focus-title"><a class="weekly-focus-link" href="{_u(link)}" target="_blank" rel="noopener noreferrer">{_t(title)}</a></div>
            <div class="weekly-focus-meta">{''.join(meta_parts)}</div>
            {deep}
            <div class="weekly-core-actions"><a href="{_u(link)}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a></div>
          </div>
        </li>
        """)
    return f"""
    <section id="focus-interest" class="weekly-section weekly-focus-section">
      <div class="weekly-section-head"><span class="weekly-section-index">🎯</span><h2 class="weekly-section-title">与你方向相关</h2><span class="weekly-focus-count">{len(items)} 篇</span></div>
      <ol class="weekly-focus-list">{''.join(cards)}</ol>
    </section>
    """


class WeeklySummarizer:
    """周报生成器"""
    
    # 顶刊列表 - 精确匹配，避免误匹配
    TOP_JOURNALS = [
        # Nature 系列
        'Nature',
        'Nature Materials',
        'Nature Physics',
        'Nature Chemistry',
        'Nature Communications',
        'Nature Nanotechnology',
        'Nature Electronics',
        'Nature Energy',
        'Nat. Mater.',
        'Nat. Phys.',
        'Nat. Chem.',
        'Nat. Commun.',
        'Nat. Nanotechnol.',
        'Nat. Electron.',
        'Nat. Energy',
        # Science 系列（注意：只包含正刊，不包含 ScienceDirect）
        'Science',
        'Science Advances',
        'Sci. Adv.',
        # 其他顶刊
        'Physical Review Letters',
        'Phys. Rev. Lett.',
        'PRL',
        'Advanced Materials',
        'Adv. Mater.'
        ,
        # arXiv（允许非常相关的预印本出现在周报）
        'arXiv'
    ]

    # RSS feed 标题常常带厂商前缀和栏目后缀，例如
    # 'Wiley: Advanced Materials: Table of Contents'、'AAAS: Science: Table of Contents'。
    # 直接拿原始 feed 标题去比对 TOP_JOURNALS 会把 Adv. Mater./Science Advances/Science
    # 整批漏掉（历史上整周 0 篇非预印本顶刊），因此比对前先归一化。
    JOURNAL_FEED_PREFIXES = ('Wiley:', 'AAAS:', 'APS:', 'ACS:', 'IOP:', 'RSC:')
    JOURNAL_FEED_SUFFIXES = (
        'Table of Contents', 'Current Issue', 'Latest Articles',
        'Accepted Articles', 'EarlyView',
    )

    @classmethod
    def _canonical_journal(cls, journal: str) -> str:
        """把 RSS feed 标题归一化成真正的期刊名（去掉厂商前缀与栏目后缀）。

        'Wiley: Advanced Materials: Table of Contents' -> 'Advanced Materials'
        'AAAS: Science: Table of Contents'             -> 'Science'
        归一化结果为空时原样返回，绝不把期刊名洗成空串。
        """
        raw = str(journal or '').strip()
        if not raw:
            return ''
        name = raw
        for prefix in cls.JOURNAL_FEED_PREFIXES:
            if name.lower().startswith(prefix.lower()):
                name = name[len(prefix):].strip()
                break
        # 少数 feed 会叠加两层栏目后缀，最多剥三次即可收敛
        for _ in range(3):
            stripped = name
            for suffix in cls.JOURNAL_FEED_SUFFIXES:
                if stripped.lower().endswith(suffix.lower()):
                    stripped = stripped[: -len(suffix)].strip().strip(':-–—').strip()
            if stripped == name:
                break
            name = stripped
        return name or raw

    # 磁性/铁电关键词
    FERRO_KEYWORDS = [
        'ferroelectric', 'ferromagnet', 'multiferroic', 'piezoelectric',
        'antiferroelectric', 'antiferromagnet', 'magnetoelectric',
        'polarization', 'magnetization', 'domain wall', 'skyrmion',
        'spin', 'magnetic', 'ferroic', 'perovskite',
        '铁电', '铁磁', '多铁', '压电', '反铁电', '反铁磁', '磁电',
        '极化', '磁化', '畴壁', '斯格明子', '自旋', '磁性', '铁性'
    ]
    
    # AI/机器学习关键词（使用更精确的匹配，避免误匹配神经科学内容）
    AI_KEYWORDS = [
        'machine learning', 'deep learning', 'neural network', 'artificial intelligence',
        'graph neural network', 'graph neural', 'transformer', 'gnn', 'mlip', 'ml potential', 
        'machine-learn', 'ai-driven', 'data-driven', 'convolutional neural', 'recurrent neural',
        'reinforcement learning', 'generative model', 'diffusion model', 
        'large language model', 'llm', 'transformer model', 'attention mechanism',
        '机器学习', '深度学习', '神经网络', '人工智能', '生成式', '卷积神经网络', '循环神经网络'
    ]

    # 本次运行中 AI 相关性判断失败的次数（网关抖动/限流时用于打醒目日志）
    _judge_failures = 0

    def __init__(self, api_key: str = None, provider: str = None, model: str = None):
        """初始化周报生成器"""
        api_key = (
            (api_key or "").strip()
            or (os.environ.get("AI_API_KEY") or "").strip()
            or (os.environ.get("KIMI_API_KEY") or "").strip()
            or (os.environ.get("GEMINI_API_KEY") or "").strip()
            or (DEFAULT_AI_CONFIG.get("api_key") if isinstance(DEFAULT_AI_CONFIG, dict) else "")  # type: ignore[arg-type]
        )
        provider = (
            (provider or "").strip()
            or (os.environ.get("AI_PROVIDER") or "").strip()
            or (DEFAULT_AI_CONFIG.get("provider") if isinstance(DEFAULT_AI_CONFIG, dict) else "gemini")  # type: ignore[arg-type]
            or "gemini"
        )
        model = (
            (model or "").strip()
            or (os.environ.get("AI_MODEL") or "").strip()
            or (DEFAULT_AI_CONFIG.get("model") if isinstance(DEFAULT_AI_CONFIG, dict) else "")  # type: ignore[arg-type]
        ) or None
        
        if api_key:
            self.provider = build_provider(provider, api_key, model=model)
            self.provider_name = provider
        else:
            self.provider = None
            self.provider_name = None
            print("⚠️ 未配置AI API密钥，将使用基础模板")
        
        # 初始化摘要爬取器
        self.abstract_scraper = AbstractScraper()

        # AI 判断失败计数（每次生成周报前重置）
        self._judge_failures = 0
        self._judge_lock = threading.Lock()

    def _loose_matches_ferro_keywords(self, text: str) -> bool:
        """宽松的关键词匹配（第一步筛选，获取更多候选）"""
        text_lower = text.lower()
        
        # 宽松的关键词列表（包含更多可能相关的词）
        loose_keywords = [
            # 核心关键词
            'ferroelectric', 'ferromagnet', 'multiferroic', 'piezoelectric',
            'antiferroelectric', 'antiferromagnet', 'magnetoelectric',
            'domain wall', 'skyrmion', 'ferroic', 'perovskite', 'ferro',
            '铁电', '铁磁', '多铁', '压电', '反铁电', '反铁磁', '磁电',
            '畴壁', '斯格明子', '铁性',
            # 扩展关键词（更宽松）
            'polarization', 'magnetization', 'spin', 'magnetic', 'magnet',
            '极化', '磁化', '自旋', '磁性', '磁',
            # 相关物理概念
            'spintronic', 'topological', 'quantum', 'hall effect',
            '自旋电子', '拓扑', '量子', '霍尔效应',
            # 材料相关
            '2d material', 'van der waals', 'heterostructure',
            '二维材料', '范德华', '异质结'
        ]
        
        # 简单匹配：只要包含关键词就认为可能相关
        for keyword in loose_keywords:
            if keyword.lower() in text_lower:
                return True
        
        return False
    
    def _matches_ferro_keywords(self, text: str) -> bool:
        """⚠️ 实际是宽松匹配（直接转发 _loose_matches_ferro_keywords）- 保留用于其他场景。

        仅可用于第一步召回，不要拿它当最终分类器：宽松词表里有 quantum/topological 等
        通用词，用来分类会把几乎所有凝聚态文章判成「相关」。
        """
        return self._loose_matches_ferro_keywords(text)

    def _loose_matches_ai_keywords(self, text: str) -> bool:
        """宽松的AI关键词匹配（第一步筛选，获取更多候选）"""
        text_lower = text.lower()
        
        # 宽松的关键词列表（包含更多可能相关的词）
        loose_keywords = [
            # 核心关键词
            'learning', 'artificial intelligence',
            'network', 'neural', 'transformer', 'gnn',
            'mlip', 'ml potential', 'machine-learn', 'ai-driven', 'data-driven',
            'convolutional neural', 'recurrent neural', 'reinforcement learning',
            'generative model', 'diffusion model', 'large language model', 'llm',
            'transformer model', 'attention mechanism',
            '机器学习', '深度学习', '神经网络', '人工智能', '生成式',
            '卷积神经网络', '循环神经网络',
            # 扩展关键词（更宽松）
            'neural network', 'neural', 'network', 'learning', 'model',
            'algorithm', 'prediction', 'classification', 'optimization',
            '神经网络', '学习', '模型', '算法', '预测', '分类', '优化',
            # AI在科学中的应用
            'ml potential', 'mlip', 'dft', 'quantum', 'molecular',
            '材料发现', '性质预测', '分子设计'
        ]
        
        # 简单匹配：只要包含关键词就认为可能相关
        for keyword in loose_keywords:
            if keyword.lower() in text_lower:
                return True
        
        return False
    
    def _matches_ai_keywords(self, text: str) -> bool:
        """⚠️ 实际是宽松匹配（直接转发 _loose_matches_ai_keywords）- 保留用于其他场景。

        仅可用于第一步召回，不要拿它当最终分类器：宽松词表里有 model/network/learning/
        quantum 等通用词，用来分类会把几乎所有文章判成「AI 相关」。
        """
        return self._loose_matches_ai_keywords(text)

    def _note_judge_failure(self):
        """AI 判断失败计数。判断现在跑在 ThreadPoolExecutor 里，+= 不是原子操作，必须加锁。"""
        lock = getattr(self, "_judge_lock", None)
        if lock is None:
            self._judge_failures += 1
            return
        with lock:
            self._judge_failures += 1

    def _strict_matches_ai_keywords(self, text: str) -> bool:
        """严格AI关键词匹配（AI_KEYWORDS 词表），AI 判断不可用时的降级兜底。"""
        text_lower = (text or '').lower()
        return any(keyword.lower() in text_lower for keyword in self.AI_KEYWORDS)
    
    def _ai_judge_ai_relevance(self, text: str) -> bool:
        """使用AI判断文本是否与AI/机器学习相关（用于边缘情况）"""
        if not self.provider:
            return False
        
        try:
            prompt = f"""请判断以下文献摘要是否与AI/机器学习、材料科学中的AI应用、AI驱动的物理/化学研究相关。

判断标准：
1. 明确使用机器学习、深度学习、神经网络等方法
2. AI在材料科学、物理、化学中的应用（如MLIP、材料发现、性质预测）
3. 数据驱动的科学研究方法
4. 排除：纯神经科学、生物学中的神经网络（除非明确是AI方法）

摘要：
{text[:1000]}

请只回答"是"或"否"，不要其他内容："""
            
            response = self.provider.call_api(prompt)
            return '是' in response or 'yes' in response.lower()
        except Exception as e:
            # 不能把「调用失败」当成「判定为否」：网关抖动时会把整周的 AI 文献静默清空。
            # 降级到严格关键词词表——真正写了 machine learning / 神经网络 的文章一定保留，
            # 只有仅靠宽松词命中的边缘候选才落选，且失败必定打日志。
            self._note_judge_failure()
            fallback = self._strict_matches_ai_keywords(text)
            print(f"    ⚠️ AI判断失败(AI相关性): {e}，降级为严格关键词判断 → {'保留' if fallback else '排除'}")
            return fallback
    
    def _ai_judge_ferro_relevance(self, text: str) -> bool:
        """使用AI判断文本是否与铁性材料相关（用于排除明显不相关的）"""
        if not self.provider:
            return True  # 如果没有AI，默认保留（宽松策略）
        
        try:
            prompt = f"""请判断以下文献是否与铁性材料（铁电、铁磁、多铁、压电等）相关。

判断标准（必须满足至少一条）：
1. 研究铁电、铁磁、多铁、压电材料及其性质
2. 研究极化、磁化、畴壁、斯格明子等铁性物理现象
3. 研究磁电耦合、自旋电子学、拓扑磁性
4. 研究铁性材料的应用（如存储、传感器等）

排除标准（如果符合以下情况，则不是铁性相关）：
- 纯磁性应用（如MRI、磁共振成像）但不涉及材料研究
- 生物磁性（如生物体内的磁性）但不涉及材料研究
- 仅提到"磁"但指的是磁场、磁力等，不涉及铁性材料
- 仅提到"电"但指的是电子、电路等，不涉及铁性材料

文献内容：
{text[:1500]}

请只回答"是"或"否"，不要其他内容："""
            
            response = self.provider.call_api(prompt)
            result = '是' in response or 'yes' in response.lower()
            if not result:
                print(f"    🤖 AI判断：不相关（铁性）")
            return result
        except Exception as e:
            self._note_judge_failure()
            print(f"    ⚠️ AI判断失败(铁性): {e}，默认保留")
            return True  # 如果AI判断失败，默认保留（宽松策略）
    
    def _analyze_single_article(self, article: Dict) -> str:
        """为单篇文章生成AI简要分析"""
        if not self.provider:
            return ""
        
        try:
            title = article.get('title_zh') or article.get('title', '')
            abstract = article.get('abstract_zh') or article.get('abstract', '')
            journal = article.get('journal', '')
            
            if not abstract:
                return ""
            
            prompt = _build_analyze_prompt(title, journal, abstract)
            
            response = self.provider.call_api(prompt)
            # 清理响应，移除可能的引号、JSON格式或格式
            analysis = response.strip()
            # 移除JSON格式
            if analysis.startswith('{'):
                try:
                    data = json.loads(analysis)
                    # 尝试提取analysis字段
                    if 'analysis' in data:
                        analysis = data['analysis']
                    elif 'summary' in data:
                        analysis = data['summary']
                    else:
                        # 如果无法解析，使用原始响应
                        analysis = response.strip()
                except Exception:
                    analysis = response.strip()
            # 移除引号
            analysis = analysis.strip('"').strip("'").strip()
            # 移除可能的markdown格式
            if analysis.startswith('```'):
                lines = analysis.split('\n')
                analysis = '\n'.join([l for l in lines if not l.startswith('```')])
            return analysis[:130]  # 限制长度(≤100字留余量)
            
        except Exception as e:
            print(f"⚠️ 文章分析失败: {e}")
            return ""
    
    def filter_articles(self, articles: List[Dict], start_date: str, end_date: str, category: str = 'all') -> List[Dict]:
        """
        筛选符合条件的文献
        
        Args:
            articles: 文献列表
            start_date: 开始日期
            end_date: 结束日期
            category: 'all' | 'ferro' | 'ai' - 筛选类别
        """
        filtered = []
        pending = []   # [article, text, is_ferro_candidate, is_ai_candidate]
        
        # 排除关键词（这些不是真正的期刊）
        # 注意：'table of contents'/'toc' 已移除——它们只是 feed 标题的栏目后缀，
        # 会把 'Wiley: Advanced Materials: Table of Contents' 这类真顶刊整批误杀；
        # 栏目后缀改由 _canonical_journal 剥离，顶刊列表仍然是唯一的准入闸门。
        EXCLUDE_KEYWORDS = [
            'sciencedirect', 'springer',
            'editor', 'suggestion', 'news', 'highlight'
        ]

        skipped_journal = 0

        for article in articles:
            # 检查日期范围
            pub_date = article.get('pub_date', '')
            if not (start_date <= pub_date <= end_date):
                continue
            
            # 检查期刊 - 精确匹配，避免误匹配
            journal = article.get('journal', '')
            if not journal:
                continue
            
            journal_lower = journal.lower()

            # 排除非期刊内容（对原始 feed 标题判断，保证 ScienceDirect 等仍被拦住）
            if any(exclude in journal_lower for exclude in EXCLUDE_KEYWORDS):
                skipped_journal += 1
                continue

            # 归一化后再比对顶刊列表（去掉 'Wiley: '、': Table of Contents' 等 feed 修饰）
            journal_clean = self._canonical_journal(journal)
            journal_clean_lower = journal_clean.lower()

            # 精确匹配顶刊列表
            is_top_journal = False
            for top_journal in self.TOP_JOURNALS:
                top_lower = top_journal.lower()

                # 精确匹配
                if journal_clean == top_journal:
                    is_top_journal = True
                    break

                # 包含匹配（但要小心处理）
                if top_lower in journal_clean_lower:
                    # 特殊处理 Science：必须是 "Science" 或 "Science Advances"，不能是 "ScienceDirect"
                    if top_lower == 'science':
                        if journal_clean_lower == 'science' or journal_clean_lower.startswith('science advances') or journal_clean_lower.startswith('sci. adv'):
                            is_top_journal = True
                            break
                        else:
                            continue
                    else:
                        is_top_journal = True
                        break

            if not is_top_journal:
                skipped_journal += 1
                continue

            # 第一步：宽松的关键词筛选（获取更多候选）
            text = ' '.join([
                article.get('title', ''),
                article.get('title_zh', ''),
                article.get('abstract', ''),
                article.get('abstract_zh', '')
            ])
            
            # 宽松匹配
            is_ferro_candidate = False
            is_ai_candidate = False
            
            if category == 'ferro':
                is_ferro_candidate = self._loose_matches_ferro_keywords(text)
            elif category == 'ai':
                is_ai_candidate = self._loose_matches_ai_keywords(text)
            else:  # 'all'
                is_ferro_candidate = self._loose_matches_ferro_keywords(text)
                is_ai_candidate = self._loose_matches_ai_keywords(text)
            
            # 如果宽松匹配失败，直接跳过
            if not (is_ferro_candidate or is_ai_candidate):
                continue
            
            # 第二步的 AI 判断挪到循环外并发执行（见下），这里只登记候选
            pending.append([article, text, is_ferro_candidate, is_ai_candidate])

        # 第二步：使用AI排除明显不相关的文章
        # 注意：只在有足够文本内容时才使用AI判断（避免对标题等短文本误判）
        # 并发执行：这里每篇文章一次往返，实测单周 1315 次串行调用 ≈ 110 分钟纯等待，
        # 是 weekly workflow 逼近 240 分钟 timeout 的主因（曾被取消过）。
        # 用与 _analyze_single_article 相同的 3 线程池，行为完全不变，只是不再串行。
        jobs = []
        for row in pending:
            article, text, is_ferro_candidate, is_ai_candidate = row
            if not (self.provider and len(text.strip()) > 100):
                continue
            if is_ferro_candidate and category in ['ferro', 'all']:
                jobs.append((row, 2, self._ai_judge_ferro_relevance, text))
            if is_ai_candidate and category in ['ai', 'all']:
                jobs.append((row, 3, self._ai_judge_ai_relevance, text))
        if jobs:
            print(f"    🤖 [{category}] AI 相关性判断 {len(jobs)} 次（3 线程并发）")
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {executor.submit(fn, txt): (row, slot) for row, slot, fn, txt in jobs}
                for fut in as_completed(futures):
                    row, slot = futures[fut]
                    try:
                        keep = fut.result()
                    except Exception as e:
                        # 与 _ai_judge_* 内部同样的原则：判断失败绝不等于「判为否」
                        self._note_judge_failure()
                        print(f"    ⚠️ AI判断线程异常: {e}，保留该文献")
                        keep = True
                    if not keep:
                        row[slot] = False

        # 根据最终判断结果添加文章
        for article, text, is_ferro_candidate, is_ai_candidate in pending:
            if category == 'ferro' and is_ferro_candidate:
                filtered.append(article)
            elif category == 'ai' and is_ai_candidate:
                filtered.append(article)
            elif category == 'all' and (is_ferro_candidate or is_ai_candidate):
                filtered.append(article)

        if skipped_journal:
            print(f"    ⏭️ [{category}] 期刊闸门过滤 {skipped_journal} 篇（非期刊来源或不在顶刊列表）")

        return filtered
    
    @staticmethod
    def _article_key(article: Dict) -> str:
        """文章唯一键：id > link > title，用于跨列表比对（与合并去重口径一致）。"""
        if not isinstance(article, dict):
            return ''
        return str(article.get('id') or article.get('link') or article.get('title') or '')

    def _collect_focus_articles(self, articles: List[Dict], start_date: str, end_date: str) -> List[Dict]:
        """收集当周 focus_score 命中的文章，按相关度降序。

        🎯 与你方向相关 是个性化区块，日报（generate_daily_pages）不做任何期刊闸门，
        周报如果沿用顶刊白名单，PRB / npj / MLST 上被 focus_filter 标记的论文
        会在周报里凭空消失（实测 58 篇里丢 19 篇）。这里直接从原始文献池按周窗口取，
        不经过 filter_articles 的期刊/关键词闸门。
        """
        picked: List[Dict] = []
        seen = set()
        for article in articles or []:
            if not isinstance(article, dict) or not article.get('focus_score'):
                continue
            pub_date = article.get('pub_date') or ''
            if not (start_date <= pub_date <= end_date):
                continue
            key = self._article_key(article)
            if key:
                if key in seen:
                    continue
                seen.add(key)
            picked.append(article)

        def _score(a: Dict) -> float:
            try:
                return float(a.get('focus_score') or 0)
            except (TypeError, ValueError):
                return 0.0

        picked.sort(key=_score, reverse=True)
        return picked

    def _split_by_category(self, all_articles: List[Dict], ferro_articles: List[Dict],
                           ai_articles: List[Dict]) -> tuple:
        """按 ferro/ai 命中集合把文章分成 (ferro_only, ai_only, both)，并写回 is_ferro/is_ai。

        以对象身份 + id/link/title 键双重比对；两边字段都为空时不再互相误命中。
        """
        ferro_ids = {id(a) for a in ferro_articles or []}
        ai_ids = {id(a) for a in ai_articles or []}
        ferro_keys = {self._article_key(a) for a in ferro_articles or []} - {''}
        ai_keys = {self._article_key(a) for a in ai_articles or []} - {''}

        ferro_only: List[Dict] = []
        ai_only: List[Dict] = []
        both: List[Dict] = []
        for article in all_articles or []:
            key = self._article_key(article)
            is_ferro = id(article) in ferro_ids or (bool(key) and key in ferro_keys)
            is_ai = id(article) in ai_ids or (bool(key) and key in ai_keys)
            article['is_ferro'] = is_ferro
            article['is_ai'] = is_ai
            if is_ferro and is_ai:
                both.append(article)
            elif is_ferro:
                ferro_only.append(article)
            elif is_ai:
                ai_only.append(article)
        return ferro_only, ai_only, both

    def generate_weekly_summary(self, articles: List[Dict], week_start: str) -> Dict:
        """生成周报"""
        # 计算周结束日期
        start_date = datetime.strptime(week_start, '%Y-%m-%d')
        end_date = start_date + timedelta(days=6)
        week_end = end_date.strftime('%Y-%m-%d')

        self._judge_failures = 0

        # 分别筛选铁性和AI文献
        ferro_articles = self.filter_articles(articles, week_start, week_end, 'ferro')
        ai_articles = self.filter_articles(articles, week_start, week_end, 'ai')

        # 🎯 focus 命中的文章不受顶刊闸门约束（与日报口径一致），单独收集
        focus_articles = self._collect_focus_articles(articles, week_start, week_end)

        if self._judge_failures:
            print(f"⚠️ 本周共有 {self._judge_failures} 次AI相关性判断失败（已降级为关键词判断），分类结果可能不完整")

        # 合并去重，并识别交叉研究
        all_articles = []
        seen_ids = set()
        ferro_set = set()
        ai_set = set()
        
        # 先建立集合以便快速查找
        for article in ferro_articles:
            article_id = article.get('id') or article.get('link', '')
            ferro_set.add(article_id)
        
        for article in ai_articles:
            article_id = article.get('id') or article.get('link', '')
            ai_set.add(article_id)
        
        # 合并所有文章
        for article in ferro_articles + ai_articles:
            article_id = article.get('id') or article.get('link', '')
            if article_id not in seen_ids:
                all_articles.append(article)
                seen_ids.add(article_id)
        
        if not all_articles:
            empty = self._empty_summary(week_start, week_end)
            empty['focus_articles'] = focus_articles
            return empty

        # 增强摘要：为所有文章爬取完整摘要并翻译（并行处理）
        print(f"\n📄 正在增强 {len(all_articles)} 篇文章的摘要...")
        
        def enhance_single_article(article_data):
            """增强单篇文章的摘要"""
            i, article = article_data
            link = article.get('link', '')
            current_abstract = article.get('abstract', '')
            enhanced = False
            
            # 检查是否需要爬取摘要
            if not self.abstract_scraper.is_abstract_valid(current_abstract) and link:
                new_abstract, status = self.abstract_scraper.scrape_abstract(link)
                if new_abstract:
                    article['abstract'] = new_abstract
                    # 翻译摘要
                    try:
                        article['abstract_zh'] = translate_text(new_abstract)
                    except Exception as e:
                        article['abstract_zh'] = ""
                    enhanced = True
                    return (i, True, len(new_abstract), None)
                else:
                    return (i, False, 0, status)
            elif current_abstract and not article.get('abstract_zh'):
                # 如果有摘要但没有翻译，进行翻译
                try:
                    article['abstract_zh'] = translate_text(current_abstract)
                except Exception as e:
                    article['abstract_zh'] = ""
            return (i, False, 0, None)
        
        # 并行处理摘要增强（最多5个线程）
        enhanced_count = 0
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(enhance_single_article, (i+1, article)): article 
                      for i, article in enumerate(all_articles)}
            
            for future in as_completed(futures):
                try:
                    i, enhanced, length, status = future.result()
                    if enhanced:
                        enhanced_count += 1
                        print(f"  [{i}/{len(all_articles)}] ✅ 成功获取摘要 ({length} 字符)")
                    elif status:
                        print(f"  [{i}/{len(all_articles)}] ⚠️ 无法获取摘要: {status}")
                except Exception as e:
                    print(f"  ⚠️ 处理失败: {e}")
        
        if enhanced_count > 0:
            print(f"📊 共增强 {enhanced_count} 篇文章的摘要")
        
        # 并行处理AI分析（如果启用）
        skip_ai = os.environ.get('SKIP_AI_ANALYSIS', '').lower() == '1'
        if self.provider and not skip_ai:
            print(f"\n🤖 正在并行分析 {len(all_articles)} 篇文章...")
            
            def analyze_article(article_data):
                """分析单篇文章"""
                idx, article = article_data
                title = article.get('title_zh') or article.get('title', '')
                if article.get('ai_analysis'):
                    return (idx, True, None)  # 已有分析
                try:
                    analysis = self._analyze_single_article(article)
                    if analysis:
                        article['ai_analysis'] = analysis
                        return (idx, True, None)
                    return (idx, False, "无分析结果")
                except Exception as e:
                    return (idx, False, str(e))
            
            # 并行处理（最多3个线程，避免API限流）
            analyzed_count = 0
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {executor.submit(analyze_article, (i+1, article)): article 
                          for i, article in enumerate(all_articles)}
                
                for future in as_completed(futures):
                    try:
                        idx, success, error = future.result()
                        if success:
                            analyzed_count += 1
                            if analyzed_count % 5 == 0:
                                print(f"  ✅ 已分析 {analyzed_count}/{len(all_articles)} 篇")
                        elif error:
                            print(f"  [{idx}] ⚠️ 分析失败: {error}")
                    except Exception as e:
                        print(f"  ⚠️ 处理失败: {e}")
            
            print(f"📊 共完成 {analyzed_count} 篇文章的AI分析")
        
        # 按期刊分组
        by_journal = {}
        for article in all_articles:
            journal = article.get('journal', '其他')
            if journal not in by_journal:
                by_journal[journal] = []
            by_journal[journal].append(article)
        
        # 使用AI生成总结（如果可用）
        summary = None
        if self.provider:
            try:
                summary = self._generate_ai_summary(all_articles, ferro_articles, ai_articles,
                                                   week_start, week_end, by_journal)
            except Exception as e:
                print(f"❌ AI生成失败: {e}")
        if summary is None:
            summary = self._fallback_summary(all_articles, ferro_articles, ai_articles,
                                             week_start, week_end, by_journal)

        # focus 区块用未过闸门的当周命中列表渲染（见 _collect_focus_articles）
        summary['focus_articles'] = focus_articles
        if focus_articles:
            print(f"🎯 与你方向相关：{len(focus_articles)} 篇（不受顶刊闸门约束）")
        return summary
    
    def _generate_ai_summary(self, all_articles: List[Dict], ferro_articles: List[Dict],
                            ai_articles: List[Dict], week_start: str, week_end: str,
                            by_journal: Dict) -> Dict:
        """使用AI生成周报 — index-based，不在 prompt 里传 URL 防止被改写。"""

        # 构建 index→article 映射；prompt 仅输入序号 / 期刊 / 标题 / 摘要，不输 URL。
        articles_text = []
        for i, article in enumerate(all_articles, 1):
            title = article.get('title_zh') or article.get('title', '')
            journal = article.get('journal', '')
            abstract = (article.get('abstract_zh') or article.get('abstract', ''))[:400]

            tags = []
            if article in ferro_articles:
                tags.append('铁电/磁性')
            if article in ai_articles:
                tags.append('AI/ML')
            tag_str = f"[{'/'.join(tags)}]" if tags else "[其他]"

            articles_text.append(
                f"[{i}] {tag_str}【{journal}】{title}\n    摘要: {abstract}"
            )

        articles_str = '\n'.join(articles_text)

        prompt = (
            f"你是一位资深凝聚态物理/材料科学研究员。下面是 {week_start} 至 {week_end} 这一周"
            f"Nature/Science 系列期刊的 {len(all_articles)} 篇磁性/铁电/AI 相关文献。"
            f"本周其中铁电/磁性 {len(ferro_articles)} 篇、AI/ML {len(ai_articles)} 篇。\n\n"
            f"【文献列表】(格式: [序号] [类型]【期刊】标题 / 摘要)\n{articles_str}\n\n"
            # ⚠️ 只索要页面真正会渲染的三段（overview / trends / outlook）。
            # 历史上还要过 article_summaries（每篇一句，实测一周 366 条）、highlights、
            # by_topic，程序解析完整整齐齐地丢掉——没有任何渲染路径读它们，
            # 却挤占 AI_MAX_TOKENS，把排在 JSON 末尾、真正上页面的 trends/outlook 截断。
            "【写作硬性要求】\n"
            "1. 全部中文。禁止 '本研究/具有重要意义/取得进展/为…提供新思路/重要科学意义' 等套话；"
            "必须写出具体材料体系（如 BaTiO3、BiFeO3、CrI3、MoTe2）、方法（DFT、GGA+U、MLIP、中子衍射等）、"
            "或关键数值/结论。若原文没有，可留空，不得编造。\n"
            "2. 不要在输出中填任何 URL。\n"
            "3. 长度约束：overview 3-4 句；trends 4-5 句；outlook 2-3 句。\n"
            "4. 三段都必须写满，不得省略 outlook。\n\n"
            "【输出格式】只输出 JSON，不要 markdown 标记：\n"
            "{\n"
            '  "overview": "...",\n'
            '  "trends": "...",\n'
            '  "outlook": "..."\n'
            "}\n"
        )

        response = self.provider.call_api(prompt)

        try:
            from ai_summarizer import AISummarizer as _AS
            data = _AS._load_json_lenient(response, context="weekly summary")
        except Exception:
            # Fallback: find outermost JSON object span
            import re
            m = re.search(r'\{[\s\S]*\}', response)
            if not m:
                raise ValueError("无法解析周报 JSON")
            data = json.loads(m.group())
        if not isinstance(data, dict):
            raise ValueError("周报 JSON 根不是对象")

        def _clamp(t: str, n: int) -> str:
            t = (t or "").strip()
            return t if len(t) <= n else t[: n - 1].rstrip() + "…"

        # 给每篇文章添加类型标记，并计算统计
        ferro_only, ai_only, both = self._split_by_category(all_articles, ferro_articles, ai_articles)

        # ---- Core-focus: ML × ferro/凝聚态 ----
        try:
            from config import CORE_FOCUS_CONFIG
        except Exception:
            CORE_FOCUS_CONFIG = {"enabled": True, "weekly_max_items": 20, "min_score": 0.60}

        core_items_enriched: List[Dict] = []
        weekly_note = ""
        if CORE_FOCUS_CONFIG.get("enabled", True):
            from focus_core import is_core_focus as _icf, core_score as _cs
            min_s = float(CORE_FOCUS_CONFIG.get("min_score", 0.60))
            core_items_raw = [a for a in all_articles if _icf(a) and _cs(a) >= min_s]
            core_items_raw.sort(key=lambda x: -_cs(x))
            core_items_raw = core_items_raw[: int(CORE_FOCUS_CONFIG.get("weekly_max_items", 20))]

            core_deep: Dict[str, Dict[str, str]] = {}
            if core_items_raw:
                weekly_note, core_deep = self._generate_core_weekly(core_items_raw, week_start, week_end)

            for a in core_items_raw:
                link = a.get("link") or ""
                info = core_deep.get(link, {})
                core_items_enriched.append({
                    "title": a.get("title_zh") or a.get("title", ""),
                    "title_en": a.get("title", ""),
                    "link": a.get("link", ""),
                    "journal": a.get("journal", ""),
                    "abstract_zh": a.get("abstract_zh", ""),
                    "abstract_zh_full": a.get("abstract_zh_full", ""),
                    "method_point": info.get("method_point", ""),
                    "related_work": info.get("related_work", ""),
                    "implication": info.get("implication", ""),
                    "core_score": _cs(a),
                })

        return {
            'week_start': week_start,
            'week_end': week_end,
            'total': len(all_articles),
            'ferro_count': len(ferro_articles),
            'ai_count': len(ai_articles),
            'both_count': len(both),
            'overview': _clamp(data.get('overview', ''), 400),
            'by_journal': by_journal,
            'trends': _clamp(data.get('trends', ''), 600),
            'outlook': _clamp(data.get('outlook', ''), 300),
            'all_articles': all_articles,
            'ferro_articles': ferro_only,
            'ai_articles': ai_only,
            'both_articles': both,
            'core_items': core_items_enriched,
            'core_weekly_note': weekly_note,
            'generated_by': getattr(self, 'provider_name', None) or 'ai'
        }

    def _generate_core_weekly(
        self,
        core_items: List[Dict],
        week_start: str,
        week_end: str,
    ) -> tuple:
        """对核心关注论文批量产出 direction_weekly_note + 三深度字段。

        Returns: (note_str, {link -> {method_point, related_work, implication}})
        """
        if not core_items or not self.provider:
            return "", {}

        lines = []
        for i, it in enumerate(core_items, 1):
            title = it.get('title') or ''
            title_zh = it.get('title_zh') or ''
            abstract = (it.get('abstract_zh') or it.get('abstract') or '')[:400]
            journal = it.get('journal', '')
            lines.append(f"[{i}] 中文: {title_zh}\n    EN: {title}\n    期刊: {journal}\n    摘要: {abstract}")
        articles_str = "\n".join(lines)

        prompt = (
            f"你是深耕 ML × 铁电/磁性/凝聚态方向的资深研究员。下面是 {week_start} 至 {week_end} "
            f"这一周 {len(core_items)} 篇属于该方向的核心论文。\n\n"
            f"【文献列表】\n{articles_str}\n\n"
            "请给出两部分输出：\n"
            "A. weekly_direction_note（6-8 句中文）：回顾本周 ML × ferro/凝聚态方向的实质进展，"
            "按 '新材料 / 新方法 / 新现象' 三条主线展开，必须点名具体材料、方法与关键数值或结论。"
            "禁止 '整体来看/具有重要意义/为…提供新思路' 之类套话。\n"
            "B. items：每篇三字段（全中文，每条 2~3 句）：\n"
            "   1) method_point ≤150 字：具体技术路线（架构/训练策略/关键参数或计算设置）\n"
            "   2) related_work ≤150 字：与已知方法/体系/方向的呼应及异同\n"
            "   3) implication ≤150 字：具体可执行的启发与借鉴点\n\n"
            "只输出 JSON：\n"
            "{\n"
            '  "weekly_direction_note": "...",\n'
            '  "items": [{"index": 1, "method_point": "...", "related_work": "...", "implication": "..."}]\n'
            "}\n"
        )

        try:
            response = self.provider.call_api(prompt)
            from ai_summarizer import AISummarizer as _AS
            data = _AS._load_json_lenient(response, context="weekly core deep")
        except Exception as e:
            print(f"⚠️ _generate_core_weekly failed: {e}")
            return "", {}

        if not isinstance(data, dict):
            return "", {}

        def _c(s, n):
            s = (s or "").strip()
            return s if len(s) <= n else s[: n - 1].rstrip() + "…"

        deep: Dict[str, Dict[str, str]] = {}
        for entry in data.get("items", []) or []:
            if not isinstance(entry, dict):
                continue
            try:
                idx = int(entry.get("index"))
            except Exception:
                continue
            if not (1 <= idx <= len(core_items)):
                continue
            link = core_items[idx - 1].get("link") or ""
            if not link:
                continue
            deep[link] = {
                "method_point": _c(entry.get("method_point", ""), 200),
                "related_work": _c(entry.get("related_work", ""), 200),
                "implication": _c(entry.get("implication", ""), 200),
            }
        note = _c(data.get("weekly_direction_note", ""), 800)
        return note, deep

    def _fallback_summary(self, all_articles: List[Dict], ferro_articles: List[Dict],
                         ai_articles: List[Dict], week_start: str, week_end: str, 
                         by_journal: Dict) -> Dict:
        """降级周报（无AI）- 展示所有文献"""
        
        # 按期刊排序，Nature/Science正刊优先
        def sort_key(a):
            j = a.get('journal', '')
            if j == 'Nature':
                return 0
            elif j == 'Science':
                return 1
            elif 'Nature' in j:
                return 2
            elif 'Science' in j:
                return 3
            elif 'Phys. Rev. Lett.' in j or j == 'PRL':
                return 4
            else:
                return 5
        
        sorted_articles = sorted(all_articles, key=sort_key)
        
        # 统计期刊分布
        journal_stats = {j: len(arts) for j, arts in by_journal.items()}
        journal_list = ', '.join([f"{j}({n}篇)" for j, n in sorted(journal_stats.items(), key=lambda x: -x[1])[:5]])
        
        # 分类文献 + 打上 is_ferro/is_ai 标记
        # 注意：这里必须按 ferro_articles / ai_articles 的实际命中集合分桶，
        # 不能再用 _matches_*_keywords（那两个其实是宽松词表，quantum/model/network 都算命中，
        # 会把几乎所有文章判成「交叉研究」，导致 both_count 反超 ferro_count/ai_count）。
        ferro_only, ai_only, both = self._split_by_category(sorted_articles, ferro_articles, ai_articles)

        return {
            'week_start': week_start,
            'week_end': week_end,
            'total': len(all_articles),
            'ferro_count': len(ferro_articles),
            'ai_count': len(ai_articles),
            'both_count': len(both),
            'overview': f"本周({week_start}至{week_end})共收录{len(all_articles)}篇相关文献，其中磁性/铁电相关{len(ferro_articles)}篇，AI/机器学习相关{len(ai_articles)}篇，交叉研究{len(both)}篇。主要发表在{journal_list}。",
            'trends': '（AI分析暂不可用）',
            'ferro_articles': ferro_only,
            'ai_articles': ai_only,
            'both_articles': both,
            'by_journal': by_journal,
            'all_articles': sorted_articles,
            'generated_by': 'fallback'
        }
    
    def _empty_summary(self, week_start: str, week_end: str) -> Dict:
        """空周报"""
        return {
            'week_start': week_start,
            'week_end': week_end,
            'total': 0,
            'overview': f"本周({week_start}至{week_end})暂无符合条件的文献。",
            'by_journal': {},
            'trends': '',
            'outlook': '',
            'articles': [],
            'generated_by': 'empty'
        }
    
    def save_summary_html(self, summary: Dict, output_dir: str = 'docs/weekly') -> str:
        # 保存周报为HTML（资讯周报风格）
        week_start = str(summary['week_start'])
        week_end = str(summary.get('week_end') or '')
        os.makedirs(output_dir, exist_ok=True)

        filepath = os.path.join(output_dir, f"{week_start}.html")

        def shorten_text(value, limit: int = 160) -> str:
            compact = ' '.join(str(value or '').split())
            if len(compact) <= limit:
                return compact
            return compact[: max(0, limit - 1)].rstrip() + '…'

        def article_key(article: Dict, fallback: str) -> str:
            raw = article.get('link') or article.get('id') or article.get('title_zh') or article.get('title') or fallback
            return str(raw)

        def article_anchor(article: Dict, fallback: str) -> str:
            raw = article.get('id') or article.get('link') or article.get('title_zh') or article.get('title') or fallback
            raw_str = str(raw)
            if raw_str.startswith('article-'):
                raw_str = raw_str[8:]
            return f"article-{_safe_id(raw_str)}"

        def authors_label(article: Dict) -> str:
            return format_authors_label(article.get('authors'), max_names=4)

        def article_teaser(article: Dict, limit: int = 180) -> str:
            for key in ('ai_analysis', 'abstract_zh', 'abstract', 'title_zh', 'title'):
                value = str(article.get(key) or '').strip()
                if value:
                    return shorten_text(value, limit)
            return ''

        both_articles = list(summary.get('both_articles') or [])
        ferro_articles = list(summary.get('ferro_articles') or [])
        ai_articles = list(summary.get('ai_articles') or [])
        all_articles = list(summary.get('all_articles') or summary.get('articles') or [])

        if not all_articles:
            deduped = []
            seen_all = set()
            for index, article in enumerate(both_articles + ferro_articles + ai_articles, 1):
                key = article_key(article, f'all-{index}')
                if key in seen_all:
                    continue
                seen_all.add(key)
                deduped.append(article)
            all_articles = deduped

        # focus 区块优先用 summary['focus_articles']（未过顶刊闸门的当周命中，见
        # _collect_focus_articles）；旧的 summary 字典没有该键时回退到 all_articles 推导。
        focus_articles = [
            a for a in (summary.get('focus_articles') or [])
            if isinstance(a, dict) and a.get('focus_score')
        ]
        if not focus_articles:
            focus_articles = [a for a in all_articles if isinstance(a, dict) and a.get('focus_score')]

        if not week_end and week_start:
            try:
                week_end = (datetime.strptime(week_start, '%Y-%m-%d') + timedelta(days=6)).strftime('%Y-%m-%d')
            except Exception:
                week_end = ''

        displayed_keys = set()
        for index, article in enumerate(both_articles + ferro_articles + ai_articles, 1):
            displayed_keys.add(article_key(article, f'display-{index}'))

        other_articles = []
        seen_other = set()
        for index, article in enumerate(all_articles, 1):
            key = article_key(article, f'other-{index}')
            if key in displayed_keys or key in seen_other:
                continue
            seen_other.add(key)
            other_articles.append(article)

        raw_by_journal = summary.get('by_journal') or {}
        by_journal = {}
        if isinstance(raw_by_journal, dict) and raw_by_journal:
            has_article_lists = any(isinstance(value, list) for value in raw_by_journal.values())
            if has_article_lists:
                by_journal = raw_by_journal

        if not by_journal:
            for article in all_articles:
                journal = str(article.get('journal') or '其他来源').strip() or '其他来源'
                by_journal.setdefault(journal, []).append(article)

        def journal_item_count(value) -> int:
            if isinstance(value, list):
                return len(value)
            if isinstance(value, tuple):
                return len(value)
            try:
                return int(value)
            except Exception:
                return len(value or [])

        journal_items = sorted(by_journal.items(), key=lambda item: (-journal_item_count(item[1]), str(item[0])))
        journal_count = len(journal_items)
        arxiv_count = sum(
            1
            for article in all_articles
            if 'arxiv' in str(article.get('journal') or '').lower() or 'arxiv' in str(article.get('link') or '').lower()
        )
        top_journal_text = '、'.join(
            f"{str(journal).strip() or '其他来源'} {journal_item_count(items)}篇"
            for journal, items in journal_items[:4]
        )

        hero_quote_raw = (
            str(summary.get('trends') or '').strip()
            or str(summary.get('outlook') or '').strip()
            or str(summary.get('overview') or '').strip()
            or '覆盖 AI × 物理 / 化学 / 材料交叉研究的一周重点，并保留全文速览入口。'
        )
        hero_quote = _safe_text(shorten_text(hero_quote_raw, 180))

        tag_labels = ['本周总览', '交叉研究', '磁性/铁电', 'AI/机器学习', '期刊分布']
        if other_articles:
            tag_labels.append('其他相关文献')
        if arxiv_count:
            tag_labels.append('含 arXiv 预印本')
        tags_html = ''.join(f'<span class="insight-tag">{_safe_text(tag)}</span>' for tag in tag_labels)

        def render_overview_bucket(title: str, icon: str, articles: List[Dict], tone_class: str, limit: int = 4) -> str:
            if not articles:
                return ''
            item_html = []
            for idx, article in enumerate(articles[:limit], 1):
                journal = _safe_text(str(article.get('journal') or '来源').strip() or '来源')
                teaser = _safe_text(article_teaser(article, 82))
                anchor = article_anchor(article, f'{tone_class}-{idx}')
                item_html.append(
                    f'<li class="weekly-digest-item"><a href="#{anchor}"><span class="weekly-digest-journal">[{journal}]</span>{teaser}</a></li>'
                )
            more_html = ''
            if len(articles) > limit:
                more_html = f'<div class="weekly-digest-more">+ {len(articles) - limit} 篇继续下拉查看</div>'
            title_html = _safe_text(title)
            icon_html = _safe_text(icon)
            return f'''
            <div class="weekly-digest-card {tone_class}">
                <div class="weekly-digest-title"><span>{icon_html} {title_html}</span><span>{len(articles)} 篇</span></div>
                <ul class="weekly-digest-list">{''.join(item_html)}</ul>
                {more_html}
            </div>
            '''

        def render_article_cards(articles: List[Dict], tone_class: str) -> str:
            if not articles:
                return '<div class="insight-empty">本栏目本周暂无相关文献。</div>'

            cards = []
            for idx, article in enumerate(articles, 1):
                raw_title_zh = str(article.get('title_zh') or '').strip()
                raw_title_en = str(article.get('title') or '').strip()
                raw_title = raw_title_zh or raw_title_en or '未命名文献'
                raw_journal = str(article.get('journal') or '').strip()
                raw_link = str(article.get('link') or '#').strip()
                raw_date = str(article.get('pub_date') or article.get('date') or '').strip()
                raw_authors = authors_label(article)
                raw_ai_analysis = str(article.get('ai_analysis') or '').strip()
                raw_abstract_zh = str(article.get('abstract_zh_full') or article.get('abstract_zh') or '').strip()
                raw_abstract_en = str(article.get('abstract') or '').strip()

                title = _safe_text(raw_title)
                title_en = _safe_text(raw_title_en)
                journal = _safe_text(raw_journal)
                link = _safe_url(raw_link)
                date = _safe_text(raw_date)
                authors = _safe_text(raw_authors)
                abstract_zh = _safe_multiline(raw_abstract_zh)
                abstract_en = _safe_multiline(raw_abstract_en)
                anchor = article_anchor(article, f'{tone_class}-{idx}')

                title_en_block = f'<div class="weekly-paper-title-en">{title_en}</div>' if raw_title_en and raw_title_zh else ''

                meta_parts = []
                if raw_journal:
                    meta_parts.append(f'<span class="weekly-chip weekly-chip-journal">📚 {journal}</span>')
                if raw_authors:
                    meta_parts.append(f'<span class="weekly-chip weekly-chip-authors">👤 {authors}</span>')
                if raw_date:
                    meta_parts.append(f'<span class="weekly-chip">📅 {date}</span>')
                if article.get('is_ferro'):
                    meta_parts.append('<span class="weekly-chip weekly-chip-ferro">⚡ 磁性/铁电</span>')
                if article.get('is_ai'):
                    meta_parts.append('<span class="weekly-chip weekly-chip-ai">🤖 AI/机器学习</span>')
                meta_html = ''.join(meta_parts)

                note_raw = raw_ai_analysis or article_teaser(article, 180)
                note_label = 'AI 解读' if raw_ai_analysis else '核心摘录'
                note_html = ''
                if note_raw:
                    note_html = f'<div class="weekly-paper-summary"><strong>{_safe_text(note_label)}：</strong>{_safe_multiline(note_raw)}</div>'

                preview_raw = ''
                if raw_abstract_zh or raw_abstract_en:
                    preview_source = raw_abstract_zh or raw_abstract_en
                    preview_raw = shorten_text(preview_source, 240)
                preview_html = ''
                if preview_raw and preview_raw != note_raw:
                    preview_html = f'<div class="weekly-paper-preview">{_safe_text(preview_raw)}</div>'

                has_full_abstract = bool(raw_abstract_zh or raw_abstract_en)
                toggle_html = ''
                abstract_html = ''
                if has_full_abstract:
                    abstract_blocks = []
                    if raw_abstract_zh:
                        abstract_blocks.append(
                            f'<div class="weekly-abstract-block"><div class="weekly-abstract-label">中文摘要</div><p>{abstract_zh}</p></div>'
                        )
                    if raw_abstract_en:
                        abstract_blocks.append(
                            f'<div class="weekly-abstract-block"><div class="weekly-abstract-label">English Abstract</div><p class="weekly-abstract-en">{abstract_en}</p></div>'
                        )
                    toggle_html = f'<button class="toggle-abstract-btn" onclick="toggleAbstract(\'{anchor}\', this)">📖 查看完整摘要</button>'
                    abstract_html = f'<div class="weekly-paper-abstract" id="{anchor}-abstract" style="display:none;">{"".join(abstract_blocks)}</div>'

                cards.append(f'''
                <article class="weekly-paper-card {tone_class}" id="{anchor}" data-bookmark-key="{_safe_text((article.get('link') or '').strip())}">
                    <div class="weekly-paper-head">
                        <span class="weekly-paper-number">{idx:02d}</span>
                        <div class="weekly-paper-titles">
                            <h3 class="weekly-paper-title-zh"><a href="{link}" target="_blank" rel="noopener noreferrer">{title}</a></h3>
                            {title_en_block}
                        </div>
                    </div>
                    <div class="weekly-paper-meta">{meta_html}</div>
                    {note_html}
                    {preview_html}
                    <div class="weekly-paper-actions">
                        {toggle_html}
                        <a class="insight-btn insight-btn-secondary" href="{link}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a>
                    </div>
                    {abstract_html}
                </article>
                ''')
            return f'<div class="weekly-paper-list">{"".join(cards)}</div>'

        def render_journal_stats() -> str:
            if not journal_items:
                return '<div class="insight-empty">暂无期刊统计。</div>'
            items_html = []
            for journal, items in journal_items:
                count = journal_item_count(items)
                items_html.append(
                    f'<div class="weekly-journal-item"><div class="weekly-journal-name">{_safe_text(str(journal) or "其他来源")}</div><div class="weekly-journal-count">{count} 篇</div></div>'
                )
            return f'<div class="weekly-journal-list">{"".join(items_html)}</div>'

        overview_cards = [
            f'''
            <div class="weekly-summary-card">
                <div class="weekly-summary-title">整体概览</div>
                <p>{_safe_multiline(summary.get('overview', '本周暂无周报概览。'))}</p>
            </div>
            '''
        ]

        secondary_title = '趋势观察'
        secondary_text = str(summary.get('trends') or '').strip()
        if not secondary_text:
            secondary_title = '来源分布'
            secondary_text = f'本周主要来源包括：{top_journal_text}。' if top_journal_text else '本周来源结构将在新数据写入后自动更新。'
        overview_cards.append(
            f'''
            <div class="weekly-summary-card">
                <div class="weekly-summary-title">{_safe_text(secondary_title)}</div>
                <p>{_safe_multiline(secondary_text)}</p>
            </div>
            '''
        )

        outlook_text = str(summary.get('outlook') or '').strip()
        if outlook_text:
            overview_cards.append(
                f'''
                <div class="weekly-summary-card">
                    <div class="weekly-summary-title">后续展望</div>
                    <p>{_safe_multiline(outlook_text)}</p>
                </div>
                '''
            )
        else:
            overview_cards.append(
                '''
                <div class="weekly-summary-card">
                    <div class="weekly-summary-title">阅读提示</div>
                    <p>先看交叉研究，再按磁性/铁电与 AI 专题分区深读；所有卡片均保留中英标题、期刊、作者与摘要入口。</p>
                </div>
                '''
            )

        digest_cards = [
            render_overview_bucket('交叉研究', '🔀', both_articles, 'cross'),
            render_overview_bucket('磁性/铁电', '⚡', ferro_articles, 'ferro'),
            render_overview_bucket('AI / 机器学习', '🤖', ai_articles, 'ai'),
            render_overview_bucket('其他相关文献', '🧩', other_articles, 'other', limit=3),
        ]
        digest_cards = [card for card in digest_cards if card]
        overview_body = f'<div class="weekly-summary-grid">{"".join(overview_cards)}</div>'
        if digest_cards:
            overview_body += f'<div class="weekly-digest-grid">{"".join(digest_cards)}</div>'

        sections = []
        toc_links = []
        section_counter = {'value': 1}

        def add_section(section_id: str, title: str, subtitle: str, body_html: str, count_text: str = '') -> None:
            number = section_counter['value']
            num_text = f'{number:02d}'
            title_html = _safe_text(title)
            subtitle_html = _safe_text(subtitle)
            count_block = f'<span class="weekly-toc-meta">{_safe_text(count_text)}</span>' if count_text else f'<span class="weekly-toc-meta">{num_text}</span>'
            sections.append(f'''
            <section id="{section_id}" class="insight-panel weekly-report-section">
                <div class="weekly-section-head">
                    <span class="weekly-section-index">{num_text}</span>
                    <div class="weekly-section-copy">
                        <h2 class="insight-panel-title">{title_html}</h2>
                        <p class="insight-panel-subtitle">{subtitle_html}</p>
                    </div>
                </div>
                {body_html}
            </section>
            ''')
            toc_links.append(f'<a class="weekly-toc-link" href="#{section_id}"><span>{title_html}</span>{count_block}</a>')
            section_counter['value'] += 1

        add_section(
            'overview',
            '本周总览',
            '先把握一周的主线，再进入交叉重点与专题全文速览。',
            overview_body,
            f'{len(all_articles)} 篇文献' if all_articles else '总览',
        )

        if both_articles:
            add_section(
                'cross',
                '交叉研究',
                '优先覆盖 AI × 物理 / 化学 / 材料的直接交叉工作。',
                render_article_cards(both_articles, 'cross'),
                f'{len(both_articles)} 篇',
            )
        if ferro_articles:
            add_section(
                'ferro',
                '磁性 / 铁电专题',
                '聚焦自旋、铁电、多铁与相关功能材料研究。',
                render_article_cards(ferro_articles, 'ferro'),
                f'{len(ferro_articles)} 篇',
            )
        if ai_articles:
            add_section(
                'ai',
                'AI / 机器学习专题',
                '覆盖模型、方法与 AI for Science 应用工作。',
                render_article_cards(ai_articles, 'ai'),
                f'{len(ai_articles)} 篇',
            )
        if other_articles:
            add_section(
                'other',
                '其他相关文献',
                '补充未落入前三个专题但仍与本周主题相关的工作。',
                render_article_cards(other_articles, 'other'),
                f'{len(other_articles)} 篇',
            )
        if journal_items:
            add_section(
                'journals',
                '期刊分布',
                '按来源快速判断本周成果主要集中在哪些期刊或预印本平台。',
                render_journal_stats(),
                f'{journal_count} 个来源',
            )

        sidebar_facts = [
            ('交叉研究', len(both_articles)),
            ('磁性/铁电', len(ferro_articles)),
            ('AI/机器学习', len(ai_articles)),
        ]
        if other_articles:
            sidebar_facts.append(('其他相关', len(other_articles)))
        sidebar_stats_html = ''.join(
            f'<div class="weekly-sidebar-fact"><span>{_safe_text(label)}</span><strong>{value}</strong></div>'
            for label, value in sidebar_facts
        )

        generated_by = _safe_text(summary.get('generated_by', 'AI'))
        # runner 是 UTC，而页面上其它日期（week_start/week_end/pub_date）都是北京日历日；
        # 不带时区地写 datetime.now() 会让刚跑完的周报显示成 8 小时前
        generated_at = datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M')
        section_count = max(section_counter['value'] - 1, 1)

        html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI × Science 周报 - {week_start} 至 {week_end}</title>
    <link rel="stylesheet" href="../style.css">
    <link rel="stylesheet" href="../bookmarks.css">
    <script defer src="../exports.js"></script>
    <script defer src="../bookmarks.js"></script>
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <meta name="apple-mobile-web-app-title" content="文献追踪">
    <meta name="theme-color" content="#f59e0b">
    <style>
        body {{
            background: linear-gradient(180deg, rgba(99, 102, 241, 0.08) 0%, rgba(248, 250, 252, 0.85) 240px), var(--bg-primary);
        }}

        .weekly-report-main {{
            min-width: 0;
        }}

        .weekly-report-hero {{
            position: relative;
            overflow: hidden;
        }}

        .weekly-report-hero::after {{
            content: '';
            position: absolute;
            inset: auto -80px -110px auto;
            width: 240px;
            height: 240px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(99, 102, 241, 0.22), transparent 70%);
            pointer-events: none;
        }}

        .weekly-report-quote {{
            margin: 18px 0 0;
            padding: 16px 18px;
            border-left: 4px solid var(--accent-primary);
            border-radius: 16px;
            background: rgba(99, 102, 241, 0.08);
            color: var(--text-secondary);
            line-height: 1.8;
        }}

        .weekly-hero-actions {{
            margin-top: 18px;
        }}

        .weekly-report-section + .weekly-report-section {{
            margin-top: 20px;
        }}

        .weekly-section-head {{
            display: flex;
            gap: 14px;
            align-items: flex-start;
            margin-bottom: 18px;
        }}

        .weekly-section-copy {{
            min-width: 0;
        }}

        .weekly-section-copy .insight-panel-title {{
            margin-bottom: 6px;
        }}

        .weekly-section-index {{
            width: 42px;
            height: 42px;
            border-radius: 14px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            color: white;
            background: var(--gradient-accent);
            box-shadow: var(--shadow-sm);
            flex-shrink: 0;
        }}

        .weekly-summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
        }}

        .weekly-summary-card,
        .weekly-digest-card,
        .weekly-paper-card,
        .weekly-journal-item,
        .weekly-sidebar-fact,
        .weekly-toc-link {{
            background: linear-gradient(180deg, rgba(255, 255, 255, 0.98), rgba(244, 247, 255, 0.96));
            border: 1px solid var(--border-color);
            box-shadow: var(--shadow-sm);
        }}

        [data-theme="dark"] .weekly-summary-card,
        [data-theme="dark"] .weekly-digest-card,
        [data-theme="dark"] .weekly-paper-card,
        [data-theme="dark"] .weekly-journal-item,
        [data-theme="dark"] .weekly-sidebar-fact,
        [data-theme="dark"] .weekly-toc-link {{
            background: linear-gradient(180deg, rgba(30, 41, 59, 0.96), rgba(15, 23, 42, 0.92));
        }}

        .weekly-summary-card {{
            padding: 18px 20px;
            border-radius: 20px;
            line-height: 1.8;
        }}

        .weekly-summary-title {{
            font-size: 0.9rem;
            font-weight: 800;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: var(--accent-primary);
            margin-bottom: 10px;
        }}

        .weekly-digest-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-top: 18px;
        }}

        .weekly-digest-card {{
            padding: 18px;
            border-radius: 20px;
        }}

        .weekly-digest-card.cross {{
            border-top: 3px solid #8b5cf6;
        }}

        .weekly-digest-card.ferro {{
            border-top: 3px solid #f59e0b;
        }}

        .weekly-digest-card.ai {{
            border-top: 3px solid #10b981;
        }}

        .weekly-digest-card.other {{
            border-top: 3px solid #64748b;
        }}

        .weekly-digest-title {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            font-weight: 700;
            margin-bottom: 12px;
        }}

        .weekly-digest-list {{
            list-style: none;
            padding: 0;
            margin: 0;
            display: grid;
            gap: 10px;
        }}

        .weekly-digest-item a {{
            color: var(--text-primary);
            text-decoration: none;
            line-height: 1.7;
        }}

        .weekly-digest-item a:hover {{
            color: var(--accent-primary);
        }}

        .weekly-digest-journal {{
            color: var(--accent-primary);
            font-weight: 700;
            margin-right: 8px;
        }}

        .weekly-digest-more {{
            margin-top: 12px;
            color: var(--text-muted);
            font-size: 0.92rem;
        }}

        .weekly-paper-list {{
            display: grid;
            gap: 16px;
        }}

        .weekly-paper-card {{
            border-radius: 22px;
            padding: 20px;
            position: relative;
            overflow: hidden;
            transition: transform var(--transition-fast), box-shadow var(--transition-fast), border-color var(--transition-fast);
        }}

        .weekly-paper-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background: var(--gradient-accent);
            opacity: 0;
            transition: opacity var(--transition-fast);
        }}

        .weekly-paper-card.cross::before {{
            background: linear-gradient(180deg, #8b5cf6 0%, #6366f1 100%);
        }}

        .weekly-paper-card.ferro::before {{
            background: linear-gradient(180deg, #f59e0b 0%, #d97706 100%);
        }}

        .weekly-paper-card.ai::before {{
            background: linear-gradient(180deg, #10b981 0%, #059669 100%);
        }}

        .weekly-paper-card.other::before {{
            background: linear-gradient(180deg, #64748b 0%, #334155 100%);
        }}

        .weekly-paper-card:hover {{
            transform: translateY(-3px);
            box-shadow: var(--shadow-lg);
            border-color: var(--accent-primary);
        }}

        .weekly-paper-card:hover::before {{
            opacity: 1;
        }}

        .weekly-paper-head {{
            display: flex;
            align-items: flex-start;
            gap: 16px;
        }}

        .weekly-paper-number {{
            width: 42px;
            height: 42px;
            border-radius: 14px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            font-weight: 800;
            color: white;
            background: var(--gradient-accent);
            box-shadow: var(--shadow-sm);
        }}

        .weekly-paper-titles {{
            min-width: 0;
        }}

        .weekly-paper-title-zh {{
            margin: 0;
            font-size: 1.14rem;
            line-height: 1.6;
        }}

        .weekly-paper-title-zh a {{
            color: var(--text-primary);
            text-decoration: none;
        }}

        .weekly-paper-title-zh a:hover {{
            color: var(--accent-primary);
        }}

        .weekly-paper-title-en {{
            margin-top: 8px;
            color: var(--text-secondary);
            font-size: 0.96rem;
            line-height: 1.7;
        }}

        .weekly-paper-meta {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 14px;
        }}

        .weekly-chip {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 7px 12px;
            border-radius: 999px;
            font-size: 0.88rem;
            color: var(--text-secondary);
            background: rgba(99, 102, 241, 0.08);
        }}

        .weekly-chip-authors {{
            background: rgba(16, 185, 129, 0.08);
        }}

        .weekly-chip-journal {{
            background: rgba(99, 102, 241, 0.12);
        }}

        .weekly-chip-ferro {{
            color: #92400e;
            background: rgba(245, 158, 11, 0.16);
        }}

        .weekly-chip-ai {{
            color: #065f46;
            background: rgba(16, 185, 129, 0.16);
        }}

        [data-theme="dark"] .weekly-chip-ferro {{
            color: #fcd34d;
            background: rgba(245, 158, 11, 0.18);
        }}

        [data-theme="dark"] .weekly-chip-ai {{
            color: #6ee7b7;
            background: rgba(16, 185, 129, 0.18);
        }}

        .weekly-paper-summary {{
            margin-top: 14px;
            line-height: 1.85;
            color: var(--text-primary);
        }}

        .weekly-paper-summary strong {{
            color: var(--accent-primary);
        }}

        .weekly-paper-preview {{
            margin-top: 12px;
            color: var(--text-secondary);
            line-height: 1.8;
        }}

        .weekly-paper-actions {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-top: 16px;
        }}

        .toggle-abstract-btn {{
            appearance: none;
            border: none;
            border-radius: 14px;
            padding: 10px 16px;
            background: var(--gradient-accent);
            color: white;
            font-weight: 600;
            cursor: pointer;
            box-shadow: var(--shadow-sm);
            transition: transform var(--transition-fast), box-shadow var(--transition-fast);
        }}

        .toggle-abstract-btn:hover {{
            transform: translateY(-2px);
        }}

        .weekly-paper-abstract {{
            margin-top: 16px;
            padding-top: 16px;
            border-top: 1px solid var(--border-color);
            display: grid;
            gap: 14px;
        }}

        .weekly-abstract-block {{
            line-height: 1.8;
        }}

        .weekly-abstract-label {{
            font-size: 0.9rem;
            font-weight: 800;
            color: var(--accent-primary);
            margin-bottom: 6px;
        }}

        .weekly-abstract-en {{
            color: var(--text-secondary);
            font-style: italic;
        }}

        .weekly-journal-list {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
        }}

        .weekly-journal-item {{
            padding: 16px 18px;
            border-radius: 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 14px;
        }}

        .weekly-journal-name {{
            font-weight: 700;
        }}

        .weekly-journal-count {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 78px;
            padding: 8px 12px;
            border-radius: 999px;
            background: var(--gradient-accent);
            color: white;
            font-weight: 700;
        }}

        .weekly-report-sidebar {{
            position: sticky;
            top: 24px;
        }}

        .weekly-toc-list,
        .weekly-sidebar-stats {{
            display: grid;
            gap: 10px;
        }}

        .weekly-toc-link {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            padding: 12px 14px;
            border-radius: 16px;
            color: var(--text-primary);
            text-decoration: none;
        }}

        .weekly-toc-link:hover {{
            color: var(--accent-primary);
            border-color: rgba(99, 102, 241, 0.22);
        }}

        .weekly-toc-meta {{
            color: var(--text-muted);
            font-size: 0.88rem;
            white-space: nowrap;
        }}

        .weekly-sidebar-block + .weekly-sidebar-block {{
            margin-top: 18px;
        }}

        .weekly-sidebar-heading {{
            font-size: 0.9rem;
            font-weight: 800;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: var(--text-muted);
            margin-bottom: 10px;
        }}

        .weekly-sidebar-fact {{
            padding: 12px 14px;
            border-radius: 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }}

        .weekly-sidebar-fact strong {{
            font-size: 1.05rem;
        }}

        .weekly-report-footer {{
            margin-top: 22px;
            padding: 18px 0 0;
            border-top: 1px solid var(--border-color);
            color: var(--text-muted);
            font-size: 0.94rem;
            line-height: 1.8;
        }}

        @media (max-width: 980px) {{
            .weekly-report-sidebar {{
                position: static;
            }}
        }}

        @media (max-width: 720px) {{
            .weekly-section-head,
            .weekly-paper-head {{
                flex-direction: column;
            }}

            .weekly-paper-actions {{
                flex-direction: column;
                align-items: stretch;
            }}

            .toggle-abstract-btn,
            .weekly-paper-actions .insight-btn {{
                width: 100%;
                justify-content: center;
            }}

            .weekly-journal-item {{
                align-items: flex-start;
                flex-direction: column;
            }}
        }}

        /* ---- Core focus (ML × ferro/凝聚态) ---- */
        .weekly-core-section {{ border-radius:22px; padding:22px; margin-bottom:26px; background:linear-gradient(135deg,rgba(253,244,215,0.55),rgba(255,248,230,0.88)); border:1.5px solid rgba(245,158,11,0.45); box-shadow:0 4px 18px rgba(245,158,11,0.08); }}
        .weekly-core-section .weekly-section-title {{ color:#b45309; }}
        .weekly-core-count {{ margin-left:auto; padding:6px 12px; border-radius:999px; background:rgba(245,158,11,0.15); color:#b45309; font-weight:700; font-size:.9rem; }}
        .weekly-core-note {{ margin:12px 0 18px; padding:14px 16px; border-radius:14px; background:rgba(255,255,255,.7); border-left:3px solid #f59e0b; line-height:1.8; }}
        .weekly-core-list {{ list-style:none; margin:0; padding:0; }}
        .weekly-core-card {{ display:grid; grid-template-columns:auto minmax(0,1fr); gap:14px; padding:18px; border-radius:18px; background:rgba(255,255,255,0.95); border:1px solid rgba(245,158,11,0.25); border-left:3px solid #f59e0b; }}
        .weekly-core-card + .weekly-core-card {{ margin-top:14px; }}
        .weekly-core-number {{ width:40px; height:40px; display:inline-flex; align-items:center; justify-content:center; border-radius:14px; font-weight:800; color:white; background:linear-gradient(135deg,#f59e0b,#fbbf24); }}
        .weekly-core-title-zh {{ font-size:1.08rem; font-weight:700; line-height:1.5; margin-bottom:4px; }}
        .weekly-core-title-en {{ color:#64748b; font-size:.92rem; line-height:1.6; }}
        .weekly-core-meta {{ display:flex; flex-wrap:wrap; gap:8px; margin:10px 0; }}
        .weekly-chip {{ padding:6px 10px; border-radius:999px; background:rgba(99,102,241,.08); font-size:.88rem; color:#475569; }}
        .weekly-chip-core {{ background:rgba(245,158,11,.18); color:#b45309; font-weight:600; }}
        .weekly-core-deep {{ margin-top:10px; padding:12px 14px; border-radius:12px; background:rgba(245,158,11,.06); border:1px dashed rgba(245,158,11,.35); line-height:1.75; }}
        .weekly-core-deep p + p {{ margin-top:6px; }}
        @media (max-width:720px){{ .weekly-core-card{{ grid-template-columns:1fr; }} }}

        /* ---- Focus interest (与你方向相关) ---- */
        .weekly-focus-section {{ border-radius:22px; padding:22px; margin-bottom:26px; background:linear-gradient(135deg,rgba(224,231,255,0.55),rgba(238,242,255,0.88)); border:1.5px solid rgba(99,102,241,0.4); box-shadow:0 4px 18px rgba(99,102,241,0.08); }}
        .weekly-focus-section .weekly-section-title {{ color:var(--accent-primary); }}
        .weekly-focus-count {{ margin-left:auto; padding:6px 12px; border-radius:999px; background:rgba(99,102,241,0.14); color:var(--accent-primary); font-weight:700; font-size:.9rem; }}
        .weekly-focus-list {{ list-style:none; margin:0; padding:0; }}
        .weekly-focus-card {{ display:grid; grid-template-columns:auto minmax(0,1fr); gap:14px; padding:18px; border-radius:18px; background:rgba(255,255,255,0.95); border:1px solid rgba(99,102,241,0.22); border-left:3px solid var(--accent-primary); }}
        .weekly-focus-card + .weekly-focus-card {{ margin-top:14px; }}
        .weekly-focus-number {{ width:40px; height:40px; display:inline-flex; align-items:center; justify-content:center; border-radius:14px; font-weight:800; color:white; background:linear-gradient(135deg,#6366f1,#818cf8); }}
        .weekly-focus-title {{ font-size:1.08rem; font-weight:700; line-height:1.5; margin-bottom:4px; }}
        .weekly-focus-link {{ color:inherit; text-decoration:none; }}
        .weekly-focus-link:hover {{ color:var(--accent-primary); }}
        .weekly-focus-meta {{ display:flex; flex-wrap:wrap; gap:8px; margin:10px 0; }}
        .weekly-chip-focus {{ background:rgba(99,102,241,.16); color:var(--accent-primary); font-weight:600; }}
        .weekly-focus-deep {{ margin-top:10px; padding:12px 14px; border-radius:12px; background:rgba(99,102,241,.06); border:1px dashed rgba(99,102,241,.32); line-height:1.75; }}
        .weekly-focus-deep p + p {{ margin-top:6px; }}
        @media (max-width:720px){{ .weekly-focus-card{{ grid-template-columns:1fr; }} }}
    </style>
</head>
<body>
    <div class="insight-shell">
        <div class="insight-topbar">
            <div class="insight-topbar-left">
                <a href="../index.html" class="insight-back-link">← 返回主页</a>
                <span class="insight-mini-chip">AI × Science Weekly</span>
            </div>
            <div class="insight-topbar-right">
                <span class="insight-mini-chip">{_safe_text(week_start)} → {_safe_text(week_end)}</span>
                <button class="theme-toggle" id="themeToggle" onclick="toggleTheme()" title="切换主题">🌙</button>
            </div>
        </div>

        <div class="insight-layout">
            <main class="weekly-report-main">
                <section class="insight-page-hero weekly-report-hero">
                    <div class="insight-kicker">AI 科学周报</div>
                    <h1 class="insight-title">AI × Science 周报 {_safe_text(week_start)} → {_safe_text(week_end)}</h1>
                    <p class="insight-subtitle">参考 CloudFlare-AI-Insight-Daily 的资讯编排，将一周内 AI × 物理 / 化学 / 材料交叉文献整理为总览、交叉重点、专题速览与期刊分布。</p>
                    <blockquote class="weekly-report-quote">{hero_quote}</blockquote>
                    <div class="insight-tags">{tags_html}</div>
                    <div class="insight-stat-grid">
                        <div class="insight-stat">
                            <div class="insight-stat-label">收录文献</div>
                            <div class="insight-stat-value">{len(all_articles)}</div>
                        </div>
                        <div class="insight-stat">
                            <div class="insight-stat-label">期刊 / 来源</div>
                            <div class="insight-stat-value">{journal_count}</div>
                        </div>
                        <div class="insight-stat">
                            <div class="insight-stat-label">arXiv / 预印本</div>
                            <div class="insight-stat-value">{arxiv_count}</div>
                        </div>
                        <div class="insight-stat">
                            <div class="insight-stat-label">专题版块</div>
                            <div class="insight-stat-value">{section_count}</div>
                        </div>
                    </div>
                    <div class="insight-action-row weekly-hero-actions">
                        <a class="insight-btn insight-btn-primary" href="./index.html">周报归档</a>
                        <a class="insight-btn insight-btn-secondary" href="../daily/">查看日报</a>
                    </div>
                </section>

                <nav class="weekly-toc-sticky" aria-label="移动目录">
                  {'<a href="#core-focus">核心方向</a>' if summary.get('core_items') else ''}
                  {'<a href="#focus-interest">与你相关</a>' if focus_articles else ''}
                  <a href="#overview">本周总览</a>
                  {'<a href="#cross">交叉研究</a>' if both_articles else ''}
                  {'<a href="#ferro">磁性/铁电</a>' if ferro_articles else ''}
                  {'<a href="#ai">AI专题</a>' if ai_articles else ''}
                  {'<a href="#journals">期刊分布</a>' if journal_items else ''}
                </nav>

                {render_core_weekly_section(summary)}
                {render_focus_weekly_section(focus_articles)}
                {''.join(sections)}

                <div class="weekly-report-footer">
                    本页由文献追踪系统自动生成，保留中英标题、期刊、作者与摘要入口，便于按周追踪 AI × 物理 / 化学 / 材料交叉研究。<br>
                    生成方式：{generated_by} ｜ 更新时间：{generated_at}（北京时间）
                </div>
            </main>

            <aside class="insight-sidebar-card weekly-report-sidebar">
                <h3 class="insight-sidebar-title">快速导航</h3>
                <div class="weekly-toc-list">{''.join(toc_links)}</div>

                <div class="weekly-sidebar-block">
                    <div class="weekly-sidebar-heading">本页分区</div>
                    <div class="weekly-sidebar-stats">{sidebar_stats_html}</div>
                </div>

                <div class="weekly-sidebar-block">
                    <div class="weekly-sidebar-heading">阅读建议</div>
                    <ul class="insight-note-list">
                        <li class="insight-note-item">先读本周总览，再看交叉研究，最后按磁性/铁电与 AI 分区深挖。</li>
                        <li class="insight-note-item">期刊分布适合快速判断本周成果主要来自顶刊还是预印本平台。</li>
                        <li class="insight-note-item">若需要长期追踪某条研究线，可回到归档页连续浏览多周内容。</li>
                    </ul>
                </div>
            </aside>
        </div>
    </div>

    <script>
        const THEME_KEY = 'literature_theme';

        function initTheme() {{
            const theme = localStorage.getItem(THEME_KEY) || 'light';
            document.documentElement.setAttribute('data-theme', theme);
            updateThemeButton();
        }}

        function toggleTheme() {{
            const current = document.documentElement.getAttribute('data-theme') || 'light';
            const next = current === 'light' ? 'dark' : 'light';
            localStorage.setItem(THEME_KEY, next);
            document.documentElement.setAttribute('data-theme', next);
            updateThemeButton();
        }}

        function updateThemeButton() {{
            const btn = document.getElementById('themeToggle');
            const theme = document.documentElement.getAttribute('data-theme') || 'light';
            if (btn) btn.textContent = theme === 'light' ? '🌙' : '☀️';
        }}

        function toggleAbstract(articleId, button) {{
            const abstractDiv = document.getElementById(articleId + '-abstract');
            if (!abstractDiv) return;
            const trigger = button || window.event?.target;
            const isHidden = abstractDiv.style.display === 'none' || !abstractDiv.style.display;
            abstractDiv.style.display = isHidden ? 'grid' : 'none';
            if (trigger) {{
                trigger.textContent = isHidden ? '📖 收起摘要' : '📖 查看完整摘要';
            }}
        }}

        initTheme();
    </script>
</body>
</html>'''

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html)

        print(f"✅ 周报已保存: {filepath}")

        self._update_weekly_index(output_dir)

        return filepath

    def _update_weekly_index(self, weekly_dir: str = 'docs/weekly'):
        """更新周报索引（根据 weekly_dir 下已有 HTML 重写 index.json）"""
        n = _write_weekly_index_file(weekly_dir)
        print(f"✅ 周报索引已更新: {n} 个周报")
        enhanced = enhance_weekly_archive(os.path.join(weekly_dir, 'index.json'))
        print(f"🧭 Enhanced weekly navigation/TOC for {enhanced} page(s)")


def _write_weekly_index_file(weekly_dir: str = 'docs/weekly') -> int:
    """
    根据 weekly_dir 下已有 YYYY-MM-DD.html 文件重写 index.json。
    返回写入的周报条数。不依赖 WeeklySummarizer 实例，供 sync_weekly_index 使用。
    """
    import glob
    index_file = os.path.join(weekly_dir, 'index.json')
    weekly_files = glob.glob(os.path.join(weekly_dir, '????-??-??.html'))
    weeklies = []
    for f in sorted(weekly_files, reverse=True):
        filename = os.path.basename(f)
        date_str = filename.replace('.html', '')
        try:
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            end_date = date_obj + timedelta(days=6)
            weeklies.append({
                'week_start': date_str,
                'week_end': end_date.strftime('%Y-%m-%d'),
                'file': filename
            })
        except ValueError:
            continue
    os.makedirs(weekly_dir, exist_ok=True)
    with open(index_file, 'w', encoding='utf-8') as f:
        # 带时区写时间戳：裸 datetime.now() 在 runner 上是 UTC，和周报里的北京日历日差 8 小时
        json.dump({'weeklies': weeklies, 'updated': datetime.now(BEIJING_TZ).isoformat()},
                  f, ensure_ascii=False, indent=2)
    return len(weeklies)


def default_week_start(now: datetime = None) -> str:
    """默认周窗口 = 上一个**已完成**的周（周一~周日），按北京日历日计算。

    以前默认取「本周一」，周窗口的最后一天正好是任务运行当天：fetch.yml 当天的文献
    还没入库（00:00 UTC 起跑、要跑几个小时），而周报页此后不会再重新生成，
    当周最后一两天的文献就永久缺席（实测某周漏掉 4 篇 PRL + 1 篇 Nature Reviews Physics）。
    改成上一个已完成周后，整段窗口在生成前都已抓完。

    时区：pub_date 存的是北京日历日（rss_fetcher 按 UTC+8 落库），runner 却是 UTC，
    裸 datetime.now() 在北京时间 00:00-08:00 之间会算错一天。
    """
    now = now or datetime.now(BEIJING_TZ)
    monday = now - timedelta(days=now.weekday() + 7)
    return monday.strftime('%Y-%m-%d')


def _load_article_list(path: str) -> List[Dict]:
    """读取一个文献列表文件：index.json 的 {'articles': [...]} 或 ai_relevant.json 的裸数组。

    读不到/格式不对只记一条日志并返回空列表——这里只是取数，绝不能因为某个数据源
    坏掉就让整个周报挂掉。归一化留给调用方，只对真正进周报的那批做。
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️ 读取 {path} 失败({type(e).__name__}: {e})，跳过该数据源")
        return []
    if isinstance(data, dict):
        data = data.get('articles') or []
    if not isinstance(data, list):
        print(f"⚠️ {path} 顶层既不是数组也不是 {{'articles': [...]}}，跳过该数据源")
        return []
    return [a for a in data if isinstance(a, dict)]


def _articles_in_window(articles: List[Dict], start_date: str, end_date: str) -> List[Dict]:
    return [a for a in articles if start_date <= (a.get('pub_date') or '') <= end_date]


def _date_span(articles: List[Dict]) -> str:
    dates = [a.get('pub_date') for a in articles if a.get('pub_date')]
    return f"{min(dates)}~{max(dates)}" if dates else "空"


def generate_weekly_summary(week_start: str = None, api_key: str = None,
                            strict_window: bool = False) -> Optional[str]:
    """
    便捷函数：生成周报

    Args:
        week_start: 周开始日期 (YYYY-MM-DD)，默认为上一个已完成周的周一
        api_key: API密钥
        strict_window: 该周连一篇原始文献都没有（整周在留存数据窗口之外）时抛
            WeekOutsideDataWindow，让回填脚本能用退出码区分「这周没有值得收录的论文」
            和「这周根本没有数据、再跑也不可能生成」。默认 False（保持 fail-soft）。

    Returns:
        输出文件路径
    """
    # 确定周开始日期
    if not week_start:
        week_start = default_week_start()
    week_end = (datetime.strptime(week_start, '%Y-%m-%d') + timedelta(days=6)).strftime('%Y-%m-%d')

    # 加载文献数据
    index_path = 'data/index.json'
    articles = _load_article_list(index_path)
    if not articles and not os.path.exists(index_path):
        print(f"⚠️ 未找到 {index_path}，改用长期库")

    # 是否退回到了 data/ai_relevant.json：那是个**更小**的池子（run_optimized_sync 只把
    # 通过 AI4S 与相关性闸门的条目追加进去，且写在 zh/focus 富化之前），实测同一周
    # index.json 1224 篇原始 / 45 篇带 focus_score，ai_relevant.json 只有 922 / 0。
    # 用它重生成会得到一份更薄、且 🎯 区块为空的页面。
    used_fallback = False

    in_window = _articles_in_window(articles, week_start, week_end)
    if not in_window:
        # index.json 被 run_optimized_sync 截断到最新 5000 篇（实测只覆盖约 30 天），
        # 回填历史周时窗口内一篇都取不到，旧代码只会打一句「📭 没有符合条件的文献」，
        # 于是整轮回填全绿、一个页面都没生成，操作者以为归档已经补齐。
        # data/ai_relevant.json 是长期库（run_optimized_sync 就地累加，不截断），回退到它。
        relevant = _load_article_list('data/ai_relevant.json')
        extra = _articles_in_window(relevant, week_start, week_end)
        if extra:
            print(f"♻️ {index_path} 不含 {week_start}~{week_end} 的文献（现覆盖 {_date_span(articles)}），"
                  f"回退到长期库 data/ai_relevant.json，补入 {len(extra)} 篇候选")
            # index.json 在该窗口内是 0 篇，不可能与 extra 重复，直接拼接
            articles = articles + extra
            in_window = extra
            used_fallback = True
        else:
            msg = (f"❌ {week_start}~{week_end} 完全在留存数据窗口之外："
                   f"{index_path} 覆盖 {_date_span(articles)}、"
                   f"data/ai_relevant.json 覆盖 {_date_span(relevant)}，该周一篇原始文献都没有。"
                   f"这不是「本周没有符合条件的文献」——再跑多少次也不可能生成，"
                   f"需要先把原始数据补回来。")
            print(msg)
            if strict_window:
                raise WeekOutsideDataWindow(msg)
            return None

    # 只对真正进入本周窗口的这批做 LaTeX/符号归一化：窗口外的条目一条都不会被渲染，
    # 而 ai_relevant.json 有 1.6 万条，全量归一化白等十几秒
    normalize_articles_inplace(in_window)

    # 生成周报
    summarizer = WeeklySummarizer(api_key)
    summary = summarizer.generate_weekly_summary(articles, week_start)

    if summary['total'] == 0:
        print(f"📭 {week_start}~{week_end} 窗口内有 {len(in_window)} 篇原始文献，"
              f"但没有一篇通过顶刊闸门/关键词/AI 判断，本周不出页面")
        return None

    # 降级重生成绝不能覆盖已有的好页面。backfill-weekly.yml 默认区间覆盖 15 个**已存在**
    # 的周页并且会 git add docs/weekly 强推 main —— 没有这道闸，一次例行回填就会把
    # 2026-03-16(665KB) 这类完整页面换成明显更薄的版本。
    if used_fallback:
        existing = os.path.join('docs/weekly', f"{week_start}.html")
        if os.path.exists(existing):
            print(f"⏭️ {week_start} 页面已存在，且本周数据来自降级来源(ai_relevant.json)，"
                  f"保留现有页面不覆盖。确需重建请先删除 {existing}")
            return None

    return summarizer.save_summary_html(summary)


def sync_weekly_index(weekly_dir: str = 'docs/weekly') -> int:
    """
    根据 docs/weekly 下已有 HTML 文件刷新 index.json，确保周报列表页与文件一致。
    不依赖 API 或 WeeklySummarizer 实例，可在 CI 或本地安全调用。
    返回写入的周报条数。
    """
    n = _write_weekly_index_file(weekly_dir)
    try:
        print(f"✅ 周报索引已同步: {n} 个周报")
    except UnicodeEncodeError:
        print(f"[OK] weekly index synced: {n} entries")
    return n


def main(argv: List[str] = None) -> int:
    """CLI 入口，返回进程退出码。

    退出码 3 = 请求的这一周完全没有原始数据（见 WeekOutsideDataWindow）。
    只有显式指定周（回填）时才会返回 3：定时任务不带参数，绝不能因此变红——
    否则会连带跳过 commit/push 与 deploy 这两步。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    week_start = argv[0] if argv else None
    exit_code = 0
    try:
        generate_weekly_summary(week_start, strict_window=bool(week_start))
    except WeekOutsideDataWindow:
        exit_code = 3
    finally:
        # 每次运行结束都刷新索引，确保周报列表页能显示所有已存在的 HTML（含本次新生成的）
        sync_weekly_index()
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
