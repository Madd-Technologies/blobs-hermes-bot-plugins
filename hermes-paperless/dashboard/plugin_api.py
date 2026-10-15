"""The phone app's way to Paperless-ngx: documents a bot found, streamed through this server to the app's viewer.

Mounted by Hermes under `/api/plugins/hermes-paperless/`, behind the dashboard's own sign-in, so only someone who can already
use the server can reach it. Nothing is stored: each request is passed to Paperless (with the server's Paperless token, which
never leaves this machine) and its answer streamed back as it arrives. A `Range` header is passed on, so a viewer reading a
PDF a part at a time gets parts, if Paperless serves them.

  GET /status                      {configured, host}
  GET /documents/{id}              {id, title, created, original_file_name, mime_type, page_count}
  GET /documents/{id}/preview      the document as a PDF (Paperless's archived copy, or the original)
  GET /documents/{id}/thumb        a small picture of its first page
  GET /documents/{id}/original     the file as it was added
  GET /documents/{id}/preview-data {mime_type, size, data_url}: the preview as JSON, for Hermes Desktop's page (a Desktop
                                   plugin can only read JSON from its routes); capped at DATA_URL_MAX_BYTES
  GET /search?q=...&limit=10       {count, results: [{id, title, created, correspondent, snippet}]}, best match first
  GET /recent?limit=10             the same, newest first

The dashboard tab (`dist/index.js`) and the Desktop page (`../desktop/plugin.js`) use these too.
"""

from __future__ import annotations

import base64
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

_MODULE = "_hermes_paperless_settings"

#: Response headers worth passing back: what a viewer needs to read the body, a part at a time.
PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")

#: Tests put an httpx transport here (a fake Paperless).
transport: httpx.AsyncBaseTransport | None = None

#: The most a document may weigh to come back as a data URL: it is held in memory and grows by a third in base64.
DATA_URL_MAX_BYTES = 30 * 1024 * 1024


def _settings_module():
    """`paperless.py`, loaded once by path: Hermes imports this file apart from the plugin package."""
    if _MODULE not in sys.modules:
        spec = importlib.util.spec_from_file_location(_MODULE, Path(__file__).resolve().parent.parent / "paperless.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE] = module
        spec.loader.exec_module(module)

    return sys.modules[_MODULE]


def _settings():
    settings = _settings_module().load_settings()

    if settings is None:
        raise HTTPException(status_code=503, detail="Paperless is not set up on this server: set PAPERLESS_URL and PAPERLESS_TOKEN in its .env.")

    return settings


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True, transport=transport)


def _headers(token: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    # Identity encoding: the body is passed through byte for byte, with Paperless's length and ranges.
    return {"Authorization": f"Token {token}", "Accept-Encoding": "identity", **(extra or {})}


def _failure(status: int) -> HTTPException:
    if status == 404:
        return HTTPException(status_code=404, detail="Paperless has no such document.")

    if status in (401, 403):
        return HTTPException(status_code=502, detail="Paperless refused this server's token.")

    return HTTPException(status_code=502, detail=f"Paperless answered {status}.")


async def _json(path: str, params: dict[str, Any] | None = None) -> Any:
    settings = _settings()

    async with _client() as client:
        try:
            response = await client.get(f"{settings.url}{path}", headers=_headers(settings.token, {"Accept": "application/json"}), params=params)
        except httpx.HTTPError as error:
            raise HTTPException(status_code=502, detail=f"Paperless did not answer ({type(error).__name__}).") from None

    if response.status_code >= 400:
        raise _failure(response.status_code)

    return response.json()


async def _stream(request: Request, path: str, params: dict[str, Any] | None = None) -> StreamingResponse:
    """Paperless's answer to `path`, streamed back as it arrives, with a `Range` passed on."""
    settings = _settings()
    extra = {"Range": request.headers["range"]} if "range" in request.headers else {}
    client = _client()

    try:
        response = await client.send(client.build_request("GET", f"{settings.url}{path}", headers=_headers(settings.token, extra), params=params), stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Paperless did not answer ({type(error).__name__}).") from None

    if response.status_code >= 400:
        await response.aclose()
        await client.aclose()
        raise _failure(response.status_code)

    headers = {name: value for name, value in response.headers.items() if name.lower() in PASS_HEADERS}

    # Asked for identity; a body that came compressed anyway is decoded below, so its length no longer holds.
    if response.headers.get("content-encoding", "identity") != "identity":
        headers.pop("content-length", None)
    # Shown in the app, never saved by it.
    headers["Content-Disposition"] = "inline"
    headers["Cache-Control"] = "private, no-store"

    async def body():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=response.status_code, headers=headers)


router = APIRouter()


@router.get("/status")
async def status() -> dict[str, Any]:
    settings = _settings_module().load_settings()

    return {"configured": settings is not None, "host": settings.host if settings else None}


@router.get("/documents/{document_id}")
async def document(document_id: int) -> dict[str, Any]:
    found = await _json(f"/api/documents/{document_id}/")

    return {key: found.get(key) for key in ("id", "title", "created", "original_file_name", "mime_type", "page_count")}


@router.get("/documents/{document_id}/preview")
async def preview(document_id: int, request: Request) -> StreamingResponse:
    return await _stream(request, f"/api/documents/{document_id}/preview/")


@router.get("/documents/{document_id}/thumb")
async def thumb(document_id: int, request: Request) -> StreamingResponse:
    return await _stream(request, f"/api/documents/{document_id}/thumb/")


@router.get("/documents/{document_id}/original")
async def original(document_id: int, request: Request) -> StreamingResponse:
    return await _stream(request, f"/api/documents/{document_id}/download/", {"original": "true"})


@router.get("/documents/{document_id}/preview-data")
async def preview_data(document_id: int) -> JSONResponse:
    settings = _settings()
    chunks: list[bytes] = []
    size = 0

    async with _client() as client:
        try:
            async with client.stream("GET", f"{settings.url}/api/documents/{document_id}/preview/", headers=_headers(settings.token)) as response:
                if response.status_code >= 400:
                    raise _failure(response.status_code)

                if int(response.headers.get("content-length") or 0) > DATA_URL_MAX_BYTES:
                    raise _too_big()

                async for chunk in response.aiter_bytes():
                    size += len(chunk)

                    if size > DATA_URL_MAX_BYTES:
                        raise _too_big()

                    chunks.append(chunk)

                mime = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip() or "application/octet-stream"
        except httpx.HTTPError as error:
            raise HTTPException(status_code=502, detail=f"Paperless did not answer ({type(error).__name__}).") from None

    data_url = f"data:{mime};base64,{base64.b64encode(b''.join(chunks)).decode('ascii')}"

    return JSONResponse({"mime_type": mime, "size": size, "data_url": data_url}, headers={"Cache-Control": "private, no-store"})


def _too_big() -> HTTPException:
    return HTTPException(status_code=413, detail=f"This document is over {DATA_URL_MAX_BYTES // (1024 * 1024)} MB; open the original instead.")


def _snippet(item: dict[str, Any], limit: int = 200) -> str:
    """Where the words matched (Paperless's highlights, without their markup), else the start of the text."""
    hit = item.get("__search_hit__") or {}
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(hit.get("highlights") or item.get("content") or ""))).strip()

    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


async def _listing(params: dict[str, Any], limit: int) -> dict[str, Any]:
    found = await _json("/api/documents/", {**params, "page_size": limit, "truncate_content": "true"})
    items = found.get("results", [])[:limit]
    ids = sorted({item["correspondent"] for item in items if isinstance(item.get("correspondent"), int)})
    senders: dict[int, str] = {}

    if ids:
        try:  # best effort: a sender's name only makes a row read better
            listed = await _json("/api/correspondents/", {"id__in": ",".join(map(str, ids)), "page_size": len(ids)})
            senders = {entry["id"]: entry.get("name") or "" for entry in listed.get("results", []) if isinstance(entry.get("id"), int)}
        except HTTPException:
            pass

    return {
        "count": found.get("count", len(items)),
        "results": [
            {"id": item.get("id"), "title": item.get("title"), "created": item.get("created"), "correspondent": senders.get(item.get("correspondent")) or None, "snippet": _snippet(item)}
            for item in items
        ],
    }


@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    return await _listing({"query": q}, limit)


@router.get("/recent")
async def recent(limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    return await _listing({"ordering": "-created"}, limit)
