#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Focus filtering for AI × physics / chemistry / materials literature.

The goal is high recall for AI4Science / physical-science papers while aggressively
removing clearly off-topic biomedical, clinical, education, and social-science
content that may slip through broad keyword RSS feeds.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

AI_TERMS: Tuple[str, ...] = (
    'machine learning', 'deep learning', 'neural network', 'neural networks', 'graph neural', 'gnn',
    'transformer', 'diffusion model', 'generative model', 'foundation model', 'large language model',
    'llm', 'reinforcement learning', 'active learning', 'surrogate model', 'data-driven', 'ai-driven',
    'artificial intelligence', 'message passing', 'equivariant neural', 'ml potential', 'mlip',
    '机器学习', '深度学习', '神经网络', '大语言模型', '人工智能',
)

PHYSICS_TERMS: Tuple[str, ...] = (
    'quantum', 'spin', 'magnetic', 'magnetism', 'ferroelectric', 'ferromagnet', 'antiferromagnet',
    'multiferroic', 'condensed matter', 'superconduct', 'phonon', 'lattice', 'exciton', 'moire',
    'moiré', 'topological', 'skyrmion', 'hall effect', 'electronic structure', 'band structure',
    'weyl', 'josephson', 'magnon', 'altermagnet', 'quantum gas', 'plasma',
    '凝聚态', '量子', '磁性', '铁电', '铁磁', '反铁磁', '多铁', '超导', '声子', '晶格', '拓扑',
)

PHYSICS_CORE_TERMS: Tuple[str, ...] = PHYSICS_TERMS + (
    'quantum spin hall', 'magnetoresistance', 'magnetoelectric', 'spin hall', 'single-photon',
    'van der waals heterostructure', 'moire potential',
)

DAILY_PHYSICS_TERMS: Tuple[str, ...] = (
    'quantum', 'spin', 'magnetic', 'magnetism', 'ferroelectric', 'ferromagnet', 'antiferromagnet',
    'multiferroic', 'superconduct', 'phonon', 'exciton', 'moire', 'moiré', 'electronic structure',
    'band structure', 'hall effect', 'skyrmion', 'altermagnet', 'kagome', 'spintronic', 'spintronics',
    'photonic crystal', 'electron tomography',
)

CHEMISTRY_TERMS: Tuple[str, ...] = (
    'chemical', 'chemistry', 'molecule', 'molecular', 'catalyst', 'catalysis', 'electrochem',
    'reaction mechanism', 'reaction network', 'spectroscopy', 'photochemistry', 'surface chemistry',
    'computational chemistry', 'molecular design', 'polymerization', 'solvation', 'adsorption',
    '化学', '分子', '催化', '电化学', '反应机理', '光化学', '光谱',
)

CHEMISTRY_CORE_TERMS: Tuple[str, ...] = (
    'catalyst', 'catalysis', 'electrochem', 'reaction mechanism', 'reaction network', 'spectroscopy',
    'photochemistry', 'surface chemistry', 'computational chemistry', 'molecular design',
    'polymerization', 'adsorption', 'solvation', 'chemical space', 'ligand design',
    '催化', '电化学', '反应机理', '光化学', '光谱',
)

MATERIALS_TERMS: Tuple[str, ...] = (
    'material', 'materials', 'crystal', 'crystalline', 'perovskite', 'semiconductor', 'electrode',
    'battery', 'alloy', 'oxide', 'heterostructure', 'thin film', 'surface', 'interface', 'nanostructure',
    '2d material', '2d materials', 'device', 'optoelectronic', 'materials discovery', 'solar cell',
    'solid electrolyte', 'photovoltaic', 'memristor', 'dielectric', 'monolayer',
    '固态', '材料', '晶体', '钙钛矿', '半导体', '电极', '电池', '合金', '氧化物', '异质结构',
    '薄膜', '界面', '器件',
)

MATERIALS_CORE_TERMS: Tuple[str, ...] = (
    'perovskite', 'semiconductor', 'electrode', 'battery', 'alloy', 'oxide', 'heterostructure',
    'thin film', 'surface', 'interface', '2d material', '2d materials', 'materials discovery',
    'solar cell', 'solid electrolyte', 'photovoltaic', 'memristor', 'dielectric', 'monolayer',
    'crystal structure prediction', '晶体', '钙钛矿', '半导体', '电极', '电池', '合金', '氧化物',
    '异质结构', '薄膜', '界面', '器件',
)

SIMULATION_TERMS: Tuple[str, ...] = (
    'dft', 'density functional', 'ab initio', 'first-principles', 'first principles', 'molecular dynamics',
    'monte carlo', 'phase field', 'finite element', 'atomistic simulation', 'electronic-structure',
    'computational materials', 'physics-informed', 'simulation', 'interatomic potential',
    'crystal structure prediction', '拟合势', '模拟', '第一性原理', '分子动力学', '相场', '有限元',
    '原子模拟', '电子结构', '物理约束',
)

SIMULATION_CORE_TERMS: Tuple[str, ...] = (
    'dft', 'density functional', 'ab initio', 'first-principles', 'first principles', 'molecular dynamics',
    'monte carlo', 'phase field', 'finite element', 'atomistic simulation', 'electronic structure',
    'electronic-structure', 'computational materials', 'physics-informed', 'interatomic potential',
    'crystal structure prediction', 'materials discovery', '第一性原理', '分子动力学', '相场', '有限元',
    '原子模拟', '电子结构', '物理约束',
)

DAILY_SIMULATION_TERMS: Tuple[str, ...] = tuple(term for term in SIMULATION_CORE_TERMS if term != 'physics-informed')

CURATED_JOURNAL_HINTS: Tuple[str, ...] = (
    # ========== 1区期刊 ==========
    'phys. rev. lett', 'phys. rev. x', 'phys. rev. b', 'phys. rev. materials', 'phys. rev. applied',
    'physical review letters', 'physical review x', 'physical review b', 'physical review materials', 'physical review applied',
    'j. am. chem. soc', 'journal of the american chemical society', 'jacs',
    'nano lett', 'nano letters',
    'j. phys. chem. lett', 'journal of physical chemistry letters', 'jpcl',
    'j. chem. theory comput', 'journal of chemical theory and computation', 'jctc',
    'advanced materials', 'advanced science', 'materials today',
    'npj comput', 'npj quantum', 'nature materials', 'nature physics',
    'nature chemistry', 'nature electronics', 'nature energy', 'science advances',
    'computer physics communications', 'computational materials science', 'digital discovery',
    'nature machine intelligence',
    # ========== 2区期刊（仅保留指定）==========
    'j. appl. phys', 'journal of applied physics',  # JAP
    'chem. phys. lett', 'chemical physics letters',  # CPL
)

ALLOWED_ARXIV_PHYSICAL: Tuple[str, ...] = (
    'cond-mat', 'physics.comp-ph', 'physics.chem-ph',
)

ALLOWED_ARXIV_AI: Tuple[str, ...] = (
    'cs.lg', 'stat.ml', 'cs.ai',
)

NEGATIVE_CLINICAL_TERMS: Tuple[str, ...] = (
    'patient', 'patients', 'clinical', 'clinic', 'biomedical', 'biomed', 'disease', 'diseases', 'cancer', 'tumor', 'tumour',
    'therapy', 'therapeutic', 'diagnosis', 'diagnostic', 'hospital', 'healthcare', 'medical', 'medicine',
    'drug', 'drugs', 'pharmac', 'pathology', 'radiology', 'epidemiology', 'histology', 'oncology',
    'survival', 'prognostic', 'risk stratification', 'case report', 'sickle cell', 'stroke', 'dialysis',
    'hemodialysis', 'glioblastoma', 'alzheimer', 'alzheimer\'s', 'parkinson', 'pancreatic', 'prostate cancer',
    'breast cancer', 'colorectal cancer', 'kidney disease', 'uterine', 'fertility', 'clinical-grade',
    '临床', '患者', '疾病', '癌', '肿瘤', '治疗', '诊断', '医院', '医学', '药物', '病理', '放射',
)

NEGATIVE_LIFE_SCIENCE_TERMS: Tuple[str, ...] = (
    'immune', 'immunology', 'immunotherapy', 'protein', 'proteins', 'peptide',
    'dna', 'rna', 'genome', 'genomic', 'genomics', 'transcriptomic', 'proteomic', 'cell biology',
    'single-cell', 'stem cell', 'cell line', 'mouse', 'mice', 'rat', 'rats', 'vaccine', 'virus', 'viral',
    'bacteria', 'bacterial', 'fungal', 'microbiome', 'nuclease', 'adenocarcinoma', 'biopsy', 'in vivo',
    'biochemical recurrence', 'follicular', 'ovarian', 'lung adenocarcinoma', 'prostate', 'breast',
    '免疫', '蛋白', '肽', '基因组', '转录组', '蛋白质组', '单细胞', '小鼠', '疫苗', '病毒', '细菌',
)

NEGATIVE_SOCIAL_TERMS: Tuple[str, ...] = (
    'education', 'educational', 'student', 'students', 'school', 'qualitative', 'cross-sectional',
    'retrospective', 'survey', 'sociology', 'public health', 'teacher', 'curriculum', 'undergraduate',
    'intervention', 'competency', 'learning experience', 'online class', '访谈', '教育', '学生', '横断面',
    '回顾性', '调查', '公共卫生',
)


def _normalize_text(value: Any) -> str:
    return ' '.join(str(value or '').replace('\xa0', ' ').replace('\n', ' ').split()).lower()


def _item_text(item: Mapping[str, Any]) -> str:
    parts = [
        item.get('title') or item.get('title_en') or '',
        item.get('title_zh') or '',
        item.get('abstract') or '',
        item.get('abstract_zh') or '',
        item.get('summary') or '',
        item.get('journal') or '',
        item.get('source_url') or '',
        item.get('arxiv_category') or '',
    ]
    return _normalize_text(' '.join(parts))


def _item_title_focus_text(item: Mapping[str, Any]) -> str:
    parts = [
        item.get('title') or item.get('title_en') or '',
        item.get('title_zh') or '',
        item.get('journal') or '',
        item.get('arxiv_category') or '',
    ]
    return _normalize_text(' '.join(parts))


@lru_cache(maxsize=64)
def _whole_term_parts(terms: Tuple[str, ...]) -> Tuple[Optional[re.Pattern], Tuple[str, ...]]:
    """把整词词表拆成 (ASCII 整词正则, 需要按子串匹配的非 ASCII 词)。

    按“词表内容”做 key（不是 id(terms)：对象回收后 id 会被复用，那会命中错误的正则）。
    三个否定词表加起来 133 个词，原来每次调用都要跑 133 次全文 re.search；
    预编译成一条 alternation 后只跑一次，analyze_focus 热路径快 6 倍以上。
    """
    ascii_terms = [t for t in terms if t and not any(ord(ch) > 127 for ch in t)]
    other = tuple(t for t in terms if t and any(ord(ch) > 127 for ch in t))
    # 空词表绝不能编译成 (?:) —— 那会在任意标点边界处匹配成功，把整份词表变成“全命中”。
    pattern = re.compile(
        r'(?<![a-z0-9])(?:' + '|'.join(re.escape(t) for t in ascii_terms) + r')(?![a-z0-9])'
    ) if ascii_terms else None
    return pattern, other


def _has_any(text: str, terms: Sequence[str], *, whole_term: bool = False) -> bool:
    if not whole_term:
        for term in terms:
            if term and term in text:
                return True
        return False
    # alternation 在每个起点都会把所有分支试一遍，所以布尔结果与逐词 re.search 完全等价。
    pattern, other = _whole_term_parts(terms if isinstance(terms, tuple) else tuple(terms))
    for term in other:
        if term in text:
            return True
    return bool(pattern and pattern.search(text))


@lru_cache(maxsize=4096)
def _analyze_focus_signals(text: str, journal: str, arxiv_category: str) -> Dict[str, bool]:
    """信号计算内核：只依赖这三个派生字符串，所以可以安全地按文本缓存。

    collect_daily_articles 一轮里，同一篇文章会被 filter_focus_items / focus_priority /
    topic_bucket / is_daily_focus 反复问同样的问题（实测 ~4 次/篇）。
    这里用文本做 key，而不是 id(item)：条目被就地改写(zh/focus 富化)时 key 自然失效，
    也不需要任何调用方负责清缓存；id() 则会在 dict 被回收后复用，串到别的文章上。
    """
    has_ai = _has_any(text, AI_TERMS)
    has_physics = _has_any(text, PHYSICS_TERMS)
    has_chemistry = _has_any(text, CHEMISTRY_TERMS)
    has_materials = _has_any(text, MATERIALS_TERMS)
    has_simulation = _has_any(text, SIMULATION_TERMS)

    strong_physics = _has_any(text, PHYSICS_CORE_TERMS)
    strong_chemistry = _has_any(text, CHEMISTRY_CORE_TERMS)
    strong_materials = _has_any(text, MATERIALS_CORE_TERMS)
    strong_simulation = _has_any(text, SIMULATION_CORE_TERMS)

    curated_journal = _has_any(journal, CURATED_JOURNAL_HINTS)
    arxiv_physical = _has_any(arxiv_category, ALLOWED_ARXIV_PHYSICAL)
    arxiv_ai = _has_any(arxiv_category, ALLOWED_ARXIV_AI)

    clinical_biomed = _has_any(text, NEGATIVE_CLINICAL_TERMS, whole_term=True)
    life_science = clinical_biomed or _has_any(text, NEGATIVE_LIFE_SCIENCE_TERMS, whole_term=True)
    social = _has_any(text, NEGATIVE_SOCIAL_TERMS, whole_term=True)

    direct_science = strong_physics or strong_chemistry or strong_materials or strong_simulation or (arxiv_physical and (has_physics or has_chemistry or has_materials or strong_simulation))
    journal_supported = curated_journal and (strong_physics or strong_chemistry or strong_materials or strong_simulation)

    hard_offtopic = social or clinical_biomed or (life_science and not (strong_physics or strong_materials or strong_simulation))
    target_domain = not hard_offtopic and (direct_science or journal_supported or (arxiv_ai and direct_science))
    ai_science = not hard_offtopic and has_ai and (direct_science or journal_supported)

    return {
        'has_ai': has_ai,
        'has_physics': has_physics,
        'has_chemistry': has_chemistry,
        'has_materials': has_materials,
        'has_simulation': has_simulation,
        'strong_physics': strong_physics,
        'strong_chemistry': strong_chemistry,
        'strong_materials': strong_materials,
        'strong_simulation': strong_simulation,
        'curated_journal': curated_journal,
        'arxiv_physical': arxiv_physical,
        'arxiv_ai': arxiv_ai,
        'clinical_biomed': clinical_biomed,
        'life_science': life_science,
        'social': social,
        'direct_science': direct_science,
        'hard_offtopic': hard_offtopic,
        'target_domain': target_domain,
        'ai_science': ai_science,
    }


def analyze_focus(item: Mapping[str, Any]) -> Dict[str, bool]:
    # 返回副本：缓存里的 dict 绝不外泄，免得调用方就地改写后污染后续所有判定。
    return dict(_analyze_focus_signals(
        _item_text(item),
        _normalize_text(item.get('journal') or ''),
        _normalize_text(item.get('arxiv_category') or ''),
    ))


def is_target_domain(item: Mapping[str, Any]) -> bool:
    return analyze_focus(item)['target_domain']


def is_hard_offtopic(item: Mapping[str, Any]) -> bool:
    return analyze_focus(item)['hard_offtopic']


def filter_focus_items(items: Iterable[Mapping[str, Any]]) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    kept: List[Mapping[str, Any]] = []
    dropped: List[Mapping[str, Any]] = []
    from focus_core import priority_tier
    for item in items:
        if priority_tier(item) == 0 or is_target_domain(item):
            kept.append(item)
        else:
            dropped.append(item)
    return kept, dropped


# 1区期刊列表（用于日报筛选）
TIER1_JOURNAL_HINTS: Tuple[str, ...] = (
    'phys. rev. lett', 'phys. rev. x', 'phys. rev. b', 'phys. rev. materials', 'phys. rev. applied',
    'physical review letters', 'physical review x', 'physical review b', 'physical review materials', 'physical review applied',
    'j. am. chem. soc', 'journal of the american chemical society', 'jacs',
    'nano lett', 'nano letters',
    'j. phys. chem. lett', 'journal of physical chemistry letters', 'jpcl',
    'j. chem. theory comput', 'journal of chemical theory and computation', 'jctc',
    'advanced materials', 'advanced science', 'materials today',
    'npj comput', 'npj quantum', 'nature materials', 'nature physics',
    'nature chemistry', 'nature electronics', 'nature energy', 'science advances',
    'computer physics communications', 'computational materials science', 'digital discovery',
    'nature machine intelligence', 'nature', 'science',
)

# 2区期刊列表（仅保留指定）
TIER2_JOURNAL_HINTS: Tuple[str, ...] = (
    'j. appl. phys', 'journal of applied physics',  # JAP
    'chem. phys. lett', 'chemical physics letters',  # CPL
)


# ========== 日报第三层筛选：标题关键词匹配（精简版）==========

# 日报标题AI关键词（用户指定）
DAILY_TITLE_AI_TERMS: Tuple[str, ...] = (
    'learning', 'neural', 'network',
)

# 日报标题物理关键词（用户指定）
DAILY_TITLE_PHYSICS_TERMS: Tuple[str, ...] = (
    'quantum', 'spin', 'magnetic', 'superconduct', 'moire', 'moiré',
    # 'magnet' 覆盖 magnet/magnetism/magnetization/magnetoresistance/paramagnetism…
    # 这一整族词；只写 'magnetic' 会把 145 篇磁学标题误判成 band 4。
    'altermagnet', 'ferro', 'magnet',
    # 'magent' 是作者常见拼写错误（antiferromagentic / altermagentic），一并保留以免漏检。
    'magent',
)

# 日报标题化学关键词（暂不启用）
DAILY_TITLE_CHEMISTRY_TERMS: Tuple[str, ...] = ()

# 日报标题材料关键词（暂不启用）
DAILY_TITLE_MATERIALS_TERMS: Tuple[str, ...] = ()

# 日报标题模拟关键词（暂不启用）
DAILY_TITLE_SIMULATION_TERMS: Tuple[str, ...] = ()


def is_daily_focus(item: Mapping[str, Any]) -> bool:
    """
    日报精选过滤 - 第三层
    必须同时满足：
    1. 属于目标领域（通过第二层过滤）
    2. 标题或摘要中包含 DAILY_TITLE_AI_TERMS + DAILY_TITLE_PHYSICS_TERMS 里的任一关键词
       （learning, neural, network, quantum, spin, magnetic, superconduct, moire,
       altermagnet, ferro, magnet）
    """
    signals = analyze_focus(item)
    
    # 条件1: 必须属于目标领域
    if not signals['target_domain']:
        return False
    
    # 检查标题和摘要
    title_text = _item_title_focus_text(item)
    abstract_text = (item.get('abstract') or item.get('summary') or '').lower()
    combined_text = title_text + ' ' + abstract_text
    
    # 所有关键词（标题或摘要包含任一即可）。
    # 直接引用上面两个词表，不再抄一份字面量：抄本曾经和词表各自漂移，
    # 结果 'magent' 拼写错误在两处都躺了很久，把磁学文献挡在日报门外。
    all_keywords = DAILY_TITLE_AI_TERMS + DAILY_TITLE_PHYSICS_TERMS

    return _has_any(combined_text, all_keywords)


def daily_focus_priority(item: Mapping[str, Any]) -> tuple:
    """
    日报优先级排序
    Band 0: 标题同时包含AI词+核心科学词（最强匹配）
    Band 1: 标题仅含AI词
    Band 2: 标题仅含物理/化学/材料词
    Band 3: 标题仅含模拟词
    Band 4: 其他符合条件的文章
    """
    # 分档只看标题，不要在这里调 analyze_focus：它扫的是全文，
    # 结果也用不上，纯粹是排序键上的白花钱（120 天同步里约 40 秒）。
    title_text = _item_title_focus_text(item)
    
    # 标题关键词检测
    title_has_ai = _has_any(title_text, DAILY_TITLE_AI_TERMS)
    title_has_physics = _has_any(title_text, DAILY_TITLE_PHYSICS_TERMS)
    title_has_chemistry = _has_any(title_text, DAILY_TITLE_CHEMISTRY_TERMS)
    title_has_materials = _has_any(title_text, DAILY_TITLE_MATERIALS_TERMS)
    title_has_simulation = _has_any(title_text, DAILY_TITLE_SIMULATION_TERMS)
    
    title_core_science = title_has_physics or title_has_chemistry or title_has_materials or title_has_simulation
    
    # Band 0: 标题AI + 核心科学（最强匹配）
    if title_has_ai and title_core_science:
        band = 0
    # Band 1: 仅AI词
    elif title_has_ai:
        band = 1
    # Band 2: 仅物理/化学/材料词
    elif title_has_physics or title_has_chemistry or title_has_materials:
        band = 2
    # Band 3: 仅模拟词
    elif title_has_simulation:
        band = 3
    # Band 4: 其他
    else:
        band = 4
    
    return (band,)


def filter_daily_focus_items(
    items: Iterable[Mapping[str, Any]],
    *,
    min_keep: int = 12,
    max_keep: int = 60,
) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    """筛选日报文献：只保留满足标题关键词组合的文章。

    min_keep 是“期望下限”而不是保证：这里**故意不补位**。能补的只剩被
    priority_tier/is_daily_focus 判为非目标领域的文章（含临床/生命科学等硬负样本），
    为了凑够 12 篇把它们塞进日报，比出一份短日报糟糕得多。
    低产日（假期、feed 故障）就让它短，但必须让人看见 —— 所以这里只打日志。
    """
    from focus_core import priority_tier
    # P1 是明确研究主线，即使旧关键词组合未覆盖也必须进入日报候选。
    eligible = [item for item in items if priority_tier(item) == 0 or is_daily_focus(item)]

    # 按优先级排序
    eligible = sorted(eligible, key=lambda item: (priority_tier(item), daily_focus_priority(item)))

    def item_key(item: Mapping[str, Any]) -> str:
        return str(item.get('link') or item.get('title') or item.get('title_en') or item.get('title_zh') or '')

    selected: List[Mapping[str, Any]] = []
    selected_keys = set()

    def add(item: Mapping[str, Any]) -> None:
        key = item_key(item)
        if not key or key in selected_keys or len(selected) >= max_keep:
            return
        selected.append(item)
        selected_keys.add(key)

    # eligible 已经通过 is_daily_focus 筛选，直接添加到 selected
    for item in eligible:
        add(item)

    dropped = [item for item in items if item_key(item) not in selected_keys]

    # 数量不足不算失败，不改结果，只把“今天为什么这么短”写进日志。
    try:
        floor = int(min_keep or 0)
    except (TypeError, ValueError):
        floor = 0
    if floor > 0 and len(selected) < floor:
        print(f"⚠️ 日报候选不足: 仅 {len(selected)}/{floor} 篇通过日报关键词门槛"
              f"(另有 {len(dropped)} 篇未通过); 不补位以免混入非目标领域文献")

    return selected, dropped


def topic_bucket_from_signals(signals: Mapping[str, bool]) -> str:
    """已经算好 signals 时走这条，别再让 topic_bucket 把全文重扫一遍。"""
    if signals['strong_physics'] or signals['has_physics']:
        return 'physics'
    if signals['strong_chemistry'] or signals['has_chemistry']:
        return 'chemistry'
    if signals['strong_materials'] or signals['has_materials']:
        return 'materials'
    if signals['has_ai'] or signals['strong_simulation'] or signals['has_simulation']:
        return 'methods'
    return 'other'


def topic_bucket(item: Mapping[str, Any]) -> str:
    return topic_bucket_from_signals(analyze_focus(item))


def focus_priority(item: Mapping[str, Any]) -> tuple:
    from focus_core import core_score  # lazy import to avoid circular deps
    signals = analyze_focus(item)
    bucket = topic_bucket_from_signals(signals)  # 复用上面的 signals，不重复扫全文
    bucket_rank = {
        'physics': 0,
        'chemistry': 1,
        'materials': 2,
        'methods': 3,
        'other': 4,
    }.get(bucket, 5)
    try:
        ai_score = float(item.get('ai_score') or 0)
    except Exception:
        ai_score = 0.0
    try:
        focus_score = float(item.get('focus_score') or 0)
    except Exception:
        focus_score = 0.0
    journal = _normalize_text(item.get('journal') or '')
    is_arxiv = journal == 'arxiv'
    direct_bonus = 0 if signals['direct_science'] else 1
    cscore = core_score(item)  # 0.0 iff not core — derive flag from score, avoids 2× full-text scan
    return (
        0 if cscore > 0.0 else 1,   # 核心关注永远置顶（必须是首位键，否则该保证失效）
        -focus_score,               # 层内再按五位学者画像相关度排序
        -cscore,                    # 核心分数高者靠前
        0 if signals['ai_science'] else 1,
        direct_bonus,
        bucket_rank,
        0 if is_arxiv else 1,
        -ai_score,
        _normalize_text(item.get('title') or item.get('title_zh') or ''),
    )
