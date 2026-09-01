#!/usr/bin/env python3
"""无花括号上/下标不得吞掉普通单词、连字符和英文右括号。

真实缺陷：SUBSCRIPT_RE/SUPERSCRIPT_RE 的非花括号分支曾经是贪婪字符类
    _(?P<single>[0-9aehijklmnoprstuvx+\\-=()]+)
    \\^(?P<single>[0-9in+\\-=()]+)
`-` 和大半小写字母都在类里，而 _decode_latex 又先把 `$` 删掉，于是：

    Si$_3$N$_4$-on-sapphire  -> Si₃N₄₋ₒₙ₋ₛₐₚₚₕᵢᵣₑ   材料名不可读、"on sapphire" 不再可检索
    train_test_split         -> trainₜₑₛₜₛₚₗᵢₜ
    ?dgcid=rss_sd_all        -> ?dgcid=rssₛdₐₗₗ      链接参数被写坏
    (SiO_2)                  -> (SiO₂₎               英文右括号被吃进下标
    F_1-score                -> F₁₋ₛcore
    L^\\infty                 -> Lⁱⁿfty （第二遍 _decode_latex 把 ^infty 的 in 当上标）

这些坏文本会被 rss_fetcher.Article.__init__ 直接写进 data/index.json，再进日报/周报/
邮件，并喂给 LLM 做摘要与相关性打分。data/index.json 里今天仍能查到 300+ 处这类残骸。

修复后非花括号分支只认三种形态：数字串(可带正负号) / 短括号组 / 单个可转写字符，
且后两种后面不许紧跟数字、小写字母或下划线。匹配不上就保持原样（T_c 一直如此），
只是不再美化——绝不会把好文本改坏。
"""

from text_normalizer import normalize_text


def test_hyphenated_tail_is_not_swallowed_into_subscript():
    """finding 62 原样本：整段 "-on-sapphire" 曾被吞进下标。"""
    out = normalize_text(r"a 500-nm-thick Si$_3$N$_4$-on-sapphire platform")
    assert out == "a 500-nm-thick Si₃N₄-on-sapphire platform", out
    assert "ₒₙ" not in out
    assert "-on-sapphire" in out


def test_snake_case_identifiers_are_left_alone():
    """标识符 / 链接参数不是下标，必须原样保留（保留 `_` 才能被搜索和复制）。"""
    for src in ("train_test_split accuracy",
                "deep_learning_model",
                "https://example.org/x?dgcid=rss_sd_all",
                "Subject_term_id: applied-physics",
                "K_stat and I_split and J_parallel"):
        assert normalize_text(src) == src, normalize_text(src)


def test_closing_paren_is_not_eaten_by_script():
    """英文括号里的化学式：右括号被吞掉后括号不配对，正文直接读不通。"""
    assert normalize_text(r"silicon dioxide (SiO$_2$) on silicon") == \
        "silicon dioxide (SiO₂) on silicon"
    assert normalize_text("force field (UF^3) potential") == "force field (UF³) potential"
    assert normalize_text("LiCaYb_5(BO_3)_6") == "LiCaYb₅(BO₃)₆"
    assert normalize_text("BaFe_2(As_{1-x}P_x)_2") == "BaFe₂(As₁₋ₓPₓ)₂"


def test_hyphen_between_scripts_stays_a_hyphen():
    """J_1-J_2-J_3 是三个耦合常数的并列，不是一个大下标。"""
    assert normalize_text("J_1-J_2-J_3 Ising model") == "J₁-J₂-J₃ Ising model"
    assert normalize_text("99.81% F$_1$-score") == "99.81% F₁-score"
    assert normalize_text("MoS_2-based device") == "MoS₂-based device"


def test_equation_sign_after_script_is_preserved():
    """D_s=2 是等式，不能变成 Dₛ₌₂。"""
    assert normalize_text("D_s=2") == "Dₛ=2"
    assert normalize_text("p_i=eX_i/Z_C") == "pᵢ=eXᵢ/Z_C"
    assert normalize_text("O((N_m+1)N_zN_r)") == "O((Nₘ+1)N_zNᵣ)"


def test_superscript_does_not_swallow_word_on_second_pass():
    r"""\infty 不在命令表里，反斜杠被清掉后第二遍不能把 ^infty 的 "in" 当成上标。"""
    out = normalize_text(r"the $L^\infty_z$ norm")
    assert "ⁱⁿ" not in out, out
    assert "infty" in out, out


def test_chemistry_and_units_are_still_prettified():
    """成功路径不许回归：真正的上/下标仍然要转成 Unicode。"""
    assert normalize_text(r"Mn$_2$Ru$_{1-x}$Ga and 4\times10^10") == "Mn₂Ru₁₋ₓGa and 4×10¹⁰"
    assert normalize_text(r"$\mathrm{H_2O}$ \& 50\%") == "H₂O & 50%"
    assert normalize_text("FeTe_xSe_{1-x}") == "FeTeₓSe₁₋ₓ"
    assert normalize_text("LiHo_xEr_{1-x}F_4") == "LiHoₓEr₁₋ₓF₄"
    assert normalize_text("Bi_2Se_3 and CO_2 and x_i and y_j") == "Bi₂Se₃ and CO₂ and xᵢ and yⱼ"
    assert normalize_text("n_+ and n_- and e^-+e^+") == "n₊ and n₋ and e⁻+e⁺"


def test_digit_runs_next_to_lowercase_units_still_convert():
    """纯数字串吞不掉字母，物理量纲天天这么写（m^2g^-1、10^9atoms），必须照常美化。"""
    assert normalize_text("1,000 cm^2V^-1s^-1") == "1,000 cm²V⁻¹s⁻¹"
    assert normalize_text(r"(~260 $m^2g^{-1}$)") == "(~260 m²g⁻¹)"
    assert normalize_text("2.3x10^9atoms/s") == "2.3x10⁹atoms/s"
    assert normalize_text(r"$2\mu_0p/B^2$") == "2μ₀p/B²"
    # _12a 不能回溯成 ₁ + "2a"
    assert normalize_text("x_12a") == "x₁₂a"


def test_parenthesised_script_group_still_works():
    """非线性光学里 chi^(2)、g^(2)(0) 是常见的纯文本写法。"""
    assert normalize_text("chi^(2) and g^(2)(0) values") == "chi⁽²⁾ and g⁽²⁾(0) values"
    assert normalize_text(r"$\chi^{(1)}$ susceptibility") == "χ⁽¹⁾ susceptibility"


def test_normalize_is_idempotent_on_repaired_text():
    """normalize_text 内部会跑两遍 _decode_latex，结果必须收敛。"""
    for src in (r"a 500-nm-thick Si$_3$N$_4$-on-sapphire platform",
                "https://example.org/x?dgcid=rss_sd_all",
                r"silicon dioxide (SiO$_2$)",
                "1,000 cm^2V^-1s^-1",
                "J_1-J_2-J_3 Ising model"):
        once = normalize_text(src)
        assert normalize_text(once) == once, (src, once, normalize_text(once))


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] text_normalizer 上/下标边界检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
