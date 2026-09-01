#!/usr/bin/env python3
"""authors_label 的 max_names 截断必须真的生效。

真实缺陷:期刊 RSS(Wiley/APS/arXiv 等)多把整串作者塞进 dc_creator 一个字段,
rss_fetcher._parse_authors 于是 append 出 authors == ["A, B, C, ..., P"];
normalize_author_names 旧实现只在 _looks_like_character_stream 分支里按逗号拆分,
普通分支原样返回,导致 len(cleaned) == 1,authors_label 的 `len(cleaned) > max_names`
永远为假 —— 整串 200+ 字符的作者名直接进 RSS description 和日报 meta 行,
"等N位作者" 从来不出现。

data/index.json 实测:5000 篇里 4367 篇 authors 只有一个元素,其中 2270 篇该元素
超过 60 字符;最长的一条 label 达 5214 字符。
"""

from author_utils import authors_label, normalize_author_names


def test_single_joined_string_is_split_and_truncated():
    """核心回归:一个逗号合并串必须被拆开并触发 max_names 截断。"""
    joined = ["A, B, C, D, E, F, G"]
    assert normalize_author_names(joined) == ["A", "B", "C", "D", "E", "F", "G"]
    label = authors_label(joined, max_names=6)
    assert label.endswith("等7位作者"), label
    assert "G" not in label.split("等")[0], label


def test_oxford_and_does_not_leak_into_a_name():
    """"A, B, and C" 按逗号拆完会残留 "and C",必须去掉前导连词。"""
    raw = ["Jingying Zhang, Sigang Wang, and Zongzhi Zhang"]
    assert normalize_author_names(raw) == [
        "Jingying Zhang", "Sigang Wang", "Zongzhi Zhang",
    ]
    for name in normalize_author_names(["Valentin Leeb and Johannes Knolle"]):
        assert not name.lower().startswith("and "), name


def test_parenthesised_affiliation_does_not_become_fake_authors():
    """arXiv 的 "姓名 (单位, 城市, 国家)" 形态,括号内逗号不能拆出假作者。"""
    raw = ["C. Kadlec (Institute of Physics, Prague, Czech Republic), "
           "F. Kadlec (Institute of Physics, Prague, Czech Republic)"]
    assert normalize_author_names(raw) == ["C. Kadlec", "F. Kadlec"]
    for bad in ("Prague", "Czech Republic"):
        assert bad not in normalize_author_names(raw)


def test_trailing_ror_url_block_is_dropped():
    """PNAS 把 ROR 链接+单位粘在名字后面;拆分不能拆出 "Cornell University" 这种假作者。"""
    raw = ["Ao ChenZhou-Quan WanChristopher Rotha"
           "https://ror.org/00sekdz59Center for Computational Quantum Physics, "
           "Flatiron Institute, New York, NY 10010"]
    names = normalize_author_names(raw)
    assert names == ["Ao ChenZhou-Quan WanChristopher Rotha"], names
    for bad in ("Flatiron Institute", "New York", "ror.org"):
        assert not any(bad in n for n in names), names


def test_name_suffix_is_not_promoted_to_an_author():
    """"Xu Wang, Jr." 里的 Jr. 要接回上一位,不能多出一位叫 "Jr." 的作者。"""
    names = normalize_author_names(["Xu Wang, Jr., Li Chen"])
    assert names == ["Xu Wang, Jr.", "Li Chen"], names


def test_trailing_initial_period_is_preserved():
    """真实语料里有 "Chandrasekar S. N." 这类以缩写结尾的名字,句点不能被剥掉。"""
    names = normalize_author_names(["Chandrasekar S. N., Sai Muthukumar V."])
    assert names == ["Chandrasekar S. N.", "Sai Muthukumar V."], names


def test_scalar_string_is_split_too():
    """非 list 的标量字符串走另一条分支,旧实现同样整串返回。"""
    assert normalize_author_names("A, B, C") == ["A", "B", "C"]
    assert authors_label("A, B, C, D, E, F, G", max_names=6).endswith("等7位作者")


def test_never_returns_empty_for_non_empty_input():
    """fail-soft:拆不出东西时原样保留,绝不把已有作者信息清空。"""
    for raw in (["(Anonymous)"], ["..."], "(Collaboration)"):
        assert normalize_author_names(raw), f"{raw!r} 被清空了"


# ---- 以下确保修复没有破坏原有正确行为 ----

def test_character_stream_branch_still_works():
    """feedparser 偶尔把作者串拆成单字符列表,这条老分支必须照常工作。"""
    stream = list("Alice Smith, Bob Jones, Carol Chen")
    assert normalize_author_names(stream) == ["Alice Smith", "Bob Jones", "Carol Chen"]


def test_plain_name_list_is_unchanged():
    assert normalize_author_names(["Alice", "Bob"]) == ["Alice", "Bob"]
    assert authors_label(["Alice", "Bob"]) == "Alice, Bob"
    assert authors_label(["Alice", "Bob"], max_names=2) == "Alice, Bob"


def test_empty_inputs_still_empty():
    for raw in (None, [], "", ["", ""]):
        assert normalize_author_names(raw) == []
        assert authors_label(raw) == ""


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] author_utils 作者拆分/截断回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
