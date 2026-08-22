#!/usr/bin/env python3
"""Core-focus classifier: ML × ferro/凝聚态 方向判定，纯函数，无外部依赖。"""

from typing import Any, Dict, Mapping, Optional, Tuple

# —— 方法侧（AI/ML/势函数/哈密顿量）——
CORE_METHOD_TERMS: Tuple[str, ...] = (
    # 机器学习主流术语
    "machine learning", "deep learning", "neural network", "neural networks",
    "graph neural", "gnn", "transformer", "diffusion model", "generative model",
    "foundation model", "large language model", "llm", "reinforcement learning",
    "active learning", "surrogate model", "data-driven", "ai-driven",
    "artificial intelligence", "message passing", "equivariant neural",
    "equivariant gnn", "equivariant network",
    # 势函数 / Hamiltonian
    "ml potential", "mlip", "interatomic potential", "neural network potential",
    "nnp", "ml hamiltonian", "learnable hamiltonian",
    "equivariant force field", "mace", "nequip", "allegro", "schnet",
    # 中文
    "机器学习", "深度学习", "神经网络", "大语言模型", "人工智能",
    "神经网络势", "机器学习势",
)

# —— ferro/磁/凝聚态侧 ——
CORE_FERRO_TERMS: Tuple[str, ...] = (
    "ferroelectric", "ferromagnet", "ferromagnetic", "antiferromagnet",
    "antiferromagnetic", "altermagnet", "altermagnetic", "multiferroic",
    "piezoelectric", "magnetoelectric", "skyrmion", "magnon", "spin hall",
    "moire magnet", "moiré magnet", "spintronic", "spintronics",
    "spin current", "topological magnon", "spin wave", "spin texture",
    "magnetic order", "magnetic anisotropy", "exchange interaction",
    # 中文
    "铁电", "铁磁", "反铁磁", "交错磁", "多铁", "压电", "磁电",
    "斯格明子", "磁振子", "自旋霍尔", "自旋流", "磁性", "拓扑磁",
    "自旋波", "磁各向异性", "交换相互作用",
)

# Highest-priority research terms.  This is the single source used by sorting,
# grouping, visualisations and poster backfill.
PRIORITY_TERMS: Tuple[str, ...] = (
    "neural network potential", "machine learning potential", "ml potential", "mlip",
    "interatomic potential", "ml hamiltonian", "learnable hamiltonian",
    "density matrix", "charge density", "electron density", "electronic structure",
    "wavefunction", "dft hamiltonian", "equivariant",
    "神经网络势", "机器学习势", "原子间势", "哈密顿量", "密度矩阵",
    "电荷密度", "电子密度", "电子结构", "波函数",
)

PRIORITY_FERRO_TERMS: Tuple[str, ...] = (
    "ferroelectric", "ferromagnet", "antiferromagnet", "multiferroic",
    "ferroelectricity", "ferromagnetic", "antiferromagnetic", "magnetic", "magnetism",
    "铁电", "铁磁", "反铁磁", "多铁", "磁性",
)

# ── Taxonomy: tiered research-focus categories ────────────────────────────────
TAXONOMY: Dict[str, Dict[str, Any]] = {
    "AI×物理": {
        "terms": ["machine learning", "deep learning", "neural network", "graph neural",
                  "transformer", "generative model", "diffusion model", "foundation model",
                  "ml interatomic", "neural network potential", "ml potential",
                  "physics-informed", "scientific machine learning"],
        "domain": ["physics", "quantum", "phase transition", "hamiltonian", "spin",
                   "lattice", "electronic structure", "dft"],
        "tier": 1,
    },
    "AI×化学·材料": {
        "terms": ["machine learning", "deep learning", "neural network", "graph neural",
                  "generative model", "diffusion model", "foundation model",
                  "ml interatomic", "neural network potential", "ml potential",
                  "active learning", "bayesian optimization"],
        "domain": ["material", "chemistry", "molecule", "catalyst", "crystal",
                   "perovskite", "alloy", "polymer", "battery", "synthesis"],
        "tier": 1,
    },
    "磁性·自旋电子学": {"terms": ["magnet", "magnetism", "spintronic", "antiferromagnet",
                          "ferromagnet", "altermagnet", "spin current", "spin orbit",
                          "magnon", "skyrmion"], "tier": 2},
    "铁电·极化": {"terms": ["ferroelectric", "polarization", "piezoelectric",
                       "multiferroic", "dielectric"], "tier": 2},
    "拓扑·电子结构": {"terms": ["topological", "weyl", "dirac", "band structure",
                         "berry phase", "chern", "quantum hall"], "tier": 2},
    "超导": {"terms": ["superconduct", "cooper pair", "bcs", "meissner"], "tier": 2},
    "量子信息·计算": {"terms": ["qubit", "quantum computing", "quantum information",
                         "entanglement", "quantum error", "quantum circuit"], "tier": 2},
    "软物质·流体·统计": {"terms": ["soft matter", "fluid", "turbulence", "statistical mechanics",
                          "active matter", "colloid", "granular"], "tier": 3},
    "其他凝聚态": {"terms": ["condensed matter", "phonon", "thermal transport",
                       "2d material", "graphene"], "tier": 3},
}

_TAXONOMY_TIER: Dict[str, int] = {cat: spec["tier"] for cat, spec in TAXONOMY.items()}


def _text(article: Any) -> str:
    """Return lowercased concatenated text from title/summary/abstract fields."""
    if not article:
        return ""
    return " ".join(
        str(article.get(k, "") or "")
        for k in ("title", "title_en", "title_zh", "summary", "abstract", "abstract_zh")
    ).lower()


def classify_taxonomy(article: Any) -> str:
    """Return the best-matching TAXONOMY category, or '其他' if none matches."""
    txt = _text(article)
    if not txt.strip():
        return "其他"
    best: Optional[str] = None
    best_tier = 99
    for cat, spec in TAXONOMY.items():
        terms_hit = any(t in txt for t in spec["terms"])
        domain_hit = ("domain" not in spec) or any(d in txt for d in spec["domain"])
        if terms_hit and domain_hit and spec["tier"] < best_tier:
            best, best_tier = cat, spec["tier"]
    return best or "其他"


# —— 高分期刊（用于 score 加成，不是判定必要条件）——
_CURATED_HIGH_HINTS: Tuple[str, ...] = (
    "nature", "science", "phys. rev. lett", "physical review letters",
    "phys. rev. x", "physical review x", "nature materials", "nature physics",
    "nature communications", "npj comput", "npj quantum",
    "j. am. chem. soc", "nano letters",
)


def _normalize(text: Any) -> str:
    return " ".join(str(text or "").replace("\xa0", " ").replace("\n", " ").split()).lower()


def _item_fulltext(item: Mapping[str, Any]) -> str:
    parts = [
        item.get("title") or item.get("title_en") or "",
        item.get("title_zh") or "",
        item.get("abstract") or "",
        item.get("abstract_zh") or "",
    ]
    return _normalize(" ".join(parts))


def _item_title_text(item: Mapping[str, Any]) -> str:
    return _normalize(
        " ".join([item.get("title") or item.get("title_en") or "", item.get("title_zh") or ""])
    )


def _has_any(text: str, terms: Tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def priority_tier(item: Mapping[str, Any]) -> int:
    """Return research priority: 0=P1, 2=P2, 3=all remaining work."""
    if not item:
        return 3
    text = _item_fulltext(item)
    if _has_any(text, PRIORITY_TERMS):
        return 0
    if _has_any(text, PRIORITY_FERRO_TERMS):
        return 2
    return 3


def is_core_focus(item: Mapping[str, Any]) -> bool:
    """核心关注 = 同时命中方法侧与 ferro/凝聚态侧，或命中 tier1/tier2 taxonomy。"""
    if not item:
        return False
    if priority_tier(item) == 0:
        return True
    text = _item_fulltext(item)
    # Original signal: ML method + ferro/condensed-matter
    if _has_any(text, CORE_METHOD_TERMS) and _has_any(text, CORE_FERRO_TERMS):
        return True
    # Taxonomy signal: tier1 or tier2 category
    cat = classify_taxonomy(item)
    return cat != "其他" and _TAXONOMY_TIER.get(cat, 99) <= 2


def core_score(item: Mapping[str, Any]) -> float:
    """0.0 ~ 1.0；未命中核心关注时返回 0.0。

    Scoring layers (additive):
      - Base 0.5 for core-focus hit
      - +0.15 if ML method term in title
      - +0.15 if ferro/condensed-matter term in title
      - +0.10 for Hamiltonian+磁/铁 combo in fulltext
      - +0.05 for cond-mat arXiv source
      - +0.05 for high-impact journal
      - Taxonomy tier bonus: tier1 +0.10, tier2 +0.05, tier3 +0.01
      - +0.20 for the decided P1 neural-potential/electronic-structure cluster
    """
    if not item:
        return 0.0
    if not is_core_focus(item):
        return 0.0
    title = _item_title_text(item)
    score = 0.5
    if priority_tier(item) == 0:
        score += 0.20
    if _has_any(title, CORE_METHOD_TERMS):
        score += 0.15
    if _has_any(title, CORE_FERRO_TERMS):
        score += 0.15
    text = _item_fulltext(item)
    # Hamiltonian + 磁/铁 组合加成
    if _has_any(text, ("ml hamiltonian", "learnable hamiltonian", "neural network potential", "ml potential", "mlip")) and _has_any(text, ("ferro", "magnet", "铁", "磁")):
        score += 0.10
    # arXiv cond-mat 加成
    src = _normalize(item.get("source_url") or item.get("arxiv_category") or "")
    if "cond-mat" in src:
        score += 0.05
    # 高分期刊加成
    journal = _normalize(item.get("journal") or "")
    if any(hint in journal for hint in _CURATED_HIGH_HINTS):
        score += 0.05
    # Taxonomy tier bonus
    cat = classify_taxonomy(item)
    tier = _TAXONOMY_TIER.get(cat, 99)
    if tier == 1:
        score += 0.10
    elif tier == 2:
        score += 0.05
    elif tier == 3:
        score += 0.01
    return min(1.0, round(score, 3))
