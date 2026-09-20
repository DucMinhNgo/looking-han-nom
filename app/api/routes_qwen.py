"""Optional Qwen-assisted OCR and web references."""

from __future__ import annotations

import base64
import json
import re
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