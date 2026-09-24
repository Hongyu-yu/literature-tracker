"""
翻译模块

优先使用项目的 AI Provider（OpenRouter / Gemini 等）翻译；AI 未配置、或配置了但
调用失败（网关 503、限流、返回非中文）时，自动降级到机器翻译引擎链：

    Google（deep-translator） → Google gtx 接口 → MyMemory

2026-09 现场：网关连续多日返回 `503 no available channel for provider openai`，
而旧逻辑只在「没配 AI_API_KEY」时才用机翻 —— key 在、网关挂，翻译就整批失败，
09-16 起日报 72 篇文章里中文摘要 0 篇。所以机翻必须是「AI 失败」的兜底，而不只是
「AI 未配置」的替代。

契约：translate() / translate_text() / machine_translate() 要么返回**中文译文**，
要么抛 TranslationError。绝不把英文原文当译文返回 —— 详见 TranslationError 的说明。
"""

from __future__ import annotations

import os
from deep_translator import GoogleTranslator
import time
import re
from typing import Callable, List, Optional, Tuple

from ai_breaker import ai_available, record_ai_failure, record_ai_success
from ai_breaker import reset as reset_breakers  # noqa: F401  (测试与调用方经由本模块重置)
from ai_summarizer import build_provider
from text_normalizer import strip_announce_prefix


class TranslationError(RuntimeError):
    """翻译没有真正完成（provider 抛错 / 返回空 / 原样吐回英文）。

    旧行为是「失败时返回原文」：调用方拿到的仍然是一个字符串，于是英文被当成
    译文写进 *_zh 字段。后果是周报在「中文摘要」标题下渲染英文段落，而且
    backfill_zh.count_missing / generate_daily_pages.daily_quality_ok /
    zh_enricher 的候选判断都只看「非空」，被污染的行永远不会被重试 —— 这比留空
    更糟。所以失败必须显式抛出，由调用方决定（现有调用方都已 try/except：
    weekly_summary 置空、zh_enricher 跳过该条、highlight_guarantee 放弃兜底），
    留空的字段下次运行会自动重试。
    """


_LATIN_RE = re.compile(r"[A-Za-z]")


def _has_cjk(text: str) -> bool:
    """与 highlight_guarantee._has_cjk 保持同一判据。"""
    return any("一" <= ch <= "鿿" for ch in text or "")


# ---------------------------------------------------------------------------
# 机器翻译引擎
# ---------------------------------------------------------------------------
_HTTP_TIMEOUT = 20


def _gtx_translate(text: str) -> str:
    """Google 的 gtx 公共接口：与 deep-translator 走的网页端不是同一个限流桶。"""
    import requests

    resp = requests.post(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t"},
        data={"q": text},
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])


def _mymemory_translate(text: str) -> str:
    """MyMemory：免 key，匿名每天约 5000 字符；配 MYMEMORY_EMAIL 可提到约 5 万。"""
    import requests

    params = {"q": text, "langpair": "en|zh-CN"}
    email = (os.environ.get("MYMEMORY_EMAIL") or "").strip()
    if email:
        params["de"] = email
    resp = requests.get("https://api.mymemory.translated.net/get", params=params, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("quotaFinished"):
        raise TranslationError("MyMemory 当日额度已用完")
    status = int(data.get("responseStatus") or 0)
    if status and status != 200:
        raise TranslationError(f"MyMemory 返回状态 {status}: {data.get('responseDetails')}")
    return str((data.get("responseData") or {}).get("translatedText") or "")


# 同一引擎连续失败多少次后本进程内停用（被封 IP / 额度用完时不要每篇都再撞一次）
_ENGINE_FAILURE_LIMIT = 3


class Translator:
    def __init__(self):
        self.translator = GoogleTranslator(source='auto', target='zh-CN')
        self._ai_provider = None
        self._ai_provider_name = (os.environ.get("AI_PROVIDER") or "openrouter").strip()
        self._ai_key = (os.environ.get("AI_API_KEY") or "").strip()
        self._ai_model = (os.environ.get("AI_MODEL") or "").strip() or None
        self._mt_engines = self._default_engines()

    def _default_engines(self) -> List[Tuple[str, Callable[[str], str], int]]:
        """(名称, 翻译函数, 单次请求最大字符数)，按顺序尝试。"""
        return [
            ("google", self.translator.translate, 4500),
            ("google-gtx", _gtx_translate, 4500),
            ("mymemory", _mymemory_translate, 480),
        ]

    def _engines(self) -> List[Tuple[str, Callable[[str], str], int]]:
        engines = getattr(self, "_mt_engines", None)
        if engines is None:
            engines = self._mt_engines = self._default_engines()
        return engines

    @staticmethod
    def _clean(text: str) -> str:
        clean_text = re.sub(r'<[^>]+>', '', text or '')
        # arXiv RSS 的 "arXiv:xxxx Announce Type: new Abstract:" 前缀不是正文，
        # 不剥掉会被逐字翻进中文摘要（index.json 实测 144 处）。
        return strip_announce_prefix(clean_text).strip()

    def translate(self, text: str) -> str:
        """翻译文本到中文：AI 优先，失败自动降级机翻。

        空输入返回 ""（无事可做，不算失败）；成功返回中文译文；
        其余一律抛 TranslationError —— 见模块顶部的契约说明。
        """
        if not text or not text.strip():
            return ""
        clean_text = self._clean(text)
        if not clean_text:
            return ""

        if self._ai_key and ai_available():
            try:
                if self._ai_provider is None:
                    self._ai_provider = build_provider(self._ai_provider_name, self._ai_key, model=self._ai_model)
                # Keep translation prompt simple to reduce hallucination.
                prompt = (
                    "你是专业的学术翻译助手。请将下面英文翻译为简体中文，保持术语准确，"
                    "只输出译文，不要解释：\n\n"
                    f"{clean_text}\n"
                )
                try:
                    resp = self._ai_provider.call_api(prompt)
                except Exception as e:
                    record_ai_failure(e)   # 只有「调用抛错」才算网关故障
                    raise
                record_ai_success()
                return self._verified(resp, clean_text)
            except Exception as e:
                print(f"⚠️ AI 翻译失败，改用机器翻译: {str(e)[:160]}")

        return self.machine_translate(clean_text)

    def machine_translate(self, text: str) -> str:
        """只走机器翻译引擎链（不碰 AI）。全部失败抛 TranslationError。"""
        if not text or not text.strip():
            return ""
        clean_text = self._clean(text)
        if not clean_text:
            return ""
        failures = getattr(self, "_engine_failures", None)
        if failures is None:
            failures = self._engine_failures = {}

        errors = []
        for name, fn, max_chunk in self._engines():
            if failures.get(name, 0) >= _ENGINE_FAILURE_LIMIT:
                continue
            try:
                result = self._translate_chunks(fn, clean_text, max_chunk)
                failures[name] = 0
                return result
            except Exception as e:
                failures[name] = failures.get(name, 0) + 1
                errors.append(f"{name}: {str(e)[:120]}")
                if failures[name] == _ENGINE_FAILURE_LIMIT:
                    print(f"🛑 机翻引擎 {name} 连续失败 {_ENGINE_FAILURE_LIMIT} 次，本次运行停用")
        detail = "; ".join(errors) or "所有机翻引擎均已停用"
        print(f"⚠️ 翻译失败: {detail}")
        raise TranslationError(f"机器翻译全部失败: {detail}")

    def _translate_chunks(self, fn: Callable[[str], str], clean_text: str, max_chunk: int) -> str:
        if len(clean_text) <= max_chunk:
            return self._verified(fn(clean_text), clean_text)
        # 有字符上限的引擎需要分段翻译
        translated_chunks = []
        for chunk in self._split_text(clean_text, max_chunk):
            translated = fn(chunk)
            # 缺一段就整段作废：半截译文写进 abstract_zh 后同样会被当成
            # 「已翻译」而永不重试，比留空更难修。
            if not (translated or "").strip():
                raise TranslationError("分段翻译缺失其中一段，放弃本次译文")
            translated_chunks.append(translated.strip())
            time.sleep(0.5)  # 避免请求过快
        return self._verified(''.join(translated_chunks), clean_text)

    @staticmethod
    def _verified(translated, source: str) -> str:
        """确认返回的确实是译文，否则抛 TranslationError。"""
        result = (translated or "").strip()
        if not result:
            raise TranslationError("翻译服务返回空译文")
        # provider 限流降级、或 deep-translator 无法翻译时会把英文原样吐回来
        # （也可能是模型用英文说一句「抱歉无法翻译」）。这类「看起来成功」的返回
        # 一旦写进 *_zh 字段就再也不会被重试，必须当失败处理。
        if _LATIN_RE.search(source) and not _has_cjk(result):
            raise TranslationError(f"译文不含中文，疑似未翻译: {result[:80]}")
        return result

    def _split_text(self, text: str, max_length: int) -> list:
        """将长文本按句切块；单句超长时再按字符硬切（MyMemory 单次仅 500 字符）。"""
        chunks = []
        sentences = re.split(r'(?<=[.!?])\s+', text)
        current_chunk = ""

        for sentence in sentences:
            while len(sentence) > max_length:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                cut = sentence.rfind(" ", 0, max_length)
                cut = cut if cut > max_length // 2 else max_length
                chunks.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if len(current_chunk) + len(sentence) < max_length:
                current_chunk += sentence + " "
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence + " "

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks


# 单例实例
translator = Translator()


def translate_text(text: str) -> str:
    """翻译文本的便捷函数（AI 优先，失败降级机翻）。

    失败时抛 TranslationError（不再返回英文原文）。调用方请自行 try/except：
    宁可把 *_zh 留空让下次运行重试，也不要把英文写进中文字段。
    """
    return translator.translate(text)


def machine_translate(text: str) -> str:
    """只用机器翻译（AI 已经确定不可用、或调用方不想再花 AI 预算时用）。"""
    return translator.machine_translate(text)
