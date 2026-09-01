import os
import json
import requests
from datetime import datetime
from ai_summarizer import AISummarizer
import time

class NotionTGNotifier:
    def __init__(self, config_path=".env.lit"):
        self.config = {}
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        self.config[k] = v.strip('"')
        
        self.bot_token = os.environ.get("TG_LIT_BOT_TOKEN") or self.config.get("TG_LIT_BOT_TOKEN")
        self.chat_id = os.environ.get("TG_LIT_CHAT_ID") or self.config.get("TG_LIT_CHAT_ID")
        self.notion_token = os.environ.get("NOTION_API_KEY") or self.config.get("NOTION_API_KEY")
        self.parent_id = os.environ.get("NOTION_LIT_PARENT_ID") or self.config.get("NOTION_LIT_PARENT_ID")
        # 代理只对本机开发有意义：GitHub Actions runner 上没有任何代理。
        # 这里原本硬编码了 http://127.0.0.1:7897 兜底，于是 CI 上每条 TG 消息都被
        # 发往本机不存在的端口，连接被拒后又被 send_tg_message 的 except 吞掉，
        # 看起来像一次临时抖动，实际是一条都推不出去。没配代理就直连。
        self.proxy = (
            os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or ""
        )
        
        self.notion_headers = {
            "Authorization": f"Bearer {self.notion_token}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28"
        }

    def send_tg_message(self, text):
        if not self.bot_token or not self.chat_id:
            print("TG credentials missing")
            return
        
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            r = requests.post(url, json=payload, proxies=proxies, timeout=10)
            if r.status_code != 200:
                # 非 200 时 Telegram 返回的是错误体({"ok": false, ...})，照原样返回
                # 会让调用方把「推送失败」当成功；统一返回 None 表示没推出去。
                print(f"TG Error: {r.text}")
                return None
            return r.json()
        except Exception as e:
            print(f"TG Connection Error: {e}")
            return None

    def get_or_create_page(self, parent_id, title):
        # 父页面 ID 为空说明上一层已经建失败了。不挡一下的话，下面的
        # parent_id.replace() 会直接抛 AttributeError，把整个定时任务打断 ——
        # 一次 Notion 网络抖动不该炸掉整轮推送。
        if not parent_id:
            print(f"⚠️ Notion 父页面 ID 为空，跳过创建「{title}」")
            return None

        # 1. Try to find existing child page by title
        url = f"https://api.notion.com/v1/blocks/{parent_id.replace('-', '')}/children"
        try:
            r = requests.get(url, headers=self.notion_headers, timeout=15)
            if r.status_code == 200:
                results = r.json().get("results", [])
                for block in results:
                    if block["type"] == "child_page":
                        if block["child_page"]["title"] == title:
                            return block["id"]
        except Exception as e:
            print(f"Error fetching child pages: {e}")

        # 2. Create new page if not found
        url = "https://api.notion.com/v1/pages"
        payload = {
            "parent": {"page_id": parent_id},
            "properties": {
                "title": {
                    "title": [{"text": {"content": title}}]
                }
            }
        }
        try:
            r = requests.post(url, headers=self.notion_headers, json=payload, timeout=15)
        except Exception as e:
            print(f"Notion Create Page Error: {type(e).__name__}: {e}")
            return None
        if r.status_code == 200:
            return r.json()["id"]
        else:
            print(f"Notion Create Page Error ({r.status_code}): {r.text}")
            return None

    def append_blocks(self, page_id, blocks):
        url = f"https://api.notion.com/v1/blocks/{page_id}/children"
        try:
            r = requests.patch(url, headers=self.notion_headers, json={"children": blocks}, timeout=15)
        except Exception as e:
            print(f"Notion Append Blocks Error: {type(e).__name__}: {e}")
            return False
        if r.status_code != 200:
            print(f"Notion Append Blocks Error: {r.text}")
        return r.status_code == 200

    def sync_article(self, article_data, ai_analysis):
        if not self.notion_token or not self.parent_id:
            print("Notion credentials missing, skip Notion sync")
            return
        # 1. Get/Create Month Page
        now = datetime.now()
        month_str = now.strftime("%Y年%m月")
        month_page_id = self.get_or_create_page(self.parent_id, month_str)
        if not month_page_id: return
        
        # 2. Get/Create Day Page
        day_str = now.strftime("%Y-%m-%d")
        day_page_id = self.get_or_create_page(month_page_id, day_str)
        if not day_page_id: return
        
        # 3. Append Article blocks
        blocks = [
            {
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": article_data.get('title_zh') or article_data.get('title')}}]
                }
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": "🔗 "}, "annotations": {"bold": True}},
                        {"type": "text", "text": {"content": "原文链接", "link": {"url": article_data.get('link')}}}
                    ]
                }
            },
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": [{"type": "text", "text": {"content": ai_analysis}}],
                    "icon": {"emoji": "🤖"}
                }
            },
            {
                "object": "block",
                "type": "divider",
                "divider": {}
            }
        ]
        self.append_blocks(day_page_id, blocks)

    @staticmethod
    def _push_result(tg_ok, notion_ok):
        """汇总推送结果：一个渠道都没成功时把失败打响，别让它混在正常日志里。"""
        if not tg_ok and not notion_ok:
            print("⚠️ 每日报告一条都没推出去(TG / Notion 均失败或未配置)")
        return bool(tg_ok or notion_ok)

    def send_daily_report(self, summary_data):
        """推送每日汇总，返回是否真的推出去了(TG / Notion 任一成功即 True)。

        以前无论成败都返回 None，调用方(run_optimized_sync.send_daily_summary)
        于是无条件打印「✅ 每日报告已推送至 TG 和 Notion」，全失败也看不出来。
        """
        tg_ok = False
        # 1. Telegram
        if self.bot_token and self.chat_id:
            msg = f"<b>📊 每日文献汇总报告 ({summary_data['date']})</b>\n\n"
            msg += f"今日收录: {summary_data['total']} 篇\n\n"
            msg += f"<b>💡 总览：</b>\n{summary_data.get('overview', '无')}\n\n"
            
            # Add full list to TG
            msg += "<b>📋 文献列表：</b>\n"
            for i, item in enumerate(summary_data.get('full_list', []), 1):
                # Ensure titles and summaries exist
                t_en = item.get('title_en', 'Untitled')
                t_zh = item.get('title_zh', '')
                summary_text = item.get('summary', '')
                link = item.get('link', '#')
                
                line = f"{i}. <a href='{link}'>{t_en}</a>\n"
                if t_zh: line += f"   <i>{t_zh}</i>\n"
                if summary_text: line += f"   📝 {summary_text}\n"
                line += "\n"
                
                if len(msg) + len(line) > 3800:
                    msg += "... (列表过长，更多内容请查阅 Notion)\n"
                    break
                msg += line
            
            tg_ok = bool(self.send_tg_message(msg))
        else:
            print("TG credentials missing, skip Telegram daily report")

        # 2. Notion
        if not self.notion_token or not self.parent_id:
            print("Notion credentials missing, skip Notion daily report")
            return self._push_result(tg_ok, False)
        month_str = datetime.now().strftime("%Y年%m月")
        month_page_id = self.get_or_create_page(self.parent_id, month_str)
        day_str = summary_data['date']
        # 月份页没建出来就别再拿 None 去建日期页(会抛 AttributeError)；
        # 与 sync_article 里的守卫保持一致，留给下一轮重试。
        day_page_id = self.get_or_create_page(month_page_id, day_str) if month_page_id else None
        if not day_page_id:
            print(f"⚠️ Notion 日期页 {day_str} 未就绪，跳过 Notion 日报，下次运行重试")
            return self._push_result(tg_ok, False)

        report_blocks = [
            {
                "object": "block",
                "type": "heading_1",
                "heading_1": {
                    "rich_text": [{"type": "text", "text": {"content": f"📅 {day_str} 汇总报告"}}]
                }
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": summary_data.get('overview', '')}}]
                }
            }
        ]
        
        notion_ok = True  # 任何一批 append 失败就置 False
        for item in summary_data.get('full_list', []):
            report_blocks.extend([
                {
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {
                        "rich_text": [{"type": "text", "text": {"content": item.get('title_en', 'Untitled')}}]
                    }
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {"type": "text", "text": {"content": f"🇨🇳 {item.get('title_zh', '')}\n"}},
                            {"type": "text", "text": {"content": f"📝 {item.get('summary', '')}\n"}},
                            {"type": "text", "text": {"content": "🔗 原文链接", "link": {"url": item.get('link', 'https://example.com')}}}
                        ]
                    }
                },
                {"object": "block", "type": "divider", "divider": {}}
            ])
            
            if len(report_blocks) >= 60:
                notion_ok = self.append_blocks(day_page_id, report_blocks) and notion_ok
                report_blocks = []

        if report_blocks:
            notion_ok = self.append_blocks(day_page_id, report_blocks) and notion_ok

        return self._push_result(tg_ok, notion_ok)

if __name__ == "__main__":
    # Test sync
    notifier = NotionTGNotifier()
    # notifier.send_tg_message("Test from Pi")
