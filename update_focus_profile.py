#!/usr/bin/env python3
"""抓取关注学者的 Google Scholar 近五年论文并蒸馏研究兴趣画像。

手动 / dispatch workflow 运行（绝不进每日 CI：Scholar 频繁抓取有封 IP 风险）。
流程：Scholar 列表(sortby=pubdate) → OpenAlex 回填摘要(S2/arXiv 兜底)
     → LLM 蒸馏(ai_prompts/focus_profile.txt) → data/focus_interests.json。

网络层 _fetch_scholar_page / _fetch_openalex / _fetch_semantic_scholar /
_fetch_arxiv 为模块级函数，测试可 monkeypatch，绝不触网（契约同 arxiv_fulltext.py）。
所有 IO 失败均吞掉返回空，不阻塞主流程。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from string import Template
from typing import Any, Dict, List, Optional
from urllib.parse import quote

# 五位关注学者（均为复旦大学，Scholar 主页已核实 2026-07-29）
SCHOLARS: List[Dict[str, str]] = [
    {"scholar_id": "ES83JO4AAAAJ", "name": "Hongyu Yu"},
    {"scholar_id": "5GcATiIAAAAJ", "name": "Hongjun Xiang"},
    {"scholar_id": "0OJfPYYAAAAJ", "name": "Xin-Gao Gong"},
    {"scholar_id": "vkdFcR4AAAAJ", "name": "Ji-Hui Yang"},
    {"scholar_id": "h8769sYAAAAJ", "name": "Weibin Chu"},
]

MIN_YEAR = 2021
PROFILE_PATH = "data/focus_interests.json"
TITLE_SIM_THRESHOLD = 0.85
PROFILE_BATCH_SIZE = 25  # 每次送 LLM 蒸馏的论文数

BEIJING_TZ = timezone(timedelta(hours=8))
_PROFILE_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "ai_prompts", "focus_profile.txt")

# 浏览器 UA 伪装（Scholar 对默认 UA 容易拒绝）
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _beijing_today() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


def _request_sleep(scale: float = 1.0) -> None:
    """请求间隔限速（FOCUS_REQ_SLEEP 可调）。"""
    try:
        base = float(os.environ.get("FOCUS_REQ_SLEEP", "2.0"))
    except Exception:
        base = 2.0
    time.sleep(base * scale)


# ========== 网络层（模块级函数，测试可 monkeypatch，绝不触网）==========


def _fetch_scholar_page(scholar_id: str, cstart: int = 0) -> Optional[str]:
    """GET Scholar list_works 页 → HTML；失败 → None。可被测试 monkeypatch。"""
    try:
        import requests

        url = (
            "https://scholar.google.com/citations?view_op=list_works&hl=en"
            f"&user={scholar_id}&pagesize=100&cstart={cstart}&sortby=pubdate"
        )
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
        if r.status_code == 200 and r.text:
            return r.text
        print(f"⚠️ Scholar 列表请求失败 status={r.status_code} user={scholar_id}")
    except Exception as e:
        print(f"⚠️ Scholar 列表请求异常 user={scholar_id}: {e}")
    return None


def _fetch_openalex(title: str, broad: bool = False) -> Optional[Dict[str, Any]]:
    """OpenAlex 按标题查论文 → 解析后的 JSON dict；失败 → None。可被测试 monkeypatch。"""
    try:
        import requests

        q = quote(title or "")
        if broad:
            url = f"https://api.openalex.org/works?search={q}&per-page=5"
        else:
            url = f"https://api.openalex.org/works?filter=title.search:{q}&per-page=5"
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
        if r.status_code == 200:
            return r.json()
        print(f"⚠️ OpenAlex 请求失败 status={r.status_code}")
    except Exception as e:
        print(f"⚠️ OpenAlex 请求异常: {e}")
    return None


def _fetch_semantic_scholar(title: str) -> Optional[Dict[str, Any]]:
    """Semantic Scholar 按标题搜索（有 429 限流，带退避重试）→ JSON dict 或 None。"""
    try:
        import requests

        url = (
            "https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={quote(title or '')}&limit=5&fields=title,abstract,year"
        )
        for attempt in range(3):
            r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"⏳ S2 限流(429)，{wait}s 后重试")
                time.sleep(wait)
                continue
            print(f"⚠️ S2 请求失败 status={r.status_code}")
            return None
    except Exception as e:
        print(f"⚠️ S2 请求异常: {e}")
    return None


def _fetch_arxiv(title: str) -> Optional[str]:
    """arXiv API 按标题搜索 → Atom XML 文本或 None。可被测试 monkeypatch。"""
    try:
        import requests

        url = (
            "http://export.arxiv.org/api/query"
            f"?search_query=ti:%22{quote(title or '')}%22&max_results=3"
        )
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception as e:
        print(f"⚠️ arXiv 请求异常: {e}")
    return None


# ========== Scholar 列表解析 ==========


def _norm_title(t: Any) -> str:
    return re.sub(r"\s+", " ", str(t or "").strip().lower())


def _class_has(attrs: list, name: str) -> bool:
    cls = dict(attrs).get("class", "") or ""
    return name in cls.split()


class _ScholarListParser:
    """基于 stdlib html.parser 的 Scholar list_works 行解析（无 bs4 依赖，同 arxiv_fulltext 风格）。"""

    def __init__(self) -> None:
        from html.parser import HTMLParser

        outer = self

        class _P(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self._cur: Optional[Dict[str, Any]] = None
                self._capture: Optional[str] = None
                self._buf: List[str] = []
                self._in_year_td = False

            def handle_starttag(self, tag, attrs):
                if tag == "tr" and _class_has(attrs, "gsc_a_tr"):
                    self._cur = {"title": "", "grays": [], "year": None}
                    return
                if self._cur is None:
                    return
                if tag == "td":
                    self._in_year_td = _class_has(attrs, "gsc_a_y")
                elif tag == "a" and _class_has(attrs, "gsc_a_at"):
                    self._capture, self._buf = "title", []
                elif tag == "div" and _class_has(attrs, "gs_gray"):
                    self._capture, self._buf = "gray", []
                elif tag == "span" and self._in_year_td:
                    self._capture, self._buf = "year", []

            def handle_endtag(self, tag):
                if self._cur is None:
                    return
                if tag == "td":
                    self._in_year_td = False
                if self._capture == "title" and tag == "a":
                    self._cur["title"] = re.sub(r"\s+", " ", "".join(self._buf)).strip()
                    self._capture = None
                elif self._capture == "gray" and tag == "div":
                    self._cur["grays"].append(re.sub(r"\s+", " ", "".join(self._buf)).strip())
                    self._capture = None
                elif self._capture == "year" and tag == "span":
                    m = re.search(r"(\d{4})", "".join(self._buf))
                    self._cur["year"] = int(m.group(1)) if m else None
                    self._capture = None
                if tag == "tr":
                    cur, self._cur = self._cur, None
                    if cur["title"]:
                        grays = cur["grays"]
                        outer.works.append(
                            {
                                "title": cur["title"],
                                "year": cur["year"],
                                "venue": grays[1] if len(grays) > 1 else "",
                                "abstract": None,
                            }
                        )

            def handle_data(self, data):
                if self._capture:
                    self._buf.append(data)

        self.works: List[Dict[str, Any]] = []
        self._parser = _P()

    def feed(self, html: str) -> None:
        self._parser.feed(html)


def parse_scholar_works(html: str) -> List[Dict[str, Any]]:
    """解析 Scholar list_works 页 → [{title, year, venue, abstract: None}]（纯函数，可测）。"""
    if not html:
        return []
    try:
        p = _ScholarListParser()
        p.feed(html)
        return p.works
    except Exception as e:
        print(f"⚠️ Scholar 列表解析失败: {e}")
        return []


def scrape_scholar_works(
    scholar_id: str,
    min_year: int = MIN_YEAR,
    max_pages: int = 10,
) -> List[Dict[str, Any]]:
    """翻页抓取某学者 min_year 以来的论文（sortby=pubdate，遇更早年份即停）。"""
    works: List[Dict[str, Any]] = []
    seen = set()
    for page in range(max_pages):
        html = _fetch_scholar_page(scholar_id, cstart=page * 100)
        rows = parse_scholar_works(html or "")
        if not rows:
            break
        stop = False
        for w in rows:
            y = w.get("year")
            if y is not None and y < min_year:
                stop = True  # 按日期排序，之后的只会更旧
                continue
            key = _norm_title(w["title"])
            if key in seen:
                continue
            seen.add(key)
            works.append(w)
        if stop or len(rows) < 100:
            break
        _request_sleep()
    return works


# ========== 摘要回填（OpenAlex → S2 → arXiv，逐级兜底）==========


def _title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm_title(a), _norm_title(b)).ratio()


def _reconstruct_abstract(inverted_index: Any) -> Optional[str]:
    """OpenAlex abstract_inverted_index → 纯文本。"""
    if not isinstance(inverted_index, dict) or not inverted_index:
        return None
    try:
        positions: Dict[int, str] = {}
        for word, idxs in inverted_index.items():
            for i in idxs or []:
                positions[int(i)] = word
        text = " ".join(positions[i] for i in sorted(positions))
        return text or None
    except Exception:
        return None


def _openalex_abstract(title: str, broad: bool = False) -> Optional[str]:
    data = _fetch_openalex(title, broad=broad)
    results = (data or {}).get("results") or []
    best, best_sim = None, 0.0
    for cand in results:
        if not isinstance(cand, dict):
            continue
        sim = _title_similarity(title, cand.get("title") or cand.get("display_name") or "")
        if sim > best_sim:
            best, best_sim = cand, sim
    if best is not None and best_sim >= TITLE_SIM_THRESHOLD:
        return _reconstruct_abstract(best.get("abstract_inverted_index"))
    return None


def _s2_abstract(title: str) -> Optional[str]:
    data = _fetch_semantic_scholar(title)
    papers = (data or {}).get("data") or []
    best, best_sim = None, 0.0
    for cand in papers:
        if not isinstance(cand, dict):
            continue
        sim = _title_similarity(title, cand.get("title") or "")
        if sim > best_sim:
            best, best_sim = cand, sim
    if best is not None and best_sim >= TITLE_SIM_THRESHOLD:
        abstract = (best.get("abstract") or "").strip()
        return abstract or None
    return None


def _arxiv_abstract(title: str) -> Optional[str]:
    xml = _fetch_arxiv(title) or ""
    best, best_sim = None, 0.0
    for entry in re.findall(r"<entry>([\s\S]*?)</entry>", xml):
        tm = re.search(r"<title>([\s\S]*?)</title>", entry)
        sm = re.search(r"<summary>([\s\S]*?)</summary>", entry)
        if not tm or not sm:
            continue
        cand_title = re.sub(r"\s+", " ", tm.group(1)).strip()
        sim = _title_similarity(title, cand_title)
        if sim > best_sim:
            best, best_sim = re.sub(r"\s+", " ", sm.group(1)).strip(), sim
    if best is not None and best_sim >= TITLE_SIM_THRESHOLD:
        return best or None
    return None


def backfill_abstract(title: str) -> Optional[str]:
    """标题 → 摘要：OpenAlex(title.search+相似度校验) → OpenAlex 宽搜 → S2(退避) → arXiv。

    全部失败 → None（调用方记 abstract: null，仍以标题参与蒸馏，fail-soft）。
    """
    if not (title or "").strip():
        return None
    abstract = _openalex_abstract(title)
    if abstract:
        return abstract
    _request_sleep(0.5)
    abstract = _openalex_abstract(title, broad=True)
    if abstract:
        return abstract
    _request_sleep(0.5)
    abstract = _s2_abstract(title)
    if abstract:
        return abstract
    _request_sleep(0.5)
    return _arxiv_abstract(title)


# ========== LLM 蒸馏 ==========


def _load_prompt_template() -> Template:
    with open(_PROFILE_PROMPT_PATH, encoding="utf-8") as f:
        return Template(f.read())


def _format_works_for_prompt(works: List[Dict[str, Any]], max_abstract_chars: int = 800) -> str:
    lines = []
    for i, w in enumerate(works, 1):
        abstract = (w.get("abstract") or "").strip()
        abstract = abstract[:max_abstract_chars] if abstract else "（摘要缺失）"
        venue = (w.get("venue") or "").strip()
        year = w.get("year") or ""
        lines.append(f"[{i}] ({year}) {(w.get('title') or '').strip()}\nVenue: {venue}\nAbstract: {abstract}\n")
    return "\n".join(lines)


def _extract_json(text: str) -> Any:
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise ValueError("No JSON object found")
    return json.loads(m.group())


def _call_profile_llm(owner: str, works_text: str, provider: Any) -> Dict[str, Any]:
    """单次蒸馏调用 → {"directions_zh": str, "keywords": [str]}；失败 → 空（fail-soft）。"""
    try:
        prompt = _load_prompt_template().safe_substitute(owner=owner, works=works_text)
        data = _extract_json(provider.call_api(prompt) or "")
        keywords = data.get("keywords") or []
        if not isinstance(keywords, list):
            keywords = [keywords]
        return {
            "directions_zh": str(data.get("directions_zh", "") or ""),
            "keywords": [str(k).strip() for k in keywords if str(k).strip()],
        }
    except Exception as e:
        print(f"⚠️ 画像蒸馏失败({owner}): {e}")
        return {"directions_zh": "", "keywords": []}


def _dedupe_keywords(keywords: List[str]) -> List[str]:
    seen, out = set(), []
    for k in keywords:
        key = _norm_title(k)
        if key and key not in seen:
            seen.add(key)
            out.append(str(k).strip().lower())
    return out


def distill_scholar_directions(
    name: str,
    works: List[Dict[str, Any]],
    provider: Any,
    batch_size: int = PROFILE_BATCH_SIZE,
) -> Dict[str, Any]:
    """按批蒸馏某位学者的论文 → {"directions_zh", "keywords"}；多批时再合并一次。"""
    if provider is None or not works:
        return {"directions_zh": "", "keywords": []}

    partials = []
    for start in range(0, len(works), batch_size):
        batch = works[start : start + batch_size]
        partials.append(_call_profile_llm(name, _format_works_for_prompt(batch), provider))
        _request_sleep(0.3)

    keywords = _dedupe_keywords([k for p in partials for k in p["keywords"]])
    if len(partials) == 1:
        return {"directions_zh": partials[0]["directions_zh"], "keywords": keywords}

    merged_text = "\n".join(f"- {p['directions_zh']}" for p in partials if p["directions_zh"])
    if not merged_text:
        return {"directions_zh": "", "keywords": keywords}
    merged = _call_profile_llm(name, f"分批归纳结果：\n{merged_text}", provider)
    return {"directions_zh": merged["directions_zh"], "keywords": keywords or merged["keywords"]}


# 无 LLM 时的关键词兜底：从论文标题做词频统计
_STOPWORDS = frozenset(
    "the a an and or of in on for with by from to at as is are was were be been being "
    "we our their its it this that these those via using use used based study new two "
    "high low large small first between under over into through about".split()
)


def extract_keywords_from_works(all_works: List[Dict[str, Any]], top_n: int = 30) -> List[str]:
    counts: Dict[str, int] = {}
    for w in all_works:
        tokens = re.findall(r"[a-z][a-z\-]{2,}", _norm_title(w.get("title")))
        for t in set(tokens):
            if t not in _STOPWORDS:
                counts[t] = counts.get(t, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return [w for w, c in ranked if c >= 2][:top_n]


# ========== 画像构建与落盘 ==========


def build_profile(provider: Any = None, max_works: int = 0) -> Dict[str, Any]:
    """抓取 + 回填 + 蒸馏 → focus_interests.json 的数据 dict。"""
    scholars_out: List[Dict[str, Any]] = []
    all_keywords: List[str] = []
    all_works: List[Dict[str, Any]] = []

    for s in SCHOLARS:
        print(f"🔍 抓取 {s['name']} ({s['scholar_id']}) 的 Scholar 论文列表...")
        works = scrape_scholar_works(s["scholar_id"])
        if max_works > 0:
            works = works[:max_works]
        print(f"📊 {s['name']}: {MIN_YEAR}+ 论文 {len(works)} 篇")

        for w in works:
            w["abstract"] = backfill_abstract(w["title"])
            _request_sleep(0.5)
        missing = sum(1 for w in works if not w["abstract"])
        if missing:
            print(f"⚠️ {s['name']}: {missing} 篇摘要回填失败(记 abstract: null，仍以标题参与蒸馏)")

        distilled = distill_scholar_directions(s["name"], works, provider)
        scholars_out.append(
            {
                "scholar_id": s["scholar_id"],
                "name": s["name"],
                "works": works,
                "directions_zh": distilled["directions_zh"],
            }
        )
        all_keywords.extend(distilled["keywords"])
        all_works.extend(works)
        _request_sleep()

    # 汇总"我们的工作"描述 + 全局关键词
    our_work_zh = ""
    keywords = _dedupe_keywords(all_keywords)
    if provider is not None:
        combined = "\n".join(
            f"- {s['name']}: {s['directions_zh']}" for s in scholars_out if s["directions_zh"]
        )
        if combined:
            agg = _call_profile_llm(
                "一个五人研究团队（以下为各位学者的研究方向汇总）",
                f"各位学者的研究方向：\n{combined}",
                provider,
            )
            our_work_zh = agg["directions_zh"]
            keywords = _dedupe_keywords(keywords + agg["keywords"])
    if not keywords:
        # 无 key / 蒸馏全失败：退化为标题词频关键词，匹配阶段仍可做规则预筛
        keywords = extract_keywords_from_works(all_works)

    return {
        "generated_at": _beijing_today(),
        "scholars": scholars_out,
        "our_work_zh": our_work_zh,
        "keywords": keywords,
    }


def _distill_only(provider: Any, path: str = PROFILE_PATH) -> Dict[str, Any]:
    """不重抓 Scholar：读取既有画像文件中的 works，仅重做 LLM 蒸馏与汇总。

    用于 CI dispatch：原始论文清单由本地抓取提交（Scholar 易封云端 IP），
    CI 只承担需要 AI key 的蒸馏步骤。画像文件缺失或为空时返回 None。
    """
    if not os.path.exists(path):
        print(f"⚠️ {path} 不存在，--distill-only 无可蒸馏数据")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = json.load(f)
    except Exception as e:
        print(f"⚠️ 读取 {path} 失败: {e}")
        return None
    scholars_old = old.get("scholars") or []
    if not scholars_old:
        print("⚠️ 画像文件中没有学者数据")
        return None

    scholars_out: List[Dict[str, Any]] = []
    all_keywords: List[str] = []
    all_works: List[Dict[str, Any]] = []
    for s in scholars_old:
        works = s.get("works") or []
        print(f"🔍 蒸馏 {s.get('name')}: {len(works)} 篇（不重抓）")
        distilled = distill_scholar_directions(s.get("name", ""), works, provider)
        scholars_out.append(
            {
                "scholar_id": s.get("scholar_id", ""),
                "name": s.get("name", ""),
                "works": works,
                "directions_zh": distilled["directions_zh"],
            }
        )
        all_keywords.extend(distilled["keywords"])
        all_works.extend(works)
        _request_sleep()

    our_work_zh = ""
    keywords = _dedupe_keywords(all_keywords)
    if provider is not None:
        combined = "\n".join(
            f"- {s['name']}: {s['directions_zh']}" for s in scholars_out if s["directions_zh"]
        )
        if combined:
            agg = _call_profile_llm(
                "一个五人研究团队（以下为各位学者的研究方向汇总）",
                f"各位学者的研究方向：\n{combined}",
                provider,
            )
            our_work_zh = agg["directions_zh"]
            keywords = _dedupe_keywords(keywords + agg["keywords"])
    if not keywords:
        keywords = extract_keywords_from_works(all_works)

    return {
        "generated_at": _beijing_today(),
        "scholars": scholars_out,
        "our_work_zh": our_work_zh,
        "keywords": keywords,
    }


def save_profile(profile: Dict[str, Any], path: str = PROFILE_PATH) -> None:
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="更新研究兴趣画像 data/focus_interests.json（手动/dispatch 运行，不进每日 CI）"
    )
    parser.add_argument("--output", default=PROFILE_PATH, help="输出路径（默认 data/focus_interests.json）")
    parser.add_argument("--max-works", type=int, default=0, help="每位学者最多处理的论文数（0=不限，调试用）")
    parser.add_argument(
        "--distill-only",
        action="store_true",
        help="不重抓 Scholar：读取既有画像文件的 works，仅重做 LLM 蒸馏（CI dispatch 用）",
    )
    args = parser.parse_args()

    provider = None
    ai_key = (os.environ.get("AI_API_KEY") or "").strip()
    if ai_key:
        try:
            from ai_summarizer import build_provider

            provider = build_provider(
                (os.environ.get("AI_PROVIDER") or "gemini").strip().lower(),
                ai_key,
                model=(os.environ.get("AI_MODEL") or "").strip() or None,
            )
        except Exception as e:
            print(f"⚠️ AI provider 初始化失败，将只写原始论文清单: {e}")
            provider = None
    else:
        print("⚠️ 未配置 AI_API_KEY：只写原始论文清单，directions_zh/our_work_zh 留空")

    if args.distill_only:
        profile = _distill_only(provider, args.output)
        if profile is None:
            return 1
    else:
        profile = build_profile(provider=provider, max_works=args.max_works)
    save_profile(profile, args.output)
    total_works = sum(len(s["works"]) for s in profile["scholars"])
    print(
        f"✅ 画像已写入 {args.output} "
        f"(学者 {len(profile['scholars'])} 人, 论文 {total_works} 篇, 关键词 {len(profile['keywords'])} 个)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
