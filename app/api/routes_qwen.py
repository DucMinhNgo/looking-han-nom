"""Optional Qwen-assisted OCR and web references."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import current_user

router = APIRouter(prefix="/api/qwen")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
_redis = None
_font_choice_cache: dict[str, tuple[float, dict]] = {}


async def _qwen_cache(request: Request, key: str, value: dict | None = None):
    """Best-effort cache; JSONL remains the source of truth if Redis is down."""
    global _redis
    url = request.app.state.settings.redis_url
    if not url:
        return None
    try:
        if _redis is None:
            from redis.asyncio import Redis
            _redis = Redis.from_url(url, decode_responses=True)
        cache_key = "qwen:v1:" + key
        if value is None:
            cached = await _redis.get(cache_key)
            return json.loads(cached) if cached else None
        await _redis.set(cache_key, json.dumps(value, ensure_ascii=False), ex=86400)
    except Exception:
        return None
    return None


def _mime(path: Path) -> str:
    return IMAGE_TYPES.get(path.suffix.lower(), "image/jpeg")


def _json_answer(text: str) -> list[dict]:
    text = text.strip().removeprefix("```json").removesuffix("```").strip()
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("lines", data.get("results", data.get("items", [])))
    return data if isinstance(data, list) else []


def _web_search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Small no-key reference search; Qwen remains the vision/selection layer."""
    request = UrlRequest(
        "https://html.duckduckgo.com/html/?q=" + quote_plus(query),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    html = urlopen(request, timeout=10).read().decode("utf-8", "ignore")
    found: list[dict[str, str]] = []
    for match in re.finditer(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S
    ):
        href, title = match.groups()
        parsed = urlparse(unescape(href))
        href = unquote(parse_qs(parsed.query).get("uddg", [href])[0])
        title = re.sub(r"<[^>]+>", "", unescape(title)).strip()
        if href.startswith(("http://", "https://")):
            found.append({"title": title, "url": href})
        if len(found) >= limit:
            break
    return found


def _page_match(url: str, text: str) -> dict[str, str | int | bool]:
    """Fetch a concrete result page and report whether the text is present."""
    try:
        request = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urlopen(request, timeout=8).read(2_000_000).decode("utf-8", "ignore")
        plain = re.sub(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
        plain = re.sub(r"<[^>]+>", " ", unescape(plain))
        plain = re.sub(r"\s+", " ", plain).strip()
        position = plain.casefold().find(text.casefold())
        if position < 0:
            return {"found_in_page": False, "snippet": ""}
        start = max(0, position - 180)
        end = min(len(plain), position + len(text) + 180)
        return {
            "found_in_page": True,
            "snippet": plain[start:end],
            "position": position,
        }
    except Exception:
        return {"found_in_page": False, "snippet": ""}


def _image_search(text: str) -> dict[str, str] | None:
    """Return one direct image URL, rather than a search-results URL."""
    try:
        request = UrlRequest(
            "https://www.bing.com/images/search?q=" + quote_plus(text),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html = urlopen(request, timeout=10).read().decode("utf-8", "ignore")
        match = re.search(r'"murl":"(https?://.+?)"', html)
        if not match:
            return None
        image_url = bytes(match.group(1), "utf-8").decode("unicode_escape")
        return {"title": "Ảnh tham khảo trực tiếp", "url": image_url, "image_url": image_url}
    except Exception:
        return None


def _reference_search(
    text: str, item, search_sites: list[str], limit: int = 20
) -> list[dict[str, str]]:
    """Find one concrete image URL for this line, not a search page."""
    result = _image_search(text)
    return [result] if result else []


@router.get("/status")
async def status(request: Request, user: dict = Depends(current_user)):
    settings = request.app.state.settings
    return {"enabled": bool(settings.qwen_api_key), "model": settings.qwen_model}


@router.post("/select-font")
async def select_font(request: Request, user: dict = Depends(current_user)):
    settings = request.app.state.settings
    if not settings.qwen_api_key:
        return {"enabled": False, "message": "Chưa thêm QWEN_API_KEY vào .env."}

    body = await request.json()
    try:
        index = int(body.get("index", -1))
    except (TypeError, ValueError):
        raise HTTPException(422, "index must be an integer") from None
    item = request.app.state.runtime.dataset.get(index)
    if item is None:
        raise HTTPException(404, "row not found")

    requested_keys = body.get("font_keys")
    if not isinstance(requested_keys, list) or not requested_keys:
        raise HTTPException(422, "Select at least one main font")
    if len(requested_keys) > 16 or not all(isinstance(key, str) for key in requested_keys):
        raise HTTPException(422, "font_keys must be a list of at most 16 strings")

    registry_path = Path(__file__).resolve().parents[1] / "static" / "fonts" / "han-fonts.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Font registry unavailable: {exc}") from exc
    candidates = [
        {
            "key": key,
            "label": str(registry[key].get("label", key)),
            "fonts": [
                Path(font).stem
                for font in registry[key].get("files", [])
                if isinstance(font, str)
            ][:12],
        }
        for key in dict.fromkeys(requested_keys)
        if key in registry and registry[key].get("files")
    ]
    if not candidates:
        raise HTTPException(422, "No valid font choices were provided")

    text = (item.ground_truth or "").strip()[:1000]
    caption = (item.caption or "").strip()[:500]
    if not text:
        raise HTTPException(422, "Record has no ground-truth text")
    include_image = body.get("include_image") is True
    image_path = None
    image_cache_identity = ""
    if include_image:
        resolved_image = request.app.state.runtime.images.resolve(item.image)
        image_path = request.app.state.runtime.images.path_for(resolved_image or "")
        if image_path is None:
            raise HTTPException(404, "Record image not found")
        image_cache_identity = (
            f"{resolved_image}:{image_path.stat().st_mtime_ns}:{image_path.stat().st_size}"
        )
    per_character = body.get("per_character") is True
    preferred_font_key = body.get("preferred_font_key")
    if preferred_font_key is not None and (
        not isinstance(preferred_font_key, str)
        or preferred_font_key not in {candidate["key"] for candidate in candidates}
    ):
        raise HTTPException(422, "preferred_font_key must be one of the selected font groups")
    han_chars: list[str] = []
    if per_character:
        text_han_chars = list(dict.fromkeys(
            char for char in text
            if 0x2E80 <= ord(char) <= 0x9FFF
            or 0xF900 <= ord(char) <= 0xFAFF
            or 0x3400 <= ord(char) <= 0x4DBF
            or 0x20000 <= ord(char) <= 0x3134F
        ))
        requested_chars = body.get("characters")
        if requested_chars is not None:
            if not isinstance(requested_chars, list) or not all(
                isinstance(char, str) and len(char) == 1 for char in requested_chars
            ):
                raise HTTPException(422, "characters must be a list of single characters")
            requested_set = set(requested_chars)
            han_chars = [char for char in text_han_chars if char in requested_set]
        else:
            han_chars = text_han_chars
        if not han_chars:
            raise HTTPException(422, "Record has no Han characters to assign fonts")

    payload = json.dumps(
        {
            "model": settings.qwen_model,
            "text": text,
            "caption": caption,
            "candidates": candidates,
            "per_character": per_character,
            "characters": han_chars if per_character else [],
            "image": image_cache_identity,
            "preferred_font_key": preferred_font_key,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cache_key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    cached = _font_choice_cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return {**cached[1], "cached": True}
    _font_choice_cache.pop(cache_key, None)
    redis_cached = await _qwen_cache(request, "font-choice:" + cache_key)
    if redis_cached:
        _font_choice_cache[cache_key] = (time.monotonic() + 86400, redis_cached)
        return {**redis_cached, "cached": True}

    try:
        from openai import OpenAI

        image_content = []
        if image_path is not None:
            image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
            image_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{_mime(image_path)};base64,{image_data}"},
            })
        if per_character:
            prompt = (
                "The attached image is the visual reference for the original writing. "
                "Match its calligraphic appearance as closely as possible. For every "
                "requested Han character, choose the most suitable "
                "font group that is likely to contain a visible Unicode glyph. "
                "Use only supplied font keys and consider each group's font filenames. "
                f"Strongly prefer {preferred_font_key!r} for the original style; choose "
                "another group only if the preferred font lacks a visible glyph. "
                "If no preferred group was specified, choose the closest match to the image. "
                "Choose another "
                "group for a character if needed to avoid a missing glyph. "
                "Do not rewrite, omit, or add characters. Return JSON only as "
                '{"fonts":[{"char":"漢","font_key":"thao"}],"reason":"..."}. '
                f"Text: {text}\nCaption/context: {caption}\n"
                f"Assign exactly these Han characters: {json.dumps(han_chars, ensure_ascii=False)}\n"
                f"Allowed font candidates: {json.dumps(candidates, ensure_ascii=False)}"
            )
        else:
            prompt = (
                "Inspect the attached image and the supplied Han-Nom text. Identify "
                "the closest of the available calligraphic font groups to the writing "
                "in the image, and choose exactly one group. Consider stroke shape, "
                "brush character, layout, and readability. Do not rewrite the text. "
                "Return JSON only with keys font_key, reason. "
                f"Text: {text}\nCaption/context: {caption}\n"
                f"Allowed font candidates: {json.dumps(candidates, ensure_ascii=False)}"
            )
        message_content = (
            [{"type": "text", "text": prompt}, *image_content]
            if image_content else prompt
        )
        client = OpenAI(api_key=settings.qwen_api_key, base_url=settings.qwen_base_url)
        response = client.chat.completions.create(
            model=settings.qwen_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": message_content}],
        )
        result_data = json.loads(
            (response.choices[0].message.content or "{}")
            .strip()
            .removeprefix("```json")
            .removesuffix("```")
            .strip()
        )
        allowed = {candidate["key"] for candidate in candidates}
        if per_character:
            allowed_chars = set(han_chars)
            font_map = {}
            assignments = result_data.get("fonts", [])
            if not isinstance(assignments, list):
                assignments = []
            for assignment in assignments:
                if not isinstance(assignment, dict):
                    continue
                char = assignment.get("char")
                font_key = assignment.get("font_key")
                if char in allowed_chars and font_key in allowed:
                    font_map[char] = font_key
            result_data = {**result_data, "font_map": font_map}
            font_key = None
        else:
            font_key = result_data.get("font_key")
            if font_key not in allowed:
                raise ValueError("Qwen returned a font outside the allowed candidates")
        usage = getattr(response, "usage", None)
        result = {
            "enabled": True,
            "font_key": font_key,
            "characters": han_chars if per_character else [],
            "font_label": next(
                (candidate["label"] for candidate in candidates if candidate["key"] == font_key),
                "Nhiều font theo ký tự" if per_character else "",
            ),
            "font_map": result_data.get("font_map", {}),
            "reason": str(result_data.get("reason", ""))[:240],
            "model": settings.qwen_model,
            "usage": {
                "input_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None),
            } if usage else None,
        }
        _font_choice_cache[cache_key] = (time.monotonic() + 86400, result)
        if len(_font_choice_cache) > 256:
            oldest_key = min(_font_choice_cache, key=lambda key: _font_choice_cache[key][0])
            _font_choice_cache.pop(oldest_key, None)
        await _qwen_cache(request, "font-choice:" + cache_key, result)
        return result
    except Exception as exc:
        raise HTTPException(502, f"Qwen font selection failed: {exc}") from exc


@router.post("/analyze")
async def analyze(request: Request, user: dict = Depends(current_user)):
    settings = request.app.state.settings
    if not settings.qwen_api_key:
        return {"enabled": False, "message": "Chưa thêm QWEN_API_KEY vào .env."}

    body = await request.json()
    item = request.app.state.runtime.dataset.get(int(body.get("index", -1)))
    if item is None:
        raise HTTPException(404, "row not found")
    resolved = request.app.state.runtime.images.resolve(item.image)
    path = request.app.state.runtime.images.path_for(resolved or "")
    if path is None:
        raise HTTPException(404, "image not found")
    cache_key = f"{resolved}:{path.stat().st_mtime_ns}:{settings.qwen_model}"
    cached = await _qwen_cache(request, cache_key)
    if cached:
        cached["cached"] = True
        return cached

    try:
        from openai import OpenAI
        image = base64.b64encode(path.read_bytes()).decode("ascii")
        prompt = (
            "Detect complete vertical Han-Nom text lines. Return only JSON: "
            '{"lines":[{"text":"...","bounding_box":[ymin,xmin,ymax,xmax]}]}. '
            "Coordinates use 0-1000 scale. Preserve characters exactly."
        )
        client = OpenAI(api_key=settings.qwen_api_key, base_url=settings.qwen_base_url)
        response = client.chat.completions.create(
            model=settings.qwen_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{_mime(path)};base64,{image}"
                }},
            ]}],
        )
        lines = _json_answer(response.choices[0].message.content or "{}")
        for line in lines:
            text = str(line.get("text", "")).strip()
            line["sources"] = _reference_search(
                text, item, settings.qwen_search_sites
            ) if text else []
        result = {"enabled": True, "lines": lines, "image": str(resolved)}
        await _qwen_cache(request, cache_key, result)
        return result
    except Exception as exc:
        raise HTTPException(502, f"Qwen/search failed: {exc}") from exc