"""research_context.ensure_relation_fields 不得吞掉真实的 AI/深读三段文本。

历史现场：ensure_relation_fields 对 method_point/related_work/implication 做
`len(current) < 180 就整段替换成关键词模板`。ai_summarizer._parse_response 先把模型
返回的 method_point 写进 row，紧接着调用 ensure_relation_fields —— 而真实 AI 答案通常
只有几十字（data/daily_summary_2026-07-29.json 里 60 条全部 60~90 字），于是每一条的
具体结论都被 TOPIC_PLANS 模板顶掉。对比 data/daily_summary_2026-07-30.json：60 条全部
>=180 字，且第一条开头就是"方法上，这篇工作可归入热力学知情 ML…"的通用套话。

顺带覆盖 _topic_plans 的排序：thermo 的 terms 里有 entropy / free energy /
finite temperature 等通用词，此前按字典顺序排第一，任何提一句"有限温度"的铁电论文
都会被模板断言成热力学/CALPHAD 工作。
"""

from research_context import build_relation_fields, ensure_relation_fields


def test_short_ai_method_point_is_not_replaced_by_template():
    """几十字的真实 AI 答案必须原样保留，不得被关键词模板覆盖。"""
    ai_text = "用 E(3) 等变图网络学习电子结构算符，不只回归能量/力，而是加入可观测量监督，目标直接重建能带、DOS 与电子密度。"
    item = {
        "title_en": "Equivariant networks for electronic structure at finite temperature",
        "abstract": "We learn the Hamiltonian operator with an equivariant graph neural network at finite temperature.",
        "method_point": ai_text,
    }
    ensure_relation_fields(item)
    assert item["method_point"] == ai_text, "真实 AI 文本被覆盖了"
    assert "方法上，这篇工作可归入" not in item["method_point"]


def test_short_related_work_and_implication_are_not_replaced():
    """三个字段都要保留，不只是 method_point。"""
    rw = "与团队 HfO₂ 铁电方向直接相关。"
    im = "可先在 Hf0.5Zr0.5O2 上复现该表示。"
    item = {
        "title_en": "Ferroelectric switching with machine learning potentials",
        "abstract": "Polarization switching and domain wall motion at finite temperature.",
        "related_work": rw,
        "implication": im,
    }
    ensure_relation_fields(item)
    assert item["related_work"] == rw
    assert item["implication"] == im
    # method_point 本来就是空的，兜底文本仍然要补上
    assert item["method_point"].strip()


def test_empty_relation_fields_still_get_rule_based_fallback():
    """兜底路径不变：字段为空时照旧写入规则版长文本（AI 全挂那天的日报靠它撑住）。"""
    item = {
        "title_en": "Thermodynamics-informed machine learning for phase stability",
        "abstract": "Machine learning of free energy and phase stability at finite temperature.",
        "method_point": "",
        "related_work": None,
    }
    ensure_relation_fields(item)
    for key in ("method_point", "related_work", "implication"):
        assert len(item[key]) >= 180, f"{key} 兜底文本长度回退了"
    assert "热力学知情 ML" in item["method_point"]


def test_ferroelectric_paper_mentioning_finite_temperature_is_not_labelled_thermo():
    """只提一句 finite temperature 的铁电论文，不得被断言成热力学/CALPHAD 工作。"""
    item = {
        "title_en": "Domain wall motion in HfO2 ferroelectrics",
        "abstract": (
            "We study polarization switching and domain wall propagation in hafnia "
            "ferroelectric films at finite temperature."
        ),
    }
    fields = build_relation_fields(item)
    assert "HfO₂ 铁电" in fields["method_point"], fields["method_point"]
    assert "CALPHAD" not in fields["method_point"], "通用 thermo 模板压过了铁电方向"


def _run_all():
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✅ {name}")
            except AssertionError as e:
                failures.append(name)
                print(f"❌ {name}: {e}")
    print("全部通过" if not failures else f"失败: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
