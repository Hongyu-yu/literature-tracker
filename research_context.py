"""研究方向关联与日报规则化保底文本。

该模块不调用网络或模型：AI 正常返回和 fallback 都使用同一组字段补齐逻辑，
避免日报因单次网关故障退化成只有标题的空卡片。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List


DEFAULT_OUR_WORK = (
    "团队聚焦计算凝聚态物理、功能材料设计与人工智能材料模拟，研究铁电/多铁、"
    "磁性与自旋材料、缺陷、畴壁、有限温度动力学以及机器学习势和图神经网络。"
)

METHOD_HINTS = (
    ("machine learning potential", "机器学习势与原子级动力学"),
    ("neural network potential", "神经网络原子间势"),
    ("graph neural", "图神经网络结构—性质建模"),
    ("equivariant", "等变神经网络与对称性保持表示"),
    ("active learning", "主动学习和高通量采样"),
    ("molecular dynamics", "分子动力学与有限温度轨迹"),
    ("first-principles", "第一性原理标注与结构优化"),
    ("density functional", "密度泛函理论和电子结构计算"),
    ("hamiltonian", "有效哈密顿量或机器学习哈密顿量"),
    ("monte carlo", "蒙特卡洛统计采样"),
    ("reinforcement learning", "强化学习和闭环控制"),
    ("thermodynamic", "热力学自由能、相稳定与有限温度建模"),
    ("free energy", "自由能面和热力学积分/采样"),
    ("phase stability", "相稳定、相图与多相竞争"),
    ("calphad", "CALPHAD 约束的相图与材料信息学"),
    ("cluster expansion", "团簇展开与构型统计"),
    ("nonadiabatic", "非绝热分子动力学与载流子弛豫"),
    ("electron-phonon", "电子—声子耦合与输运"),
    ("wannier", "Wannier 插值和有效电子模型"),
    ("gw-bse", "GW-BSE 激发态与光学响应"),
)

DOMAIN_HINTS = (
    (("ferroelectric", "polarization", "piezoelectric", "multiferroic", "ferro"),
     "铁电/多铁、极化翻转和电场驱动过程"),
    (("magnetic", "magnetism", "ferromagnet", "antiferromagnet", "spin", "magnon"),
     "磁性、自旋以及自旋—晶格耦合"),
    (("defect", "vacancy", "dop", "domain wall", "switching"),
     "缺陷、畴壁、成核和开关动力学"),
    (("phonon", "thermal", "finite temperature", "transport"),
     "声子、有限温度和输运性质"),
    (("crystal", "material", "perovskite", "oxide", "semiconductor"),
     "晶体结构、功能材料和结构—性质关系"),
    (("hfo2", "hafnia", "hfo"), "HfO₂ 基铁电体、氧空位和极化翻转"),
    (("sliding ferroelectric", "van der waals ferroelectric"), "二维滑移铁电、层间堆垛和畴壁"),
    (("charged defect", "oxygen vacancy", "defect formation", "nonradiative", "trap state"), "带电缺陷、缺陷形成能、陷阱态和器件退化"),
    (("polaron", "carrier lifetime", "hot carrier"), "极化子、热载流子和非绝热载流子动力学"),
    (("dzyaloshinskii", "kitaev", "altermagnet", "noncollinear"), "非共线磁性、DMI/Kitaev 相互作用和交替磁性"),
)

TOPIC_PLANS = {
    "thermo": {"terms": ("thermodynamic", "free energy", "phase stability", "calphad", "entropy", "finite temperature", "phase diagram"), "anchor": "热力学知情 ML、机器学习势、自由能采样、CALPHAD/团簇展开", "materials": "HfO₂ 基铁电、二维滑移铁电、钙钛矿和磁性氧化物", "workflow": "在 DFT 能量/力/应力之外加入跨温度构型、相对自由能、熵或热膨胀信息；用主动学习补采相变、畴壁和相竞争区域，再用热力学积分、MC 或有限温度 MD 验证"},
    "ferro": {"terms": ("ferroelectric", "polarization", "multiferroic", "hfo2", "hafnia", "switching", "domain wall"), "anchor": "HfO₂ 铁电、二维滑移铁电、极化翻转、畴壁和疲劳/缺陷问题", "materials": "HfO₂、Hf₀.₅Zr₀.₅O₂、二维范德华铁电、钙钛矿氧化物", "workflow": "把电场、极化、应变、氧空位和局域结构序参一起纳入轨迹数据；比较均匀翻转、局域成核和畴壁传播，并用 DFT/有效 Hamiltonian 对关键路径复核"},
    "magnetic": {"terms": ("magnetic", "magnetism", "ferromagnet", "antiferromagnet", "spin", "magnon", "kitaev", "altermagnet", "dzyaloshinskii", "noncollinear"), "anchor": "磁性机器学习势、自旋 Hamiltonian、自旋—晶格动力学和非共线磁结构", "materials": "CrI₃/CrSBr 等范德华磁体、反铁磁/交替磁材料、磁性氧化物", "workflow": "训练集同时覆盖原子位移和自旋构型，显式标注交换、各向异性、DMI 或自旋力；用自旋 MD/MC 与 DFT 自旋能差验证温度、应变和缺陷下的磁序稳定性"},
    "defect": {"terms": ("defect", "vacancy", "charged defect", "oxygen vacancy", "dop", "trap", "nonradiative", "grain boundary"), "anchor": "机器学习 Hamiltonian、带电缺陷形成能、缺陷扩散和非辐射复合", "materials": "SiO₂、SiC、GaN、卤化物钙钛矿、HfO₂ 氧空位和二维半导体", "workflow": "把电荷态、有限尺寸修正、局域配位和缺陷迁移路径纳入标注；用小超胞 DFT+U/GW 或缺陷形成能基准校准，再扩展到大超胞和有限温度扩散"},
    "carrier": {"terms": ("nonadiabatic", "electron-phonon", "phonon", "transport", "polaron", "carrier", "hot carrier", "wannier", "gw-bse"), "anchor": "非绝热分子动力学、电子—声子耦合、Wannier/GW-BSE 和载流子输运", "materials": "卤化物钙钛矿、二维半导体、FeSe、光电界面和能源材料", "workflow": "先用高精度电子结构和声子/电子—声子矩阵元建立小规模基准，再训练可迁移 Hamiltonian 或 ML 势，最后在长时间尺度非绝热 MD、输运或界面电荷转移中验证"},
    "structure": {"terms": ("crystal structure prediction", "genetic algorithm", "structure search", "high-throughput", "materials discovery"), "anchor": "晶体结构搜索、高通量筛选和材料信息学", "materials": "新型铁电、磁性、钙钛矿、二维和能源材料候选", "workflow": "用 ML 代理能量和不确定性缩小结构搜索，再对候选做对称性、动力学稳定性、极化/磁序和有限温度筛选，避免只按 0 K 单点能排序"},
}

_DEGRADED_SUMMARY_FRAGMENTS = ("摘要信息不足", "需查阅原文确认具体方法与结论")


def _usable_summary(value: Any) -> str:
    text = str(value or "").strip()
    return "" if any(fragment in text for fragment in _DEGRADED_SUMMARY_FRAGMENTS) else text


def load_research_profile(path: str = "data/focus_interests.json") -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"our_work_zh": DEFAULT_OUR_WORK, "keywords": []}


def profile_direction_digest(profile: Dict[str, Any] | None = None, max_chars: int = 5200) -> str:
    """压缩五位研究人员的方向画像，避免 prompt 塞入全部 Scholar 摘要。"""
    profile = profile or {}
    parts = [str(profile.get("our_work_zh") or DEFAULT_OUR_WORK).strip()]
    for scholar in profile.get("scholars", []) or []:
        name = str(scholar.get("name") or "研究团队成员").strip()
        direction = str(scholar.get("directions_zh") or "").strip()
        if direction:
            parts.append(f"{name}：{direction}")
    digest = "\n".join(parts)
    return digest if len(digest) <= max_chars else digest[:max_chars].rstrip() + "…"


def _text(item: Dict[str, Any]) -> str:
    return " ".join(str(item.get(k) or "") for k in ("title_en", "title", "abstract", "abstract_zh", "summary")).lower()


def _matches(text: str, terms: Iterable[str]) -> bool:
    return any(term in text for term in terms)


def _method_topics(text: str) -> List[str]:
    return [label for term, label in METHOD_HINTS if term in text]


def _domain_topics(text: str) -> List[str]:
    return [label for terms, label in DOMAIN_HINTS if _matches(text, terms)]


def _topic_plans(text: str) -> List[Dict[str, str]]:
    return [plan for plan in TOPIC_PLANS.values() if _matches(text, plan["terms"])]


def build_relation_fields(item: Dict[str, Any], profile: Dict[str, Any] | None = None) -> Dict[str, str]:
    """为单篇文献生成保守、可审计的三段研究关联文本。"""
    profile = profile or {}
    title = str(item.get("title_en") or item.get("title") or "").strip()
    abstract = str(item.get("abstract") or item.get("abstract_zh_full") or item.get("abstract_zh") or "").strip()
    text = f"{title} {abstract}".lower()
    methods = _method_topics(text)
    domains = _domain_topics(text)
    plans = _topic_plans(text)
    title_label = str(item.get("title_zh") or title).strip()
    if not methods:
        methods = ["论文题目和摘要中可确认的方法"]
    if not domains:
        domains = ["AI for science 与材料/物理问题的结构化分析"]
    if plans:
        plan = plans[0]
        method_text = (
            f"方法上，这篇工作可归入{plan['anchor']}。从摘要能确认的线索包括："
            f"{', '.join(methods[:3])}，以及{', '.join(domains[:2])}对应的结构或物理量。"
            f"若迁移到团队流程，应采用“{plan['workflow']}”的分层验证，而不是只比较单点能量或单一回归误差。"
        )
        related_text = (
            f"它与五位研究人员已有工作的直接连接是：{plan['anchor']}已经出现在团队的方向画像中，"
            f"可对应到{plan['materials']}。与传统只做 0 K formation energy/静态势垒的工作相比，"
            f"这里更应关注{', '.join(domains[:3])}在温度、缺陷、应变或外场下的变化；摘要没有给出的模型细节不作延伸判断。"
        )
        implication_text = (
            f"对当前研究最具体的启示不是泛泛地“使用 ML”，而是把该文的{plan['anchor']}嵌入现有材料模拟链："
            f"先在{plan['materials']}中选择一个已有 DFT 数据基础的体系，按“{plan['workflow']}”补充训练/验证集，"
            f"再比较《{title_label}》对应模型对相稳定、极化/磁序、缺陷或动力学指标的改进。若结果只在训练分布内有效，应通过跨温度、跨应变、跨缺陷浓度和跨超胞测试检查可迁移性。"
        )
    else:
        method_text = "方法上，摘要目前只能确认：" + "、".join(methods[:3]) + "。需要回到原文核对数据来源、标签定义、模型架构、物理约束和验证集，不能把题名直接等同于可迁移算法。若要接入团队的 ML 势或 Hamiltonian 流程，应先确认它是否同时学习能量、力、应力、自旋、极化或电子结构，以及是否报告跨材料/跨温度外推。"
        related_text = "与团队方向的可能连接在于：" + "、".join(domains[:3]) + "。团队已有机器学习势、第一性原理、有效 Hamiltonian、缺陷计算、电子—声子耦合和有限温度动力学基础，但该文是否提供可复用数据或物理量仍需查证。若研究对象属于机器人、量子信息或电网等非材料场景，关联主要是物理约束学习、少样本建模或不确定性评估，而不是直接迁移材料机制。"
        implication_text = f"对《{title_label}》的启示是先做可证伪的对接：从一个已有材料体系和小规模 DFT 基准开始，明确输入、目标、基线和外推测试，再决定是否接入铁电/磁性、缺陷或载流子动力学工作。具体可先把该文的表示或损失函数在 HfO₂、二维滑移铁电、CrI₃/CrSBr、SiC/GaN 缺陷或卤化物钙钛矿上做小规模复现；若没有直接交叉，应保留为方法参考而非强行迁移。"
    if not item.get("abstract") and not item.get("abstract_zh_full") and not item.get("abstract_zh"):
        method_text = "源数据只有题名或出版元数据，无法确认具体模型、训练设置或定量结论；需查阅原文后再纳入方法基准。"
        related_text = "仅凭现有元数据无法判断与团队研究方向的直接交叉，不作强结论。"
        implication_text = "可先将该文作为待核查线索，不据题名推断材料机制、性能数值或 DREAM 专属研究结论。"
    return {"method_point": method_text, "related_work": related_text, "implication": implication_text}


def build_direction_note(items: List[Dict[str, Any]], profile: Dict[str, Any] | None = None) -> str:
    profile = profile or {}
    methods: List[str] = []
    domains: List[str] = []
    for item in items or []:
        text = _text(item)
        methods.extend(_method_topics(text))
        domains.extend(_domain_topics(text))
    def uniq(values: List[str]) -> List[str]:
        return list(dict.fromkeys(values))
    methods, domains = uniq(methods), uniq(domains)
    method_part = "、".join(methods[:4]) or "机器学习与物理建模"
    domain_part = "、".join(domains[:4]) or "材料和凝聚态问题"
    return (
        f"本日文献与团队研究的交集主要集中在{method_part}，并落到{domain_part}。"
        "对现有工作最直接的价值是提供可复用的表示、采样或验证流程，而不是替代具体材料体系的物理判断。"
        "后续筛选应优先核对原文数据是否包含结构、能量、力、应力、极化、自旋或有限温度轨迹，"
        "再决定是否迁移到铁电、磁性、缺陷和外场驱动模拟。"
    )


def pick_summary(item: Dict[str, Any], max_english_chars: int = 200) -> str:
    """Pick the best available highlight without inventing missing findings."""
    for key in ("one_sentence_summary", "abstract_zh", "abstract_zh_full"):
        value = _usable_summary(item.get(key))
        if value:
            return value
    abstract = str(item.get("abstract") or "").strip()
    if abstract:
        if len(abstract) > max_english_chars:
            return abstract[:max_english_chars].rstrip() + "…"
        return abstract
    return "本条目仅有出版元数据，完整信息请见原文。"


def ensure_relation_fields(item: Dict[str, Any], profile: Dict[str, Any] | None = None) -> Dict[str, Any]:
    out = item
    fields = build_relation_fields(out, profile)
    for key, value in fields.items():
        current = str(out.get(key) or "").strip()
        minimum = 180
        if len(current) < minimum:
            detail = {
                "method_point": "还需核对原文的训练数据规模、损失函数、边界条件和误差分解，才能判断其是否适合团队的材料模拟任务。",
                "related_work": "这种联系应通过同一材料体系上的独立基准和跨分布测试确认，不能仅凭方法名称或期刊来源下结论。",
                "implication": "第一步可以选取团队已有的 HfO₂、二维磁体、半导体缺陷或钙钛矿数据做小样本试验，再决定是否扩大到生产级计算。",
            }[key]
            out[key] = (value + detail).strip()
    title = str(out.get("title_zh") or "").strip()
    if not title:
        en = str(out.get("title_en") or out.get("title") or "").strip()
        out["title_zh"] = f"文献研究：{en}" if en else "未命名文献"
    abstract = str(out.get("abstract_zh") or "").strip()
    full = str(out.get("abstract_zh_full") or "").strip()
    if not abstract and full:
        out["abstract_zh"] = full[:240].rstrip() + ("…" if len(full) > 240 else "")
    if not full and abstract:
        out["abstract_zh_full"] = abstract
    if not _usable_summary(out.get("summary")):
        out["summary"] = pick_summary(out)
    return out
