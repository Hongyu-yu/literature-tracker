#!/usr/bin/env python3
"""email_notifier 的回归测试：外发邮件必须带 Date 和 Message-ID 头。

RFC 5322 §3.6 里 Date 是必需头、Message-ID 是强烈建议头，而 smtplib.sendmail
不会替我们补（send_message 同样不补）。缺这两个头时 SpamAssassin 会加
MISSING_DATE / MISSING_MID 分，日报本来就是「带外链外图的群发 HTML」，很容易
被判成垃圾邮件；而 send_html_multi 只看 SMTP 层是否成功，落到垃圾箱这件事在
仓库里没有任何地方会暴露出来。
"""

from datetime import datetime, timezone
from email import message_from_string
from email.utils import parsedate_to_datetime
from unittest import mock

from email_notifier import EmailNotifier


SENDER = "594836947@qq.com"


def _notifier(sender=SENDER, mode="digest"):
    return EmailNotifier(
        smtp_server="smtp.qq.com",
        smtp_port=465,
        sender_email=sender,
        sender_password="app-token",
        mode=mode,
    )


class _FakeArticle:
    """send_notification 的 _generate_html/_generate_text 需要的最小文献对象。"""

    title = "Machine learning for ferroelectric domain walls"
    title_zh = "机器学习与铁电畴壁"
    authors = ["A. One", "B. Two"]
    journal = "Physical Review B"
    pub_date = "2026-08-31"
    link = "https://example.org/abs/1234"
    abstract = "A neural network study."
    abstract_zh = "一项神经网络研究。"


def _capture_wire(fn):
    """跑一次发信流程，返回 sendmail 收到的原始邮件文本列表。"""
    wire = []
    with mock.patch("email_notifier.smtplib.SMTP_SSL") as smtp_ssl:
        server = smtp_ssl.return_value.__enter__.return_value
        server.sendmail.side_effect = lambda frm, to, raw: wire.append(raw)
        fn()
    return wire


def test_daily_html_message_carries_a_valid_date_header():
    msg = _notifier()._build_html_message("dest@example.com", "📚 每日文献日报 · 2026-08-31", "<p>hi</p>")
    raw = msg["Date"]
    assert raw, "每日邮件缺少 Date 头（RFC 5322 必需头）"
    sent_at = parsedate_to_datetime(raw)
    assert sent_at.tzinfo is not None, f"Date 头没有时区偏移: {raw!r}"
    drift = abs((sent_at - datetime.now(timezone.utc)).total_seconds())
    assert drift < 300, f"Date 头不是当前时间: {raw!r}"


def test_daily_html_message_carries_a_message_id_rooted_at_sender_domain():
    msg = _notifier()._build_html_message("dest@example.com", "主题", "<p>hi</p>")
    mid = msg["Message-ID"]
    assert mid, "每日邮件缺少 Message-ID 头（客户端无法做会话归并/去重）"
    assert mid.startswith("<") and mid.endswith(">"), f"Message-ID 语法非法: {mid!r}"
    assert mid.endswith("@qq.com>"), f"Message-ID 域名应取自发件人邮箱: {mid!r}"


def test_message_id_is_unique_per_recipient():
    notifier = _notifier()
    first = notifier._build_html_message("a@example.com", "主题", "<p>hi</p>")["Message-ID"]
    second = notifier._build_html_message("b@example.com", "主题", "<p>hi</p>")["Message-ID"]
    assert first and second and first != second, "每个收件人必须拿到各自唯一的 Message-ID"


def test_send_html_multi_puts_the_headers_on_the_wire():
    notifier = _notifier()
    wire = _capture_wire(
        lambda: notifier.send_html_multi(["a@example.com", "b@example.com"], "主题", "<p>hi</p>")
    )
    assert len(wire) == 2, f"应给 2 个收件人各发一封，实际 {len(wire)}"
    ids = []
    for raw in wire:
        parsed = message_from_string(raw)
        assert parsed["Date"], "线上真正发出去的邮件仍然缺 Date 头"
        assert parsed["Message-ID"], "线上真正发出去的邮件仍然缺 Message-ID 头"
        parsedate_to_datetime(parsed["Date"])  # 解析不了会抛异常
        ids.append(parsed["Message-ID"])
        # 成功路径的其余头部不能被改动
        assert parsed["From"] == SENDER
        assert parsed.is_multipart()
    assert ids[0] != ids[1], "两封信共用了同一个 Message-ID"
    tos = [message_from_string(raw)["To"] for raw in wire]
    assert tos == ["a@example.com", "b@example.com"]


def test_legacy_send_notification_also_sets_the_headers():
    notifier = _notifier(mode="digest")
    # _generate_digest_html/_generate_html 目前 100% 抛 KeyError（HEAD 上就如此：正文里
    # 的 CSS `{ font-family: ... }` 被 str.format 当成替换字段），会被 send_notification
    # 的兜底 except 吞掉。这里把生成器打桩掉，专测头部这一件事。
    with mock.patch.object(EmailNotifier, "_generate_digest_html", return_value="<p>hi</p>"), \
            mock.patch.object(EmailNotifier, "_generate_digest_text", return_value="hi"):
        wire = _capture_wire(lambda: notifier.send_notification("dest@example.com", [_FakeArticle()]))
    assert len(wire) == 1
    parsed = message_from_string(wire[0])
    assert parsed["Date"], "send_notification 发出的邮件缺 Date 头"
    assert parsed["Message-ID"], "send_notification 发出的邮件缺 Message-ID 头"


def test_message_id_domain_falls_back_when_sender_is_malformed():
    """发件人邮箱畸形时不能写出 <...@> 或把整串当域名的非法 Message-ID。"""
    for bad in ("", None, "not-an-email", "a@", "a@bad domain", "a@x>y"):
        notifier = _notifier(sender=bad)
        mid = notifier._build_html_message("dest@example.com", "主题", "<p>hi</p>")["Message-ID"]
        assert mid.startswith("<") and mid.endswith(">"), f"{bad!r} → 非法 Message-ID: {mid!r}"
        domain = mid[1:-1].split("@", 1)[1]
        assert domain == EmailNotifier.FALLBACK_MSGID_DOMAIN, f"{bad!r} → 域名兜底失败: {mid!r}"
        assert not any(ch.isspace() or ch in '<>,;:"' for ch in domain)


def test_apply_standard_headers_does_not_duplicate_existing_headers():
    """重复调用不能叠出两份 Date/Message-ID（多个 Date 头同样违反 RFC 5322）。"""
    notifier = _notifier()
    msg = notifier._build_html_message("dest@example.com", "主题", "<p>hi</p>")
    notifier._apply_standard_headers(msg)
    assert len(msg.get_all("Date") or []) == 1
    assert len(msg.get_all("Message-ID") or []) == 1


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn) and getattr(fn, "__module__", "") == __name__:
            fn()
            print(f"✓ {name}")
    print("[OK] email_notifier RFC 5322 头部回归测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
