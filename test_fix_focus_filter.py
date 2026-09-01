#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""focus_filter 审计修复的回归测试。

覆盖四件事：
  1. 否定词整词匹配改成一条预编译 alternation（原来每个词跑一次全文 re.search）；
  2. 'magent' 拼写错误导致整族磁学词漏检，且关键词表被抄了两份；
  3. daily_focus_priority 里那次结果被丢弃的 analyze_focus 全文扫描；
  4. analyze_focus 按文本缓存 + focus_priority/topic_bucket 不再重复扫全文；
  5. filter_daily_focus_items 的 min_keep 从“沉默的空承诺”变成可见日志（仍不补位）。

约定：模块级 def test_xxx() 且无必填参数，才会被 run_tests.py 真正执行；
只用 unittest.mock + 标准库，不用 pytest fixture。
"""

import io
import re
import sys
from contextlib import redirect_stdout
from unittest import mock

import focus_filter


# ---------------------------------------------------------------- 1. 整词匹配

def test_whole_term_negative_scan_uses_one_precompiled_pass():
    """133 个否定词不该变成 133 次全文 re.search。

    修复前：_has_any 对每个 ASCII 词现拼一条 pattern 再 re.search，
    NEGATIVE_CLINICAL_TERMS 一次调用就是 40+ 次扫描（本断言会失败）。
    修复后：整张词表编译成一条 alternation，只扫一遍。
    """
    text = ('machine learning interatomic potential for ferroelectric perovskites; '
            'we train an equivariant graph neural network on dft data.')
    real_search = re.search
    with mock.patch('re.search', wraps=real_search) as spy:
        hit = focus_filter._has_any(text, focus_filter.NEGATIVE_CLINICAL_TERMS, whole_term=True)
    assert hit is False
    assert spy.call_count <= 1, f'整词匹配跑了 {spy.call_count} 次 re.search，应当只有一条预编译正则'


def test_whole_term_matching_keeps_word_boundaries():
    """预编译 alternation 必须和逐词匹配语义完全一致（词边界 + 分支回溯）。"""
    assert focus_filter._has_any('protein folding kinetics', ('protein',), whole_term=True) is True
    # 前后粘连字母时不算命中，否则 'nanoproteins' 会把材料学论文误判成生命科学
    assert focus_filter._has_any('nanoproteins assembly', ('protein',), whole_term=True) is False
    # 短分支先失败、长分支才命中：alternation 必须在同一起点把所有分支试完
    assert focus_filter._has_any('the rats ran', ('rat', 'rats'), whole_term=True) is True
    # 非 ASCII 词继续走子串匹配（中文没有 [a-z0-9] 词边界可言）
    assert focus_filter._has_any('本文研究临床数据', ('临床',), whole_term=True) is True
    # 真实词表上的整词语义
    assert focus_filter._has_any('a study of patients in hospital',
                                 focus_filter.NEGATIVE_CLINICAL_TERMS, whole_term=True) is True


def test_whole_term_empty_list_never_matches_anything():
    """空词表不能被编译成 (?:) —— 那会在任意标点边界命中，等于“全命中”。"""
    text = 'quantum spin liquid, moire superlattice; ferroelectric domain walls.'
    assert focus_filter._has_any(text, (), whole_term=True) is False
    assert focus_filter._has_any(text, ('', ''), whole_term=True) is False
    # 三张“暂不启用”的空词表同理（daily_focus_priority 的分档依赖它们返回 False）
    for terms in (focus_filter.DAILY_TITLE_CHEMISTRY_TERMS,
                  focus_filter.DAILY_TITLE_MATERIALS_TERMS,
                  focus_filter.DAILY_TITLE_SIMULATION_TERMS):
        assert focus_filter._has_any(text, terms, whole_term=True) is False


# ------------------------------------------------------- 2. magnet 词族 / 词表

def test_daily_focus_covers_the_whole_magnet_word_family():
    """magnetoresistance 这类标题必须进日报候选并落到 band 2，而不是最低档。

    修复前词表里是拼错的 'magent'，'magnetic' 又盖不住 magnetoresistance /
    magnetism / magnetization / paramagnetism，实测 145 篇磁学标题被打成 band 4。
    """
    item = {
        'title': 'Unconventional Room-Temperature Antisymmetric Magnetoresistance in '
                 'van der Waals Fe3GaTe2/Pt Heterostructures',
        'journal': 'Advanced Science',
        'abstract': 'Advanced Science, Volume 13, Issue 48.',
    }
    assert focus_filter.is_daily_focus(item) is True
    assert focus_filter.daily_focus_priority(item) == (2,)

    for title in ('Role of defects in the paramagnetism of Fe-doped Cs2AgBiBr6',
                  'Off-Diagonal dipolar interactions in the mixed Ising--XY magnet LiHoxEr1-xF4',
                  'Magnetoelastic interlayer coupling in Ni90Fe10/Cu/Fe70Ga30 trilayers'):
        art = {'title': title, 'journal': 'Phys. Rev. B'}
        assert focus_filter.daily_focus_priority(art) == (2,), title


def test_daily_keyword_gate_reads_the_shared_term_tuples():
    """is_daily_focus 的关键词必须来自 DAILY_TITLE_* 词表本身，不能是抄来的字面量。

    修复前 is_daily_focus 里写死了一份副本，两处各自漂移，
    'magent' 的拼写错误因此在两个地方都躺着没被发现。
    """
    item = {
        'title': 'Thermal transport in perovskite thin films',
        'journal': 'Advanced Materials',
        'abstract': 'Advanced Materials, Volume 1, Issue 2.',
    }
    assert focus_filter.analyze_focus(item)['target_domain'] is True
    assert focus_filter.is_daily_focus(item) is False

    patched = focus_filter.DAILY_TITLE_PHYSICS_TERMS + ('perovskite',)
    with mock.patch.object(focus_filter, 'DAILY_TITLE_PHYSICS_TERMS', patched):
        assert focus_filter.is_daily_focus(item) is True, '扩充词表后关键词门槛没有跟着变'
    assert focus_filter.is_daily_focus(item) is False


# ------------------------------------------------- 3./4. 不再重复扫描全文

def _count_item_text_calls(fn, item):
    """统计一次调用里 _item_text（全文拼接 + 归一化）被跑了几次。"""
    calls = []
    real = focus_filter._item_text

    def spy(it):
        calls.append(it)
        return real(it)

    with mock.patch.object(focus_filter, '_item_text', spy):
        fn(item)
    return len(calls)


def test_daily_focus_priority_does_not_scan_the_full_text():
    """日报分档只看标题；修复前它还会白跑一次 analyze_focus 全文扫描。"""
    item = {
        'title': 'Machine learning interatomic potential for ferroelectric perovskites',
        'abstract': 'We train an equivariant model on DFT data of BaTiO3.',
        'journal': 'npj Computational Materials',
    }
    assert focus_filter.daily_focus_priority(item) == (0,)
    assert _count_item_text_calls(focus_filter.daily_focus_priority, item) == 0


def test_focus_priority_analyzes_each_item_only_once():
    """focus_priority 修复前调了 analyze_focus，又通过 topic_bucket 再调一次。"""
    item = {
        'title': 'First-principles study of quantum spin Hall effect in moire materials',
        'journal': 'Phys. Rev. Lett.',
    }
    assert _count_item_text_calls(focus_filter.focus_priority, item) == 1


def test_analyze_focus_reuses_signals_for_the_same_text():
    """同一篇文章在一轮里被问 4~5 次，不该把 400 个关键词重扫 4~5 遍。"""
    item = {
        'title': 'Equivariant neural network potential for ferroelectric perovskites',
        'abstract': 'We train MACE on BaTiO3 and predict polarization switching.',
        'journal': 'npj Computational Materials',
    }
    first = focus_filter.analyze_focus(item)  # 预热

    calls = []
    real = focus_filter._has_any

    def spy(*args, **kwargs):
        calls.append(args[:1])
        return real(*args, **kwargs)

    with mock.patch.object(focus_filter, '_has_any', spy):
        again = focus_filter.analyze_focus(item)
    assert again == first
    assert calls == [], f'重复调用又跑了 {len(calls)} 次关键词扫描，缓存没生效'


def test_analyze_focus_returns_a_private_copy():
    """缓存里的 dict 不能外泄：调用方改写返回值不许污染后续判定。"""
    item = {'title': 'Ferroelectric domain walls in BaTiO3', 'journal': 'Phys. Rev. B'}
    first = focus_filter.analyze_focus(item)
    first['target_domain'] = 'tampered'
    second = focus_filter.analyze_focus(item)
    assert second['target_domain'] is True
    assert second is not first


def test_analyze_focus_follows_in_place_edits_of_the_item():
    """run_optimized_sync 会就地富化条目；缓存必须按文本失效，不能按 id(item)。"""
    item = {'title': 'Ferroelectric domain walls in BaTiO3', 'journal': 'Phys. Rev. B'}
    before = focus_filter.analyze_focus(item)
    assert before['has_ai'] is False
    item['abstract'] = 'A graph neural network potential trained on DFT data.'
    after = focus_filter.analyze_focus(item)
    assert after['has_ai'] is True

    # 反向：同一个 dict 对象被回收/复用也不能串味（用文本做 key 天然免疫）
    other = {'title': 'Ferroelectric domain walls in BaTiO3', 'journal': 'Phys. Rev. B'}
    assert focus_filter.analyze_focus(other)['has_ai'] is False


def test_topic_bucket_from_signals_matches_topic_bucket():
    """抽出来的 topic_bucket_from_signals 必须和原函数结论一致。"""
    for item in (
        {'title': 'Quantum spin liquid in a kagome antiferromagnet', 'journal': 'Phys. Rev. Lett.'},
        {'title': 'Machine learning assisted alkene polymerization mechanism', 'journal': 'ACS'},
        {'title': 'Perovskite solar cell interface engineering', 'journal': 'Advanced Materials'},
        {'title': 'A transformer for tabular data', 'journal': 'arXiv'},
    ):
        signals = focus_filter.analyze_focus(item)
        assert focus_filter.topic_bucket_from_signals(signals) == focus_filter.topic_bucket(item)


# ----------------------------------------------------------- 5. min_keep 可见

_P1_ITEM = {
    'title': 'Machine learning interatomic potential for ferroelectric perovskites',
    'journal': 'arXiv',
    'arxiv_category': 'cond-mat.mtrl-sci',
    'link': 'http://example.org/p1',
}
_OFFTOPIC_ITEM = {
    'title': 'Deep learning from routine histology improves risk stratification in prostate cancer patients',
    'journal': 'Nature',
    'link': 'http://example.org/offtopic',
}


def test_filter_daily_focus_items_reports_when_below_min_keep():
    """min_keep 达不到时必须留下日志；修复前这个参数在函数体里根本没被读过。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        selected, dropped = focus_filter.filter_daily_focus_items(
            [_P1_ITEM, _OFFTOPIC_ITEM], min_keep=12, max_keep=60)
    out = buf.getvalue()
    assert selected == [_P1_ITEM]
    assert dropped == [_OFFTOPIC_ITEM]
    assert '12' in out and '日报候选不足' in out, f'低产日没有任何提示: {out!r}'


def test_filter_daily_focus_items_never_backfills_offtopic_items():
    """凑数比短日报更糟：不达 min_keep 也绝不把非目标领域文章塞进来。"""
    with redirect_stdout(io.StringIO()):
        selected, _ = focus_filter.filter_daily_focus_items(
            [_P1_ITEM, _OFFTOPIC_ITEM], min_keep=12, max_keep=60)
    assert _OFFTOPIC_ITEM not in selected
    assert len(selected) == 1


def test_filter_daily_focus_items_is_quiet_when_the_floor_is_met():
    """正常日不该有任何噪音日志。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        selected, _ = focus_filter.filter_daily_focus_items(
            [_P1_ITEM], min_keep=1, max_keep=60)
    assert selected == [_P1_ITEM]
    assert buf.getvalue() == ''


def test_filter_daily_focus_items_tolerates_a_broken_min_keep():
    """min_keep 传成 None/字符串也不能把日报流程搞崩（fail-soft）。"""
    for bad in (None, '', 'twelve', 0, -3):
        with redirect_stdout(io.StringIO()):
            selected, _ = focus_filter.filter_daily_focus_items(
                [_P1_ITEM], min_keep=bad, max_keep=60)
        assert selected == [_P1_ITEM]


def main() -> int:
    failed = 0
    for name in sorted(n for n in globals() if n.startswith('test_')):
        fn = globals()[name]
        if not callable(fn):
            continue
        try:
            fn()
            print(f'✓ {name}')
        except Exception as exc:  # noqa: BLE001 - 本地跑测试要看全部失败项
            failed += 1
            print(f'✗ {name}: {type(exc).__name__}: {exc}')
    print(f'\n{failed} failed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
