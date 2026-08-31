#!/usr/bin/env python3
"""--rerender-only --send-email 与质量门解耦的回归测试。

历史问题：质量门（daily_quality_ok）同时把守 sidecar 落盘，而 sidecar 是邮件唯一的
数据来源，于是质量一不达标，当天的日报邮件就整天发不出去。现在的约定是：
  * 质量不达标 → 不重渲染 HTML（保住已有的好页面，见 20c00dc05），但**照常发邮件**；
  * sidecar 缺失 → 才真正跳过（无数据可发）。
"""

import json
import os
import shutil
import sys
import tempfile
from unittest import mock

import daily_email
import generate_daily_pages


DAY = "2026-08-21"


def _summary(quality_ok):
    return {
        "date": DAY, "overview": "总览", "trends": "热点", "quality_ok": quality_ok,
        "full_list": [{
            "title": "Neural network potential", "title_zh": "神经网络势",
            "abstract": "An English abstract.", "one_sentence_summary": "该工作构建神经网络势。",
            "link": "https://arxiv.org/abs/1234.5678", "journal": "arXiv", "focus_score": 9,
        }],
    }


def _run_rerender(tmp, summary=None, page_body="旧页面正文"):
    """在临时工作目录里跑一次 --rerender-only --send-email，返回 (是否发信, 页面内容)。"""
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "docs/daily"), exist_ok=True)
    page = os.path.join(tmp, "docs/daily", f"{DAY}.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(page_body)
    if summary is not None:
        with open(os.path.join(tmp, "data", f"daily_summary_{DAY}.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False)

    config = {
        "recipients": ["a@example.com"], "smtp_server": "smtp.qq.com", "smtp_port": 465,
        "sender_email": "sender@qq.com", "sender_password": "secret", "mode": "digest",
    }
    smtp = mock.MagicMock()
    smtp.__enter__.return_value = smtp
    argv = ["generate_daily_pages.py", "--rerender-only", "--days", "1", "--date", DAY, "--send-email"]
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(daily_email, "EMAIL_CONFIG", config), \
             mock.patch("email_notifier.smtplib.SMTP_SSL", return_value=smtp), \
             mock.patch.dict(os.environ, {"EMAIL_ENABLED": "1"}):
            generate_daily_pages.main()
    finally:
        os.chdir(cwd)
    return smtp.sendmail.call_count == 1, open(page, encoding="utf-8").read()


def test_low_quality_day_still_sends_email_but_keeps_existing_page():
    tmp = tempfile.mkdtemp()
    try:
        sent, page = _run_rerender(tmp, _summary(quality_ok=False))
        assert sent, "质量不达标也必须照常发送日报邮件"
        assert page == "旧页面正文", "质量不达标时不得覆盖已有页面"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_good_quality_day_sends_email_and_rerenders_page():
    tmp = tempfile.mkdtemp()
    try:
        sent, page = _run_rerender(tmp, _summary(quality_ok=True))
        assert sent, "质量达标必须发送日报邮件"
        assert "神经网络势" in page and page != "旧页面正文", "质量达标时应重渲染页面"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sidecar_without_quality_flag_is_treated_as_good():
    """兼容 2026-07-31 之前落盘的旧 sidecar（没有 quality_ok 字段）。"""
    tmp = tempfile.mkdtemp()
    try:
        summary = _summary(quality_ok=True)
        summary.pop("quality_ok")
        sent, page = _run_rerender(tmp, summary)
        assert sent
        assert "神经网络势" in page
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_sidecar_skips_email():
    tmp = tempfile.mkdtemp()
    try:
        sent, page = _run_rerender(tmp, None)
        assert not sent, "没有 sidecar 就无数据可发，应跳过"
        assert page == "旧页面正文"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("[OK] rerender/email decoupling sanity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
