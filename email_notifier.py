"""
邮件通知模块 - 发送新文献通知
增强版：完善的错误处理、配置验证、支持完整版和摘要版两种模式
"""

import smtplib
import socket
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Tuple, Optional


class EmailNotifier:
    """邮件通知器"""
    
    def __init__(self, smtp_server: str, smtp_port: int, 
                 sender_email: str, sender_password: str,
                 mode: str = "full"):
        """
        初始化邮件通知器
        
        Args:
            smtp_server: SMTP服务器地址
            smtp_port: SMTP端口
            sender_email: 发件人邮箱
            sender_password: 发件人密码/授权码
            mode: 邮件模式 - "full" 完整版, "digest" 摘要版
        """
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.sender_email = sender_email
        self.sender_password = sender_password
        self.mode = mode
    
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
    
    def send_notification(self, recipient: str, articles: list) -> bool:
        """
        发送新文献通知邮件
        
        Args:
            recipient: 收件人邮箱
            articles: 文献列表
            
        Returns:
            bool: 是否发送成功
        """
        if not articles:
            print("没有新文献，跳过邮件发送")
            return True
        
        # 验证配置
        is_valid, error_msg = self.validate_config()
        if not is_valid:
            print(f"❌ 邮件配置不完整: {error_msg}")
            print("   请检查环境变量 EMAIL_SENDER 和 EMAIL_PASSWORD 是否已设置")
            return False

        # 验证收件人
        if not recipient or '@' not in recipient:
            print(f"❌ 收件人邮箱无效: {recipient}")
            return False
        
        try:
            # 创建邮件
            msg = MIMEMultipart('alternative')
            mode_label = "摘要版" if self.mode == "digest" else "完整版"
            msg['Subject'] = f"📚 文献追踪更新 - {len(articles)}篇新文献 [{mode_label}] ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
            msg['From'] = self.sender_email
            msg['To'] = recipient
            
            # 根据模式生成邮件内容
            if self.mode == "digest":
                html_content = self._generate_digest_html(articles)
                text_content = self._generate_digest_text(articles)
            else:
                html_content = self._generate_html(articles)
                text_content = self._generate_text(articles)
            
            msg.attach(MIMEText(text_content, 'plain', 'utf-8'))
            msg.attach(MIMEText(html_content, 'html', 'utf-8'))
            
            # 发送邮件 - 使用SSL连接
            print(f"   正在连接 {self.smtp_server}:{self.smtp_port}...")
            
            # 创建SSL上下文
            context = ssl.create_default_context()
            
            try:
                # 尝试SSL连接（端口465）
                with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30) as server:
                    print("   SSL连接成功，正在登录...")
                    server.login(self.sender_email, self.sender_password)
                    print("   登录成功，正在发送邮件...")
                    server.sendmail(self.sender_email, recipient, msg.as_string())
            except ssl.SSLError as e:
                # 如果SSL失败，尝试STARTTLS（端口587）
                print(f"   SSL连接失败，尝试STARTTLS...")
                with smtplib.SMTP(self.smtp_server, 587, timeout=30) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(self.sender_email, self.sender_password)
                    server.sendmail(self.sender_email, recipient, msg.as_string())
            
            print(f"✅ 邮件发送成功: {len(articles)}篇文献通知已发送到 {recipient}")
            return True
            
        except socket.timeout:
            print(f"❌ 邮件发送失败: 连接超时")
            print(f"   请检查网络连接和SMTP服务器地址: {self.smtp_server}:{self.smtp_port}")
            return False
            
        except socket.gaierror as e:
            print(f"❌ 邮件发送失败: 无法解析服务器地址")
            print(f"   服务器: {self.smtp_server}")
            print(f"   错误详情: {e}")
            return False
            
        except smtplib.SMTPAuthenticationError as e:
            print(f"❌ 邮件发送失败: 认证失败")
            print(f"   请检查邮箱地址和授权码是否正确")
            print(f"   注意: QQ邮箱需要使用授权码而非登录密码")
            print(f"   错误代码: {e.smtp_code}, 错误信息: {e.smtp_error}")
            return False
            
        except smtplib.SMTPRecipientsRefused as e:
            print(f"❌ 邮件发送失败: 收件人被拒绝")
            print(f"   收件人: {recipient}")
            print(f"   错误详情: {e}")
            return False
            
        except smtplib.SMTPSenderRefused as e:
            print(f"❌ 邮件发送失败: 发件人被拒绝")
            print(f"   发件人: {self.sender_email}")
            print(f"   错误详情: {e}")
            return False
            
        except smtplib.SMTPException as e:
            print(f"❌ 邮件发送失败: SMTP错误")
            print(f"   错误类型: {type(e).__name__}")
            print(f"   错误详情: {e}")
            return False
            
        except Exception as e:
            print(f"❌ 邮件发送失败: 未知错误")
            print(f"   错误类型: {type(e).__name__}")
            print(f"   错误详情: {e}")
            return False

    def _build_html_message(self, recipient: str, subject: str, html_content: str) -> MIMEMultipart:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.sender_email
        msg["To"] = recipient
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

    def send_html(self, recipient: str, subject: str, html_content: str) -> bool:
        """Send caller-built rich HTML to a single recipient.  All SMTP errors are fail-soft."""
        return bool(self.send_html_multi([recipient], subject, html_content))
    
    def _generate_html(self, articles: list) -> str:
        """生成HTML格式邮件内容"""
        html = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
        .header h1 { margin: 0; font-size: 24px; }
        .header p { margin: 5px 0 0; opacity: 0.9; }
        .article { background: #f8f9fa; border-radius: 8px; padding: 15px; margin-bottom: 15px; border-left: 4px solid #667eea; }
        .article h2 { margin: 0 0 10px; font-size: 16px; color: #333; }
        .article h3 { margin: 5px 0; font-size: 14px; color: #666; font-weight: normal; }
        .meta { font-size: 12px; color: #888; margin-bottom: 10px; }
        .abstract { font-size: 13px; color: #555; margin-top: 10px; }
        .link { display: inline-block; margin-top: 10px; color: #667eea; text-decoration: none; font-size: 13px; }
        .link:hover { text-decoration: underline; }
        .footer { text-align: center; color: #888; font-size: 12px; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; }
        .ai-tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-left: 8px; }
        .ai-tag.ai { background: #d1fae5; color: #059669; }
        .ai-tag.non-ai { background: #f3f4f6; color: #6b7280; }
    </style>
</head>
<body>
    <div class="header">
        <h1>📚 文献追踪更新</h1>
        <p>发现 {count} 篇符合关键词的新文献</p>
    </div>
""".format(count=len(articles))
        
        for article in articles:
            authors = ", ".join(article.authors[:3]) if article.authors else "未知"
            if article.authors and len(article.authors) > 3:
                authors += " et al."
            
            abstract_zh = getattr(article, 'abstract_zh', '') or ''
            abstract_preview = abstract_zh[:300] + "..." if len(abstract_zh) > 300 else abstract_zh
            
            # 判断是否AI相关
            ai_keywords = ['machine', 'learn', 'neural', 'network']
            text = f"{article.title} {getattr(article, 'title_zh', '')} {getattr(article, 'abstract', '')} {abstract_zh}".lower()
            is_ai = any(kw in text for kw in ai_keywords)
            ai_tag = '<span class="ai-tag ai">🤖 AI</span>' if is_ai else '<span class="ai-tag non-ai">📚 非AI</span>'
            
            html += f"""
    <div class="article">
        <h2>{article.title} {ai_tag}</h2>
        <h3>{getattr(article, 'title_zh', '')}</h3>
        <div class="meta">
            <strong>{article.journal}</strong> | {article.pub_date} | {authors}
        </div>
        <div class="abstract">{abstract_preview}</div>
        <a href="{article.link}" class="link">查看原文 →</a>
    </div>
"""
        
        html += """
    <div class="footer">
        <p>此邮件由文献追踪系统自动发送</p>
    </div>
</body>
</html>
"""
        return html
    
    def _generate_text(self, articles: list) -> str:
        """生成纯文本格式邮件内容"""
        text = f"文献追踪更新 - 发现 {len(articles)} 篇新文献\n"
        text += "=" * 50 + "\n\n"
        
        for i, article in enumerate(articles, 1):
            authors = ", ".join(article.authors[:3]) if article.authors else "未知"
            title_zh = getattr(article, 'title_zh', '')
            
            text += f"{i}. {article.title}\n"
            if title_zh:
                text += f"   中文: {title_zh}\n"
            text += f"   期刊: {article.journal} | 日期: {article.pub_date}\n"
            text += f"   作者: {authors}\n"
            text += f"   链接: {article.link}\n\n"
        
        return text

    def _generate_digest_html(self, articles: list) -> str:
        """生成摘要版HTML格式邮件内容（仅标题列表）"""
        # 统计AI相关文献
        ai_keywords = ['machine', 'learn', 'neural', 'network']
        ai_articles = []
        non_ai_articles = []
        
        for article in articles:
            text = f"{article.title} {getattr(article, 'title_zh', '')} {getattr(article, 'abstract', '')}".lower()
            if any(kw in text for kw in ai_keywords):
                ai_articles.append(article)
            else:
                non_ai_articles.append(article)
        
        html = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
        .header h1 { margin: 0; font-size: 24px; }
        .header p { margin: 5px 0 0; opacity: 0.9; }
        .stats { background: #f8f9fa; padding: 15px; border-radius: 8px; margin-bottom: 20px; display: flex; gap: 20px; flex-wrap: wrap; }
        .stat-item { font-size: 14px; }
        .stat-item strong { color: #667eea; }
        .stat-item.ai strong { color: #059669; }
        .stat-item.non-ai strong { color: #6b7280; }
        .section { margin-bottom: 25px; }
        .section h2 { font-size: 16px; color: #333; border-bottom: 2px solid #667eea; padding-bottom: 8px; margin-bottom: 15px; }
        .section h2.ai { border-color: #059669; }
        .section h2.non-ai { border-color: #6b7280; }
        .article-item { padding: 10px 0; border-bottom: 1px solid #eee; }
        .article-item:last-child { border-bottom: none; }
        .article-title { font-size: 14px; font-weight: 600; color: #333; margin-bottom: 4px; }
        .article-title a { color: inherit; text-decoration: none; }
        .article-title a:hover { color: #667eea; }
        .article-meta { font-size: 12px; color: #888; }
        .footer { text-align: center; color: #888; font-size: 12px; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; }
    </style>
</head>
<body>
    <div class="header">
        <h1>📚 文献追踪更新 [摘要版]</h1>
        <p>发现 {total} 篇符合关键词的新文献</p>
    </div>
    
    <div class="stats">
        <div class="stat-item">总计: <strong>{total}</strong> 篇</div>
        <div class="stat-item ai">🤖 AI相关: <strong>{ai_count}</strong> 篇</div>
        <div class="stat-item non-ai">📚 非AI: <strong>{non_ai_count}</strong> 篇</div>
    </div>
""".format(total=len(articles), ai_count=len(ai_articles), non_ai_count=len(non_ai_articles))
        
        # 按期刊分组显示AI相关文献
        if ai_articles:
            html += '<div class="section"><h2 class="ai">🤖 AI相关文献</h2>'
            html += self._generate_journal_groups(ai_articles)
            html += '</div>'
        
        # 按期刊分组显示非AI文献
        if non_ai_articles:
            html += '<div class="section"><h2 class="non-ai">📚 非AI文献</h2>'
            html += self._generate_journal_groups(non_ai_articles)
            html += '</div>'
        
        html += """
    <div class="footer">
        <p>此邮件由文献追踪系统自动发送 [摘要版 - 仅显示标题列表]</p>
    </div>
</body>
</html>
"""
        return html
    
    def _generate_journal_groups(self, articles: list) -> str:
        """按期刊分组生成文献列表HTML"""
        by_journal = {}
        for article in articles:
            journal = article.journal or '未知期刊'
            if journal not in by_journal:
                by_journal[journal] = []
            by_journal[journal].append(article)
        
        html = ""
        for journal, journal_articles in by_journal.items():
            html += f'<div style="margin-bottom: 15px;"><strong style="color: #667eea; font-size: 13px;">📰 {journal}</strong>'
            for article in journal_articles:
                title_zh = getattr(article, 'title_zh', '') or article.title
                html += f'''
                <div class="article-item">
                    <div class="article-title"><a href="{article.link}" target="_blank">{title_zh}</a></div>
                    <div class="article-meta">📅 {article.pub_date or '未知日期'}</div>
                </div>
'''
            html += '</div>'
        
        return html
    
    def _generate_digest_text(self, articles: list) -> str:
        """生成摘要版纯文本格式邮件内容"""
        # 统计
        ai_keywords = ['machine', 'learn', 'neural', 'network']
        ai_count = sum(1 for a in articles if any(kw in f"{a.title} {getattr(a, 'abstract', '')}".lower() for kw in ai_keywords))
        
        text = f"文献追踪更新 [摘要版] - 发现 {len(articles)} 篇新文献\n"
        text += f"AI相关: {ai_count} 篇 | 非AI: {len(articles) - ai_count} 篇\n"
        text += "=" * 50 + "\n\n"
        
        # 按期刊分组
        by_journal = {}
        for article in articles:
            journal = article.journal or '未知期刊'
            if journal not in by_journal:
                by_journal[journal] = []
            by_journal[journal].append(article)
        
        for journal, journal_articles in by_journal.items():
            text += f"【{journal}】\n"
            for i, article in enumerate(journal_articles, 1):
                title_zh = getattr(article, 'title_zh', '') or article.title
                text += f"  {i}. {title_zh}\n"
                text += f"     链接: {article.link}\n"
            text += "\n"
        
        return text
