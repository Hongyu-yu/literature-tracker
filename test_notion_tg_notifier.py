#!/usr/bin/env python3
"""notion_tg_notifier 网络健壮性回归测试(stdlib + unittest.mock)。

约束:所有 requests 调用必须显式 timeout;Notion 写调用网络异常不得向上抛
(与类内既有错误处理风格一致:打印并返回 None/False)。
"""
import os
from unittest import mock

import notion_tg_notifier
from notion_tg_notifier import NotionTGNotifier


def _make_notifier():
    n = NotionTGNotifier(config_path="/nonexistent/.env.lit")
    n.bot_token = "tk"
    n.chat_id = "cid"
    n.notion_token = "ntk"
    n.parent_id = "pid"
    n.proxy = None
    return n


def _resp(status=200, payload=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = "mocked"
    return r


def test_all_requests_calls_have_timeout():
    n = _make_notifier()
    with mock.patch.object(notion_tg_notifier, "requests") as req:
        req.get.return_value = _resp(200, {"results": []})
        req.post.return_value = _resp(200, {"id": "page-1", "ok": True})
        req.patch.return_value = _resp(200)

        n.send_tg_message("hi")
        n.get_or_create_page("parent-1", "2026年06月")
        n.append_blocks("page-1", [])

        calls = list(req.get.call_args_list) + list(req.post.call_args_list) + list(req.patch.call_args_list)
        assert calls, "应当发生过 requests 调用"
        missing = [c for c in calls if not c.kwargs.get("timeout")]
        assert not missing, f"存在未设置 timeout 的 requests 调用: {missing}"


def test_notion_write_errors_do_not_raise():
    n = _make_notifier()
    with mock.patch.object(notion_tg_notifier, "requests") as req:
        req.get.side_effect = ConnectionError("net down")
        req.post.side_effect = ConnectionError("net down")
        req.patch.side_effect = ConnectionError("net down")

        page = n.get_or_create_page("parent-1", "标题")  # 不应抛异常
        assert page is None
        ok = n.append_blocks("page-1", [])  # 不应抛异常
        assert ok is False


def _summary(n_items=1):
    return {
        "date": "2026-06-10",
        "total": n_items,
        "overview": "今日概览",
        "full_list": [
            {
                "title_en": f"Paper {i}",
                "title_zh": f"论文 {i}",
                "summary": "一句话摘要",
                "link": f"https://example.com/{i}",
            }
            for i in range(n_items)
        ],
    }


def test_proxy_only_comes_from_env():
    """没配代理时不能兜底到本机 127.0.0.1:7897 —— CI 上会把每条 TG 消息发进黑洞。"""
    clean = {k: v for k, v in os.environ.items()
             if k.lower() not in ("http_proxy", "https_proxy")}
    with mock.patch.dict(os.environ, clean, clear=True):
        n = NotionTGNotifier(config_path="/nonexistent/.env.lit")
    assert not n.proxy, f"未配置代理时不该有兜底代理，实际 = {n.proxy!r}"

    # 真正发消息时也不该带上 proxies（走直连）
    n.bot_token, n.chat_id = "tk", "cid"
    with mock.patch.object(notion_tg_notifier, "requests") as req:
        req.post.return_value = _resp(200, {"ok": True})
        n.send_tg_message("hi")
        assert req.post.call_args.kwargs.get("proxies") is None, "未配代理却仍传了 proxies"

    # 反过来：显式配了代理仍要照用（本机开发路径不能被改坏）
    with mock.patch.dict(os.environ, {"http_proxy": "http://proxy.local:8080"}, clear=False):
        n2 = NotionTGNotifier(config_path="/nonexistent/.env.lit")
    assert n2.proxy == "http://proxy.local:8080", f"配置的代理被丢了: {n2.proxy!r}"


def test_tg_api_error_is_not_reported_as_success():
    """Telegram 非 200 返回的是错误体，不能当成功值回给调用方。"""
    n = _make_notifier()
    with mock.patch.object(notion_tg_notifier, "requests") as req:
        req.post.return_value = _resp(400, {"ok": False, "description": "chat not found"})
        assert n.send_tg_message("hi") is None, "TG 400 却返回了真值，调用方会误判成功"


def test_get_or_create_page_with_empty_parent_does_not_raise():
    """父页面 ID 为空时只能跳过，不能抛 AttributeError 打断整轮任务。"""
    n = _make_notifier()
    with mock.patch.object(notion_tg_notifier, "requests") as req:
        assert n.get_or_create_page(None, "2026-06-10") is None
        assert n.get_or_create_page("", "2026-06-10") is None
        assert not req.get.called and not req.post.called, "父 ID 为空却仍发了 Notion 请求"


def test_daily_report_survives_notion_month_page_failure():
    """Notion 月份页建失败（网络/额度）时，日报不该整个崩掉，TG 那半边要照样算数。"""
    n = _make_notifier()

    def _route_post(url, *a, **kw):
        if "api.telegram.org" in url:
            return _resp(200, {"ok": True, "result": {}})
        return _resp(500, {})  # Notion 建页失败

    with mock.patch.object(notion_tg_notifier, "requests") as req:
        req.get.return_value = _resp(200, {"results": []})
        req.post.side_effect = _route_post
        req.patch.return_value = _resp(200)

        ok = n.send_daily_report(_summary())  # 修复前：AttributeError: 'NoneType' ... 'replace'

    assert ok is True, "TG 已成功推送，却报告成整体失败"
    assert not req.patch.called, "日期页都没建出来，不该再去 append 区块"


def test_daily_report_returns_push_status():
    """全成功返回 True、全失败返回 False —— 以前一律返回 None，调用方无条件打「✅ 已推送」。"""
    n = _make_notifier()
    with mock.patch.object(notion_tg_notifier, "requests") as req:
        req.get.return_value = _resp(200, {"results": []})
        req.post.return_value = _resp(200, {"ok": True, "id": "page-1"})
        req.patch.return_value = _resp(200)
        assert n.send_daily_report(_summary()) is True, "全部推送成功却没返回 True"

    n2 = _make_notifier()
    with mock.patch.object(notion_tg_notifier, "requests") as req:
        req.get.side_effect = ConnectionError("net down")
        req.post.side_effect = ConnectionError("net down")
        req.patch.side_effect = ConnectionError("net down")
        assert n2.send_daily_report(_summary()) is False, "一条都没推出去却没返回 False"


if __name__ == "__main__":
    test_all_requests_calls_have_timeout()
    test_notion_write_errors_do_not_raise()
    test_proxy_only_comes_from_env()
    test_tg_api_error_is_not_reported_as_success()
    test_get_or_create_page_with_empty_parent_does_not_raise()
    test_daily_report_survives_notion_month_page_failure()
    test_daily_report_returns_push_status()
    print("OK")
