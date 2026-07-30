#!/usr/bin/env python3
"""
AI摘要生成器 - 使用免费AI API生成每日文献摘要
支持: Gemini, OpenRouter (OpenAI-compatible)
修复: 解决 AI 在长列表中容易将链接与文章标题搞混的问题
"""

import os
import json
import time
import re
import requests
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple
from abc import ABC, abstractmethod
from urllib.parse import urlsplit, urlunsplit

try:
    from config import AI_CONFIG as DEFAULT_AI_CONFIG
except ImportError:
    DEFAULT_AI_CONFIG = {}

try:
    from json_repair import repair_json
except ImportError:
    repair_json = None


def _clamp_text(text: str, max_chars: int) -> str:
    """Clamp text to max_chars (character count, not bytes). Empty input → empty output."""
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _cjk_ratio(text: str) -> float:
    """Return fraction of CJK characters among letter-like characters (ignores whitespace/punctuation)."""
    if not text:
        return 0.0
    letters = 0
    cjk = 0
    for ch in text:
        if ch.isalpha() or '\u4e00' <= ch <= '\u9fff':
            letters += 1
            if '\u4e00' <= ch <= '\u9fff':
                cjk += 1
    return (cjk / letters) if letters else 0.0


def _looks_untranslated_title(candidate: str, english_title: str) -> bool:
    """True if AI returned the English title (or near-copy) as `title_zh`."""
    if not candidate:
        return False
    c = candidate.strip()
    if not c:
        return False
    # No Chinese at all → untranslated
    if _cjk_ratio(c) < 0.3:
        return True
    # Direct copy of English (case/whitespace-insensitive, first 60 chars)
    def norm(s): return "".join((s or "").lower().split())[:60]
    if norm(c) == norm(english_title):
        return True
    return False


class AIProvider(ABC):
    """AI提供商基类"""
    
    @abstractmethod
    def call_api(self, prompt: str) -> str:
        pass


class GeminiProvider(AIProvider):
    """Google Gemini API"""
    
    def __init__(self, api_key: str, model: str = None):
        self.api_key = api_key
        self.model = model or os.environ.get('GEMINI_MODEL', 'gemini-3-flash-preview')
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        self.max_retries = 3
    
    def call_api(self, prompt: str) -> str:
        wait_max_seconds = int(os.environ.get("AI_WAIT_MAX_SECONDS", "0") or "0")
        wait_base_seconds = float(os.environ.get("AI_WAIT_BASE_SECONDS", "10") or "10")
        wait_max_sleep_seconds = float(os.environ.get("AI_WAIT_MAX_SLEEP_SECONDS", "300") or "300")

        headers = {"Content-Type": "application/json"}
        data = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2, # 降低随机性，减少幻觉
                "maxOutputTokens": 8192,
                "topP": 0.95,
                "topK": 40,
                "responseMimeType": "application/json"
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"}
            ]
        }
        
        url = f"{self.base_url}/{self.model}:generateContent?key={self.api_key}"

        start = time.monotonic()
        attempt = 0
        last_err: Optional[Exception] = None

        while True:
            attempt += 1
            try:
                response = requests.post(url, headers=headers, json=data, timeout=120)
                if response.status_code == 200:
                    result = response.json()
                    if 'candidates' not in result or not result['candidates']:
                        raise Exception("Gemini API返回空响应")
                    return result['candidates'][0]['content']['parts'][0]['text']

                # Retryable
                if response.status_code in (429, 500, 502, 503, 504):
                    last_err = Exception(f"Gemini API错误 ({response.status_code}): {response.text}")
                else:
                    raise Exception(f"Gemini API错误 ({response.status_code}): {response.text}")
            except Exception as e:
                last_err = e

            elapsed = time.monotonic() - start
            if wait_max_seconds > 0:
                if elapsed >= wait_max_seconds:
                    raise last_err or Exception("Gemini API failed")
            else:
                if attempt >= self.max_retries:
                    raise last_err or Exception("Gemini API failed")

            sleep_s = min(wait_base_seconds * (2 ** max(0, attempt - 1)), wait_max_sleep_seconds)
            # Keep a bit of jitterless wait to reduce flakiness in CI.
            time.sleep(min(sleep_s, max(1.0, (wait_max_seconds - elapsed) if wait_max_seconds > 0 else sleep_s)))

class KimiClaudeCodeProvider(AIProvider):
    """Kimi-for-Coding endpoint (Anthropic Messages protocol, Claude Code client spoofing).

    Base URL defaults to https://api.kimi.com/coding; endpoint is {base_url}/v1/messages.
    """

    DEFAULT_BASE_URL = "https://api.kimi.com/coding"
    DEFAULT_MODEL = "kimi-k2-turbo-preview"

    def __init__(self, api_key: str, model: str = None):
        self.api_key = (api_key or "").strip()
        self.model = (
            model
            or os.environ.get("AI_MODEL")
            or os.environ.get("KIMI_MODEL")
            or self.DEFAULT_MODEL
        )
        base = (
            os.environ.get("KIMI_BASE_URL")
            or os.environ.get("AI_BASE_URL")
            or self.DEFAULT_BASE_URL
        ).rstrip("/")
        # Accept both ".../coding" and ".../coding/v1/messages"
        if base.endswith("/v1/messages"):
            self.endpoint = base
        elif base.endswith("/v1"):
            self.endpoint = f"{base}/messages"
        else:
            self.endpoint = f"{base}/v1/messages"
        self.timeout = int(os.environ.get("AI_TIMEOUT_SECONDS", "120"))
        self.max_retries = int(os.environ.get("AI_MAX_RETRIES", "3"))

    def _headers(self) -> Dict[str, str]:
        # Header recipe verified against api.kimi.com/coding on 2026-04-15.
        # Spoofs a Claude Code CLI client (required by the endpoint).
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
            "user-agent": os.environ.get("KIMI_USER_AGENT", "claude-cli/1.0.60 (external, cli)"),
            "x-app": "cli",
            "x-stainless-lang": "js",
            "x-stainless-package-version": "0.60.0",
        }

    def call_api(self, prompt: str) -> str:
        if not self.api_key:
            raise ValueError("Kimi api_key is empty (set KIMI_API_KEY or AI_API_KEY).")

        wait_max_seconds = int(os.environ.get("AI_WAIT_MAX_SECONDS", "0") or "0")
        wait_base_seconds = float(os.environ.get("AI_WAIT_BASE_SECONDS", "10") or "10")
        wait_max_sleep_seconds = float(os.environ.get("AI_WAIT_MAX_SLEEP_SECONDS", "300") or "300")

        system_prompt = (
            "你是一位资深的凝聚态物理/计算材料科学研究员，擅长把英文学术文献压缩为"
            "精准、信息密度高、无套话的中文要点；严格按要求输出 JSON。"
        )
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(os.environ.get("AI_MAX_TOKENS", "8192")),
            "temperature": 0.2,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        }

        start = time.monotonic()
        attempt = 0
        last_err: Optional[Exception] = None

        while True:
            attempt += 1
            response = None
            try:
                response = requests.post(
                    self.endpoint, headers=self._headers(), json=payload, timeout=self.timeout
                )
                if response.status_code == 200:
                    data = response.json()
                    blocks = data.get("content") or []
                    text_parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                    text = "".join(text_parts).strip()
                    if not text:
                        raise Exception(f"Kimi API 返回空文本: {data}")
                    return text

                if response.status_code in (401, 403):
                    # Fatal: don't waste retry budget on auth failure.
                    raise Exception(f"Kimi API 鉴权失败 ({response.status_code}): {response.text}")

                if response.status_code in (429, 500, 502, 503, 504):
                    last_err = Exception(f"Kimi API错误 ({response.status_code}): {response.text}")
                else:
                    raise Exception(f"Kimi API错误 ({response.status_code}): {response.text}")
            except Exception as e:
                last_err = e

            elapsed = time.monotonic() - start
            if wait_max_seconds > 0:
                if elapsed >= wait_max_seconds:
                    raise last_err or Exception("Kimi API failed")
            else:
                if attempt >= self.max_retries:
                    raise last_err or Exception("Kimi API failed")

            retry_after = 0.0
            if response is not None:
                try:
                    ra = (response.headers.get("Retry-After") or "").strip()
                    if ra:
                        retry_after = float(ra)
                except Exception:
                    retry_after = 0.0

            sleep_s = min(wait_base_seconds * (2 ** max(0, attempt - 1)), wait_max_sleep_seconds)
            if retry_after > 0:
                sleep_s = max(sleep_s, retry_after)
            if wait_max_seconds > 0:
                remaining = max(0.0, wait_max_seconds - elapsed)
                sleep_s = min(sleep_s, max(1.0, remaining))
            time.sleep(sleep_s)


class OpenRouterProvider(AIProvider):
    """
    OpenAI-compatible chat completions API.

    Docs: https://openrouter.ai/docs
    """

    def __init__(self, api_key: str, model: str = None):
        self.api_key = (api_key or "").strip()
        self.model = (
            model
            or os.environ.get("AI_MODEL")
            or os.environ.get("OPENROUTER_MODEL")
            or (DEFAULT_AI_CONFIG.get("model") if isinstance(DEFAULT_AI_CONFIG, dict) else None)
            or "gpt-5.4(auto)"
        )
        raw_base_url = (
            os.environ.get("AI_BASE_URL")
            or os.environ.get("OPENROUTER_BASE_URL")
            or (DEFAULT_AI_CONFIG.get("base_url") if isinstance(DEFAULT_AI_CONFIG, dict) else None)
            or "https://supercodex.space/v1"
        )
        self.base_url = normalize_chat_completions_url(raw_base_url)
        self.max_retries = int(os.environ.get("AI_MAX_RETRIES", "3"))
        self.timeout = int(os.environ.get("AI_TIMEOUT_SECONDS", "120"))

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        # Optional attribution headers; keep configurable.
        def _sanitize(v: Optional[str]) -> str:
            v = (v or "").strip()
            if "\r" in v or "\n" in v:
                return ""
            return v

        http_referer = _sanitize(os.environ.get("OPENROUTER_HTTP_REFERER") or os.environ.get("HTTP_REFERER"))
        x_title = _sanitize(os.environ.get("OPENROUTER_X_TITLE") or os.environ.get("X_TITLE"))
        if http_referer:
            headers["HTTP-Referer"] = http_referer
        if x_title:
            headers["X-Title"] = x_title
        return headers

    def call_api(self, prompt: str) -> str:
        if not self.api_key:
            raise ValueError("OpenRouter api_key is empty (set AI_API_KEY).")

        wait_max_seconds = int(os.environ.get("AI_WAIT_MAX_SECONDS", "0") or "0")
        wait_base_seconds = float(os.environ.get("AI_WAIT_BASE_SECONDS", "10") or "10")
        wait_max_sleep_seconds = float(os.environ.get("AI_WAIT_MAX_SLEEP_SECONDS", "300") or "300")
        json_mode_enabled = (os.environ.get("AI_RESPONSE_JSON", "") or "").strip().lower() in ("1", "true", "yes")

        start = time.monotonic()
        attempt = 0
        last_err: Optional[Exception] = None

        while True:
            attempt += 1
            response = None
            try:
                payload: Dict[str, Any] = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": int(os.environ.get("AI_MAX_TOKENS", "4096")),
                }
                # Optional JSON mode. Some models do not support `response_format`.
                if json_mode_enabled:
                    payload["response_format"] = {"type": "json_object"}

                response = requests.post(
                    self.base_url, headers=self._headers(), json=payload, timeout=self.timeout
                )

                if response.status_code == 200:
                    data = response.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise Exception("OpenRouter API返回空 choices")
                    msg = (choices[0] or {}).get("message") or {}
                    content = msg.get("content")
                    if not content:
                        raise Exception("OpenRouter API返回空 content")
                    return content

                if (
                    response.status_code == 400
                    and json_mode_enabled
                    and any(
                        token in response.text.lower()
                        for token in ("response_format", "json_object", "structured output", "not support")
                    )
                ):
                    print("⚠️ OpenRouter JSON mode unsupported by current model, retrying without response_format")
                    json_mode_enabled = False
                    attempt -= 1
                    continue

                # Retryable server / rate limit errors
                if response.status_code in (429, 500, 502, 503, 504):
                    last_err = Exception(f"OpenRouter API错误 ({response.status_code}): {response.text}")
                else:
                    raise Exception(f"OpenRouter API错误 ({response.status_code}): {response.text}")
            except Exception as e:
                last_err = e

            elapsed = time.monotonic() - start
            if wait_max_seconds > 0:
                if elapsed >= wait_max_seconds:
                    raise last_err or Exception("OpenRouter API failed")
            else:
                if attempt >= self.max_retries:
                    raise last_err or Exception("OpenRouter API failed")

            retry_after = 0.0
            if response is not None:
                try:
                    ra = (response.headers.get("Retry-After") or "").strip()
                    if ra:
                        retry_after = float(ra)
                except Exception:
                    retry_after = 0.0

            sleep_s = min(wait_base_seconds * (2 ** max(0, attempt - 1)), wait_max_sleep_seconds)
            if retry_after > 0:
                sleep_s = max(sleep_s, retry_after)

            if wait_max_seconds > 0:
                remaining = max(0.0, wait_max_seconds - elapsed)
                sleep_s = min(sleep_s, max(1.0, remaining))
            time.sleep(sleep_s)


def normalize_chat_completions_url(raw_url: Optional[str]) -> str:
    """
    Accept either a root OpenAI-compatible base URL (e.g. https://host/v1)
    or a full chat-completions endpoint, and normalize to the latter.
    """
    default_url = "https://openrouter.ai/api/v1/chat/completions"
    candidate = (raw_url or "").strip()
    if not candidate:
        return default_url

    parsed = urlsplit(candidate)
    if not parsed.scheme or not parsed.netloc:
        return default_url

    normalized_path = parsed.path.rstrip("/")
    if not normalized_path:
        normalized_path = "/chat/completions"
    elif not (
        normalized_path.endswith("/chat/completions")
        or normalized_path.endswith("/completions")
    ):
        normalized_path = f"{normalized_path}/chat/completions"

    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, parsed.query, parsed.fragment))


def build_provider(api_provider: str, api_key: str, model: str = None) -> AIProvider:
    """Factory for AI providers."""
    name = (api_provider or "").strip().lower()
    if name in ("kimi", "kimi-coding", "moonshot-coding", "kimi-claude"):
        return KimiClaudeCodeProvider(api_key=api_key, model=model)
    if name in ("openrouter", "open-router", "or"):
        return OpenRouterProvider(api_key=api_key, model=model)
    if name in ("aigw", "gpt5", "openai-gateway", "sota"):
        return OpenRouterProvider(api_key=api_key, model=model)
    # default: gemini
    gemini_model = model if model and "/" not in model else None
    return GeminiProvider(api_key=api_key, model=gemini_model)


class AISummarizer:
    """AI摘要生成器"""
    
    def __init__(self, api_provider: str, api_key: str, model: str = None):
        model = (
            model
            or os.environ.get("AI_MODEL")
            or (DEFAULT_AI_CONFIG.get("model") if isinstance(DEFAULT_AI_CONFIG, dict) else None)
        )
        self.provider = build_provider(api_provider, api_key, model=model)
        self.provider_name = (api_provider or "gemini").strip().lower()
    
    def generate_daily_summary(self, articles: List[Dict], date: str) -> Dict:
        if not articles:
            return self.fallback_summary(articles, date)

        wait_max_seconds = int(os.environ.get("AI_DAILY_WAIT_MAX_SECONDS") or os.environ.get("AI_WAIT_MAX_SECONDS", "0") or "0")
        wait_base_seconds = float(os.environ.get("AI_DAILY_WAIT_BASE_SECONDS") or os.environ.get("AI_WAIT_BASE_SECONDS", "10") or "10")
        wait_max_sleep_seconds = float(os.environ.get("AI_DAILY_WAIT_MAX_SLEEP_SECONDS") or os.environ.get("AI_WAIT_MAX_SLEEP_SECONDS", "300") or "300")
        no_fallback = (os.environ.get("AI_DAILY_NO_FALLBACK") or os.environ.get("AI_NO_FALLBACK") or "").strip().lower() in ("1", "true", "yes")

        start = time.monotonic()
        attempt = 0
        last_err: Optional[Exception] = None

        while True:
            attempt += 1
            try:
                max_per_call = int(os.environ.get("AI_DAILY_MAX_PER_CALL", "40"))
                if len(articles) <= max_per_call:
                    prompt = self._build_prompt(articles, date)
                    response = self.provider.call_api(prompt)
                    summary = self._parse_response(response, articles, date)
                    # compat alias (some callers expect `summaries`)
                    if "summaries" not in summary:
                        summary["summaries"] = summary.get("full_list", [])
                    return summary

                # Chunking to avoid context overflow and missing items.
                chunks = [articles[i:i + max_per_call] for i in range(0, len(articles), max_per_call)]
                merged_full_list: List[Dict] = []
                merged_ml: List[Dict] = []
                merged_ferro: List[Dict] = []

                for chunk in chunks:
                    prompt = self._build_prompt(chunk, date)
                    response = self.provider.call_api(prompt)
                    part = self._parse_response(response, chunk, date)
                    merged_full_list.extend(part.get("full_list", []))
                    merged_ml.extend(part.get("ml_highlights", []))
                    merged_ferro.extend(part.get("ferro_highlights", []))

                # Dedup highlights across chunks by article link.
                def _dedup_by_link(items: List[Dict]) -> List[Dict]:
                    seen: set = set()
                    out: List[Dict] = []
                    for it in items:
                        key = it.get("link") or it.get("title_en")
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(it)
                    return out

                merged_ml = _dedup_by_link(merged_ml)
                merged_ferro = _dedup_by_link(merged_ferro)

                # Overview/trends: second-pass with titles only (cheap prompt).
                overview, trends = self._build_overview_trends(articles, date)

                summary = {
                    "date": date,
                    "total": len(articles),
                    "overview": overview,
                    "trends": trends,
                    "full_list": merged_full_list,
                    "ml_highlights": merged_ml[:5],
                    "ferro_highlights": merged_ferro[:5],
                    "generated_by": self.provider_name,
                }
                summary["summaries"] = summary.get("full_list", [])
                return summary

            except Exception as e:
                last_err = e
                print(f"❌ AI 摘要生成失败 (attempt={attempt}): {e}")

                # Fatal errors: waiting won't help (e.g., missing/invalid API key).
                msg_lower = str(e).lower()
                is_fatal = (
                    isinstance(e, ValueError)
                    and ("api_key is empty" in msg_lower or "api key is empty" in msg_lower)
                ) or ("401" in msg_lower) or ("403" in msg_lower)

                if is_fatal:
                    if no_fallback:
                        raise last_err
                    return self.fallback_summary(articles, date)

                elapsed = time.monotonic() - start
                if wait_max_seconds > 0 and elapsed < wait_max_seconds:
                    sleep_s = min(wait_base_seconds * (2 ** max(0, attempt - 1)), wait_max_sleep_seconds)
                    remaining = max(0.0, wait_max_seconds - elapsed)
                    sleep_s = min(sleep_s, max(1.0, remaining))
                    print(f"⏳ 等待 {sleep_s:.0f}s 后重试（已等待 {elapsed:.0f}s / {wait_max_seconds}s）")
                    time.sleep(sleep_s)
                    continue

                if no_fallback:
                    raise last_err

                return self.fallback_summary(articles, date)

    def _build_overview_trends(self, articles: List[Dict], date: str) -> Tuple[str, str]:
        titles = []
        for i, a in enumerate(articles[:200], 1):  # cap to keep prompt bounded
            t = a.get("title") or "Unknown Title"
            titles.append(f"[{i}] {t}")
        titles_str = "\n".join(titles)

        prompt = (
            f"你是一位资深凝聚态物理/计算材料科学研究员。请基于 {date} 的以下标题列表，"
            "输出今日文献的高信息密度总览与热点分析，面向同行读者。\n\n"
            f"标题列表：\n{titles_str}\n\n"
            "严格要求：\n"
            "- overview 2-3 句，必须包含具体的材料体系/方法/现象名称（如 BaTiO3、Moire 超晶格、DFT+U、MACE），"
            "不得使用 '本研究/具有重要意义/取得进展/为…提供新思路' 之类套话。\n"
            "- trends 3-5 句，列出 2-3 个真正的研究热点，每个热点写清 '方向 → 具体做法/现象 → 体现它的典型工作'。\n"
            "- 全中文，不输出任何英文。\n\n"
            "仅输出如下 JSON，禁止任何额外文字：\n"
            '{"overview": "...", "trends": "..."}'
        )
        wait_max_seconds = int(os.environ.get("AI_DAILY_WAIT_MAX_SECONDS") or os.environ.get("AI_WAIT_MAX_SECONDS", "0") or "0")
        wait_base_seconds = float(os.environ.get("AI_DAILY_WAIT_BASE_SECONDS") or os.environ.get("AI_WAIT_BASE_SECONDS", "10") or "10")
        wait_max_sleep_seconds = float(os.environ.get("AI_DAILY_WAIT_MAX_SLEEP_SECONDS") or os.environ.get("AI_WAIT_MAX_SLEEP_SECONDS", "300") or "300")

        start = time.monotonic()
        attempt = 0

        while True:
            attempt += 1
            try:
                response = self.provider.call_api(prompt)
                data = self._load_json_lenient(response, context="overview/trends")
                if not isinstance(data, dict):
                    raise ValueError("overview/trends: response is not an object")
                return data.get("overview", ""), data.get("trends", "")
            except Exception:
                if wait_max_seconds <= 0:
                    return "", ""
                elapsed = time.monotonic() - start
                if elapsed >= wait_max_seconds:
                    return "", ""
                sleep_s = min(wait_base_seconds * (2 ** max(0, attempt - 1)), wait_max_sleep_seconds)
                remaining = max(0.0, wait_max_seconds - elapsed)
                time.sleep(min(sleep_s, max(1.0, remaining)))
    
    def _build_prompt(self, articles: List[Dict], date: str) -> str:
        """构建提示词，增加序列号锚点防止链接错位"""
        
        articles_text = []
        for i, article in enumerate(articles, 1):
            title = article.get('title', 'Unknown Title')
            journal = article.get("journal", "")
            authors = article.get("authors", [])
            if isinstance(authors, list):
                authors = ", ".join([str(a) for a in authors[:6]]) + (" 等" if len(authors) > 6 else "")
            else:
                authors = str(authors or "")

            abstract = (article.get('abstract', ''))[:600]
            # 不在提示词里给链接，防止 AI 试图复述链接导致出错
            # 仅给序号、标题、期刊、作者、摘要
            articles_text.append(
                f"[{i}] Title: {title}\nJournal: {journal}\nAuthors: {authors}\nAbstract: {abstract}\n"
            )
        
        articles_str = '\n'.join(articles_text)
        
        example_block = (
            "示例（仅示范风格，不要直接复制）：\n"
            "输入: [X] Title: Room-temperature ferroelectricity in two-dimensional van der Waals NbOI2\n"
            "      Abstract: We report robust out-of-plane ferroelectric switching ...\n"
            "输出: {\n"
            '  "index": X,\n'
            '  "title_zh": "二维范德华 NbOI2 中的室温铁电性",\n'
            '  "abstract_zh": "在二维 NbOI2 薄层中观测到稳定的面外铁电翻转，矫顽场约 0.3 V/nm，"\n'
            '                "室温保持时间 > 10^4 s；通过二次谐波与压电力显微镜确认极化方向，"\n'
            '                "并给出层厚依赖的相变温度，为低维非易失存储提供候选体系。",\n'
            '  "one_sentence_summary": "首次在二维 NbOI2 中实现室温稳定的面外铁电翻转，矫顽场低至 0.3 V/nm，'
            '极化保持时间超过 10^4 秒。作者结合二次谐波与压电力显微镜直接确认极化方向，并给出层厚依赖的'
            '铁电相变温度，排除了衬底应变的干扰。该体系兼具薄层可集成性与长保持时间，为低功耗非易失存储'
            '与可重构光电器件提供了新的二维材料平台。"\n'
            "}\n"
        )

        return (
            f"你将分析 {date} 的 {len(articles)} 篇学术文献（凝聚态物理 / 计算材料科学 / AI for science 方向），"
            "生成一份面向同行研究者的高信息密度中文日报。\n\n"
            f"【文献列表】（格式: [序号] Title / Journal / Authors / Abstract）\n{articles_str}\n\n"
            "【写作硬性要求】\n"
            "1. title_zh：**必须**把英文标题翻成中文，不超过 40 字。只有**化学式/材料符号/缩写**"
            "（如 BaTiO3、MoS2、GaN/AlN、DFT、GNN、MBQC）可原样保留，其他英文词一律译成中文。"
            "**禁止**把 title_zh 填成英文原标题或英文多数词；若检测到输出的 title_zh 里中文字符占比 < 50%，视为违反要求。\n"
            "2. abstract_zh：用中文把摘要写成 ≤200 字的研究要点概括，必须写出：体系/方法/关键数值或结论，"
            "尽量多覆盖。禁止任何套话：'本研究/取得进展/具有重要意义/为…提供新思路/点击查看' 等一律不允许。\n"
            "3. one_sentence_summary：一段 3~5 句、≤250 字的中文亮点详述，必须覆盖：核心创新点 + "
            "关键方法细节（模型架构/训练策略/关键参数）+ 最强定量结论 + 对凝聚态/"
            "AI for science 方向的意义。要落到具体材料/现象/方法，不得空泛。\n"
            "4. 全部用中文；不输出链接（程序按序号自动补全）；不得编造原文没有的数据。\n"
            "5. summaries 必须覆盖所有输入序号，index 严格一致。\n"
            "6. highlights：仅挑选 ≤3 篇**真正**最突出的工作（创新点、方法论或关键结论）。"
            "reason ≤25 字，必须落到具体材料/现象/方法，不得写 '重要进展/意义重大' 之流。\n\n"
            f"{example_block}\n"
            "【输出格式】只输出以下 JSON，不要任何额外文字、不要 markdown 代码块标记：\n"
            "{\n"
            '  "overview": "今日文献总览（中文，2-3句，含具体方向与代表性工作）",\n'
            '  "trends": "研究热点分析（中文，3-5句）",\n'
            '  "summaries": [\n'
            '    {"index": 1, "title_zh": "...", "abstract_zh": "...", "one_sentence_summary": "..."},\n'
            f'    ... (共 {len(articles)} 条)\n'
            "  ],\n"
            '  "highlights": [\n'
            '    {"index": <序号>, "reason": "具体亮点（≤25字）"}\n'
            "  ]\n"
            "}\n"
        )

    def _build_missing_summaries_prompt(self, original_articles: List[Dict], missing_indices: List[int], date: str) -> str:
        articles_text = []
        for idx in missing_indices:
            # 边界检查：防止索引越界
            if idx < 1 or idx > len(original_articles):
                print(f"⚠️ 跳过无效索引 {idx} (有效范围: 1-{len(original_articles)})")
                continue
            article = original_articles[idx - 1]
            title = article.get('title', 'Unknown Title')
            journal = article.get("journal", "")
            authors = article.get("authors", [])
            if isinstance(authors, list):
                authors = ", ".join([str(a) for a in authors[:6]]) + (" 等" if len(authors) > 6 else "")
            else:
                authors = str(authors or "")
            abstract = (article.get('abstract', ''))[:600]
            articles_text.append(
                f"[{idx}] Title: {title}\nJournal: {journal}\nAuthors: {authors}\nAbstract: {abstract}\n"
            )

        articles_str = "\n".join(articles_text)
        return f"""你是一位专业的计算材料科学文献分析助手。请只补全以下 {date} 缺失的文献条目。

文献列表:
{articles_str}

请严格输出 JSON：
{{
  "summaries": [
    {{
      "index": 1,
      "title_zh": "中文标题（翻译原标题）",
      "abstract_zh": "摘要中文概括（≤200字，写出体系/方法/关键结论）",
      "one_sentence_summary": "一段3~5句、≤250字的中文亮点详述（创新点+方法细节+最强定量结论+方向意义）"
    }}
  ]
}}

要求：
1. 只返回上面给出的缺失 index。
2. 每个 index 都必须返回 title_zh、abstract_zh 和 one_sentence_summary，且都使用中文。
3. 不要输出 JSON 以外的任何内容。
"""

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        value = (text or "").strip()
        if not value.startswith("```"):
            return value
        lines = value.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @classmethod
    def _extract_json_object(cls, text: str) -> str:
        value = cls._strip_code_fence(text)
        start = value.find("{")
        if start < 0:
            raise ValueError("No JSON object found in model response")

        depth = 0
        in_string = False
        escape = False

        for idx in range(start, len(value)):
            ch = value[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return value[start:idx + 1]

        return value[start:].strip()

    @classmethod
    def _candidate_json_strings(cls, response: str) -> List[str]:
        raw = response or ""
        stripped = raw.strip()
        candidates = [raw, stripped, cls._strip_code_fence(stripped)]
        try:
            candidates.append(cls._extract_json_object(stripped))
        except Exception:
            pass

        normalized_candidates: List[str] = []
        seen = set()
        for candidate in candidates:
            candidate = (candidate or "").strip()
            if not candidate or candidate in seen:
                continue
            normalized_candidates.append(candidate)
            seen.add(candidate)
        return normalized_candidates

    @staticmethod
    def _normalize_json_string(raw: str) -> str:
        return (
            (raw or "")
            .replace("\ufeff", "")
            .replace("“", '"')
            .replace("”", '"')
            .replace("‘", "'")
            .replace("’", "'")
        )

    @classmethod
    def _load_json_lenient(cls, response: str, *, context: str = "response") -> Any:
        last_error: Optional[Exception] = None
        for candidate in cls._candidate_json_strings(response):
            for attempt_text in (
                candidate,
                cls._normalize_json_string(candidate),
                re.sub(r",\s*([}\]])", r"\1", cls._normalize_json_string(candidate)),
            ):
                try:
                    return json.loads(attempt_text)
                except Exception as exc:
                    last_error = exc

            if repair_json is not None:
                try:
                    return repair_json(candidate, return_objects=True)
                except Exception as exc:
                    last_error = exc

        raise ValueError(f"{context}: failed to parse model JSON ({last_error})")

    @staticmethod
    def _summary_fields_missing(item: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(item, dict):
            return True
        # 检查关键字段：中文标题、摘要翻译、一句话总结
        has_title_zh = bool(str(item.get("title_zh") or "").strip())
        has_abstract_zh = bool(str(item.get("abstract_zh") or "").strip())
        has_summary = bool(str(item.get("one_sentence_summary") or "").strip())
        return not (has_title_zh and has_abstract_zh and has_summary)

    def _fill_missing_summaries(
        self,
        summaries_map: Dict[int, Dict[str, Any]],
        original_articles: List[Dict],
        date: str,
    ) -> Dict[int, Dict[str, Any]]:
        missing_indices = [
            idx
            for idx in range(1, len(original_articles) + 1)
            if self._summary_fields_missing(summaries_map.get(idx))
        ]
        if not missing_indices:
            return summaries_map

        preview = missing_indices[:15]
        more = "" if len(missing_indices) <= 15 else f" ...+{len(missing_indices) - 15}"
        print(
            f"⚠️ AI summaries incomplete for {len(missing_indices)} article(s) "
            f"(indices: {preview}{more}); requesting targeted refill"
        )
        try:
            prompt = self._build_missing_summaries_prompt(original_articles, missing_indices, date)
            response = self.provider.call_api(prompt)
            data = self._load_json_lenient(response, context="missing summaries")
            if not isinstance(data, dict):
                return summaries_map
            for item in data.get("summaries", []) or []:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("index"))
                except Exception:
                    continue
                if idx not in missing_indices:
                    continue
                current = dict(summaries_map.get(idx) or {})
                current.update(item)
                summaries_map[idx] = current
        except Exception as exc:
            print(f"⚠️ Failed to refill missing summaries: {exc}")
        return summaries_map

    def _parse_response(self, response: str, original_articles: List[Dict], date: str) -> Dict:
        """解析响应并与原始文章精准合并链接"""
        try:
            data = self._load_json_lenient(response, context="daily summary")
            if not isinstance(data, dict):
                raise ValueError("Invalid JSON response: root is not an object")
            
            # 建立序号到原始文章的映射 (1-based index)
            # original_articles 是按顺序传入的
            
            full_list = []
            summaries_map = {}
            for item in data.get("summaries", []) or []:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("index"))
                except Exception:
                    continue
                summaries_map[idx] = item

            summaries_map = self._fill_missing_summaries(summaries_map, original_articles, date)

            truncated_count = 0
            missing_summary_count = 0
            untranslated_title_count = 0
            for i, article in enumerate(original_articles, 1):
                ai_info = summaries_map.get(i, {})
                raw_title_zh = ai_info.get('title_zh') or article.get('title_zh') or ""
                if _looks_untranslated_title(raw_title_zh, article.get('title') or ""):
                    untranslated_title_count += 1
                    raw_title_zh = ""  # blank → renderer falls back to English once
                title_zh = _clamp_text(raw_title_zh, 80)
                abstract_zh_raw = ai_info.get('abstract_zh') or ""
                abstract_zh = _clamp_text(abstract_zh_raw, 240)
                one_sentence = _clamp_text(ai_info.get('one_sentence_summary') or "", 300)
                if not (title_zh or abstract_zh or one_sentence):
                    missing_summary_count += 1
                if any(
                    len(x or "") < len(raw or "")
                    for x, raw in (
                        (title_zh, ai_info.get('title_zh') or article.get('title_zh') or ""),
                        (abstract_zh, abstract_zh_raw),
                        (one_sentence, ai_info.get('one_sentence_summary') or ""),
                    )
                ):
                    truncated_count += 1
                # lazy import to avoid circular deps at module top
                from focus_core import is_core_focus as _icf, core_score as _cs
                full_list.append({
                    "title_en": article.get('title'),
                    # Empty-on-failure (front-end shows "—"), never leak "标题翻译失败" style placeholders.
                    "title_zh": title_zh,
                    "abstract_zh": abstract_zh,
                    # 透传给日报卡片/focus 专区渲染：英文原文折叠、完整中文翻译、兴趣匹配三字段
                    "abstract": article.get('abstract'),
                    "abstract_zh_full": article.get('abstract_zh_full'),
                    "focus_score": article.get('focus_score'),
                    "focus_summary": article.get('focus_summary'),
                    "focus_relation": article.get('focus_relation'),
                    "focus_suggestion": article.get('focus_suggestion'),
                    "summary": one_sentence,
                    "link": article.get('link'),
                    "journal": article.get("journal", ""),
                    "authors": article.get("authors", []),
                    "pub_date": article.get("pub_date", ""),
                    "ai_score": article.get("ai_score"),
                    "source_url": article.get("source_url", ""),
                    "arxiv_category": article.get("arxiv_category", ""),
                    "is_core_focus": _icf(article),
                    "core_score": _cs(article),
                })
            if truncated_count:
                print(f"ℹ️ _parse_response: clamped {truncated_count} over-long field(s)")
            if missing_summary_count:
                print(f"⚠️ _parse_response: {missing_summary_count}/{len(original_articles)} 文章 AI 总结为空（将显示 '—'）")
            if untranslated_title_count:
                print(
                    f"⚠️ _parse_response: {untranslated_title_count}/{len(original_articles)} 条 title_zh 未翻译（已清空，将回退显示英文原标题）"
                )
            
            # 处理 highlights
            ml_highlights = []
            ferro_highlights = []
            seen_highlight_idx: set = set()
            for h in data.get('highlights', []) or []:
                try:
                    idx = int(h.get('index'))
                except Exception:
                    continue
                if idx in seen_highlight_idx:
                    continue
                seen_highlight_idx.add(idx)
                if idx and 1 <= idx <= len(original_articles):
                    art = original_articles[idx-1]
                    info = summaries_map.get(idx, {})
                    h_item = {
                        "title_en": art.get('title'),
                        "title_zh": _clamp_text(info.get('title_zh') or "", 80),
                        "abstract_zh": _clamp_text(info.get('abstract_zh') or "", 240),
                        "link": art.get('link'),
                        "summary": _clamp_text(info.get('one_sentence_summary') or "", 300),
                        "reason": _clamp_text(h.get('reason') or "", 50),
                        "journal": art.get("journal", ""),
                        "authors": art.get("authors", []),
                        "pub_date": art.get("pub_date", ""),
                        "ai_score": art.get("ai_score"),
                        "source_url": art.get("source_url", ""),
                        "arxiv_category": art.get("arxiv_category", ""),
                    }
                    # 简单分类（也可以让 AI 返回分类）
                    if self._is_ml_related(art): ml_highlights.append(h_item)
                    elif self._is_ferro_related(art): ferro_highlights.append(h_item)
            
            return {
                'date': date,
                'total': len(original_articles),
                'overview': data.get('overview', ''),
                'trends': data.get('trends', ''),
                'full_list': full_list,
                'ml_highlights': ml_highlights,
                'ferro_highlights': ferro_highlights,
                'generated_by': self.provider_name
            }
        except Exception as e:
            print(f"解析响应并映射链接失败: {e}")
            raise

    def _is_ml_related(self, article: Dict) -> bool:
        text = (article.get('title', '') + article.get('abstract', '')).lower()
        return any(kw in text for kw in ['machine learn', 'deep learn', 'neural network', 'gnn', 'mlip', 'ml potential'])

    def _is_ferro_related(self, article: Dict) -> bool:
        text = (article.get('title', '') + article.get('abstract', '')).lower()
        return any(kw in text for kw in ['ferroelectric', 'ferromagnet', 'multiferroic', 'piezoelectric', 'antiferromagnet'])

    def generate_core_deep_fields(
        self,
        core_items: List[Dict],
        date: str,
    ) -> Tuple[Dict[str, Dict[str, str]], str]:
        """对核心关注论文批量生成 3 深度字段 + 方向点评。

        Returns: (deep_fields_by_link, direction_note)
          deep_fields_by_link: {link: {method_point, related_work, implication}}
          direction_note: 3-4 句中文段落，失败时返回空串
        """
        if not core_items:
            return {}, ""

        lines = []
        for i, it in enumerate(core_items, 1):
            title = it.get('title_en') or it.get('title') or ''
            title_zh = it.get('title_zh') or ''
            abstract = (it.get('abstract_zh') or it.get('abstract') or '')[:400]
            journal = it.get('journal', '')
            lines.append(
                f"[{i}] 中文标题: {title_zh}\n    EN: {title}\n    期刊: {journal}\n    摘要: {abstract}"
            )
        articles_str = "\n".join(lines)

        prompt = (
            f"你是深耕 ML × 铁电/磁性/凝聚态方向的资深研究员。以下是 {date} 当日的 "
            f"{len(core_items)} 篇核心关注论文（均已判定为 ML × ferro/凝聚态方向）。\n\n"
            f"【文献列表】\n{articles_str}\n\n"
            "请给出两部分输出：\n"
            "A. direction_note（3-4 句中文）：概括本日 ML × ferro/凝聚态方向的**实质进展**，"
            "必须点名具体材料（如 NbOI2、CrI3、CrSBr、BaTiO3）与具体方法（如 equivariant GNN、"
            "MACE、NEP、DFT+U），禁止 '整体来看 / 值得关注 / 有望 / 为…提供新思路' 之类套话。\n"
            "B. items：对每篇文章输出三条线索（全中文、信息密度高，每条 2~3 句）：\n"
            "   1) method_point（≤150 字）：核心技术/方法/模型的具体路线——架构、训练策略、"
            "关键参数或计算设置，避免泛泛而谈；\n"
            "   2) related_work（≤150 字）：与哪些已知方法/体系/方向呼应及异同，只写方向名不编造文献；\n"
            "   3) implication（≤150 字）：对 ML × ferro/凝聚态研究者的具体启发，给出可执行的借鉴点。\n\n"
            "【输出格式】只输出 JSON，无 markdown、无额外文字：\n"
            "{\n"
            '  "direction_note": "...",\n'
            '  "items": [\n'
            '    {"index": 1, "method_point": "...", "related_work": "...", "implication": "..."},\n'
            "    ...\n"
            "  ]\n"
            "}\n"
        )

        try:
            response = self.provider.call_api(prompt)
            data = self._load_json_lenient(response, context="core deep fields")
        except Exception as e:
            print(f"⚠️ generate_core_deep_fields failed: {e}")
            return {}, ""

        if not isinstance(data, dict):
            return {}, ""

        def _clamp(s, n):
            s = (s or "").strip()
            return s if len(s) <= n else s[: n - 1].rstrip() + "…"

        deep_fields: Dict[str, Dict[str, str]] = {}
        for entry in data.get("items", []) or []:
            if not isinstance(entry, dict):
                continue
            try:
                idx = int(entry.get("index"))
            except Exception:
                continue
            if not (1 <= idx <= len(core_items)):
                continue
            link = core_items[idx - 1].get("link") or ""
            if not link:
                continue
            deep_fields[link] = {
                "method_point": _clamp(entry.get("method_point", ""), 200),
                "related_work": _clamp(entry.get("related_work", ""), 200),
                "implication": _clamp(entry.get("implication", ""), 200),
            }

        direction_note = _clamp(data.get("direction_note", ""), 400)
        return deep_fields, direction_note

    def fallback_summary(self, articles: List[Dict], date: str) -> Dict:
        data = {
            'date': date,
            'total': len(articles),
            'overview': f"今日共收录{len(articles)}篇文献。",
            'trends': "",
            'full_list': [
                {
                    "title_en": a.get('title'),
                    "title_zh": a.get('title_zh') or "",
                    "abstract_zh": "",
                    "summary": "",
                    "link": a.get('link'),
                    "journal": a.get("journal", ""),
                    "authors": a.get("authors", []),
                    "pub_date": a.get("pub_date", ""),
                    "ai_score": a.get("ai_score"),
                    "source_url": a.get("source_url", ""),
                    "arxiv_category": a.get("arxiv_category", ""),
                } for a in articles
            ],
            'generated_by': 'fallback'
        }
        data["summaries"] = data.get("full_list", [])
        return data


def generate_daily_summary(
    articles: List[Dict],
    *,
    date: str,
    api_provider: str,
    api_key: str,
    model: str = None,
) -> Dict:
    """
    Backward-compatible wrapper for legacy callers (e.g. `main.py`).

    Note: this function only returns the JSON summary. Rendering HTML pages is handled by
    `generate_daily_pages.py` in the current GitHub Pages pipeline.
    """
    day_articles = [a for a in articles if (a.get("pub_date") or "").startswith(date)]
    summarizer = AISummarizer(api_provider, api_key, model=model)
    return summarizer.generate_daily_summary(day_articles, date)
