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


def test_local_scalar_config_is_promoted_to_environ():
    """config.local.py 里的大写标量键要能真正生效。

    此前 config.py 只读 EMAIL_CONFIG/WECHAT_CONFIG/AI_CONFIG 三个字典，而
    config.local.py.example 还教用户写 APS_HTTP_*/AI_* 这些标量 —— 它们没有任何
    读取方，照着文档配完毫无效果。
    """
    env = {k: v for k, v in os.environ.items() if k != "APS_HTTP_BASE"}
    with mock.patch.dict(os.environ, env, clear=True):
        config._export_local_scalars({"APS_HTTP_BASE": "http://real:8080"})
        assert os.environ["APS_HTTP_BASE"] == "http://real:8080"


def test_local_scalar_promotion_skips_placeholders_and_non_scalars():
    with mock.patch.dict(os.environ, {}, clear=True):
        config._export_local_scalars({
            "AI_MODEL": "<your-model>",        # 占位符
            "AI_KEY2": "YOUR_KEY",             # 占位符
            "EMAIL_CONFIG": {"a": 1},          # 字典
            "lowercase_key": "x",              # 非大写
            "EMPTY": "   ",                    # 空白
        })
        for k in ("AI_MODEL", "AI_KEY2", "EMAIL_CONFIG", "lowercase_key", "EMPTY"):
            assert k not in os.environ, f"{k} 不该被提升"


def test_real_environment_wins_over_local_file():
    """CI 的 Secrets 必须压过本地文件。"""
    with mock.patch.dict(os.environ, {"APS_HTTP_USER": "from-ci"}, clear=True):
        config._export_local_scalars({"APS_HTTP_USER": "from-local-file"})
        assert os.environ["APS_HTTP_USER"] == "from-ci"
