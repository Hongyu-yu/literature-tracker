#!/usr/bin/env python3
"""cross_relevance：AI × 物理/材料/化学 交叉相关度的规则层与 LLM 层。"""

import os
from unittest import mock

import cross_relevance as cr


# ---------------------------------------------------------------- 规则分层

def _tier0():
    return {"title": "Machine learning interatomic potential for ferroelectric perovskites",
            "journal": "arXiv", "arxiv_category": "cond-mat"}


def _tier1_ai_title():
    return {"title": "Deep learning accelerates high-throughput screening",
            "abstract": "We screen ferroelectric perovskite thin films with density "
                        "functional theory and molecular dynamics.",
            "journal": "arXiv", "arxiv_category": "cond-mat"}


def _tier2_pure_physics():
    return {"title": "Topological pairing density wave in a kagome superconductor",
            "abstract": "We report charge order and superconductivity in a kagome metal.",
            "journal": "Phys. Rev. B"}


def _tier3_robotics():
    """ai_science 成立、但两侧都不在标题上 —— 旧规则会误判成交叉。"""
    return {"title": "Cooperative online scheduling for multi-robot coordination",
            "abstract": "We use reinforcement learning to plan contact on the robot "
                        "surface under communication limits.",
            "journal": "arXiv", "arxiv_category": "cs.LG"}


def test_rule_cross_tier_separates_the_four_layers():
    assert cr.rule_cross_tier(_tier0()) == 0
    assert cr.rule_cross_tier(_tier1_ai_title()) == 1
    assert cr.rule_cross_tier({
        "title": "Ferroelectric domain wall dynamics in HfO2",
        "abstract": "A graph neural network surrogate is trained on first-principles data.",
        "journal": "Phys. Rev. B",
    }) == 1
    assert cr.rule_cross_tier(_tier2_pure_physics()) == 2


def test_incidental_science_words_in_abstract_are_not_a_crossover():
    """摘要里的 surface / interface 不构成"科学侧"。

    focus_filter 的 MATERIALS_CORE_TERMS 含裸词 surface/interface（那张表服务
    target_domain 的宽进，本来就该宽）。实测 2026-09-01 当天，多机器人控制、
    金融 PPO、地理遥感、蛋白质工程四篇论文全靠摘要里的 surface 命中
    direct_science，再叠一个 learning 就被判成 AI×科学交叉，直接进日报邮件。
    """
    for item in (_tier3_robotics(),
                 {"title": "Understanding representation collapse via notions of rank",
                  "abstract": "We analyse deep learning optimisation on the loss surface "
                              "of transformers.",
                  "journal": "arXiv", "arxiv_category": "cs.LG"}):
        # 前置条件：这些论文确实触发了 ai_science，否则本测试是空守卫
        assert cr.cross_signals(item)["ai_science"] is True
        assert cr.rule_cross_tier(item) == 3
        assert cr.is_cross_item(item) is False



def test_physics_diffusion_model_is_not_an_ai_signal():
    """"energy diffusion model" 是物理里的扩散模型，不是生成式扩散模型。

    focus_filter.AI_TERMS 含 'diffusion model'（服务全流程的宽召回，本身没错）。
    交叉判定若直接用它，2026-08-31 的「Energy relaxation due to two-phonon
    scattering of electrons: Breakdown of the energy diffusion model」就会被判成
    标题级交叉，排进日报邮件主区第 8 张卡片。
    """
    physics = {"title": "Energy relaxation due to two-phonon scattering of electrons: "
                        "Breakdown of the energy diffusion model",
               "journal": "Phys. Rev. B"}
    assert cr.cross_signals(physics)["ai_in_title"] is False
    assert cr.rule_cross_tier(physics) != 0
    # 真正的生成式扩散模型仍要认出来
    generative = {"title": "Denoising diffusion model for crystal structure generation",
                  "journal": "arXiv", "arxiv_category": "cond-mat"}
    assert cr.cross_signals(generative)["ai_in_title"] is True
    assert cr.rule_cross_tier(generative) == 0


# ---------------------------------------------------------------- 分数

def test_effective_cross_score_falls_back_to_rules_without_ai():
    assert cr.effective_cross_score(_tier0()) == 8.0
    assert cr.effective_cross_score(_tier1_ai_title()) == 6.0
    assert cr.effective_cross_score(_tier2_pure_physics()) == 2.0
    assert cr.effective_cross_score(_tier3_robotics()) == 0.0


def test_effective_cross_score_prefers_ai_score_and_clamps():
    item = _tier0()
    item["cross_score"] = 3
    assert cr.effective_cross_score(item) == 3.0
    assert cr.effective_cross_score({**item, "cross_score": 42}) == 10.0
    assert cr.effective_cross_score({**item, "cross_score": -5}) == 0.0
    assert cr.effective_cross_score({**item, "cross_score": "7"}) == 7.0
    # 坏类型不得让排序炸掉，退回规则分
    assert cr.effective_cross_score({**item, "cross_score": "n/a"}) == 8.0
    assert cr.effective_cross_score({**item, "cross_score": None}) == 8.0
    assert cr.effective_cross_score({**item, "cross_score": True}) == 8.0


def test_ai_score_overrides_rule_tier_for_section_choice():
    """标题级交叉不给无条件后门：LLM 看过摘要，它说不相关就是不相关。

    真实例子：Benchmarking Quantum Feature Encoding Strategies —— 标题里同时
    有 AI 与 quantum，实际是量子机器学习基准，不属于 AI×凝聚态交叉。
    """
    item = _tier0()
    assert cr.is_cross_item(item) is True
    item["cross_score"] = 1
    assert cr.is_cross_item(item) is False


def test_cross_min_score_is_env_tunable():
    item = dict(_tier2_pure_physics(), cross_score=4)
    assert cr.is_cross_item(item) is False
    with mock.patch.dict(os.environ, {"CROSS_MIN_SCORE": "3"}):
        assert cr.is_cross_item(item) is True



def test_journal_name_cannot_fake_a_crossover_signal():
    """刊名不算"标题信号"。

    focus_filter._item_title_focus_text 会把 journal 拼进去，交叉判定若用它，
    "Machine Learning: Science and Technology" 上的任何论文都白拿一个 AI 信号，
    "Crystal Growth & Design" 白拿一个科学信号 —— 周报的顶刊闸门可以被这样绕开。
    """
    faked = {"title": "Anomalous transport in a bulk sample",
             "journal": "Machine Learning: Science and Technology",
             "abstract": "We measure resistivity of a crystal."}
    assert cr.cross_signals(faked)["ai_in_title"] is False
    assert cr.rule_cross_tier(faked) != 0
    # 真的写在标题里就要认
    real = {"title": "Machine learning transport in a bulk crystal",
            "journal": "Machine Learning: Science and Technology",
            "abstract": "We measure resistivity of a crystal."}
    assert cr.cross_signals(real)["ai_in_title"] is True

# ---------------------------------------------------------------- LLM 层

class _Provider:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.calls = payload, exc, []

    def call_api(self, prompt):
        self.calls.append(prompt)
        if self.exc:
            raise self.exc
        return self.payload


def test_enrich_writes_all_three_fields_and_is_idempotent():
    items = [_tier0(), _tier1_ai_title()]
    provider = _Provider('{"items":[{"index":1,"cross_score":9,'
                         '"cross_reason":"等变神经网络势用于铁电钙钛矿","cross_side":"simulation"},'
                         '{"index":2,"cross_score":7,"cross_reason":"深度学习筛选薄膜",'
                         '"cross_side":"materials"}]}')
    assert cr.enrich_cross_relevance(items, provider=provider) == 2
    assert items[0]["cross_score"] == 9
    assert items[0]["cross_reason"] == "等变神经网络势用于铁电钙钛矿"
    assert items[0]["cross_side"] == "simulation"
    assert items[1]["cross_score"] == 7
    # 幂等：第二遍不再花钱
    again = _Provider('{"items":[]}')
    assert cr.enrich_cross_relevance(items, provider=again) == 0
    assert again.calls == []


def test_enrich_only_pays_for_crossover_candidates():
    """纯物理/纯 ML 不送 LLM —— 它们本来就归「其他」区，不需要花钱确认。"""
    items = [_tier2_pure_physics(), _tier3_robotics()]
    provider = _Provider('{"items":[]}')
    assert cr.enrich_cross_relevance(items, provider=provider) == 0
    assert provider.calls == []


def test_enrich_is_fail_soft():
    # 无 provider
    items = [_tier0()]
    assert cr.enrich_cross_relevance(items, provider=None) == 0
    assert "cross_score" not in items[0]
    # provider 抛错：不抛异常、不写脏值，排序仍可用规则分
    boom = _Provider(exc=RuntimeError("gateway 502"))
    assert cr.enrich_cross_relevance(items, provider=boom) == 0
    assert "cross_score" not in items[0]
    assert cr.effective_cross_score(items[0]) == 8.0
    # 响应不是 JSON
    assert cr.enrich_cross_relevance(items, provider=_Provider("sorry, no")) == 0
    assert "cross_score" not in items[0]


def test_enrich_respects_max_items():
    items = [_tier0() for _ in range(5)]
    for i, it in enumerate(items):
        it["title"] += f" number {i}"
    provider = _Provider('{"items":[{"index":1,"cross_score":8,"cross_reason":"x","cross_side":"physics"},'
                         '{"index":2,"cross_score":8,"cross_reason":"x","cross_side":"physics"}]}')
    assert cr.enrich_cross_relevance(items, provider=provider, max_items=2) == 2
    assert sum(1 for it in items if "cross_score" in it) == 2


def test_partial_batch_response_leaves_the_rest_untouched():
    """只回了一半的批次，另一半必须保持无分状态，等下次重试；绝不写 0 分冒充。"""
    items = [_tier0(), _tier1_ai_title()]
    provider = _Provider('{"items":[{"index":2,"cross_score":6,"cross_reason":"a","cross_side":"materials"}]}')
    assert cr.enrich_cross_relevance(items, provider=provider) == 1
    scored = [it for it in items if "cross_score" in it]
    assert len(scored) == 1


# ---------------------------------------------------------------- 分区

def test_split_cross_sections_never_drops_an_item():
    items = [_tier0(), _tier1_ai_title(), _tier2_pure_physics(), _tier3_robotics()]
    cross, other = cr.split_cross_sections(items)
    assert len(cross) + len(other) == len(items)
    assert _tier0()["title"] in [i["title"] for i in cross]
    assert _tier2_pure_physics()["title"] in [i["title"] for i in other]


def test_cross_sort_key_orders_by_score_then_falls_back_to_focus_priority():
    high = dict(_tier0(), cross_score=9)
    low = dict(_tier0(), title="Machine learning potential for ferroelectric oxides", cross_score=4)
    assert cr.cross_sort_key(high) < cr.cross_sort_key(low)
    # 同分时不能退化成按标题字母排：focus_priority 要生效
    a = dict(_tier2_pure_physics(), title="Zzz ferroelectric switching", focus_score=9, core_score=0.9)
    b = dict(_tier2_pure_physics(), title="Aaa ferroelectric polarization", focus_score=1, core_score=0.9)
    assert cr.cross_sort_key(a) < cr.cross_sort_key(b)


# ---------------------------------------------------------------- 提示词

def test_prompt_template_is_wired_and_json_mode_safe():
    with open(cr._CROSS_PROMPT_PATH, encoding="utf-8") as f:
        text = f.read()
    # 网关的 JSON-mode 校验区分大小写，正文必须出现小写 json 字面量
    assert "json" in text
    assert "${articles}" in text
    prompt = cr._build_batch_prompt([_tier0()])
    assert "${articles}" not in prompt
    assert "Machine learning interatomic potential" in prompt
    assert "cross_score" in prompt


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✅ {name}")
            except Exception as exc:
                fails += 1
                print(f"❌ {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if fails else 0)
