#!/usr/bin/env python3
"""Deterministic sanity tests for focus filtering heuristics."""

from focus_filter import analyze_focus, filter_daily_focus_items, filter_focus_items, focus_priority, is_daily_focus, is_target_domain, topic_bucket


def test_focus_priority_uses_focus_score_first_within_same_priority_layer():
    low = {"title": "Ferroelectric polarization", "focus_score": 2, "core_score": 0.9}
    high = {"title": "Ferroelectric switching", "focus_score": 9, "core_score": 0.1}
    assert focus_priority(high) < focus_priority(low)


def test_daily_selection_budget_keeps_p1_before_lower_priority():
    p1 = {"title": "Density matrix reconstruction for electronic systems", "journal": "arXiv"}
    p3 = {"title": "Machine learning catalyst materials discovery", "journal": "arXiv"}
    kept, _ = filter_focus_items([p1])
    assert kept == [p1]
    selected, _ = filter_daily_focus_items([p3, p1], min_keep=1, max_keep=1)
    assert selected == [p1]


def main() -> int:
    medical = {
        "title": "Wireless, non-invasive, high-resolution thrill sensor for continuous vascular access monitoring of hemodialysis patients",
        "journal": "Nature Communications",
    }
    cancer_ai = {
        "title": "Deep learning from routine histology improves risk stratification for biochemical recurrence in prostate cancer",
        "journal": "Preprints",
    }
    education = {
        "title": "Exploring students perceptions of their learning experience and self efficacy in physics online class with project based learning",
        "journal": "arXiv",
        "arxiv_category": "cs.LG",
    }
    ai_materials = {
        "title": "Machine learning interatomic potential for ferroelectric perovskites",
        "journal": "arXiv",
        "arxiv_category": "cs.LG",
    }
    condensed_matter = {
        "title": "First-principles study of quantum spin Hall effect in moire materials",
        "journal": "Phys. Rev. Lett.",
    }
    methods = {
        "title": "Toward Reaction Vessel Mimicry: Machine Learning-Assisted Automated Exploration of Alkene Polymerization and Its Mechanism",
        "journal": "ACS",
    }
    pure_ai = {
        "title": "A Federated Many-to-One Hopfield model for associative Neural Networks",
        "journal": "arXiv",
        "arxiv_category": "cs.LG",
    }

    assert is_target_domain(ai_materials)
    assert is_target_domain(condensed_matter)
    assert is_target_domain(methods)
    assert not is_target_domain(medical)
    assert not is_target_domain(cancer_ai)
    assert not is_target_domain(education)
    assert is_daily_focus(ai_materials)
    assert is_daily_focus(methods)
    assert not is_daily_focus(pure_ai)
    assert topic_bucket(ai_materials) in {"physics", "materials", "methods"}
    assert topic_bucket(condensed_matter) == "physics"
    assert analyze_focus(medical)["hard_offtopic"] is True
    assert analyze_focus(cancer_ai)["hard_offtopic"] is True
    assert analyze_focus(education)["hard_offtopic"] is True

    kept, dropped = filter_focus_items([medical, cancer_ai, education, ai_materials, condensed_matter, methods])
    assert len(kept) == 3
    assert len(dropped) == 3
    assert {item["title"] for item in dropped} == {medical["title"], cancer_ai["title"], education["title"]}

    print("[OK] focus filter sanity checks passed")

    # ——— 核心关注置顶测试 ———
    from focus_filter import focus_priority as _fp
    core_item = {
        "title": "Equivariant neural network potential for ferroelectric perovskites",
        "abstract": "We train MACE on BaTiO3.",
        "journal": "npj Computational Materials",
    }
    noncore_item = {
        "title": "A transformer for protein structure prediction",
        "journal": "Nature",
    }
    if _fp(core_item) >= _fp(noncore_item):
        print("FAIL: core_focus item should sort BEFORE non-core")
        return 1
    # Assert the first tuple element is *specifically* the core_focus rank.
    # This fails if focus_priority doesn't expose core_focus as its top-most sort key.
    if _fp(core_item)[0] != 0:
        print(f"FAIL: core_focus item's priority[0] must be 0, got {_fp(core_item)[0]}")
        return 1
    if _fp(noncore_item)[0] != 1:
        print(f"FAIL: non-core item's priority[0] must be 1, got {_fp(noncore_item)[0]}")
        return 1
    # Stress case: a weak-on-everything-else core item must still beat a strong non-core.
    weak_core = {
        "title": "Some note about ferroelectric coupling",
        "abstract": "machine learning mentioned.",
        "journal": "Unknown Journal",
    }
    # Genuinely non-core after the AI×physics/chem/materials taxonomy update:
    # strong ML signal but a medical/off-topic domain (no physics/chem/materials hit).
    strong_noncore = {
        "title": "Deep learning for tumor segmentation in clinical radiology",
        "abstract": "Convolutional neural network for medical imaging diagnosis.",
        "journal": "Nature",
        "ai_score": 10,
    }
    if _fp(weak_core) >= _fp(strong_noncore):
        print("FAIL: weak core_focus item must still beat strong non-core via priority[0]")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
