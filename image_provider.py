"""gpt-image-2 via OpenAI-compatible Responses API（必须流式）+ WebP 压缩 + 超时/重试。"""
import os, io, json, time
import requests

def _env_int(name, default):
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default

def _responses_url(base):
    base = (base or "").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if not base.endswith("/v1"):
        base = base + "/v1" if "/v1" not in base else base
    return base + "/responses"

def generate_image_b64(prompt, api_key=None, base=None, timeout=None, retries=None, backoff=None):
    """返回 PNG base64 字符串；失败返回 None。

    超时/重试可经环境变量调（图生成偶发 Read timeout，单次失败会丢整张海报）：
    - IMAGE_TIMEOUT_SECONDS：读超时，默认回退 AI_TIMEOUT_SECONDS，再回退 300
    - IMAGE_MAX_RETRIES：失败重试次数（初试之外），默认 2
    - IMAGE_RETRY_BACKOFF_SECONDS：重试退避基数（第 n 次等 backoff*n 秒），默认 5
    - IMAGE_ACCEPT_PARTIAL：=1 时，重试用尽仍只有中间帧就退而求其次收下它；默认 0，
      即宁可这轮没图（image 为空的条目下一轮 backfill_posters 会重生成），也不把半成品
      糊图永久钉进 image 字段——一旦写进去就再没人回头补了。
    """
    api_key = api_key or os.environ.get("IMAGE_API_KEY") or os.environ.get("AI_API_KEY")
    base = base or os.environ.get("IMAGE_API_BASE") or os.environ.get("AI_BASE_URL")
    url = _responses_url(base)
    if timeout is None:
        timeout = _env_int("IMAGE_TIMEOUT_SECONDS", _env_int("AI_TIMEOUT_SECONDS", 300))
    if retries is None:
        retries = _env_int("IMAGE_MAX_RETRIES", 2)
    if backoff is None:
        backoff = _env_int("IMAGE_RETRY_BACKOFF_SECONDS", 5)
    payload = {"model": "gpt-5.5",
               "input": [{"role": "user", "content": prompt}],
               "tools": [{"type": "image_generation"}],
               "stream": True}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    attempts = max(1, retries + 1)
    accept_partial = str(os.environ.get("IMAGE_ACCEPT_PARTIAL", "")).strip().lower() \
        in ("1", "true", "yes", "on")
    best_partial_b64 = None      # 跨尝试记住见过的最后一帧中间图，仅 accept_partial 时兜底
    for attempt in range(1, attempts + 1):
        # 成品图与中间帧分开存：中间帧永远不能盖掉已经拿到的成品图
        final_b64, partial_b64, partial_idx, stream_ok = None, None, None, False
        try:
            with requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                for raw in r.iter_lines(decode_unicode=True):
                    if not raw or not raw.startswith("data:"):
                        continue
                    data = raw[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try: ev = json.loads(data)
                    except Exception: continue
                    if ev.get("type") == "response.output_item.done":
                        item = ev.get("item", {})
                        if item.get("type") == "image_generation_call" and item.get("result"):
                            final_b64 = item["result"]
                    elif ev.get("type") == "response.image_generation_call.partial_image":
                        # 中间帧按到达顺序越来越清晰：留最后一帧（旧代码锁死第一帧＝最糊的那张）
                        if ev.get("partial_image_b64"):
                            partial_b64 = ev["partial_image_b64"]
                            partial_idx = ev.get("partial_image_index")
            stream_ok = True
        except Exception as e:
            print(f"⚠️ image generation failed (attempt={attempt}/{attempts}): {e}")
        if final_b64:
            # 成品图已到手就收下——哪怕流在这之后才断，也别白扔一张已经生成好的图
            return final_b64
        if partial_b64:
            # 只有中间帧＝流被截断，按失败处理去重试；半成品图不能当成功返回，否则它会被写进
            # image 字段，run_deep（有 poster 即复用）和 backfill_posters（有 image 即跳过）
            # 从此都当这条已完成，糊图永远留在每日页上。
            best_partial_b64 = partial_b64
            print(f"⚠️ image stream truncated: only a partial frame "
                  f"(partial_image_index={partial_idx}) (attempt={attempt}/{attempts})")
        elif stream_ok:
            print(f"⚠️ image generation empty (attempt={attempt}/{attempts})")
        if attempt < attempts and backoff > 0:
            time.sleep(backoff * attempt)
    if best_partial_b64 and accept_partial:
        print("⚠️ still only a partial image after retries, accepting it (IMAGE_ACCEPT_PARTIAL=1)")
        return best_partial_b64
    return None

def compress_to_webp(png_bytes, out_path, max_edge=768, quality=80):
    from PIL import Image  # lazy import: Pillow only needed at compression time
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = im.size
    if max(w, h) > max_edge:
        scale = max_edge / max(w, h)
        im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    im.save(out_path, "WEBP", quality=quality, method=6)
    return out_path

def generate_and_save(prompt, out_path, max_edge=768, quality=80, **kw):
    import base64
    b64 = generate_image_b64(prompt, **kw)
    if not b64:
        return None
    try:
        compress_to_webp(base64.b64decode(b64), out_path, max_edge, quality)
        return out_path
    except Exception as e:
        print(f"⚠️ compress failed: {e}"); return None
