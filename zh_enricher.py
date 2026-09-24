"""
Chinese enrichment utilities:
- Ensure every article has `title_zh`, `abstract_zh` (2-4 句浓缩版) and
  `abstract_zh_full` (完整忠实中文翻译, 同一次 LLM 调用产出).

Strategy:
- Prefer LLM batch translation/summarization via the configured AI provider (OpenRouter recommended).
- Fallback to machine translation (translator.machine_translate: Google → gtx → MyMemory)
  when AI is not configured **or** when an AI batch fails. 2026-09 网关连续 503 期间，
  旧逻辑只在「没配 key」时才机翻，key 在、网关挂 → 中文摘要整批留空。
- 机翻写入的条目带 `zh_source="mt"`；AI 恢复后会被重新纳入候选、用 AI 译文升级。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from ai_summarizer import AISummarizer, build_provider
from text_normalizer import is_suspicious_text, normalize_articles_inplace, normalize_text, strip_announce_prefix


def _extract_json(text: str) -> Any:
    """严格解析:花括号配平截取首个 JSON 对象后 json.loads。

    比旧的贪婪正则(首个 `{` 到最后一个 `}`)稳健:模型在 JSON 后面补一句
    含花括号的客套话(如 `说明：{以上为翻译}`)时不会把整批译文废掉。
    """
    return json.loads(AISummarizer._extract_json_object(text or ""))


def _json_object_is_balanced(text: str) -> bool:
    """响应里的顶层 JSON 对象是否完整闭合 —— 用来识别 max_tokens 截断。

    截断的响应经 json_repair 抢救后,最后一条往往是半截译文;写进
    abstract_zh_full 会被 `_full_needs_translation` 认作"已翻译"而永不重试,
    所以必须先认出截断。
    """
    value = AISummarizer._strip_code_fence(text or "")
    # 必须同时跟踪 {} 和 []，并且**扫完整段文本再看总深度**，不能一见 depth 归零就收工：
    #   * 顶层是数组时（`[{...},{...},{"index":3,"abstract_zh_full":"第3篇完`），
    #     第一个 `{` 是数组的首个元素，它正常闭合 → 旧写法直接判「完整」，漏掉截断；
    #   * 响应前面有含花括号的客套话（`好的，按 {index} 格式输出：\n{"items":[...` 被截断），
    #     `{index}` 先闭合 → 同样漏判。
    # 两种情况下半截译文都会被写进 abstract_zh_full，而 _full_needs_translation 认它「已翻译」
    # 从此永不重试 —— 比旧代码「整批丢弃」更糟，正是这个守卫要防的事。
    opener = min([p for p in (value.find("{"), value.find("[")) if p >= 0], default=-1)
    if opener < 0:
        return False

    depth = 0
    in_string = False
    escape = False
    for ch in value[opener:]:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth < 0:      # 闭合多于开启：结构本身就坏了
                return False
    # 扫到结尾仍有未闭合的容器，或停在字符串中间 → 被截断
    return depth == 0 and not in_string


def _parse_llm_batch(text: str) -> tuple[Dict[int, Dict[str, str]], bool]:
    """把模型响应解析成 {index: 字段} 映射,并返回响应是否被截断。

    先走严格解析(等价于旧代码的成功路径);失败再复用 ai_summarizer 的宽松修复
    (代码块围栏 / 中文引号 / 尾逗号 / json_repair),免得一点 JSON 噪声就丢掉整批译文。
    宽松路径若仍取不到条目则抛出异常,由调用方记录日志并跳过该批。
    """
    strict_error: Optional[Exception] = None
    try:
        return _parse_batch_result(_extract_json(text)), False
    except Exception as exc:
        # `as` 变量在 except 块结束时会被解绑,先转存以便写进日志
        strict_error = exc

    data = AISummarizer._load_json_lenient(text or "", context="zh_enricher 批次")
    truncated = not _json_object_is_balanced(text)
    mapping = _parse_batch_result(data, drop_last=truncated)
    if not mapping:
        raise ValueError(f"宽松解析仍未取到条目(严格解析错误: {type(strict_error).__name__}: {strict_error})")
    return mapping, truncated


def _default_ai_model() -> Optional[str]:
    return (os.environ.get("AI_MODEL") or "").strip() or None


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _full_needs_translation(a: Dict[str, Any]) -> bool:
    """abstract_zh_full 缺失/可疑,或原样等于英文摘要(LLM 未翻译) → 需重填。
    已含中文且与原文一致(源摘要本身是中文)视为正常,保持幂等。"""
    full = (a.get("abstract_zh_full") or "").strip()
    if not full or is_suspicious_text(full):
        return True
    abstract = (a.get("abstract") or "").strip()
    return bool(abstract) and full == abstract and not _has_cjk(full)


def enrich_articles_zh(
    articles: List[Dict[str, Any]],
    *,
    provider_name: str,
    api_key: str,
    model: Optional[str] = None,
    max_items: int = 120,
    batch_size: int = 12,
    abstract_char_limit: int = 3000,
    on_progress: Optional[Any] = None,
) -> int:
    """
    Mutates `articles` in-place. Returns number of articles updated.

    `abstract_zh` is allowed to be a concise Chinese abstract/summary (2-4 sentences).
    `abstract_zh_full` is a complete faithful Chinese translation of the full abstract.
    `on_progress` (optional) is invoked after each processed batch so callers can
    checkpoint-persist partial progress (protects long backfills from job timeouts).
    """

    provider_name = (provider_name or "").strip().lower()
    api_key = (api_key or "").strip()
    model = (model or "").strip() or _default_ai_model()

    normalize_articles_inplace(articles)

    # Candidates: missing or corrupted zh fields（有 AI key 时，机翻过的条目也纳入，等 AI 升级）
    candidates = [
        a
        for a in articles
        if (
            (api_key and a.get("zh_source") == "mt")
            or not (a.get("title_zh") or "").strip()
            or not (a.get("abstract_zh") or "").strip()
            or _full_needs_translation(a)
            or is_suspicious_text(a.get("title_zh"))
            or is_suspicious_text(a.get("abstract_zh"))
        )
        and (a.get("title") or "").strip()
        and (a.get("link") or "").strip()
    ]
    if not candidates:
        return 0

    # Keep the newest first (pub_date is YYYY-MM-DD)
    candidates.sort(key=lambda x: (x.get("pub_date") or ""), reverse=True)
    candidates = candidates[: max_items if max_items > 0 else len(candidates)]

    updated = 0

    if api_key and not _ai_breaker_open():
        provider = _with_breaker(build_provider(provider_name, api_key, model=model))
        # AI 没能给出译文的条目：本轮结束前统一交给机翻，保证每篇都有中文
        mt_pending: List[Dict[str, Any]] = []
        consecutive_failures = 0
        for start in range(0, len(candidates), batch_size):
            batch = candidates[start : start + batch_size]
            if consecutive_failures >= _AI_BATCH_FAILURE_LIMIT:
                mt_pending.extend(batch)
                continue
            batch_payload = []
            for i, a in enumerate(batch, 1):
                title = (a.get("title") or "").strip()
                journal = (a.get("journal") or "").strip()
                authors = a.get("authors") or []
                if isinstance(authors, list):
                    authors_str = ", ".join([str(x) for x in authors[:6]]) + (" 等" if len(authors) > 6 else "")
                else:
                    authors_str = str(authors or "")
                abstract = (a.get("abstract") or "").strip()
                abstract = abstract[:abstract_char_limit]
                batch_payload.append(
                    {
                        "index": i,
                        "title": title,
                        "journal": journal,
                        "authors": authors_str,
                        "abstract": abstract,
                    }
                )

            prompt = _build_batch_prompt(batch_payload)
            batch_no = start // batch_size + 1
            try:
                resp = provider.call_api(prompt)
                mapping, truncated = _parse_llm_batch(resp)
            except Exception as e:
                # 整批丢弃必须留痕,否则只表现为 updated 偏小,与"没有待翻译条目"无法区分
                print(f"⚠️ 中文富化批次 {batch_no} 失败,{len(batch)} 篇改用机器翻译: {type(e).__name__}: {e}")
                mt_pending.extend(batch)
                consecutive_failures += 1
                if consecutive_failures == _AI_BATCH_FAILURE_LIMIT:
                    print(f"🛑 中文富化: AI 连续 {consecutive_failures} 批失败,剩余条目全部改用机器翻译")
                time.sleep(1)
                continue
            consecutive_failures = 0

            if truncated:
                print(f"⚠️ 中文富化批次 {batch_no} 响应被截断,已丢弃末条残缺译文(建议调小 batch_size 或提高 AI_MAX_TOKENS)")
            missing = [i for i in range(1, len(batch) + 1) if i not in mapping]
            if missing:
                print(f"⚠️ 中文富化批次 {batch_no}: {len(missing)}/{len(batch)} 篇未拿到译文,字段保持原样,留待后续运行重试")

            no_write = 0
            for i, a in enumerate(batch, 1):
                item = mapping.get(i)
                if not item:
                    mt_pending.append(a)
                    continue
                # 机翻过的条目：AI 译文一律覆盖（这正是把它们重新纳入候选的目的）
                upgrade = a.get("zh_source") == "mt"
                title_zh = normalize_text((item.get("title_zh") or "").strip())
                abstract_zh = normalize_text((item.get("abstract_zh") or "").strip())
                abstract_zh_full = normalize_text((item.get("abstract_zh_full") or "").strip())
                # updated 只统计**真正写入**的条目:三处写入都有守卫(已有好内容不覆盖),
                # 若照旧按"模型回了非空字符串"计数,典型场景 —— 文章已有 title_zh/abstract_zh、
                # 只缺 abstract_zh_full,而模型恰好漏掉最长的 abstract_zh_full(被 max_tokens 截掉)
                # —— 每轮都报 updated=N 却一字未改,日志看起来在推进,实则永远不收敛。
                changed = False
                if title_zh:
                    if upgrade or not (a.get("title_zh") or "").strip() or is_suspicious_text(a.get("title_zh")):
                        a["title_zh"] = title_zh
                        changed = True
                if abstract_zh:
                    if upgrade or not (a.get("abstract_zh") or "").strip() or is_suspicious_text(a.get("abstract_zh")):
                        a["abstract_zh"] = abstract_zh
                        changed = True
                if abstract_zh_full:
                    if upgrade or _full_needs_translation(a):
                        a["abstract_zh_full"] = abstract_zh_full
                        changed = True
                if upgrade and title_zh and abstract_zh and abstract_zh_full:
                    a.pop("zh_source", None)
                if changed:
                    updated += 1
                else:
                    no_write += 1
                    if _zh_incomplete(a):
                        mt_pending.append(a)

            if no_write:
                print(f"⚠️ 中文富化批次 {batch_no}: {no_write}/{len(batch)} 篇拿到的译文未落盘"
                      f"(模型漏字段或字段已有内容),本批无进展,留待后续运行重试")

            if on_progress:
                try:
                    on_progress()
                except Exception:
                    pass
            time.sleep(0.2)

        if mt_pending:
            print(f"🌐 中文富化: {len(mt_pending)} 篇 AI 未给出译文,改用机器翻译兜底")
            updated += _machine_fill(mt_pending)
        return updated

    # 未配置 AI，或 AI 已熔断：直接机翻
    return _machine_fill(candidates)


def _ai_breaker_open() -> bool:
    """进程内 AI 熔断已打开（翻译/周报先撞到网关故障）→ 本轮直接机翻，不再逐批等重试。"""
    try:
        from ai_breaker import ai_available
        return not ai_available()
    except Exception:
        return False


def _with_breaker(provider: Any) -> Any:
    try:
        from ai_breaker import with_breaker
        return with_breaker(provider)
    except Exception:
        return provider


# AI 批次连续失败多少次后，本轮剩余条目不再尝试 AI（网关整体不可用时每批都要等完整重试）
_AI_BATCH_FAILURE_LIMIT = 2


# research_context.ensure_relation_fields 在缺中文标题时写入的占位：含中文字符，
# 但并不是译文，不能因此被当成「已翻译」。
_PLACEHOLDER_TITLE_PREFIX = "文献研究："


def needs_title_zh(a: Dict[str, Any]) -> bool:
    title_zh = str(a.get("title_zh") or "").strip()
    return (
        not _has_cjk(title_zh)
        or title_zh.startswith(_PLACEHOLDER_TITLE_PREFIX)
        or is_suspicious_text(title_zh)
    )


def needs_abstract_zh(a: Dict[str, Any]) -> bool:
    """有英文摘要、却没有一段真正的中文摘要（abstract_zh 或 abstract_zh_full）。"""
    if not str(a.get("abstract") or "").strip():
        return False
    return not (_has_cjk(a.get("abstract_zh") or "") or _has_cjk(a.get("abstract_zh_full") or ""))


def _zh_incomplete(a: Dict[str, Any]) -> bool:
    return (
        needs_title_zh(a)
        or not (a.get("abstract_zh") or "").strip()
        or _full_needs_translation(a)
    )


def _brief_from_full(full: str, limit: int = 240) -> str:
    """机翻没有「浓缩版」：取完整译文的前几句（≤limit 字，按句号截断）作 abstract_zh。"""
    full = (full or "").strip()
    if len(full) <= limit:
        return full
    cut = max(full.rfind(p, 0, limit) for p in "。；！？")
    return full[: cut + 1] if cut >= limit // 3 else full[:limit].rstrip() + "…"


def _machine_fill(items: List[Dict[str, Any]]) -> int:
    """逐条机翻补齐 title_zh / abstract_zh / abstract_zh_full，返回真正写入的条目数。

    摘要只翻一次：abstract_zh_full 用完整译文，abstract_zh 取其前几句（旧代码对同一段
    摘要翻两遍，机翻额度与耗时都翻倍）。单条失败不影响其余条目，已写入的字段保留。
    """
    try:
        import translator as _translator_mod
    except Exception:
        return 0
    translate = getattr(_translator_mod, "machine_translate", None) or _translator_mod.translate_text

    updated = 0
    for a in items:
        # 同上:只统计真正写入的条目。译文为空时不写(translate 对空输入返回 ""),
        # 免得把空串盖到已有字段上 —— 留空等下次重试,不制造"看似已翻译"的假象。
        changed = False
        try:
            if needs_title_zh(a):
                title_zh = normalize_text(translate(a.get("title") or a.get("title_en") or ""))
                if title_zh:
                    a["title_zh"] = title_zh
                    changed = True
            need_brief = not (a.get("abstract_zh") or "").strip() or is_suspicious_text(a.get("abstract_zh"))
            need_full = _full_needs_translation(a)
            if need_brief or need_full:
                full = normalize_text(translate(strip_announce_prefix(a.get("abstract") or "")))
                if full:
                    if need_full:
                        a["abstract_zh_full"] = full
                    if need_brief:
                        a["abstract_zh"] = _brief_from_full(full)
                    changed = True
        except Exception:
            # 单条翻译失败不影响其余条目(translator 自身已打印 ⚠️ 翻译失败);
            # 本条已写入的字段保留并计入 updated,其余字段留空等下次运行重试
            pass
        if changed:
            a["zh_source"] = "mt"
            updated += 1
    return updated


def _build_batch_prompt(items: List[Dict[str, str]]) -> str:
    lines = []
    for item in items:
        lines.append(
            f"[{item['index']}] Title: {item['title']}\nJournal: {item.get('journal','')}\nAuthors: {item.get('authors','')}\nAbstract: {item['abstract']}\n"
        )
    joined = "\n".join(lines)

    return f"""你是专业的学术翻译与摘要助手。请对以下每条文献生成中文标题、中文摘要与完整中文翻译。\n\n输入列表:\n{joined}\n\n请严格输出 JSON（不要 markdown，不要多余解释）：\n{{\n  \"items\": [\n    {{\"index\": 1, \"title_zh\": \"中文标题\", \"abstract_zh\": \"中文摘要(2-4句,忠实且简洁)\", \"abstract_zh_full\": \"摘要的完整忠实中文翻译(逐句对应原文,不删减不浓缩)\"}},\n    ...\n  ]\n}}\n\n要求:\n1. items 必须包含全部输入条目，index 与输入的 [序号] 严格一致。\n2. 如果原摘要为空/过短/仅为元数据（如 EarlyView、Published online 等），abstract_zh 与 abstract_zh_full 仍应给出基于标题与期刊信息的谨慎概述（不要编造具体数值/结论，允许以“该工作围绕...展开，详情需查阅原文”表述）。\n3. 不要输出任何链接。\n"""


def _parse_batch_result(data: Any, *, drop_last: bool = False) -> Dict[int, Dict[str, str]]:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        # json_repair 遇到"JSON + 尾部说明文字"时会返回 [{...}, [...]] 这类列表,
        # 真正的 items 藏在其中某个元素里,先捞出来再说
        items = data
        for element in data:
            if isinstance(element, dict) and isinstance(element.get("items"), list):
                items = element["items"]
                break
    else:
        raise ValueError("Unexpected JSON schema")

    if drop_last and items:
        # 响应被截断:末条多半是半截译文。宁可留空等下次重试,也不要写入残缺内容
        items = items[:-1]

    mapping: Dict[int, Dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        try:
            idx_int = int(idx)
        except Exception:
            continue
        mapping[idx_int] = {
            "title_zh": str(item.get("title_zh", "") or ""),
            "abstract_zh": str(item.get("abstract_zh", "") or ""),
            "abstract_zh_full": str(item.get("abstract_zh_full", "") or ""),
        }
    return mapping
