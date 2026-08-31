#!/usr/bin/env python3
"""收件人配置解析的测试（EMAIL_RECIPIENTS → config.EMAIL_CONFIG["recipients"]）。"""

import importlib
import os
from unittest import mock

import config


def test_parse_recipients_splits_comma_semicolon_and_whitespace():
    assert config._parse_recipients("a@x.com, b@y.com; c@z.com", []) == ["a@x.com", "b@y.com", "c@z.com"]


def test_parse_recipients_dedupes_preserving_order_and_drops_junk():
    raw = "a@x.com, , not-an-email, a@x.com,  b@y.com "
    assert config._parse_recipients(raw, []) == ["a@x.com", "b@y.com"]


def test_parse_recipients_accepts_a_list_and_strips_angle_brackets():
    assert config._parse_recipients(["<a@x.com>", " b@y.com "], []) == ["a@x.com", "b@y.com"]


def test_parse_recipients_falls_back_to_default_when_empty():
    assert config._parse_recipients("", ["fallback@x.com"]) == ["fallback@x.com"]
    assert config._parse_recipients(None, ["fallback@x.com"]) == ["fallback@x.com"]


def test_email_recipients_env_overrides_default():
    with mock.patch.dict(os.environ, {"EMAIL_RECIPIENTS": "one@x.com,two@y.com"}):
        reloaded = importlib.reload(config)
        try:
            assert reloaded.EMAIL_CONFIG["recipients"] == ["one@x.com", "two@y.com"]
            # 单数 recipient 作为向后兼容别名，取首项
            assert reloaded.EMAIL_CONFIG["recipient"] == "one@x.com"
        finally:
            importlib.reload(config)


def test_default_recipient_is_kept_when_env_absent():
    env = {k: v for k, v in os.environ.items() if k != "EMAIL_RECIPIENTS"}
    with mock.patch.dict(os.environ, env, clear=True):
        reloaded = importlib.reload(config)
        try:
            assert reloaded.EMAIL_CONFIG["recipients"] == reloaded.DEFAULT_EMAIL_RECIPIENTS
            assert "594836947@qq.com" in reloaded.EMAIL_CONFIG["recipients"]
        finally:
            importlib.reload(config)


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] email recipients config sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
