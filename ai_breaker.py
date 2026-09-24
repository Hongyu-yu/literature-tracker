"""AI 网关熔断器（进程内共享）。

2026-09 现场：网关连续多日对每个请求返回 `503 no available channel for provider openai`。
每次 call_api 自带 3 次指数退避重试，于是：
  * 周报要做 1300+ 次逐篇 AI 判定，609 次就耗掉 1h46m，连续三周（09-07/14/21）在
    240 分钟上限被取消，一份周报都没发出来；
  * 抓取阶段几百次翻译请求全部白等，fetch 拖到近 2 小时。

熔断规则：连续 AI_BREAKER_LIMIT（默认 2）次调用失败 → 熔断，此后调用立即抛
AIUnavailableError（调用方已有的 except 分支照常走降级逻辑）；熔断 AI_BREAKER_COOLDOWN
秒（默认 600）后放行一次试探调用，成功即恢复 —— 网关中途恢复时不会整轮都不用 AI。

只统计「调用抛错」，不统计「返回内容不合格」（例如翻译吐回英文）：后者说明网关是通的。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_STATE = {"failures": 0, "open": False, "opened_at": 0.0, "probing": False}


class AIUnavailableError(RuntimeError):
    """熔断期间的快速失败（不发请求）。"""


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def ai_available() -> bool:
    """当前是否允许发 AI 请求。熔断冷却期满时放行一次试探（half-open）。"""
    with _LOCK:
        if not _STATE["open"]:
            return True
        cooldown = _env_number("AI_BREAKER_COOLDOWN", 600)
        if not _STATE["probing"] and time.monotonic() - _STATE["opened_at"] >= cooldown:
            _STATE["probing"] = True
            return True
        return False


def record_ai_failure(exc: object = None) -> None:
    with _LOCK:
        _STATE["failures"] += 1
        was_probe = _STATE["probing"]
        _STATE["probing"] = False
        limit = max(1, int(_env_number("AI_BREAKER_LIMIT", 2)))
        if was_probe or (not _STATE["open"] and _STATE["failures"] >= limit):
            first = not _STATE["open"]
            _STATE["open"] = True
            _STATE["opened_at"] = time.monotonic()
            if first:
                print(f"🛑 AI 连续失败 {_STATE['failures']} 次，熔断：后续 AI 调用直接走降级逻辑"
                      f"（翻译改用机器翻译）；{int(_env_number('AI_BREAKER_COOLDOWN', 600))} 秒后试探恢复。"
                      f"最近一次错误: {str(exc)[:160]}")


def record_ai_success() -> None:
    with _LOCK:
        if _STATE["open"]:
            print("✅ AI 试探调用成功，熔断解除")
        _STATE.update(failures=0, open=False, probing=False)


def reset() -> None:
    """测试用。"""
    with _LOCK:
        _STATE.update(failures=0, open=False, opened_at=0.0, probing=False)


class BreakerProvider:
    """给任意 provider 套上熔断：熔断中直接抛 AIUnavailableError，不发请求。"""

    def __init__(self, inner: Any):
        self._inner = inner

    def call_api(self, *args, **kwargs):
        if not ai_available():
            raise AIUnavailableError("AI 网关熔断中，跳过本次调用")
        try:
            result = self._inner.call_api(*args, **kwargs)
        except Exception as exc:
            record_ai_failure(exc)
            raise
        record_ai_success()
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def with_breaker(provider: Any) -> Any:
    """None / 已包过的原样返回。"""
    if provider is None or isinstance(provider, BreakerProvider):
        return provider
    return BreakerProvider(provider)
