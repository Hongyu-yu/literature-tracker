import json
from unittest import mock

import highlight_guarantee


class FakeProvider:
    def call_api(self, prompt):
        assert "Neural network potential" in prompt
        return json.dumps({"items": [{"index": 1, "highlight": "该工作提出神经网络势并验证了其电子结构预测能力。"}]}, ensure_ascii=False)


def _items():
    return [
        {"title": "ML potential", "abstract": "Neural network potential for electronic structure."},
        {"title": "Ready", "abstract": "Already done.", "one_sentence_summary": "已有亮点"},
    ]


def test_ensure_highlights_writes_ai_highlight_and_is_idempotent():
    items = _items()
    updated = highlight_guarantee.ensure_highlights(items, provider=FakeProvider(), max_items=60)
    assert updated == 1
    assert items[0]["one_sentence_summary"].startswith("该工作")
    assert items[1]["one_sentence_summary"] == "已有亮点"


def test_ensure_highlights_provider_failure_uses_translation_fallback():
    class BrokenProvider:
        def call_api(self, prompt):
            raise RuntimeError("offline")

    items = _items()
    with mock.patch.object(highlight_guarantee, "translate_text", return_value="中文翻译兜底") as translate:
        updated = highlight_guarantee.ensure_highlights(items, provider=BrokenProvider(), max_items=1)
    assert updated == 1
    assert items[0]["one_sentence_summary"] == "中文翻译兜底"
    translate.assert_called_once()


def test_ensure_highlights_both_fail_is_soft_and_does_not_write_dirty_value():
    class BrokenProvider:
        def call_api(self, prompt):
            raise RuntimeError("offline")

    items = _items()
    with mock.patch.object(highlight_guarantee, "translate_text", side_effect=RuntimeError("translator offline")):
        updated = highlight_guarantee.ensure_highlights(items, provider=BrokenProvider(), max_items=1)
    assert updated == 0
    assert "one_sentence_summary" not in items[0]


def test_ensure_highlights_respects_zero_and_max_items():
    items = [
        {"title": "A", "abstract": "abstract A"},
        {"title": "B", "abstract": "abstract B"},
    ]
    provider = mock.Mock()
    assert highlight_guarantee.ensure_highlights(items, provider=provider, max_items=0) == 0
    provider.call_api.assert_not_called()


def test_ensure_highlights_replaces_degraded_legacy_highlight():
    item = {"title": "A", "abstract": "abstract A",
            "one_sentence_summary": "摘要信息不足，需查阅原文确认具体方法与结论。"}
    with mock.patch.object(highlight_guarantee, "translate_text", return_value="忠实中文亮点"):
        assert highlight_guarantee.ensure_highlights([item], provider=None, max_items=1) == 1
    assert item["one_sentence_summary"] == "忠实中文亮点"
