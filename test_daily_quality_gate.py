#!/usr/bin/env python3
"""日报质量门的回归测试。

背景：daily_quality_ok 曾引用一个作用域内不存在的 `items`，total>0 时必抛 NameError，
被调用处的宽 except 吞掉 → data/daily_summary_*.json 自 2026-07-31 起从未落盘 →
--rerender-only 天天空转、每日邮件一封都没发出去。该函数当时零测试覆盖，故补上。
"""

from generate_daily_pages import daily_quality_ok, daily_quality_report


def _item(**over):
    item = {
        "title_zh": "中文标题",
        "abstract_zh": "中文摘要",
        "summary": "一句话总结",
        "method_point": "方" * 200,
        "related_work": "相" * 200,
        "implication": "意" * 200,
    }
    item.update(over)
    return item


def _summary(items=None, **over):
    data = {"overview": "总览", "trends": "热点", "full_list": items if items is not None else [_item()]}
    data.update(over)
    return data


def test_quality_ok_does_not_raise_when_items_present():
    """守住那次 NameError：total>0 时必须返回 bool，而不是抛异常。"""
    assert daily_quality_ok(_summary()) is True


def test_quality_report_counts_required_fields():
    report = daily_quality_report(_summary(items=[_item(), _item(title_zh="")]))
    assert report["total"] == 2
    assert report["title_zh"] == 1
    assert report["relation"] == 2


def test_quality_gate_rejects_missing_field():
    assert daily_quality_ok(_summary(items=[_item(summary="")])) is False


def test_quality_gate_rejects_empty_relation_field():
    """三段文本要求「非空」，不再要求「≥180 字」。

    旧的 180 字下限只在 research_context 会把短文本整段替换成长模板时才成立；
    那个替换会删掉真实但简短的 AI 分析，已经修掉，长度门随之失去依据 ——
    留着它反而会在 AI 正常作答时恒为 False，让 rerender_ok 永远关着。
    """
    assert daily_quality_ok(_summary(items=[_item(method_point="")])) is False
    # 简短但真实的分析必须被判为合格，不能因为「不够长」被否掉
    assert daily_quality_ok(_summary(items=[_item(method_point="用 MACE 训练势函数。")])) is True


def test_quality_gate_rejects_missing_overview_or_trends():
    assert daily_quality_ok(_summary(trends="")) is False


def test_quality_gate_falls_back_to_overview_when_no_items():
    assert daily_quality_ok(_summary(items=[])) is True
    assert daily_quality_ok(_summary(items=[], overview="")) is False


def test_quality_gate_reads_summaries_when_full_list_absent():
    data = {"overview": "总览", "trends": "热点", "summaries": [_item()]}
    assert daily_quality_report(data)["total"] == 1
    assert daily_quality_ok(data) is True


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] daily quality gate sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
