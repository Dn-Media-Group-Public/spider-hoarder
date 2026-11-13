"""The OCR sidecar (docs/plans/parser-wrapper.md, "Remote OCR"): a stateless forwarder. POST /ocr with image bytes (PNG/JPEG, already
downscaled by the parser) and `Authorization: Bearer <profile token>`; the image goes to that profile's OpenAI-compatible endpoint, the
transcript comes back. It takes no URL and no path, has no blob access and never decodes an image. The token picks the profile, so a
parser can only reach the profile its job was given. OCR_PROFILES: {name: {token, base_url, model, auth: none|bearer|google, api_key?,
audience?, prompt?}} (JSON)."""

import asyncio
import base64
import hmac
import json
import os

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

IMAGE_TYPES = ("image/png", "image/jpeg")
PROMPT = "Transcribe all text in this image exactly as written. Output only the text."


def profile_for(profiles: dict, auth_header: str) -> dict | None:
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    found = None
    for p in profiles.values():  # no early exit: the time does not tell which profile matched
        if hmac.compare_digest(token.encode(), p["token"].encode()):
            found = p
    return found


async def upstream_headers(p: dict) -> dict:
    mode = p.get("auth", "none")
    if mode == "bearer":
        return {"Authorization": f"Bearer {p['api_key']}"}
    if mode == "google":  # an ID token for the endpoint's audience, from the metadata server or the service account
        import google.auth.transport.requests
        import google.oauth2.id_token
        tok = await asyncio.to_thread(google.oauth2.id_token.fetch_id_token, google.auth.transport.requests.Request(), p["audience"])
        return {"Authorization": f"Bearer {tok}"}
    return {}


def make_app(profiles: dict, max_body: int = 10 << 20, max_concurrent: int = 4, timeout: float = 120, transport=None) -> Starlette:
    busy = asyncio.Semaphore(max_concurrent)

    async def ocr(request: Request):
        p = profile_for(profiles, request.headers.get("authorization", ""))
        if p is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in IMAGE_TYPES:
            return JSONResponse({"error": "PNG or JPEG only"}, 415)
        if int(request.headers.get("content-length") or 0) > max_body:
            return JSONResponse({"error": "body too large"}, 413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > max_body:
                return JSONResponse({"error": "body too large"}, 413)
        if busy.locked():
            return JSONResponse({"error": "busy"}, 503)
        async with busy:
            msg = {"role": "user", "content": [{"type": "text", "text": p.get("prompt") or PROMPT},
                                               {"type": "image_url", "image_url": {"url": f"data:{ctype};base64,{base64.b64encode(body).decode()}"}}]}
            try:
                async with httpx.AsyncClient(timeout=timeout, transport=transport) as c:
                    r = await c.post(p["base_url"].rstrip("/") + "/chat/completions", headers=await upstream_headers(p),
                                     json={"model": p["model"], "messages": [msg], "temperature": 0})
                    r.raise_for_status()
                    return JSONResponse({"text": r.json()["choices"][0]["message"]["content"] or ""})
            except Exception as e:  # noqa: BLE001  the parser keeps its Tesseract text and notes why
                return JSONResponse({"error": f"upstream failed ({type(e).__name__})"}, 502)

    return Starlette(routes=[Route("/ocr", ocr, methods=["POST"]), Route("/health", lambda r: JSONResponse({"ok": True}))])


def app_from_env() -> Starlette:
    e = os.environ
    return make_app(json.loads(e["OCR_PROFILES"]), int(e.get("OCR_MAX_BODY") or 10 << 20), int(e.get("OCR_MAX_CONCURRENT") or 4),
                    float(e.get("OCR_TIMEOUT") or 120))
