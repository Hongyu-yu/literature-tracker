"""pytest 本地运行用：与 run_tests.py 一致，每个测试前清零进程内的 AI 熔断状态。"""

import pytest


@pytest.fixture(autouse=True)
def _reset_ai_breaker():
    try:
        import ai_breaker
        ai_breaker.reset()
    except Exception:
        pass
    yield
