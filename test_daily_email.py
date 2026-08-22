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


def test_send_daily_email_uses_smtp_and_prevents_duplicate():
    root = tempfile.mkdtemp()
    sent_path = os.path.join(root, "email_sent.json")
    config = {
        "recipient": "594836947@qq.com", "smtp_server": "smtp.qq.com", "smtp_port": 465,
        "sender_email": "sender@qq.com", "sender_password": "test-secret", "mode": "digest",
    }
    smtp = mock.MagicMock()
    smtp.__enter__.return_value = smtp
    with mock.patch.object(daily_email, "EMAIL_CONFIG", config), \
         mock.patch("email_notifier.smtplib.SMTP_SSL", return_value=smtp) as smtp_ssl:
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path, site_base=SITE)
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path, site_base=SITE)
    assert smtp_ssl.call_count == 1
    smtp.login.assert_called_once_with("sender@qq.com", "test-secret")
    assert smtp.sendmail.call_count == 1
    marker = json.load(open(sent_path, encoding="utf-8"))
    assert DAY in marker
    sent_message = smtp.sendmail.call_args.args[2]
    assert "test-secret" not in sent_message


def test_send_daily_email_missing_credentials_and_smtp_failure_are_soft():
    root = tempfile.mkdtemp()
    sent_path = os.path.join(root, "email_sent.json")
    missing = {"recipient": "594836947@qq.com", "smtp_server": "smtp.qq.com", "smtp_port": 465,
               "sender_email": "", "sender_password": ""}
    with mock.patch.object(daily_email, "EMAIL_CONFIG", missing), \
         mock.patch("email_notifier.smtplib.SMTP_SSL") as smtp_ssl:
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path) is False
        smtp_ssl.assert_not_called()
    failing = dict(missing, sender_email="sender@qq.com", sender_password="secret")
    with mock.patch.object(daily_email, "EMAIL_CONFIG", failing), \
         mock.patch("email_notifier.smtplib.SMTP_SSL", side_effect=OSError("offline")):
        assert daily_email.send_daily_email(_summary(), DAY, sent_path=sent_path) is False
    assert not os.path.exists(sent_path)


def test_generate_deep_workflow_enables_single_daily_email_path():
    workflow = open(".github/workflows/generate-deep.yml", encoding="utf-8").read()
    assert "--rerender-only --days 4 --send-email" in workflow
    assert "EMAIL_SENDER: ${{ secrets.EMAIL_SENDER }}" in workflow
    assert "EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}" in workflow
