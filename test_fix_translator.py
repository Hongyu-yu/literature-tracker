"""translator 翻译失败时不得把英文原文伪装成译文返回。

历史现场：`Translator.translate` 有两条静默的「英文直通」路径 ——
  * AI 分支 `return (resp or "").strip() or clean_text`：provider 返回空体时交还英文；
  * `except Exception: return text`：连 HTML 标签都一起还给调用方（连 clean_text 都不是）。
两条路径都不向调用方报告失败，函数的唯一契约只是「返回一个字符串」。于是英文被写进
abstract_zh / title_zh，而 backfill_zh.count_missing、generate_daily_pages.daily_quality_ok、
zh_enricher 的候选判断全都只看「非空」——被污染的行永远不会被重试，比留空还难修；
周报还会在「中文摘要」标题下渲染英文段落。

修复后 translate()/translate_text() 要么返回中文，要么抛 TranslationError；
调用方（weekly_summary 置空、zh_enricher 跳过、highlight_guarantee 放弃兜底）都已 try/except，
留空的字段下次运行自然重试。
"""

from unittest import mock

import translator as translator_mod
from translator import TranslationError, Translator


ENGLISH = (
    "Machine learning interatomic potentials reproduce the ferroelectric "
    "switching barrier of HfO2 at finite temperature."
)


class _FakeProvider:
    """假的 AI provider：按 behaviour 决定返回什么。"""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    def call_api(self, prompt):
        self.calls += 1
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour


class _FakeGoogle:
    """假的 GoogleTranslator：按顺序吐出预置结果。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def translate(self, text):
        self.calls.append(text)
        value = self.results.pop(0) if self.results else ""
        if isinstance(value, Exception):
            raise value
        return value


def _make_translator(*, provider=None, google=None):
    """绕开 __init__ 造一个纯内存 Translator（不碰 GoogleTranslator 真实构造/网络）。"""
    t = Translator.__new__(Translator)
    t.translator = google if google is not None else _FakeGoogle([])
    t._ai_provider = provider
    t._ai_provider_name = "openrouter"
    t._ai_key = "fake-key" if provider is not None else ""
    t._ai_model = None
    return t


def _assert_raises(fn, message):
    try:
        result = fn()
    except TranslationError:
        return
    raise AssertionError(f"{message}（实际返回了 {result!r}）")


# --- AI 分支 --------------------------------------------------------------

def test_ai_empty_response_does_not_return_english_source():
    """provider 返回空体：旧代码 `or clean_text` 把英文当译文交还，现在必须抛错。"""
    provider = _FakeProvider("")
    t = _make_translator(provider=provider)
    _assert_raises(lambda: t.translate(ENGLISH), "provider 返回空体时仍然把英文当成译文返回了")
    assert provider.calls == 1


def test_ai_whitespace_only_response_does_not_return_english_source():
    """只有空白的响应同样是失败，不能退回英文。"""
    t = _make_translator(provider=_FakeProvider("  \n\t "))
    _assert_raises(lambda: t.translate(ENGLISH), "provider 只返回空白时仍然把英文当成译文返回了")


def test_ai_exception_does_not_return_raw_html_source():
    """provider 抛错：旧代码 `return text` 连 HTML 标签一起还回去，现在必须抛错。"""
    html_source = f"<p>{ENGLISH}</p>"
    t = _make_translator(provider=_FakeProvider(RuntimeError("502 upstream")))
    _assert_raises(
        lambda: t.translate(html_source),
        "provider 抛错时仍然把带 HTML 标签的英文原文当成译文返回了",
    )


def test_untranslated_english_echo_is_rejected():
    """provider 限流降级原样吐回英文（或用英文说一句抱歉）——不含中文一律算失败。"""
    t = _make_translator(provider=_FakeProvider(ENGLISH))
    _assert_raises(lambda: t.translate(ENGLISH), "provider 原样吐回英文时被当成了成功译文")

    t2 = _make_translator(provider=_FakeProvider("Sorry, I cannot translate this text."))
    _assert_raises(lambda: t2.translate(ENGLISH), "英文的拒绝答复被当成了成功译文")


def test_ai_success_path_returns_translation_unchanged():
    """成功路径不变：仍然返回去掉首尾空白的中文译文。"""
    t = _make_translator(provider=_FakeProvider("\n机器学习原子间势可复现 HfO2 的铁电翻转势垒。\n"))
    assert t.translate(ENGLISH) == "机器学习原子间势可复现 HfO2 的铁电翻转势垒。"


def test_source_without_latin_letters_skips_the_cjk_check():
    """源文本本身没有拉丁字母（纯化学式/公式）时不做中文判据，避免误伤。"""
    t = _make_translator(provider=_FakeProvider("2 + 2 = 4"))
    assert t.translate("2 + 2 = 4") == "2 + 2 = 4"


# --- deep-translator 兜底分支 ---------------------------------------------

def test_google_fallback_echo_is_rejected():
    """deep-translator 翻不动时会原样返回输入，这同样不能写进 *_zh。"""
    t = _make_translator(google=_FakeGoogle([ENGLISH]))
    _assert_raises(lambda: t.translate(ENGLISH), "deep-translator 原样返回英文时被当成了成功译文")


def test_google_fallback_exception_does_not_return_source():
    t = _make_translator(google=_FakeGoogle([ConnectionError("network down")]))
    _assert_raises(lambda: t.translate(ENGLISH), "deep-translator 抛错时仍然返回了英文原文")


def test_google_fallback_missing_chunk_is_not_silently_truncated():
    """长文本分段翻译缺一段时，旧代码 ''.join 出半截译文照样返回。

    半截译文写进 abstract_zh 后同样会被下游判为「已翻译」而永不重试，
    所以必须整段作废、抛错让下次运行重来。
    """
    long_source = "Ferroelectric switching is studied with machine learning. " * 120
    assert len(long_source) > 4500
    google = _FakeGoogle(["第一段中文译文。", ""])
    t = _make_translator(google=google)
    with mock.patch.object(translator_mod.time, "sleep"):
        _assert_raises(
            lambda: t.translate(long_source),
            "分段翻译缺段时返回了半截译文",
        )


def test_google_fallback_success_path_unchanged():
    t = _make_translator(google=_FakeGoogle(["铁电翻转的机器学习研究。"]))
    assert t.translate(ENGLISH) == "铁电翻转的机器学习研究。"


# --- 空输入与调用方契约 ---------------------------------------------------

def test_empty_input_still_returns_empty_string():
    """空输入不是失败：仍然返回 ""，不抛错。"""
    t = _make_translator(provider=_FakeProvider("不应被调用"))
    for value in ("", "   ", "\n\t", "<p></p>", None):
        assert t.translate(value) == "", f"空输入 {value!r} 应返回空串"


def test_translation_error_is_catchable_by_existing_except_exception():
    """现有调用方都是 `except Exception`，TranslationError 必须能被它们兜住。"""
    assert issubclass(TranslationError, Exception)


def test_translate_text_propagates_failure_to_caller():
    """便捷函数不再吞掉失败——weekly_summary/zh_enricher 才能把字段留空而不是写英文。"""
    failing = _make_translator(provider=_FakeProvider(RuntimeError("gateway flaky")))
    with mock.patch.object(translator_mod, "translator", failing):
        _assert_raises(
            lambda: translator_mod.translate_text(ENGLISH),
            "translate_text 仍然把英文原文交给了调用方",
        )

        # weekly_summary.enhance_single_article 的写法：失败时置空，页面回退显示英文摘要，
        # 而不是在「中文摘要」标题下渲染英文。
        article = {"abstract": ENGLISH, "abstract_zh": ""}
        try:
            article["abstract_zh"] = translator_mod.translate_text(article["abstract"])
        except Exception:
            article["abstract_zh"] = ""
        assert article["abstract_zh"] == ""
        assert article["abstract"] == ENGLISH, "英文原摘要必须原样保留"


def test_failure_leaves_existing_zh_field_untouched():
    """zh_enricher 的写法：失败时 continue，已有的中文字段不被覆盖或清空。"""
    failing = _make_translator(provider=_FakeProvider(RuntimeError("gateway flaky")))
    with mock.patch.object(translator_mod, "translator", failing):
        article = {"title": "Ferroelectric HfO2", "title_zh": "铁电 HfO2 的旧译文"}
        try:
            article["title_zh"] = translator_mod.translate_text(article["title"])
        except Exception:
            pass
        assert article["title_zh"] == "铁电 HfO2 的旧译文"


def _run_all():
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✅ {name}")
            except AssertionError as e:
                failures.append(name)
                print(f"❌ {name}: {e}")
    print("全部通过" if not failures else f"失败: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
