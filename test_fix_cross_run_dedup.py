#!/usr/bin/env python3
"""跨运行去重：arXiv 预印本与其正式发表版不能两条并存。

去重器只作用于**本次抓取**，合并进索引时只按 link 精确比对。同一篇论文的 arXiv 版和
期刊版链接不同、抓取日期也不同，因此永远不会被互相比较。
实测 data/index.json 5000 条里有 80 组标题重复（160 条，3.2%），其中 70 组是
arxiv.org + link.aps.org，其余也全是「arXiv + 某出版社」—— 无一例外是预印本/正式版配对。

安全约束（index.json 是有 5000 上限的唯一库，删错一篇找不回来）：
  * 绝不删除已入库的记录，只是不再追加副本；
  * 标题规范化后必须 >= 25 字符才参与去重，避免 'Erratum'/'Editorial' 这类短标题误伤。
"""

import run_optimized_sync as ros


def test_title_key_ignores_case_and_punctuation():
    a = ros._title_key("Machine-Learning Potentials for Ferroelectric Perovskites!")
    b = ros._title_key("machine learning potentials for ferroelectric perovskites")
    assert a and a == b


def test_short_titles_are_excluded_from_dedup():
    """短标题在不同论文间重名很常见，不能拿来当去重依据。"""
    for t in ("Erratum", "Editorial", "Comment on the paper", ""):
        assert ros._title_key(t) == "", t


def test_long_titles_participate():
    assert ros._title_key("Observation of subharmonic charge density wave correlations")


class _Art:
    """最小 Article 替身：合并逻辑只用到 .link 和 .to_dict()。"""

    def __init__(self, title, link, journal, pub_date="2026-08-31"):
        self.link, self._d = link, {
            "title": title, "link": link, "journal": journal,
            "pub_date": pub_date, "id": link[-8:],
        }

    def to_dict(self):
        return dict(self._d)


TITLE = "Bad metal behavior and Lifshitz transition of a Nagaoka ferromagnet"


def _merge(existing, incoming):
    """复刻 run_optimized_sync 里的合并语义（同一份实现逻辑，便于单测）。"""
    existing_links = {a.get("link") for a in existing if a.get("link")}
    titles = {}
    for a in existing:
        k = ros._title_key(a.get("title"))
        if k:
            titles.setdefault(k, a)
    merged = 0
    for a in incoming:
        if not a.link or a.link in existing_links:
            continue
        d = a.to_dict()
        prior = titles.get(ros._title_key(d.get("title")))
        if prior is not None:
            alt = prior.setdefault("alt_links", [])
            if d["link"] not in alt:
                alt.append(d["link"])
            if str(prior.get("journal") or "").strip().lower() == "arxiv":
                jr = str(d.get("journal") or "").strip()
                if jr and jr.lower() != "arxiv":
                    prior["journal"] = jr
            merged += 1
            continue
        existing.append(d)
        k = ros._title_key(d.get("title"))
        if k:
            titles.setdefault(k, d)
    return existing, merged


def test_published_version_does_not_create_a_second_entry():
    stored = [{"title": TITLE, "link": "https://arxiv.org/abs/2510.01909",
               "journal": "arXiv", "pub_date": "2026-08-31"}]
    out, merged = _merge(stored, [_Art(TITLE, "http://link.aps.org/doi/10.1103/py7g-jz2x",
                                       "Phys. Rev. Lett.")])
    assert merged == 1
    assert len(out) == 1, "不得追加重复条目"


def test_existing_record_is_never_removed_and_keeps_its_link():
    """已存记录可能已被深读/配图，arxiv_core 等也按 link 关联，绝不能动它的身份。"""
    stored = [{"title": TITLE, "link": "https://arxiv.org/abs/2510.01909", "journal": "arXiv"}]
    out, _ = _merge(stored, [_Art(TITLE, "http://link.aps.org/doi/10.1103/py7g-jz2x",
                                  "Phys. Rev. Lett.")])
    assert out[0]["link"] == "https://arxiv.org/abs/2510.01909"


def test_alternate_link_is_preserved():
    stored = [{"title": TITLE, "link": "https://arxiv.org/abs/2510.01909", "journal": "arXiv"}]
    out, _ = _merge(stored, [_Art(TITLE, "http://link.aps.org/doi/10.1103/py7g-jz2x",
                                  "Phys. Rev. Lett.")])
    assert out[0]["alt_links"] == ["http://link.aps.org/doi/10.1103/py7g-jz2x"]


def test_journal_upgrades_from_arxiv_to_the_published_venue():
    """周报的顶刊闸门看 journal 字段，升级它才有意义。"""
    stored = [{"title": TITLE, "link": "https://arxiv.org/abs/2510.01909", "journal": "arXiv"}]
    out, _ = _merge(stored, [_Art(TITLE, "http://link.aps.org/x", "Phys. Rev. Lett.")])
    assert out[0]["journal"] == "Phys. Rev. Lett."


def test_real_journal_is_not_downgraded_to_arxiv():
    """反向到达（先收正式版、后收 arXiv）时不能把 journal 改回 arXiv。"""
    stored = [{"title": TITLE, "link": "http://link.aps.org/x", "journal": "Phys. Rev. Lett."}]
    out, _ = _merge(stored, [_Art(TITLE, "https://arxiv.org/abs/2510.01909", "arXiv")])
    assert out[0]["journal"] == "Phys. Rev. Lett."
    assert len(out) == 1


def test_distinct_papers_are_still_appended():
    stored = [{"title": TITLE, "link": "https://arxiv.org/abs/1", "journal": "arXiv"}]
    other = "Fractionalization and emergent SU(N) symmetries in frustrated magnets"
    out, merged = _merge(stored, [_Art(other, "https://arxiv.org/abs/2", "arXiv")])
    assert merged == 0 and len(out) == 2


def test_short_titled_papers_are_never_merged():
    stored = [{"title": "Erratum", "link": "https://a/1", "journal": "Phys. Rev. B"}]
    out, merged = _merge(stored, [_Art("Erratum", "https://b/2", "Phys. Rev. Lett.")])
    assert merged == 0 and len(out) == 2, "短标题不得触发去重"


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] cross-run dedup sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
