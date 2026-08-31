import json
import os
import tempfile
from unittest import mock

import daily_email


DAY = "2026-08-21"
SITE = "https://hongyu-yu.github.io/literature-tracker"


def _summary():
    return {
        "overview": "今日重点关注电子结构。",
        "full_list": [{
            "title": "Neural network potential",
            "title_zh": "神经网络势",
            "abstract": "A faithful English abstract.",
            "one_sentence_summary": "该工作构建神经网络势。",
            "link": "https://arxiv.org/abs/1234.5678",
            "journal": "arXiv", "focus_score": 9,
            "image": "images/posters/ax123.webp",
        }],
    }


def test_build_daily_email_html_contains_highlight_links_poster_and_subject():
    subject, html = daily_email.build_daily_email_html(_summary(), DAY, SITE)
    assert subject == f"📚 每日文献日报 · {DAY}"
    assert "💡 亮点" in html and "该工作构建神经网络势" in html
    assert f'{SITE}/daily/{DAY}.html' in html
    assert f'{SITE}/images/posters/ax123.webp' in html
    assert "<img" in html and "P1" in html
    assert "信息不足" not in html


def test_build_daily_email_html_empty_summary_is_graceful():
    subject, html = daily_email.build_daily_email_html({}, DAY, SITE)
    assert DAY in subject and "今日暂无目标方向文献" in html


def _config(**over):
    base = {
        "recipients": ["594836947@qq.com"], "smtp_server": "smtp.qq.com", "smtp_port": 465,
        "sender_email": "sender@qq.com", "sender_password": "test-secret", "mode": "digest",
    }
    base.update(over)
    return base


def test_send_daily_email_uses_smtp_and_prevents_duplicate():
    root = tempfile.mkdtemp()
    sent_path = os.path.join(root, "email_sent.json")
    smtp = mock.MagicMock()
    smtp.__enter__.return_value = smtp
    with mock.patch.object(daily_email, "EMAIL_CONFIG", _config()), \
         mock.patch("email_notifier.smtplib.SMTP_SSL", return_value=smtp) as smtp_ssl:
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path, site_base=SITE)
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path, site_base=SITE)
    assert smtp_ssl.call_count == 1
    smtp.login.assert_called_once_with("sender@qq.com", "test-secret")
    assert smtp.sendmail.call_count == 1
    marker = json.load(open(sent_path, encoding="utf-8"))
    assert marker[DAY] == {"594836947@qq.com": mock.ANY}
    sent_message = smtp.sendmail.call_args.args[2]
    assert "test-secret" not in sent_message


def test_send_daily_email_sends_one_message_per_recipient_over_one_connection():
    root = tempfile.mkdtemp()
    sent_path = os.path.join(root, "email_sent.json")
    people = ["594836947@qq.com", "a@example.com", "b@example.com"]
    smtp = mock.MagicMock()
    smtp.__enter__.return_value = smtp
    with mock.patch.object(daily_email, "EMAIL_CONFIG", _config(recipients=people)), \
         mock.patch("email_notifier.smtplib.SMTP_SSL", return_value=smtp) as smtp_ssl:
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path, site_base=SITE)
    # 一次连接、登录一次，但每人各一封
    assert smtp_ssl.call_count == 1
    smtp.login.assert_called_once_with("sender@qq.com", "test-secret")
    assert smtp.sendmail.call_count == len(people)
    # 每封的 To 只含收件人自己，彼此不可见
    for call, addr in zip(smtp.sendmail.call_args_list, people):
        assert call.args[1] == addr
        to_line = [ln for ln in call.args[2].splitlines() if ln.startswith("To:")]
        assert to_line == [f"To: {addr}"]
        for other in people:
            if other != addr:
                assert other not in call.args[2]
    assert json.load(open(sent_path, encoding="utf-8"))[DAY].keys() == set(people)


def test_send_daily_email_retries_only_the_recipients_that_failed():
    root = tempfile.mkdtemp()
    sent_path = os.path.join(root, "email_sent.json")
    people = ["ok@example.com", "flaky@example.com"]
    smtp = mock.MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.sendmail.side_effect = [None, OSError("mailbox busy")]
    with mock.patch.object(daily_email, "EMAIL_CONFIG", _config(recipients=people)), \
         mock.patch("email_notifier.smtplib.SMTP_SSL", return_value=smtp):
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path, site_base=SITE)
    # 只有成功的那个被标记
    assert list(json.load(open(sent_path, encoding="utf-8"))[DAY]) == ["ok@example.com"]

    smtp2 = mock.MagicMock()
    smtp2.__enter__.return_value = smtp2
    with mock.patch.object(daily_email, "EMAIL_CONFIG", _config(recipients=people)), \
         mock.patch("email_notifier.smtplib.SMTP_SSL", return_value=smtp2):
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path, site_base=SITE)
    # 下次只补发失败的那个，不重复打扰已收到的人
    assert smtp2.sendmail.call_count == 1
    assert smtp2.sendmail.call_args.args[1] == "flaky@example.com"
    assert json.load(open(sent_path, encoding="utf-8"))[DAY].keys() == set(people)


def test_send_daily_email_accepts_legacy_flat_marker_and_singular_recipient():
    root = tempfile.mkdtemp()
    sent_path = os.path.join(root, "email_sent.json")
    with open(sent_path, "w", encoding="utf-8") as f:
        json.dump({DAY: "2026-08-21T00:00:00+00:00"}, f)  # 旧的扁平格式
    smtp = mock.MagicMock()
    smtp.__enter__.return_value = smtp
    # 旧配置只有单数 recipient，也应能工作
    legacy = {k: v for k, v in _config().items() if k != "recipients"}
    legacy["recipient"] = "594836947@qq.com"
    with mock.patch.object(daily_email, "EMAIL_CONFIG", legacy), \
         mock.patch("email_notifier.smtplib.SMTP_SSL", return_value=smtp) as smtp_ssl:
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path, site_base=SITE)
    smtp_ssl.assert_not_called()  # 旧标记已覆盖该收件人，不重复发送


def test_send_daily_email_missing_credentials_and_smtp_failure_are_soft():
    root = tempfile.mkdtemp()
    sent_path = os.path.join(root, "email_sent.json")
    missing = _config(sender_email="", sender_password="")
    with mock.patch.object(daily_email, "EMAIL_CONFIG", missing), \
         mock.patch("email_notifier.smtplib.SMTP_SSL") as smtp_ssl:
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path) is False
        smtp_ssl.assert_not_called()
    failing = _config(sender_password="secret")
    with mock.patch.object(daily_email, "EMAIL_CONFIG", failing), \
         mock.patch("email_notifier.smtplib.SMTP_SSL", side_effect=OSError("offline")):
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path) is False
    assert not os.path.exists(sent_path)


def test_send_daily_email_without_recipients_is_soft():
    root = tempfile.mkdtemp()
    sent_path = os.path.join(root, "email_sent.json")
    with mock.patch.object(daily_email, "EMAIL_CONFIG", _config(recipients=[])), \
         mock.patch("email_notifier.smtplib.SMTP_SSL") as smtp_ssl:
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path) is False
        smtp_ssl.assert_not_called()


def test_generate_deep_workflow_enables_single_daily_email_path():
    workflow = open(".github/workflows/generate-deep.yml", encoding="utf-8").read()
    assert "--rerender-only --days 4 --send-email" in workflow
    assert "EMAIL_SENDER: ${{ secrets.EMAIL_SENDER }}" in workflow
    assert "EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}" in workflow
