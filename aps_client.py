"""APS 全文源 HTTP 客户端（basic-auth 浏览器）。所有 IO 失败均吞掉返回空，不阻塞主流程。

但**失败必须可见**：网关返回 401/403/404 的 HTML 错误页时，旧实现会把错误页当成正常内容用——
列表页解析出 0 个日期(静默返回 [] ，看起来跟「APS 今天没更新」一模一样)，更糟的是 fetch_markdown
会把错误页原样当作论文全文喂给模型。因此 _get 对非 2xx 直接抛错交给各方法的 except 打日志降级，
fetch_markdown 另外拦截「200 但内容是 HTML/XML 错误页」的情况。
"""
import os, re, json, datetime
from urllib.parse import quote
import requests

_DATE_RE = re.compile(r"prefix=APS%2F(\d{4}-\d{2}-\d{2})%2F")
# 网关/对象存储的错误页特征：nginx 的 <html>、OSS/S3 的 <?xml ...><Error>、SSO 登录页
_HTML_HEAD_RE = re.compile(r"<\s*(!doctype|html|head|body|\?xml|error|title)\b", re.I)


class ApsHttpError(Exception):
    """APS 网关返回非 2xx（凭证过期 / 端点变更 / 对象不存在）。"""


class ApsClient:
    def __init__(self, base=None, user=None, password=None, timeout=40):
        self.base = (base or os.environ.get("APS_HTTP_BASE", "")).rstrip("/")
        self.user = user or os.environ.get("APS_HTTP_USER", "")
        self.password = password or os.environ.get("APS_HTTP_PASS", "")
        self.timeout = timeout

    @property
    def _auth(self):
        return (self.user, self.password) if self.user else None

    def _get(self, path):
        if not path.startswith("http") and not self.base:
            raise ApsHttpError("APS_HTTP_BASE 未配置，APS 层已停用")
        url = path if path.startswith("http") else f"{self.base}{path}"
        r = requests.get(url, auth=self._auth, timeout=self.timeout, allow_redirects=True)
        status = getattr(r, "status_code", 200)
        if status >= 400:
            # 绝不把错误页当内容返回：它会伪装成「空结果」或「论文全文」
            raise ApsHttpError(f"HTTP {status} ← {getattr(r, 'url', '') or url}")
        return r

    def list_dates(self, window_days=30, today=None):
        try:
            r = self._get("/?prefix=APS%2F")
            body = r.text or ""
            found = sorted(set(_DATE_RE.findall(body)))
        except Exception as e:
            print(f"⚠️ APS list_dates failed: {e}"); return []
        if not found:
            # 200 却一个日期目录都解析不出来 = 端点/页面结构变了，或被重定向到登录页。
            # 这跟「APS 今天没更新」是完全不同的故障，必须能在日志里区分出来。
            print(f"⚠️ APS 列表页解析到 0 个日期目录（端点或页面结构变化？响应 {len(body)} 字符）")
            return []
        today = today or datetime.date.today().isoformat()
        cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=window_days)).isoformat()
        recent = [d for d in found if d >= cutoff]
        if not recent:
            print(f"📋 APS 列表正常（共 {len(found)} 天，最新 {found[-1]}），但都早于窗口起点 {cutoff}")
        return recent

    def fetch_metadata(self, date):
        try:
            key = f"APS/{date}/metadata.jsonl"
            r = self._get(f"/download?key={quote(key)}")
            body = r.content.decode("utf-8", "replace")
        except Exception as e:
            print(f"⚠️ APS fetch_metadata {date} failed: {e}"); return []
        metas, bad = [], 0
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                bad += 1; continue
            # 只收 dict：非 dict（如错误页里恰好能解析的数字）流到下游会 .get() 崩掉整轮任务
            if isinstance(obj, dict):
                metas.append(obj)
            else:
                bad += 1
        if bad:
            print(f"⚠️ APS fetch_metadata {date}: {bad} 行不是合法 JSON 对象，已跳过")
        if not metas and body.strip():
            print(f"⚠️ APS fetch_metadata {date}: 响应 {len(body)} 字符但解析不出任何记录，疑似错误页/登录页")
        return metas

    def fetch_markdown(self, meta):
        key = (meta or {}).get("markdown_oss_key") or ""
        if not key:
            return ""
        try:
            r = self._get(f"/download?key={quote(key)}")
            text = r.content.decode("utf-8", "replace")
        except Exception as e:
            print(f"⚠️ APS fetch_markdown {key} failed: {e}"); return ""
        # 即便 200 也可能是登录页/OSS 错误 XML；当成全文喂给模型 = 白烧 token + 污染深读缓存
        ctype = str((getattr(r, "headers", None) or {}).get("Content-Type", "")).lower()
        if "html" in ctype or "xml" in ctype or _HTML_HEAD_RE.match(text.lstrip()[:200]):
            print(f"⚠️ APS fetch_markdown {key}: 返回的是 HTML/XML 而非 Markdown（{len(text)} 字符），已丢弃")
            return ""
        return text

    def list_images(self, meta):
        prefix = (meta or {}).get("image_oss_prefix") or ""
        if not prefix:
            return []
        m = re.search(r"aps-papers/(.+)$", prefix)
        if not m:
            return []
        oss_path = m.group(1)
        try:
            r = self._get(f"/?prefix={quote(oss_path)}")
            return re.findall(r"key=([^'\"]+\.(?:png|jpg|jpeg))", r.text or "")
        except Exception as e:
            print(f"⚠️ APS list_images failed: {e}"); return []
