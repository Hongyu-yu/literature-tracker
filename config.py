"""
配置文件 - RSS文献追踪系统
"""

# RSS订阅源列表
RSS_FEEDS = [
    "http://feeds.aps.org/rss/allsuggestions.xml",
    "http://feeds.aps.org/rss/recent/prl.xml",
    "http://feeds.aps.org/rss/recent/prx.xml",
    "http://feeds.aps.org/rss/recent/physics.xml",
    "http://feeds.aps.org/rss/recent/rmp.xml",
    "https://phys.org/rss-feed/physics-news/",
    "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
    "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv",
    "https://www.nature.com/nature.rss",
    "https://www.nature.com/natcomputsci.rss",
    "https://www.nature.com/nchem.rss",
    "https://www.nature.com/natmachintell.rss",
    "https://www.nature.com/natrevmats.rss",
    "https://www.nature.com/nphys.rss",
    "https://www.nature.com/natrevchem.rss",
    "https://www.nature.com/natelectron.rss",
    "https://www.nature.com/nnano.rss",
    "https://www.nature.com/nphoton.rss",
    "https://www.nature.com/natrevphys.rss",
    "https://www.nature.com/ncomms.rss",
    "https://www.nature.com/npjcompumats.rss",
    "https://academic.oup.com/rss/site_5332/3198.xml",
    "https://rss.sciencedirect.com/publication/science/20959273",
    "https://onlinelibrary.wiley.com/action/showFeed?jc=15213773&type=etoc&feed=rss",
    "https://www.pnas.org/rss/Physics.xml",
    "https://www.pnas.org/rss/Applied_Physical_Sciences.xml",
    "https://pubs.aip.org/rss/site_1000043/1000024.xml",  # J. Chem. Phys.（scitation 版已停用，此为迁移后地址）
    "https://pubs.aip.org/rss/site_1000045/1000025.xml",  # Physics Today（替代已崩溃的 feedburner 版）
    "http://feeds.aps.org/rss/recent/prxenergy.xml",
    "http://feeds.aps.org/rss/recent/prmaterials.xml",
    "http://feeds.aps.org/rss/recent/prresearch.xml",
    "http://feeds.aps.org/rss/recent/prb.xml",
    "https://iopscience.iop.org/journal/rss/2632-2153",
    "https://onlinelibrary.wiley.com/action/showFeed?jc=15214095&type=etoc&feed=rss",
    "https://onlinelibrary.wiley.com/action/showFeed?jc=16163028&type=etoc&feed=rss",
    "https://onlinelibrary.wiley.com/action/showFeed?jc=21983844&type=etoc&feed=rss",
    "https://rss.arxiv.org/rss/cond-mat",
    "https://rss.arxiv.org/rss/physics",
    # AI 相关 arXiv 分类（用于 AI×材料/物理/化学交叉，提升召回）
    "https://rss.arxiv.org/rss/cs.LG",
    "https://rss.arxiv.org/rss/stat.ML",
    "https://rss.arxiv.org/rss/cs.AI",
    "https://www.researchsquare.com/rss.xml",
    "https://rss.arxiv.org/rss/cond-mat.supr-con+cond-mat.mtrl-sci+cond-mat.str-el+physics.comp-ph+physics.chem-ph",
    "http://feeds.rsc.org/rss/dd",  # Digital Discovery (RSC - AI for chemistry)
    "https://rss.sciencedirect.com/publication/science/09270256",  # Computational Materials Science
    "https://rss.sciencedirect.com/publication/science/00104655",  # Computer Physics Communications
    "https://www.nature.com/npjquantmats.rss",  # npj Quantum Materials
    "https://rss.sciencedirect.com/publication/science/13697021",  # Materials Today
    "https://www.nature.com/npj2dmaterials.rss",  # npj 2D Materials and Applications
    "http://feeds.aps.org/rss/recent/prapplied.xml",  # Physical Review Applied
    # ========== 2区期刊（仅保留指定）==========
    "https://rss.sciencedirect.com/publication/science/00092614",  # Chemical Physics Letters (CPL)
]

# ========== 已停用的源（2026-09-01 实测，逐条探测过）==========
# 保留在这里而不是直接删掉：知道「为什么不能用」比源地址本身更有价值，
# 免得半年后有人凭印象把它们又加回去。想复活请先用浏览器 UA 实测。
# 之前它们静默零产出长达数月：feedparser 对 403 不抛异常，日志与「今天没有新论文」一模一样。
DISABLED_RSS_FEEDS = {
    # 403 —— 站点封禁自动抓取（已验证换成真实浏览器 UA 仍然 403，不是 UA 问题）
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=jctcce": "ACS JCTC: 403 反爬",
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=jpclcd": "ACS JPCL: 403 反爬",
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=nalefd": "ACS Nano Lett.: 403 反爬",
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=ancac3": "ACS Nano: 403 反爬",
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=chreay": "ACS Chem. Rev.: 403 反爬",
    "https://chemrxiv.org/engage/rss/chemrxiv": "ChemRxiv: 403 反爬",
    "https://www.annualreviews.org/action/showFeed?ui=45mu4&mi=3fndc3&ai=68t8&jc=conmatphys&type=etoc&feed=atom": "Annu. Rev. Condens. Matter Phys.: 403 反爬",
    "https://www.annualreviews.org/action/showFeed?ui=45mu4&mi=3fndc3&ai=sy&jc=physchem&type=etoc&feed=atom": "Annu. Rev. Phys. Chem.: 403 反爬",
    # 域名已停用 —— aip.scitation.org 整体迁到 pubs.aip.org（会 302 到 showFeed 然后 403）
    "https://aip.scitation.org/action/showFeed?type=etoc&feed=rss&jc=jcp": "JCP: 已由 pubs.aip.org/rss/site_1000043/1000024.xml 取代",
    "http://aip.scitation.org/action/showFeed?type=etoc&feed=rss&jc=apl": "APL: 迁站后未找到对应 RSS，待补",
    "https://aip.scitation.org/action/showFeed?type=etoc&feed=rss&jc=jap": "JAP: 迁站后未找到对应 RSS，待补",
    # 上游自己坏了
    "http://feeds.feedburner.com/acs/jacsat": "只返回一条『The location of this RSS feed has changed』占位",
    "http://feeds.feedburner.com/acs/nalefd": "同上，feedburner 占位",
    "http://feeds.feedburner.com/acs/achre4": "同上，feedburner 占位",
    "http://feeds.feedburner.com/physicstodaynews": "返回 'Database error: Table ./rss/feeds is marked as crashed'",
}

# 多用户关键词配置
# 每个用户可以定义自己的关键词列表，用于在网页上筛选相关文献
USER_KEYWORDS = {
    "于宏宇": [
        "ferro",
        "machine",
        "learn",
        "magne",
        "neural",
        "network",
        "potential",
        "hamiltonian",
    ],
    "朱海燕": [
        "twist",
        "magne",
        "moire",
        "multiferroics",
        "magnetoelectric coupling",
        "CrSBr",
        "altermagnet",
        "ferro",
        "CrTe",
        "magnetic nanotube",
        "topological curvature",
        "curvature-driven",
    ],
    "戴智浩": [
        "symmetry",
        "group theory",
        "altermagnet",
        "ferromagnetoelectric",
        "multiferroics",
        "compensated magnet",
        "unconventional magnet",
    ],
}

# 关键词列表（保持向后兼容，使用于宏宇的关键词）
KEYWORDS = USER_KEYWORDS.get("于宏宇", [])

import importlib.util
import os
import re


def _load_local_config() -> dict:
    """按路径加载同目录下的 config.local.py（可选，已 gitignore）。

    不能写成 `from config.local import ...`：config 是模块不是包，那样写必抛
    ModuleNotFoundError（ImportError 的子类）而被静默吞掉，config.local.py 从未真正生效过。
    文件名含点也不可能用普通 import 语法，只能按路径加载。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.py")
    if not os.path.exists(path):
        return {}
    try:
        spec = importlib.util.spec_from_file_location("config_local", path)
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {k: v for k, v in vars(module).items() if not k.startswith("__")}
    except Exception as exc:  # 本地配置坏了不该拖垮整个流程
        print(f"⚠️ config.local.py 加载失败，改用环境变量: {exc}")
        return {}


_LOCAL_CONFIG = _load_local_config()


def _export_local_scalars(local: dict) -> None:
    """把 config.local.py 里的大写标量键提升成环境变量。

    config.py 只读取 EMAIL_CONFIG / WECHAT_CONFIG / AI_CONFIG 三个字典，
    而 config.local.py.example 还教用户写 APS_HTTP_BASE/USER/PASS、AI_PROVIDER、
    AI_MODEL、AI_BASE_URL、AI_API_KEY 这些**标量**——它们此前没有任何读取方，
    照着文档配完却毫无效果。这些值在 CI 里本来就是走环境变量的，这里补上同一条通路。

    用 setdefault：真实环境变量（CI 的 Secrets）永远优先，本地文件只作缺省兜底。
    """
    for key, value in (local or {}).items():
        if not key.isupper() or not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        # example 文件里的占位符不要污染环境
        if not text or text.startswith("<") or text.startswith("YOUR_"):
            continue
        os.environ.setdefault(key, text)


_export_local_scalars(_LOCAL_CONFIG)


def _parse_recipients(raw, default: list) -> list:
    """把 "a@x.com, b@y.com; c@z.com" 解析成去重保序的地址列表。"""
    if isinstance(raw, (list, tuple, set)):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r"[,;\s]+", str(raw or ""))
    seen, out = set(), []
    for part in parts:
        addr = part.strip().strip("<>").strip()
        if not addr or "@" not in addr or addr in seen:
            continue
        seen.add(addr)
        out.append(addr)
    return out or list(default)


# 邮件配置
# 优先从本地配置文件读取
_local_email_config = _LOCAL_CONFIG.get("EMAIL_CONFIG", {}) or {}

# 默认收件人；用 EMAIL_RECIPIENTS 环境变量/Secret 覆盖（逗号分隔，可配多个）
DEFAULT_EMAIL_RECIPIENTS = ["594836947@qq.com"]

EMAIL_CONFIG = {
    # 多收件人：EMAIL_RECIPIENTS > config.local.py > 代码默认值
    "recipients": _parse_recipients(
        os.environ.get("EMAIL_RECIPIENTS")
        or _local_email_config.get("recipients")
        or _local_email_config.get("recipient"),
        DEFAULT_EMAIL_RECIPIENTS,
    ),
    "smtp_server": _local_email_config.get("smtp_server") or "smtp.qq.com",
    "smtp_port": int(_local_email_config.get("smtp_port") or 465),
    "sender_email": _local_email_config.get("sender_email") or os.environ.get("EMAIL_SENDER", ""),  # 优先从config.local.py读取
    "sender_password": _local_email_config.get("sender_password") or os.environ.get("EMAIL_PASSWORD", ""),  # 优先从config.local.py读取
}
# 向后兼容：旧代码（main.py）仍读单数 recipient，取列表首项
EMAIL_CONFIG["recipient"] = EMAIL_CONFIG["recipients"][0] if EMAIL_CONFIG["recipients"] else ""

# 微信推送配置（Server酱）
# 优先从本地配置文件读取
_local_wechat_config = _LOCAL_CONFIG.get("WECHAT_CONFIG", {}) or {}

WECHAT_CONFIG = {
    "enabled": _local_wechat_config.get("enabled", False),  # 是否启用微信推送
    "sendkey": _local_wechat_config.get("sendkey") or os.environ.get("SERVERCHAN_KEY", ""),  # Server酱SendKey，优先从config.local.py读取
}

# AI摘要配置
# 优先从本地配置文件读取，然后从环境变量读取
_local_ai_config = _LOCAL_CONFIG.get("AI_CONFIG", {}) or {}

AI_CONFIG = {
    "enabled": True,
    # provider 可选: aigw（默认，OpenAI-compatible gateway）、kimi、gemini、openrouter
    # Fallback / 回退 Kimi 配置: 将下行改为 "kimi" 即可切回
    # "provider": _local_ai_config.get("provider") or os.environ.get("AI_PROVIDER", "kimi"),
    # 注意用 `or` 而不是 os.environ.get(key, default)：GitHub 对**已声明但为空**的
    # secret 会注入空字符串，此时 key 是存在的，get 的默认值根本不会生效，
    # provider 会变成 ""，build_provider 于是走到完全不同的客户端上。
    "provider": _local_ai_config.get("provider") or (os.environ.get("AI_PROVIDER") or "").strip() or "aigw",
    # API key：优先 AI_API_KEY，其次 KIMI_API_KEY，再其次 GEMINI_API_KEY（运行时注入，不可硬编码）
    "api_key": (
        _local_ai_config.get("api_key")
        or os.environ.get("AI_API_KEY")
        or os.environ.get("KIMI_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or ""
    ),
    # model：aigw 默认 gpt-5.5
    # Fallback / 回退 Kimi 配置: "kimi-k2-turbo-preview"
    # "model": _local_ai_config.get("model") or os.environ.get("AI_MODEL", "kimi-k2-turbo-preview"),
    "model": _local_ai_config.get("model") or (os.environ.get("AI_MODEL") or "").strip() or "gpt-5.5",
    # base_url：aigw gateway；切回 Kimi 时可删除此行（Kimi 用 KIMI_BASE_URL）
    # Fallback / 回退: "https://supercodex.space/v1"
    "base_url": (_local_ai_config.get("base_url")
                 or (os.environ.get("AI_BASE_URL") or "").strip()
                 or (os.environ.get("OPENROUTER_BASE_URL") or "").strip()
                 or "https://aigw.sotatts.online/v1"),
}

# 去重配置
DEDUP_CONFIG = {
    "enabled": True,  # 是否启用去重
    "similarity_threshold": 0.98,  # 标题相似度阈值（0-1）
}

# GitHub配置
GITHUB_CONFIG = {
    "repo_name": "literature-tracker",
    "branch": "main",
    "pages_branch": "gh-pages",
}

# 数据文件路径
DATA_DIR = "data"
ARTICLES_DIR = "articles"
HISTORY_FILE = "data/history.json"
FAVORITES_FILE = "data/favorites.json"


# 核心关注（ML × ferro/凝聚态）开关与阈值
CORE_FOCUS_CONFIG = {
    "enabled": (os.environ.get("CORE_FOCUS_ENABLED", "1").strip().lower() not in ("0", "false", "no")),
    "daily_max_items": int(os.environ.get("CORE_FOCUS_DAILY_MAX", "8")),
    "weekly_max_items": int(os.environ.get("CORE_FOCUS_WEEKLY_MAX", "20")),
    "min_score": float(os.environ.get("CORE_FOCUS_MIN_SCORE", "0.60")),
}
