"""
翻译模块

优先使用项目的 AI Provider（OpenRouter / Gemini 等）进行翻译与摘要式翻译；
当未配置 AI_API_KEY 时，降级使用 GoogleTranslator（deep-translator）。

契约：translate() / translate_text() 要么返回**中文译文**，要么抛 TranslationError。
绝不把英文原文当译文返回 —— 详见 TranslationError 的说明。
"""

from __future__ import annotations

import os
from deep_translator import GoogleTranslator
import time
import re

from ai_summarizer import build_provider


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
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


class Translator:
    def __init__(self):
        self.translator = GoogleTranslator(source='en', target='zh-CN')
        self._ai_provider = None
        self._ai_provider_name = (os.environ.get("AI_PROVIDER") or "openrouter").strip()
        self._ai_key = (os.environ.get("AI_API_KEY") or "").strip()
        self._ai_model = (os.environ.get("AI_MODEL") or "").strip() or None
    
    def translate(self, text: str) -> str:
        """翻译文本到中文

        空输入返回 ""（无事可做，不算失败）；成功返回中文译文；
        其余一律抛 TranslationError —— 见模块顶部的契约说明。
        """
        if not text or not text.strip():
            return ""

        try:
            # 清理HTML标签
            clean_text = re.sub(r'<[^>]+>', '', text)
            clean_text = clean_text.strip()

            if not clean_text:
                return ""

            # Prefer AI provider when configured
            if self._ai_key:
                if self._ai_provider is None:
                    self._ai_provider = build_provider(self._ai_provider_name, self._ai_key, model=self._ai_model)

                # Keep translation prompt simple to reduce hallucination.
                prompt = (
                    "你是专业的学术翻译助手。请将下面英文翻译为简体中文，保持术语准确，"
                    "只输出译文，不要解释：\n\n"
                    f"{clean_text}\n"
                )
                resp = self._ai_provider.call_api(prompt)
                return self._verified(resp, clean_text)

            # deep-translator有字符限制，需要分段翻译
            if len(clean_text) > 4500:
                chunks = self._split_text(clean_text, 4500)
                translated_chunks = []
                for chunk in chunks:
                    translated = self.translator.translate(chunk)
                    # 缺一段就整段作废：半截译文写进 abstract_zh 后同样会被当成
                    # 「已翻译」而永不重试，比留空更难修。
                    if not (translated or "").strip():
                        raise TranslationError("分段翻译缺失其中一段，放弃本次译文")
                    translated_chunks.append(translated)
                    time.sleep(0.5)  # 避免请求过快
                return self._verified(''.join(translated_chunks), clean_text)

            return self._verified(self.translator.translate(clean_text), clean_text)
        except TranslationError as e:
            print(f"⚠️ 翻译失败: {e}")
            raise
        except Exception as e:
            print(f"⚠️ 翻译失败: {e}")
            # 旧代码此处 `return text`，把带 HTML 标签的英文原文当译文交还调用方。
            raise TranslationError(f"翻译调用失败: {e}") from e

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
        """将长文本分割成小块"""
        chunks = []
        sentences = re.split(r'(?<=[.!?])\s+', text)
        current_chunk = ""
        
        for sentence in sentences:
            if len(current_chunk) + len(sentence) < max_length:
                current_chunk += sentence + " "
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence + " "
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        return chunks


# 单例实例
translator = Translator()


def translate_text(text: str) -> str:
    """翻译文本的便捷函数

    失败时抛 TranslationError（不再返回英文原文）。调用方请自行 try/except：
    宁可把 *_zh 留空让下次运行重试，也不要把英文写进中文字段。
    """
    return translator.translate(text)
