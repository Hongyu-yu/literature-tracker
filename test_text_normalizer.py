#!/usr/bin/env python3
"""Sanity tests for mojibake / LaTeX text repair helpers."""

from text_normalizer import is_suspicious_text, normalize_text


def main() -> int:
    mojibake = "è½¨é\x81\x93å¡\x9eè´\x9då\x85\x8bæ\x95\x88åº\x94"
    assert normalize_text(mojibake) == "轨道塞贝克效应"

    latex = 'J\\"orn St\\"ohler, V. Bal\\\'edent, St\\v{r}eda, {\\L}ukasz'
    fixed_latex = normalize_text(latex)
    assert "Jörn" in fixed_latex
    assert "Stöhler" in fixed_latex
    assert "Balédent" in fixed_latex
    assert "Středa" in fixed_latex
    assert "Łukasz" in fixed_latex

    formula = "Mn$_2$Ru$_{1-x}$Ga and 4\\times10^10"
    fixed_formula = normalize_text(formula)
    assert "Mn₂Ru₁₋ₓGa" in fixed_formula
    assert "4×10¹⁰" in fixed_formula

    assert is_suspicious_text(mojibake)
    assert is_suspicious_text(latex)
    assert not is_suspicious_text("正常中文标题")

    print("[OK] text normalizer sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_strip_announce_prefix_removes_arxiv_rss_boilerplate():
    """arXiv RSS 的公告前缀不是摘要正文，却被存进 abstract 再翻译进 abstract_zh_full。

    实测 data/index.json 5000 篇里 3344 处英文摘要、144 处中译摘要以它开头，
    日报邮件与周报卡片都会把它当正文显示出来。
    """
    from text_normalizer import strip_announce_prefix as strip

    assert strip("arXiv:2608.28719v1 Announce Type: new Abstract: We study X.") == "We study X."
    assert strip("arXiv:2608.27850v1 Announce Type: cross Abstract: Catalysis.") == "Catalysis."
    assert strip("arXiv:2502.17050v3 Announce Type: replace Abstract: Segregation.") == "Segregation."
    # 中译版本（zh_enricher 把前缀一起翻译了），两种见过的写法都要认
    assert strip("arXiv:2608.22177v1；公告类型：新提交。本文研究中子气体。") == "本文研究中子气体。"
    assert strip("arXiv:2604.20821v3 发布类型：替换 摘要：深度生成模型的预测。") == "深度生成模型的预测。"


def test_strip_announce_prefix_leaves_real_text_alone():
    """只剥开头的公告前缀，绝不碰正文——正文里提到 arXiv 是正常的。"""
    from text_normalizer import strip_announce_prefix as strip

    assert strip("We study X.") == "We study X."
    assert strip("arXiv 数据集上的实验表明模型可迁移。") == "arXiv 数据集上的实验表明模型可迁移。"
    assert strip("The dataset is hosted on arXiv:2601.00001 for reference.") == \
        "The dataset is hosted on arXiv:2601.00001 for reference."
    assert strip("") == ""
    assert strip(None) == ""
