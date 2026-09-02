"""AI × 物理/材料/化学 交叉相关度：规则分层 + LLM 批量打分。

设计要点（与 focus_interest.py 保持同款形状，便于复用既有测试套路）：

- **规则层零成本**：`rule_cross_tier` 只读 `focus_filter.analyze_focus` 已经算好并带
  lru_cache 的信号，不新增任何全文扫描。`analyze_focus` 里的 `ai_science`
  （= 非硬负样本 ∧ 有 AI 词 ∧ 有实打实的科学侧）本来就是"AI×科学交叉"的定义，
  此前只是没有任何选文/排序路径读它。
- **打分永远可降级**：`effective_cross_score` 在没有 LLM 分时用规则层兜底，
  所以排序、分区、邮件在 provider 全挂的那天照常工作（沿用"AI 失败的那天
  也必须有可用日报"这条既定约束）。
- **fail-soft 铁律**：无 provider / 批次失败 → 跳过，绝不抛异常、绝不阻塞流水线。
"""

from __future__ import annotations

import json
import os
import time
from string import Template
from typing import Any, Dict, List, Mapping, Optional, Tuple

from focus_filter import (
    AI_TERMS,
    _has_any,
    _normalize_text,
    analyze_focus,
    focus_priority,
)

_CROSS_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "ai_prompts", "cross_relevance.txt")

# 每批文章数：与 focus_interest.FOCUS_BATCH_SIZE 一致（批量省请求，单批过大易截断）
CROSS_BATCH_SIZE = 8

# 规则层兜底分。LLM 没跑/跑挂时排序与分区都要照常工作：
#   tier0/tier1 给到 >= 默认阈值 6，保证"AI 全挂的那天交叉区仍然非空"；
#   tier2（纯物理/纯材料）明确低于阈值，落到「其他」区而不是被丢掉。
_RULE_FALLBACK_SCORE: Dict[int, float] = {0: 8.0, 1: 6.0, 2: 2.0, 3: 0.0}

# 交叉侧标签 → 判定该侧成立的 analyze_focus 信号名
_SIDE_SIGNALS: Tuple[Tuple[str, str], ...] = (
    ("simulation", "strong_simulation"),
    ("materials", "strong_materials"),
    ("chemistry", "strong_chemistry"),
    ("physics", "strong_physics"),
)

# 交叉判定专用的"科学侧标题词表"。**刻意不复用 focus_filter 的 *_CORE_TERMS**：
# 那几张表服务的是 target_domain（宽进），里面有 surface / interface / device /
# material 这类到处都是的裸词。实测 09-01 当天，多机器人控制、金融 PPO、
# 地理遥感、蛋白质工程四篇论文全是靠摘要里的 surface / interface / monte carlo
# 命中 direct_science，再叠加一个 learning 就被判成"AI×科学交叉"。
# 这里只收"出现在标题里就基本能确定研究对象"的词，并要求科学侧落在标题上。
# 同理排除 'finite element'（结构工程通用）与 'physics-informed'（属 AI 侧）。
# 交叉判定专用的"AI 侧标题词表"。以 focus_filter.AI_TERMS 为底，剔除在凝聚态语境
# 里会撞车的词：'diffusion model' 在物理里是"扩散模型"（energy diffusion model /
# anomalous diffusion model），跟生成式扩散模型毫无关系。实测 2026-08-31 的
# "Energy relaxation due to two-phonon scattering of electrons: Breakdown of the
# energy diffusion model" 就是靠它被判成标题级交叉，排进邮件主区第 8 张卡片。
# 生成式扩散模型改用不会撞车的写法匹配。
_AI_TERM_FALSE_FRIENDS: Tuple[str, ...] = ('diffusion model',)

_AI_TITLE_TERMS: Tuple[str, ...] = tuple(
    t for t in AI_TERMS if t not in _AI_TERM_FALSE_FRIENDS
) + (
    'denoising diffusion', 'diffusion generative', 'score-based generative',
    'equivariant', 'physics-informed', 'differentiable', 'autoencoder',
    'symbolic regression', 'gaussian process', 'kernel ridge', 'graph network',
    'operator learning', 'neural operator',
    '等变', '图神经网络', '代理模型', '主动学习',
)

_SCIENCE_TITLE_TERMS: Tuple[str, ...] = (
    # —— 物理 ——
    'quantum', 'spin', 'magnetic', 'magnetism', 'magnet', 'ferroelectric', 'ferromagnet',
    'antiferromagnet', 'multiferroic', 'altermagnet', 'superconduct', 'phonon', 'exciton',
    'polaron', 'moire', 'moiré', 'skyrmion', 'topological', 'weyl', 'magnon', 'hall effect',
    'condensed matter', 'lattice', 'domain wall', 'polarization',
    # —— 化学 ——
    'catalyst', 'catalysis', 'catalytic', 'electrochem', 'molecule', 'molecular',
    'reaction', 'spectroscopy', 'photochemistry', 'adsorption', 'solvation',
    'polymer', 'polymerization', 'ligand', 'chemical bond',
    # —— 材料 ——
    'perovskite', 'semiconductor', 'electrode', 'battery', 'electrolyte', 'alloy', 'oxide',
    'heterostructure', 'thin film', '2d material', '2d materials', 'monolayer', 'bilayer',
    'crystal', 'crystalline', 'nanostructure', 'dielectric', 'memristor', 'photovoltaic',
    'solar cell', 'metal-organic framework', 'mof', 'graphene', 'defect', 'grain boundary',
    # —— 计算方法（作为研究对象出现在标题时才算科学侧）——
    'dft', 'density functional', 'ab initio', 'first-principles', 'first principles',
    'molecular dynamics', 'monte carlo', 'phase field', 'interatomic potential',
    'electronic structure', 'band structure', 'hamiltonian', 'free energy', 'force field',
    'potential energy surface',
    # —— 中文 ——
    '量子', '自旋', '磁性', '铁电', '铁磁', '反铁磁', '多铁', '超导', '声子', '晶格',
    '拓扑', '畴壁', '极化', '催化', '电化学', '分子', '光谱', '吸附', '聚合物',
    '钙钛矿', '半导体', '电极', '电池', '电解质', '合金', '氧化物', '异质结构', '薄膜',
    '单层', '晶体', '介电', '缺陷', '晶界', '第一性原理', '分子动力学', '蒙特卡洛',
    '相场', '原子间势', '电子结构', '能带', '哈密顿量', '自由能', '势能面',
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip() or default)
    except (TypeError, ValueError):
        return default


def cross_min_score() -> float:
    """进入「AI×科学交叉」区的分数线（env CROSS_MIN_SCORE，默认 6）。"""
    return _env_float("CROSS_MIN_SCORE", 6.0)


def _title_text(item: Mapping[str, Any]) -> str:
    """只取标题本身，**不含刊名与 arXiv 分类**。

    focus_filter._item_title_focus_text 会把 journal/arxiv_category 拼进去（那对
    它的用途没问题），但用在交叉判定上会让刊名冒名顶替：
    "Machine Learning: Science and Technology" 上的任何论文都白拿一个 AI 信号，
    "Crystal Growth & Design" 白拿一个科学信号。实测 2026-08-31/09-01 两天，
    去掉刊名后交叉候选数一篇不差（36→36、32→32），纯粹是堵漏。
    """
    return _normalize_text(" ".join([
        item.get("title") or item.get("title_en") or "",
        item.get("title_zh") or "",
    ]))


def cross_signals(item: Mapping[str, Any]) -> Dict[str, Any]:
    """交叉判定用到的全部派生信号。纯函数，不修改输入。"""
    signals = analyze_focus(item)
    title_text = _title_text(item)
    ai_in_title = _has_any(title_text, _AI_TITLE_TERMS)
    science_in_title = _has_any(title_text, _SCIENCE_TITLE_TERMS)
    side = ""
    for label, key in _SIDE_SIGNALS:
        if signals.get(key):
            side = label
            break
    return {
        "has_ai": bool(signals.get("has_ai")),
        "ai_science": bool(signals.get("ai_science")),
        "target_domain": bool(signals.get("target_domain")),
        "hard_offtopic": bool(signals.get("hard_offtopic")),
        "ai_in_title": ai_in_title,
        "science_in_title": science_in_title,
        "side": side,
    }


def rule_cross_tier(item: Mapping[str, Any]) -> int:
    """零成本交叉分层：0 最强，3 最弱。

    0 —— AI 词与科学词同时出现在标题：最强交叉信号
    1 —— analyze_focus 的 ai_science 成立：全文层面的交叉
    2 —— 属目标领域但没有 AI 信号：纯物理 / 纯材料
    3 —— 其余（纯 ML/CS 理论、非目标领域、硬负样本）
    """
    sig = cross_signals(item)
    if sig["hard_offtopic"]:
        return 3
    if sig["ai_in_title"] and sig["science_in_title"]:
        return 0
    # ai_science 单独成立还不够：它的科学侧可能只是摘要里的 surface/interface。
    # 要求至少有一侧落在标题上，才认作全文层面的交叉。
    if sig["ai_science"] and (sig["ai_in_title"] or sig["science_in_title"]):
        return 1
    if sig["target_domain"] and not sig["has_ai"]:
        return 2
    return 3


def effective_cross_score(item: Mapping[str, Any]) -> float:
    """排序用的交叉分：优先 LLM 写入的 cross_score，缺失时退回规则兜底。

    排序键绝不能依赖"AI 已经跑过"——那样 provider 一挂当天就退化成随机序。
    """
    raw = item.get("cross_score")
    if isinstance(raw, bool):  # bool 是 int 的子类，先挡掉
        raw = None
    if isinstance(raw, (int, float)):
        return max(0.0, min(10.0, float(raw)))
    if isinstance(raw, str) and raw.strip():
        try:
            return max(0.0, min(10.0, float(raw.strip())))
        except ValueError:
            pass
    return _RULE_FALLBACK_SCORE.get(rule_cross_tier(item), 0.0)


def is_cross_item(item: Mapping[str, Any], min_score: Optional[float] = None) -> bool:
    """是否进「AI × 物理/材料/化学」主区。

    单一判据：effective_cross_score >= 阈值。有 LLM 分就以 LLM 为准（它看过摘要，
    信息比规则多）；没有分才用规则兜底。刻意**不给 tier0 开无条件后门**——
    "Benchmarking Quantum Feature Encoding" 这种标题里同时有 AI 和 quantum、
    实际是量子机器学习基准的论文，正该被一个低 LLM 分挡在主区之外。
    """
    threshold = cross_min_score() if min_score is None else float(min_score)
    return effective_cross_score(item) >= threshold


def cross_sort_key(item: Mapping[str, Any]) -> tuple:
    """交叉优先的排序键。

    同分时退回既有的 focus_priority（核心关注置顶 → 画像分 → core_score …），
    否则「其他」区里 153 篇规则兜底分全是 2.0，就只能按标题字母排，
    会把水泥电池、地下水 GIS 顶到最前面。
    """
    return (-effective_cross_score(item), rule_cross_tier(item), focus_priority(item))


# ---------------------------------------------------------------- LLM 打分层

def _extract_json(text: str) -> Any:
    import re

    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise ValueError("No JSON object found")
    return json.loads(m.group())


def _load_prompt_template() -> Template:
    with open(_CROSS_PROMPT_PATH, encoding="utf-8") as f:
        return Template(f.read())


def _build_batch_prompt(batch: List[Dict[str, Any]]) -> str:
    lines = []
    for i, a in enumerate(batch, 1):
        title = (a.get("title") or a.get("title_en") or "").strip()
        journal = (a.get("journal") or "").strip()
        abstract = (a.get("abstract") or "").strip()[:1200]
        lines.append(f"[{i}] Title: {title}\nJournal: {journal}\nAbstract: {abstract}\n")
    return _load_prompt_template().safe_substitute(articles="\n".join(lines))


_VALID_SIDES = {"physics", "chemistry", "materials", "simulation"}


def _parse_items(data: Any) -> Dict[int, Dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("Unexpected JSON schema")

    mapping: Dict[int, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        try:
            score = int(float(item.get("cross_score", 0) or 0))
        except (TypeError, ValueError):
            score = 0
        side = str(item.get("cross_side", "") or "").strip().lower()
        mapping[idx] = {
            "cross_score": max(0, min(10, score)),
            "cross_reason": str(item.get("cross_reason", "") or "").strip()[:120],
            "cross_side": side if side in _VALID_SIDES else "",
        }
    return mapping


def analyze_cross_batch(
    articles: List[Dict[str, Any]],
    provider: Any,
    batch_size: int = CROSS_BATCH_SIZE,
) -> Dict[int, Dict[str, Any]]:
    """批量 LLM 打分 → {输入列表下标(0基): 结果 dict}。

    批次失败只打印 ⚠️，对应文章不出现在返回值中（留待下次再被预筛命中时重试），
    与 focus_interest.analyze_focus_batch 完全同款。
    """
    results: Dict[int, Dict[str, Any]] = {}
    for start in range(0, len(articles), batch_size):
        batch = articles[start : start + batch_size]
        try:
            text = provider.call_api(_build_batch_prompt(batch))
            mapping = _parse_items(_extract_json(text))
        except Exception as e:
            print(f"⚠️ 交叉相关度批量打分失败(批次 {start // batch_size + 1}): {e}")
            mapping = {}

        for i in range(1, len(batch) + 1):
            item = mapping.get(i)
            if item is not None:
                results[start + i - 1] = item

        time.sleep(0.2)
    return results


def enrich_cross_relevance(
    articles: List[Dict[str, Any]],
    provider: Any = None,
    max_items: Optional[int] = None,
) -> int:
    """编排入口：规则预筛 + LLM 批量打分，就地写入 cross_* 三个字段。

    只给 rule_cross_tier <= 1 的条目花钱——纯物理/纯材料本来就归「其他」区，
    不需要一个 LLM 分来确认。幂等：已有 cross_score 的跳过。
    provider 为 None → 打印 ⚠️ 返回 0（fail-soft，排序仍走规则兜底）。
    """
    if provider is None:
        print("⚠️ 未配置 AI provider，跳过 AI×科学 交叉打分（排序退回规则分层）")
        return 0

    if max_items is None:
        max_items = _env_int("AI_CROSS_DAILY_MAX", 60)

    pending = [
        a for a in articles
        if isinstance(a, dict) and "cross_score" not in a and rule_cross_tier(a) <= 1
    ]
    pending.sort(key=cross_sort_key)
    if max_items and max_items > 0:
        pending = pending[:max_items]
    if not pending:
        return 0

    results = analyze_cross_batch(pending, provider)
    enriched = 0
    for idx, item in results.items():
        a = pending[idx]
        a["cross_score"] = item["cross_score"]
        a["cross_reason"] = item["cross_reason"]
        if item["cross_side"]:
            a["cross_side"] = item["cross_side"]
        elif not a.get("cross_side"):
            a["cross_side"] = cross_signals(a)["side"]
        enriched += 1
    return enriched


def split_cross_sections(
    items: List[Dict[str, Any]],
    min_score: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按交叉与否切成 (主区, 其他区)，两边各自按交叉分排序。绝不丢条目。"""
    cross, other = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        (cross if is_cross_item(item, min_score) else other).append(item)
    cross.sort(key=cross_sort_key)
    other.sort(key=cross_sort_key)
    return cross, other
