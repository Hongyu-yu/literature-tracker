from pathlib import Path

from research_context import build_direction_note, ensure_relation_fields, pick_summary


def test_relation_fields_are_nonempty_for_ml_ferro_article():
    item = {
        "title_en": "Machine learning potential for ferroelectric switching",
        "abstract": "We use molecular dynamics to study polarization switching.",
        "title_zh": "铁电翻转机器学习势",
        "abstract_zh": "用分子动力学研究极化翻转。",
    }
    ensure_relation_fields(item)
    for key in ("method_point", "related_work", "implication", "summary"):
        assert item[key].strip()
    assert "铁电" in item["related_work"]


def test_metadata_only_article_does_not_invent_results():
    item = {"title_en": "A material study", "title_zh": "材料研究"}
    ensure_relation_fields(item)
    assert "无法确认" in item["method_point"]
    assert "不作强结论" in item["related_work"]


def test_direction_note_is_concrete_and_nonempty():
    note = build_direction_note([{"title_en": "Graph neural network for magnetic materials"}])
    assert note
    assert "图神经网络" in note


def test_thermodynamics_relation_connects_to_team_materials_and_workflows():
    item = {
        "title_en": "Thermodynamics-Informed Machine Learning for Energy Materials Discovery",
        "abstract": "Machine learning should learn free energy and phase stability at finite temperature.",
    }
    ensure_relation_fields(item)
    assert len(item["method_point"]) >= 180
    assert len(item["related_work"]) >= 180
    assert len(item["implication"]) >= 180
    assert "HfO₂" in item["implication"]
    assert "有限温度" in item["method_point"]
    assert "主动学习" in item["implication"]


def test_profile_digest_contains_five_research_directions():
    from research_context import load_research_profile, profile_direction_digest
    digest = profile_direction_digest(load_research_profile())
    assert "Hongyu Yu" in digest
    assert "HfO2" in digest or "HfO₂" in digest
    assert "非绝热" in digest


def test_pick_summary_falls_back_to_truncated_english_abstract():
    abstract = "Electronic structure is predicted from a learnable Hamiltonian. " * 8
    item = {"title_en": "A paper", "abstract": abstract}
    summary = pick_summary(item)
    assert summary
    assert summary.startswith("Electronic structure")
    assert len(summary) <= 201
    assert summary.endswith("…")
    ensure_relation_fields(item)
    assert item["summary"] == summary
    assert "摘要信息不足" not in item["summary"]
    assert "需查阅原文确认具体方法与结论" not in item["summary"]


def test_pick_summary_priority_and_metadata_only_copy():
    item = {
        "one_sentence_summary": "AI 中文亮点",
        "abstract_zh": "中文摘要",
        "abstract_zh_full": "完整中文摘要",
        "abstract": "English abstract",
    }
    assert pick_summary(item) == "AI 中文亮点"
    assert "信息不足" not in pick_summary({"title": "metadata only"})


def test_research_context_has_no_degraded_default_summary_literal():
    source = Path("research_context.py").read_text(encoding="utf-8")
    assert '"摘要信息不足，需查阅原文确认具体方法与结论。"' not in source


def test_existing_degraded_summary_is_replaced_during_rerender():
    item = {
        "title": "Electronic structure",
        "abstract": "We predict the electronic structure from density matrices.",
        "summary": "摘要信息不足，需查阅原文确认具体方法与结论。",
    }
    ensure_relation_fields(item)
    assert item["summary"].startswith("We predict")
    assert "信息不足" not in item["summary"]
