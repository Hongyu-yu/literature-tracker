import json
import os
import tempfile
from unittest import mock

import backfill_top_posters


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def test_select_top_candidates_priority_existing_image_and_budget():
    items = [
        {"title": "General methods", "link": "https://ex/p3", "abstract": "a"},
        {"title": "Ferroelectric polarization", "link": "https://ex/p2", "abstract": "a"},
        {"title": "Neural network potential", "link": "https://ex/p1", "abstract": "a"},
        {"title": "Electronic structure", "link": "https://ex/image", "abstract": "a", "image": "images/posters/x.webp"},
    ]
    selected = backfill_top_posters.select_top_candidates(items, existing=[], max_items=2)
    assert [item["link"] for item in selected] == ["https://ex/p1", "https://ex/p2"]


def test_select_top_candidates_leaves_deep_read_for_existing_backfill_path():
    item = {"title": "Neural network potential", "link": "https://ex/deep", "abstract": "a"}
    existing = [{"link": "https://ex/deep", "deep_analysis": "already deeply read"}]
    assert backfill_top_posters.select_top_candidates([item], existing, max_items=12) == []


def test_process_day_parallel_writes_compact_core_records():
    root = tempfile.mkdtemp()
    summary_path = os.path.join(root, "daily_summary_2026-08-21.json")
    index_path = os.path.join(root, "index.json")
    output_path = os.path.join(root, "arxiv_core_2026-08-21.json")
    items = [
        {"title": "Neural network potential", "link": "https://ex/1", "abstract": "abstract 1"},
        {"title": "Ferroelectric polarization", "link": "https://ex/2", "abstract": "abstract 2"},
        {"title": "General materials", "link": "https://ex/3", "abstract": "abstract 3"},
    ]
    _write(summary_path, {"full_list": items})
    _write(index_path, {"articles": items})

    def fake_poster(meta, src, provider, out_dir):
        return {"doc_id": meta["doc_id"], "image": f"images/posters/{meta['doc_id']}.webp",
                "elements": {"研究问题": src}, "title_zh": "海报标题"}

    with mock.patch.object(backfill_top_posters, "generate_poster", side_effect=fake_poster) as generate:
        filled = backfill_top_posters.process_day(
            "2026-08-21", provider=object(), max_items=2,
            summary_path=summary_path, index_path=index_path, output_path=output_path,
            max_workers=2,
        )
    assert filled == 2 and generate.call_count == 2
    raw = open(output_path, encoding="utf-8").read()
    assert "\n" not in raw
    records = json.loads(raw)
    assert len(records) == 2
    assert all(record["source"] == "top_poster" and record["image"] for record in records)


def test_process_day_is_idempotent_and_fail_soft():
    root = tempfile.mkdtemp()
    summary_path = os.path.join(root, "daily.json")
    index_path = os.path.join(root, "index.json")
    output_path = os.path.join(root, "core.json")
    item = {"title": "Neural network potential", "link": "https://ex/1", "abstract": "a"}
    _write(summary_path, {"full_list": [item]})
    _write(index_path, {"articles": [item]})
    _write(output_path, [{"link": "https://ex/1", "image": "images/posters/existing.webp"}])
    with mock.patch.object(backfill_top_posters, "generate_poster", side_effect=AssertionError("must skip")):
        assert backfill_top_posters.process_day("2026-08-21", provider=object(), max_items=12,
            summary_path=summary_path, index_path=index_path, output_path=output_path) == 0

    os.unlink(output_path)
    with mock.patch.object(backfill_top_posters, "generate_poster", side_effect=RuntimeError("offline")):
        assert backfill_top_posters.process_day("2026-08-21", provider=object(), max_items=1,
            summary_path=summary_path, index_path=index_path, output_path=output_path) == 0


def test_workflow_runs_top_poster_backfill_with_cost_limit():
    workflow = open(".github/workflows/generate-deep.yml", encoding="utf-8").read()
    assert "python backfill_top_posters.py --max 12" in workflow
    assert "TOP_POSTER_MAX" in workflow
