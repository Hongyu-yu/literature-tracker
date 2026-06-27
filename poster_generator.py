"""概念海报：gpt-5.5 抽 5 要素 + elements_en + title_zh，gpt-image-2 生成英文信息图。"""
import os, re, json
from string import Template
from image_provider import generate_and_save

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "ai_prompts", "poster_elements.txt")
_IMAGE_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "ai_prompts", "poster_image.txt")
_KEYS = ["研究问题", "创新方法", "工作流程", "关键结果", "应用价值"]
_EN_KEYS = ["research_question", "method", "workflow", "result", "value"]

def _load_template():
    with open(_PROMPT_PATH, encoding="utf-8") as f:
        return f.read()

def _parse_elements(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    if not all(k in d for k in _KEYS):
        return None
    elements = {k: str(d.get(k, "")) for k in _KEYS}
    en_raw = d.get("elements_en") or {}
    elements_en = {k: str(en_raw.get(k, "")) for k in _EN_KEYS} if isinstance(en_raw, dict) else {}
    return {"elements": elements, "elements_en": elements_en,
            "title_zh": str(d.get("title_zh", "") or "")}

def extract_elements(meta, markdown, provider, language="中文", max_chars=40000):
    if not markdown or provider is None:
        return None
    try:
        ctx = markdown[:max_chars]
        prompt = Template(_load_template()).safe_substitute(
            language=language, title=str(meta.get("title", "")), context=ctx)
        return _parse_elements(provider.call_api(prompt))
    except Exception as e:
        print(f"⚠️ extract_elements failed: {e}"); return None

def _build_summary_for_image(elements_zh):
    elements_zh = elements_zh or {}
    lines = []
    for i, k in enumerate(_KEYS, 1):
        v = str(elements_zh.get(k, "") or "").strip()
        if v:
            lines.append(f"{i}. {k}：{v}")
    return "\n".join(lines)

def _load_image_template():
    with open(_IMAGE_PROMPT_PATH, encoding="utf-8") as f:
        return f.read()

def build_infographic_prompt(elements_zh, title, language="中文"):
    summary = _build_summary_for_image(elements_zh)
    return Template(_load_image_template()).safe_substitute(
        summaryForImage=summary, title=str(title or ""), language=language)

def generate_poster(meta, markdown, provider, out_dir="docs/images/posters",
                    api_key=None, base=None):
    parsed = extract_elements(meta, markdown, provider)
    if not parsed:
        return None
    doc_id = meta.get("doc_id") or meta.get("paper_id") or "unknown"
    out_path = os.path.join(out_dir, f"{doc_id}.webp")
    prompt = build_infographic_prompt(parsed["elements"], meta.get("title", ""))
    saved = generate_and_save(prompt, out_path, max_edge=1536,
                              api_key=api_key, base=base)
    return {"elements": parsed["elements"],
            "elements_en": parsed["elements_en"],
            "title_zh": parsed["title_zh"],
            "image": (saved or "").replace("docs/", "") or None,
            "doc_id": doc_id}
