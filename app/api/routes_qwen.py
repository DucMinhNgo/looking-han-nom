"""Optional Qwen-assisted OCR and web references."""

from __future__ import annotations

import base64
import json
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import current_user

router = APIRouter(prefix="/api/qwen")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


def _mime(path: Path) -> str:
    return IMAGE_TYPES.get(path.suffix.lower(), "image/jpeg")


def _json_answer(text: str) -> list[dict]:
    text = text.strip().removeprefix("```json").removesuffix("```").strip()
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("lines", data.get("results", data.get("items", [])))
    return data if isinstance(data, list) else []


def _web_search(query: str, limit: int = 3) -> list[dict[str, str]]:
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
            line["sources"] = _web_search(text) if text else []
        return {"enabled": True, "lines": lines, "image": str(resolved)}
    except Exception as exc:
        raise HTTPException(502, f"Qwen/search failed: {exc}") from exc