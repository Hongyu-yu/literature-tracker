"""
邮件通知模块 - 发送新文献通知
增强版：完善的错误处理、配置验证、支持完整版和摘要版两种模式
"""

import smtplib
import socket
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid
from datetime import datetime
from typing import Tuple, Optional


class EmailNotifier:
    """邮件通知器"""
    
    def __init__(self, smtp_server: str, smtp_port: int,
                 sender_email: str, sender_password: str):
        """
        初始化邮件通知器
        
        Args:
            smtp_server: SMTP服务器地址
            smtp_port: SMTP端口
            sender_email: 发件人邮箱
            sender_password: 发件人密码/授权码
        """
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.sender_email = sender_email
        self.sender_password = sender_password
    
    def validate_config(self) -> Tuple[bool, str]:
        """
        验证邮件配置完整性
        
        Returns:
            Tuple[bool, str]: (是否有效, 错误信息)
        """
        errors = []
        
        if not self.smtp_server:
            errors.append("SMTP服务器地址未配置")
        
        if not self.smtp_port:
            errors.append("SMTP端口未配置")
        elif not isinstance(self.smtp_port, int) or self.smtp_port <= 0:
            errors.append(f"SMTP端口无效: {self.smtp_port}")
        
        if not self.sender_email:
            errors.append("发件人邮箱未配置")
        elif '@' not in self.sender_email:
            errors.append(f"发件人邮箱格式无效: {self.sender_email}")
        
        if not self.sender_password:
            errors.append("发件人密码/授权码未配置")
        
        if errors:
            return False, "; ".join(errors)
        
        return True, ""
    
    # 发件人邮箱缺失/畸形时 Message-ID 的兜底域名（RFC 2606 保留的 .invalid）。
    # 这里刻意不做 socket.getfqdn() 反查——定时任务里 DNS 不通会整整卡住一次发信。
    FALLBACK_MSGID_DOMAIN = "literature-tracker.invalid"

    def _msgid_domain(self) -> str:
        """Message-ID 用的域名，优先取发件人邮箱的域名部分。"""
        sender = str(self.sender_email or "").strip().strip("<>")
        domain = sender.rsplit("@", 1)[1].strip() if "@" in sender else ""
        # 域名里混进空白或头部分隔符说明配置有问题，退回兜底域名，免得写出非法头部
        if not domain or any(ch.isspace() or ch in '<>@,;:"' for ch in domain):
            return self.FALLBACK_MSGID_DOMAIN
        return domain

    def _apply_standard_headers(self, msg: MIMEMultipart) -> None:
        """补齐 RFC 5322 要求的 Date 头和强烈建议的 Message-ID 头。

        smtplib.sendmail 不会自动补这两个头（send_message 同样不补），缺失时
        SpamAssassin 直接加 MISSING_DATE / MISSING_MID 分，本来就是「带外链外图的
        群发 HTML 邮件」的日报很容易被丢进垃圾箱；客户端也没法按发信时间排序、
        没法按 Message-ID 做会话归并与去重。
        每个收件人各建一封 msg，所以每封天然拿到各自唯一的 Message-ID。
        """
        if "Date" not in msg:
            msg["Date"] = formatdate(localtime=True)
        if "Message-ID" not in msg:
            msg["Message-ID"] = make_msgid(domain=self._msgid_domain())

    def _build_html_message(self, recipient: str, subject: str, html_content: str) -> MIMEMultipart:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.sender_email
        msg["To"] = recipient
        self._apply_standard_headers(msg)
        msg.attach(MIMEText("请使用支持 HTML 的邮件客户端查看每日文献日报。", "plain", "utf-8"))
        msg.attach(MIMEText(html_content, "html", "utf-8"))
        return msg

    def send_html_multi(self, recipients, subject: str, html_content: str) -> list:
        """给多个收件人各发一封 caller-built 富 HTML，返回发送成功的地址列表。

        一次 SMTP 连接、登录一次，然后逐个地址单独 sendmail：
        - 每封的 To 只含收件人自己，彼此看不到对方地址；
        - 单个地址失败只跳过该地址，不影响其余人（调用方可据返回值补发）。
        所有 SMTP 错误 fail-soft，绝不打断日报流程。
        """
        is_valid, error_msg = self.validate_config()
        if not is_valid:
            print(f"⚠️ 邮件跳过: {error_msg}")
            return []
        targets, seen = [], set()
        for raw in (recipients or []):
            addr = str(raw or "").strip()
            if not addr or "@" not in addr or addr in seen:
                continue
            seen.add(addr)
            targets.append(addr)
        if not targets:
            print("⚠️ 邮件跳过: 收件人无效")
            return []
        sent = []
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30) as server:
                server.login(self.sender_email, self.sender_password)
                for addr in targets:
                    try:
                        msg = self._build_html_message(addr, subject, html_content)
                        server.sendmail(self.sender_email, addr, msg.as_string())
                        sent.append(addr)
                        print(f"✅ 邮件发送成功 → {addr}")
                    except Exception as exc:
                        print(f"⚠️ 单个收件人发送失败({addr})，继续其余收件人: {type(exc).__name__}: {exc}")
        except Exception as exc:
            print(f"⚠️ 每日邮件发送失败，日报流程继续: {type(exc).__name__}: {exc}")
        return sent
