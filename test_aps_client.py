import io, json, contextlib
from unittest import mock
import aps_client
from aps_client import ApsClient

class FakeResp:
    def __init__(self, text="", content=b"", status=200, headers=None, url=""):
        self.text = text; self.content = content; self.status_code = status
        self.headers = headers or {}; self.url = url
    def raise_for_status(self):
        if self.status_code >= 400: raise Exception(self.status_code)

def test_list_dates_parses_folder_links():
    html = ("<a href='/browse?prefix=APS%2F2026-05-27%2F'>2026-05-27/</a>"
            "<a href='/browse?prefix=APS%2F2026-05-28%2F'>2026-05-28/</a>"
            "<a href='/browse?prefix=APS%2Fbegin%2F'>begin/</a>")
    with mock.patch.object(aps_client.requests, "get", return_value=FakeResp(text=html)):
        c = ApsClient(base="http://h", user="u", password="p")
        dates = c.list_dates(window_days=3650, today="2026-05-30")
    assert "2026-05-28" in dates and "2026-05-27" in dates
    assert "begin" not in dates

def test_fetch_metadata_follows_redirect_jsonl():
    jsonl = ('{"title":"A","journal":"PRL","has_full_text":true,"markdown_oss_key":"k1","doc_id":"d1"}\n'
             '{"title":"B","journal":"PRX","has_full_text":true,"markdown_oss_key":"k2","doc_id":"d2"}\n')
    with mock.patch.object(aps_client.requests, "get", return_value=FakeResp(content=jsonl.encode())):
        c = ApsClient(base="http://h", user="u", password="p")
        metas = c.fetch_metadata("2026-05-28")
    assert len(metas) == 2 and metas[0]["doc_id"] == "d1"

def test_fetch_markdown_returns_text():
    with mock.patch.object(aps_client.requests, "get", return_value=FakeResp(content=b"# Title\n\nbody")):
        c = ApsClient(base="http://h", user="u", password="p")
        md = c.fetch_markdown({"markdown_oss_key": "APS/2026-05-28/markdown/d1/d1.md"})
    assert md.startswith("# Title")

def test_errors_are_swallowed():
    def boom(*a, **k): raise Exception("network down")
    with mock.patch.object(aps_client.requests, "get", side_effect=boom):
        c = ApsClient(base="http://h", user="u", password="p")
        assert c.fetch_metadata("2026-05-28") == []
        assert c.fetch_markdown({"markdown_oss_key": "k"}) == ""


# ---- 回归：HTTP 错误页不得伪装成「空结果」或「论文全文」 ----

def _run(fn, resp):
    """在 mock 掉 requests.get 的情况下跑 fn，返回 (结果, 打印出来的日志)。"""
    buf = io.StringIO()
    with mock.patch.object(aps_client.requests, "get", return_value=resp):
        c = ApsClient(base="http://h", user="u", password="p")
        with contextlib.redirect_stdout(buf):
            out = fn(c)
    return out, buf.getvalue()


def test_error_page_is_never_returned_as_markdown():
    """403 的 HTML 错误页曾被当作论文全文喂给模型（白烧 token + 污染深读缓存）。"""
    page = b"<html><head><title>403 Forbidden</title></head><body>nope</body></html>"
    md, log = _run(lambda c: c.fetch_markdown({"markdown_oss_key": "k1"}),
                   FakeResp(content=page, status=403))
    assert md == "", f"错误页被当成全文返回: {md!r}"
    assert "fetch_markdown" in log and "⚠️" in log


def test_login_page_with_200_is_not_returned_as_markdown():
    """网关重定向到登录页时状态码是 200，只靠 status 判断挡不住。"""
    page = b"<!DOCTYPE html>\n<html><body>Please sign in</body></html>"
    md, log = _run(lambda c: c.fetch_markdown({"markdown_oss_key": "k1"}),
                   FakeResp(content=page, status=200, headers={"Content-Type": "text/html"}))
    assert md == "", f"登录页被当成全文返回: {md!r}"
    assert "HTML" in log


def test_oss_error_xml_is_not_returned_as_markdown():
    xml = b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>'
    md, _log = _run(lambda c: c.fetch_markdown({"markdown_oss_key": "k1"}), FakeResp(content=xml))
    assert md == ""


def test_real_markdown_still_passes_through():
    """成功路径不能被误伤。"""
    md, log = _run(lambda c: c.fetch_markdown({"markdown_oss_key": "k1"}),
                   FakeResp(content=b"# Title\n\n<img src='f1.png'> body"))
    assert md.startswith("# Title") and log == ""


def test_list_dates_http_error_is_logged_not_silent():
    """401 时旧代码返回 [] 且一行日志都没有，与「APS 今天没更新」无法区分。"""
    dates, log = _run(lambda c: c.list_dates(window_days=4, today="2026-08-31"),
                      FakeResp(text="<html>401 Unauthorized</html>", status=401))
    assert dates == []            # 仍然 fail-soft，不抛给调用方
    assert "list_dates failed" in log and "401" in log


def test_list_dates_unparseable_listing_is_logged():
    """200 但一个日期目录都没解析出来 → 端点/页面结构变了，必须报警。"""
    dates, log = _run(lambda c: c.list_dates(window_days=4, today="2026-08-31"),
                      FakeResp(text="<html>welcome</html>"))
    assert dates == []
    assert "0 个日期目录" in log


def test_list_dates_out_of_window_says_so():
    """列表本身是好的、只是没有窗口内的新日期 —— 日志要跟故障区分开。"""
    html = "<a href='/browse?prefix=APS%2F2026-05-28%2F'>x</a>"
    dates, log = _run(lambda c: c.list_dates(window_days=4, today="2026-08-31"),
                      FakeResp(text=html))
    assert dates == []
    assert "2026-05-28" in log and "窗口起点" in log
    assert "0 个日期目录" not in log


def test_fetch_metadata_drops_non_dict_lines():
    """非 dict 记录流到 run_deep 会 m.get() 抛 AttributeError，整轮任务当场死掉。"""
    body = ('404\n"denied"\n{"doc_id":"d1","has_full_text":true}\n').encode()
    metas, log = _run(lambda c: c.fetch_metadata("2026-08-30"), FakeResp(content=body))
    assert [m["doc_id"] for m in metas] == ["d1"]
    assert all(isinstance(m, dict) for m in metas)
    assert "2 行" in log


def test_fetch_metadata_error_page_is_logged():
    metas, log = _run(lambda c: c.fetch_metadata("2026-08-30"),
                      FakeResp(content=b"<html>401 Unauthorized</html>"))
    assert metas == []
    assert "疑似错误页" in log
