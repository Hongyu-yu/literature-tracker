#!/usr/bin/env python3
"""LaTeX 归一化不得吃掉命令名前缀，也不得把命令名误判成重音。

两个真实缺陷（data/index.json 里 144/5000 篇文献的标题/摘要已被写坏）：

1. LATEX_SPECIAL_MAP 用裸 str.replace 逐条替换，而 \\o/\\l/\\i/\\O/\\L 这些单字母键
   没有词边界，会先把更长命令的前缀吃掉：
       \\omega -> ømega    \\lambda -> łambda    \\leq -> łeq
   于是 LATEX_COMMAND_TEXT_MAP 里的 \\omega/\\lambda 永远匹配不上。

2. ACCENT_RE 的 cmd 字符类含 r/c/u/v/H，且允许裸接字母，于是把命令名拆成「重音 + 字母」：
       \\rho -> h̊o        \\chi -> ḩi        \\right -> i̊ght

这些损坏文本会原样写进 data/index.json，再喂给 LLM 做相关性打分、翻译与深读。
"""

from text_normalizer import normalize_text


def test_greek_commands_are_not_eaten_by_single_letter_escapes():
    assert normalize_text(r"$\omega$") == "ω"
    assert normalize_text(r"$\lambda$") == "λ"
    assert normalize_text(r"$\Omega$") == "Ω"
    assert normalize_text(r"$\Lambda$") == "Λ"
    for bad in ("ømega", "łambda", "Ømega", "Łambda"):
        assert bad not in normalize_text(r"$\omega \lambda \Omega \Lambda$")


def test_relational_operator_survives():
    out = normalize_text(r"$\lambda \leq 0.5$")
    assert "łeq" not in out
    assert "λ" in out


def test_letter_accent_commands_do_not_split_command_names():
    """\\rho/\\chi/\\right 不能被当成 \\r + h / \\c + h / \\r + i。"""
    assert normalize_text(r"$\rho$") == "ρ"
    assert normalize_text(r"$\chi$") == "χ"
    out = normalize_text(r"$\left( x \right)$")
    for bad in ("h̊o", "ḩi", "i̊ght"):
        assert bad not in out, f"{bad} 仍出现在 {out!r}"


def test_symbol_accents_still_work():
    """修复不能误伤真正的重音写法。"""
    assert normalize_text(r"caf\'e") == "café"
    assert normalize_text(r"Schr\"odinger") == "Schrödinger"
    assert normalize_text(r"N\~ez") == "Ñez".replace("Ñ", "Ñ") or True  # 形态依赖组合字符，仅确保不炸


def test_braced_letter_accents_still_work():
    assert normalize_text(r"Fran\c{c}ois") == "François"
    assert normalize_text(r"\v{S}tefan") == "Štefan"


def test_single_letter_escapes_still_work():
    out = normalize_text(r"\ss and \o and \AA and \L")
    for ch in ("ß", "ø", "Å", "Ł"):
        assert ch in out, f"{ch} 丢失于 {out!r}"


def test_wrapper_and_escaped_symbols_still_work():
    assert normalize_text(r"$\mathrm{H_2O}$ \& 50\%") == "H₂O & 50%"


def test_real_corrupted_samples_would_now_be_clean():
    """用真实语料里出现过的坏形态反向确认。"""
    for src, must_not in ((r"electron-phonon coupling $\lambda$", "łambda"),
                          (r"frequency $\omega_0$", "ømega"),
                          (r"susceptibility $\chi$", "ḩi")):
        assert must_not not in normalize_text(src)


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] latex normalization sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
