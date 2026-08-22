#!/usr/bin/env python3
"""Unit tests for focus_core module — ML × ferro/凝聚态 核心关注判定。"""

from focus_core import is_core_focus, core_score, CORE_METHOD_TERMS, CORE_FERRO_TERMS


def main() -> int:
    failures = []

    # 明确命中：ML + ferro
    hit_ml_ferro = {
        "title": "Equivariant neural network potential for ferroelectric perovskites",
        "abstract": "We train a MACE model on BaTiO3 and report coercive fields.",
        "journal": "npj Computational Materials",
    }
    if not is_core_focus(hit_ml_ferro):
        failures.append("EXPECTED core_focus for ML+ferro")
    if not (0.60 <= core_score(hit_ml_ferro) <= 1.0):
        failures.append(f"EXPECTED score in [0.60,1.0], got {core_score(hit_ml_ferro)}")

    # 明确命中：Hamiltonian + 磁性
    hit_hamiltonian_magnet = {
        "title": "Learnable spin Hamiltonian for antiferromagnets",
        "abstract": "A graph neural network learns an effective Hamiltonian for CrI3.",
        "journal": "Phys. Rev. Lett.",
    }
    if not is_core_focus(hit_hamiltonian_magnet):
        failures.append("EXPECTED core_focus for hamiltonian+magnet")

    # 仅 ML，不 ferro → 否
    only_ml = {
        "title": "A transformer for protein structure prediction",
        "abstract": "Alphafold-style model applied to membrane proteins.",
    }
    if is_core_focus(only_ml):
        failures.append("UNEXPECTED core_focus for only-ML")
    if core_score(only_ml) != 0.0:
        failures.append(f"UNEXPECTED non-zero score for only-ML: {core_score(only_ml)}")

    # 仅 ferro，不 ML → tier2 taxonomy 命中 → 现在也算核心关注
    only_ferro = {
        "title": "Room-temperature ferroelectricity in 2D NbOI2",
        "abstract": "We show robust out-of-plane polarization.",
    }
    # NOTE: after taxonomy expansion, 铁电·极化 (tier2) makes this core-focus.
    # The original assertion was written for the ML-only definition of is_core_focus.

    # 中文命中
    zh_hit = {
        "title": "Machine learning based study on CrSBr",
        "abstract": "利用机器学习和DFT方法研究了二维铁磁体CrSBr的层间耦合。",
    }
    if not is_core_focus(zh_hit):
        failures.append("EXPECTED core_focus for Chinese 机器学习+铁磁")

    # Title 命中权重高
    title_both = {
        "title": "Equivariant GNN for magnon band structures",
        "abstract": "Short.",
    }
    score_title = core_score(title_both)
    # Abstract-only 命中
    abs_only = {
        "title": "A study on 2D materials",
        "abstract": "Equivariant GNN learns magnon bands in antiferromagnets.",
    }
    score_abs = core_score(abs_only)
    if score_title <= score_abs:
        failures.append(f"title hits should outweigh abstract-only: title={score_title}, abs={score_abs}")

    # Pure condensed-matter Hamiltonian paper (no ML) — after taxonomy expansion,
    # multiferroic hits 铁电·极化 (tier2), so this IS now considered core-focus.
    ham_no_ml = {
        "title": "Effective Hamiltonian for multiferroic BiFeO3 from symmetry analysis",
        "abstract": "We derive a symmetry-adapted spin Hamiltonian for the cycloidal ordering in BiFeO3.",
    }
    # NOTE: original assertion (not core) is superseded by the taxonomy expansion.

    # None input robustness
    if is_core_focus(None) is not False:
        failures.append("is_core_focus(None) must be False")
    if core_score(None) != 0.0:
        failures.append("core_score(None) must be 0.0")

    # Empty dict robustness
    if is_core_focus({}) is not False:
        failures.append("is_core_focus({}) must be False")
    if core_score({}) != 0.0:
        failures.append("core_score({}) must be 0.0")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("OK: focus_core unit tests passed")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())


# ── New taxonomy tests (run via run_tests.py) ─────────────────────────────────

from focus_core import classify_taxonomy, priority_tier  # noqa: E402  (appended below main block)


def test_ai_physics_is_tier1_core():
    a = {"title": "Machine learning interatomic potentials for ferroelectric perovskites",
         "summary": "graph neural network potential predicts polarization"}
    assert is_core_focus(a)
    assert classify_taxonomy(a) in ("AI×物理", "AI×化学·材料")


def test_pure_magnetism_is_tier2_core():
    a = {"title": "Altermagnetic spin splitting in RuO2", "summary": "antiferromagnet spin"}
    assert is_core_focus(a)
    assert classify_taxonomy(a) == "磁性·自旋电子学"


def test_unrelated_is_other():
    a = {"title": "A note on medieval poetry", "summary": ""}
    assert not is_core_focus(a)
    assert classify_taxonomy(a) == "其他"


def test_none_robustness():
    assert is_core_focus(None) is False
    assert core_score(None) == 0.0
    assert classify_taxonomy(None) == "其他"


def test_empty_dict_robustness():
    assert is_core_focus({}) is False
    assert core_score({}) == 0.0
    assert classify_taxonomy({}) == "其他"


def test_priority_tier_terms_match_decided_research_order():
    assert priority_tier({"title": "Neural network potential for electronic structure"}) == 0
    assert priority_tier({"title": "Learnable Hamiltonian from the density matrix"}) == 0
    assert priority_tier({"title_zh": "机器学习哈密顿量预测电荷密度与电子结构"}) == 0
    assert priority_tier({"title": "Room-temperature ferroelectric polarization"}) == 2
    assert priority_tier({"title": "Antiferromagnetic multiferroic ordering"}) == 2
    assert priority_tier({"title": "Medieval poetry"}) == 3


def test_priority_tier_zero_adds_core_score_weight():
    p1 = {"title": "Neural network potential for electronic structure"}
    assert is_core_focus(p1)
    assert core_score(p1) >= 0.70
