"""ai_breaker：网关整体不可用时快速失败，恢复后自动放行。

现场（2026-09）：网关对每个请求都回 503，每次 call_api 自带 3 次退避重试；周报的
1300+ 次逐篇判定因此连续三周在 240 分钟上限被取消。
"""

import os
from unittest import mock

import ai_breaker
from ai_breaker import AIUnavailableError, BreakerProvider, with_breaker


class _Flaky:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def call_api(self, prompt):
        self.calls += 1
        value = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(value, Exception):
            raise value
        return value

    model = "fake-model"


def _call(provider):
    try:
        return provider.call_api("p")
    except Exception as exc:
        return exc


def test_trips_after_consecutive_failures_and_stops_sending_requests():
    ai_breaker.reset()
    inner = _Flaky([RuntimeError("503")] * 10)
    p = BreakerProvider(inner)
    _call(p), _call(p)
    assert inner.calls == 2
    for _ in range(20):
        assert isinstance(_call(p), AIUnavailableError)
    assert inner.calls == 2, "熔断后仍在发请求"
    ai_breaker.reset()


def test_success_resets_the_failure_streak():
    ai_breaker.reset()
    inner = _Flaky([RuntimeError("503"), "ok", RuntimeError("503"), "ok"])
    p = BreakerProvider(inner)
    for _ in range(4):
        _call(p)
    assert ai_breaker.ai_available()
    ai_breaker.reset()


def test_half_open_probe_recovers_when_gateway_comes_back():
    """冷却期满放行一次试探：成功 → 恢复；网关中途恢复时不会整轮都不用 AI。"""
    ai_breaker.reset()
    inner = _Flaky([RuntimeError("503"), RuntimeError("503"), "back"])
    p = BreakerProvider(inner)
    with mock.patch.dict(os.environ, {"AI_BREAKER_COOLDOWN": "0"}):
        _call(p), _call(p)
        assert inner.calls == 2
        assert _call(p) == "back"
        assert ai_breaker.ai_available()
    ai_breaker.reset()


def test_failed_probe_reopens_immediately():
    ai_breaker.reset()
    inner = _Flaky([RuntimeError("503")] * 5)
    p = BreakerProvider(inner)
    with mock.patch.dict(os.environ, {"AI_BREAKER_COOLDOWN": "0"}):
        _call(p), _call(p), _call(p)   # 两次失败熔断 + 一次试探失败
        assert inner.calls == 3
    with mock.patch.dict(os.environ, {"AI_BREAKER_COOLDOWN": "3600"}):
        assert isinstance(_call(p), AIUnavailableError)
        assert inner.calls == 3
    ai_breaker.reset()


def test_wrapper_is_transparent_and_idempotent():
    inner = _Flaky(["ok"])
    p = with_breaker(inner)
    assert p.model == "fake-model"
    assert with_breaker(p) is p
    assert with_breaker(None) is None


def test_weekly_judge_fast_fails_once_gateway_is_dead():
    """周报逐篇判定：熔断后不再发请求，照常走各自的降级分支（铁性默认保留）。"""
    ai_breaker.reset()
    import weekly_summary

    inner = _Flaky([RuntimeError("503")] * 1000)
    with mock.patch.object(weekly_summary, "build_provider", return_value=inner):
        ws = weekly_summary.WeeklySummarizer(provider="openrouter", api_key="k")
    assert isinstance(ws.provider, BreakerProvider)
    results = [ws._ai_judge_ferro_relevance("ferroelectric domain wall") for _ in range(50)]
    assert all(results), "铁性判定失败时应默认保留"
    assert inner.calls == 2, f"熔断后仍发出 {inner.calls} 次请求"
    ai_breaker.reset()
