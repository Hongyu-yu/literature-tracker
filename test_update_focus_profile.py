"""update_focus_profile 画像构建测试(无网络:monkeypatch 模块级 _fetch_* 函数)。"""

import json
import os
import tempfile
from unittest import mock

import update_focus_profile as ufp


def _row(title, year, venue="Phys Rev B 100, 1-5", authors="A Author, B Author"):
    return (
        '<tr class="gsc_a_tr">'
        '<td class="gsc_a_t">'
        f'<a class="gsc_a_at" href="/citations?view_op=view_citation">{title}</a>'
        f'<div class="gs_gray">{authors}</div>'
        f'<div class="gs_gray">{venue}</div>'
        "</td>"
        f'<td class="gsc_a_y"><span class="gsc_a_h gsc_a_hc gs_ibl">{year}</span></td>'
        "</tr>"
    )


def _page(rows):
    return "<html><body><table id='gsc_a_b'>" + "".join(rows) + "</table></body></html>"


def _no_sleep(*a, **k):
    return None


# ---------- Scholar 列表解析 ----------

def test_parse_scholar_works_fixture_html():
    html = _page([
        _row("Ferroelectric domain walls in thin films", 2024,
             venue="Physical Review Letters 132 (1), 1-6"),
        _row("Machine learning potential for perovskites", 2022, venue="Nat Commun 13, 1"),
    ])
    works = ufp.parse_scholar_works(html)
    assert len(works) == 2
    assert works[0]["title"] == "Ferroelectric domain walls in thin films"
    assert works[0]["year"] == 2024
    assert works[0]["venue"] == "Physical Review Letters 132 (1), 1-6"  # 第二个 gs_gray
    assert works[0]["abstract"] is None
    assert works[1]["year"] == 2022
    assert ufp.parse_scholar_works("") == []


def test_scrape_scholar_works_stops_before_min_year():
    # 第一页满 100 行(继续翻页),第二页全是 <2021 的旧文 → 停止且不收录
    page1 = _page([_row(f"Ferroelectric paper {i}", 2023) for i in range(100)])
    page2 = _page([_row("Ancient work", 2019), _row("Older work", 2015)])
    calls = []

    def fake_fetch(scholar_id, cstart=0):
        calls.append(cstart)
        return page1 if cstart == 0 else page2

    with mock.patch.object(ufp, "_fetch_scholar_page", side_effect=fake_fetch), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        works = ufp.scrape_scholar_works("TESTID")
    assert calls == [0, 100]
    assert len(works) == 100
    assert all(w["year"] >= ufp.MIN_YEAR for w in works)


def test_scrape_scholar_works_dedups_titles():
    page = _page([_row("Same Title", 2024), _row("same   title", 2024),
                  _row("Other", 2024)])
    with mock.patch.object(ufp, "_fetch_scholar_page", return_value=page), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        works = ufp.scrape_scholar_works("TESTID")
    assert len(works) == 2  # 规范化后重名只留一篇


# ---------- 摘要回填 ----------

def test_reconstruct_abstract_inverted_index():
    inv = {"quantum": [0, 2], "spin": [1], "liquid": [3]}
    assert ufp._reconstruct_abstract(inv) == "quantum spin quantum liquid"
    assert ufp._reconstruct_abstract({}) is None
    assert ufp._reconstruct_abstract(None) is None


def test_backfill_abstract_openalex_accept():
    def fake_openalex(title, broad=False):
        return {"results": [{
            "title": "Ferroelectric thin films",
            "abstract_inverted_index": {"We": [0], "study": [1], "ferroelectric": [2]},
        }]}

    with mock.patch.object(ufp, "_fetch_openalex", side_effect=fake_openalex), \
         mock.patch.object(ufp, "_fetch_semantic_scholar",
                           side_effect=AssertionError("S2 must not be called")), \
         mock.patch.object(ufp, "_fetch_arxiv",
                           side_effect=AssertionError("arXiv must not be called")), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        abstract = ufp.backfill_abstract("Ferroelectric thin films")
    assert abstract == "We study ferroelectric"


def test_backfill_abstract_rejects_low_similarity_and_degrades_to_s2():
    calls = []

    def fake_openalex(title, broad=False):
        calls.append(("openalex", broad))
        if broad:
            return {"results": []}
        return {"results": [{"title": "Completely unrelated quantum optics study",
                             "abstract_inverted_index": {"Nope": [0]}}]}

    def fake_s2(title):
        calls.append(("s2", False))
        return {"data": [{"title": "Ferroelectric thin films",
                          "abstract": "S2 abstract text"}]}

    with mock.patch.object(ufp, "_fetch_openalex", side_effect=fake_openalex), \
         mock.patch.object(ufp, "_fetch_semantic_scholar", side_effect=fake_s2), \
         mock.patch.object(ufp, "_fetch_arxiv",
                           side_effect=AssertionError("arXiv must not be called")), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        abstract = ufp.backfill_abstract("Ferroelectric thin films")
    assert abstract == "S2 abstract text"
    # 链路顺序:窄搜 → 宽搜 → S2
    assert calls == [("openalex", False), ("openalex", True), ("s2", False)]


def test_backfill_abstract_full_degrade_returns_none():
    with mock.patch.object(ufp, "_fetch_openalex", return_value=None), \
         mock.patch.object(ufp, "_fetch_semantic_scholar", return_value={"data": []}), \
         mock.patch.object(ufp, "_fetch_arxiv",
                           return_value="<feed><entry><title>Unrelated</title>"
                                       "<summary>x</summary></entry></feed>"), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        assert ufp.backfill_abstract("Ferroelectric thin films") is None
        assert ufp.backfill_abstract("") is None


# ---------- 画像构建与落盘 ----------

def test_build_profile_schema_keys():
    works = [{"title": "ferroelectric domain dynamics", "year": 2023,
              "venue": "PRB", "abstract": None}]
    with mock.patch.object(ufp, "scrape_scholar_works", return_value=[dict(w) for w in works]), \
         mock.patch.object(ufp, "backfill_abstract", return_value="回填摘要"), \
         mock.patch.object(ufp, "_request_sleep", side_effect=_no_sleep):
        profile = ufp.build_profile(provider=None)
    # primary 是主学者(Hongyu Yu)单独一份画像：周报非顶刊闸门要判「与他强相关」，
    # 用五人并集分判不了（并集 205 个关键词里 155 个来自另外四位）。
    assert set(profile.keys()) == {"generated_at", "scholars", "primary", "our_work_zh", "keywords"}
    assert profile["primary"]["name"] == ufp.PRIMARY_SCHOLAR
    # 逐人关键词不再被 extend 进并集后丢弃
    assert all("keywords" in s for s in profile["scholars"])
    assert profile["generated_at"]
    assert len(profile["scholars"]) == len(ufp.SCHOLARS)
    s0 = profile["scholars"][0]
    assert set(s0.keys()) == {"scholar_id", "name", "works", "directions_zh", "keywords"}
    assert s0["works"][0]["abstract"] == "回填摘要"
    assert s0["works"][0]["year"] == 2023
    # 无 provider:蒸馏字段留空,关键词退化为标题词频(5 位学者同题 → ferroelectric 入选)
    assert s0["directions_zh"] == ""
    assert profile["our_work_zh"] == ""
    assert "ferroelectric" in profile["keywords"]


def test_save_profile_roundtrip():
    profile = {
        "generated_at": "2026-07-29",
        "scholars": [{"scholar_id": "x", "name": "S",
                      "works": [{"title": "t", "year": 2024, "venue": "v",
                                 "abstract": None}],
                      "directions_zh": "方向"}],
        "our_work_zh": "我们的工作",
        "keywords": ["ferroelectric"],
    }
    d = tempfile.mkdtemp()
    path = os.path.join(d, "sub", "focus_interests.json")  # 子目录不存在 → makedirs
    ufp.save_profile(profile, path)
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == profile


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] update_focus_profile")
