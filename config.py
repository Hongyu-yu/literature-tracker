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
    "http://feeds.feedburner.com/acs/jacsat",
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=ancac3",
    "https://onlinelibrary.wiley.com/action/showFeed?jc=15213773&type=etoc&feed=rss",
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=nalefd",
    "https://www.annualreviews.org/action/showFeed?ui=45mu4&mi=3fndc3&ai=68t8&jc=conmatphys&type=etoc&feed=atom",
    "https://www.annualreviews.org/action/showFeed?ui=45mu4&mi=3fndc3&ai=sy&jc=physchem&type=etoc&feed=atom",
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=jpclcd",
    "https://www.pnas.org/rss/Physics.xml",
    "https://www.pnas.org/rss/Applied_Physical_Sciences.xml",
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=jctcce",
    "https://aip.scitation.org/action/showFeed?type=etoc&feed=rss&jc=jcp",
    "http://aip.scitation.org/action/showFeed?type=etoc&feed=rss&jc=apl",
    "https://pubs.aip.org/rss/site_1000043/1000024.xml",
    "http://feeds.aps.org/rss/recent/prxenergy.xml",
    "http://feeds.aps.org/rss/recent/prmaterials.xml",
    "http://feeds.aps.org/rss/recent/prresearch.xml",
    "http://feeds.aps.org/rss/recent/prb.xml",
    "https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=chreay",
    "http://feeds.feedburner.com/acs/nalefd",
    "http://feeds.feedburner.com/acs/achre4",
    "http://feeds.feedburner.com/physicstodaynews",
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
    "https://chemrxiv.org/engage/rss/chemrxiv",
    "https://www.researchsquare.com/rss.xml",
    "https://rss.arxiv.org/rss/cond-mat.supr-con+cond-mat.mtrl-sci+cond-mat.str-el+physics.comp-ph+physics.chem-ph",
    "https://feeds.rsc.org/rss/dd",  # Digital Discovery (RSC - AI for chemistry)
    "https://rss.sciencedirect.com/publication/science/09270256",  # Computational Materials Science
    "https://rss.sciencedirect.com/publication/science/00104655",  # Computer Physics Communications
    "https://www.nature.com/npjquantmats.rss",  # npj Quantum Materials
    "https://rss.sciencedirect.com/publication/science/13697021",  # Materials Today
    "https://www.nature.com/npj2dmaterials.rss",  # npj 2D Materials and Applications
    "http://feeds.aps.org/rss/recent/prapplied.xml",  # Physical Review Applied
    # ========== 2区期刊（仅保留指定）==========
    "https://aip.scitation.org/action/showFeed?type=etoc&feed=rss&jc=jap",  # Journal of Applied Physics (JAP)
    "https://rss.sciencedirect.com/publication/science/00092614",  # Chemical Physics Letters (CPL)
]

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
    "mode": _local_email_config.get("mode") or "digest",  # 邮件模式: "full" 完整版（含摘要）, "digest" 摘要版（仅标题列表）
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
    "provider": _local_ai_config.get("provider") or os.environ.get("AI_PROVIDER", "aigw"),
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
    "model": _local_ai_config.get("model") or os.environ.get("AI_MODEL", "gpt-5.5"),
    # base_url：aigw gateway；切回 Kimi 时可删除此行（Kimi 用 KIMI_BASE_URL）
    # Fallback / 回退: "https://supercodex.space/v1"
    "base_url": _local_ai_config.get("base_url") or os.environ.get("AI_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL", "https://aigw.sotatts.online/v1"),
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
