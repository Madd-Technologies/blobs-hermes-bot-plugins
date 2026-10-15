"""The phone app's way to Google Drive: files a bot found, streamed through this server to the app's viewer.

Mounted by Hermes under `/api/plugins/hermes-drive/`, behind the dashboard's own sign-in. Nothing is stored: each request asks
Drive with the server's Google sign-in (see `drive.py`; the token never leaves this machine) and streams the answer back. A
`Range` header is passed on for real files, so the app's PDF viewer reads a part at a time. Google's own files (Docs, Sheets,
Slides, Drawings) are not files: they are exported, as PDF to look at.

  GET /status                {configured, drive, account}: from the token file, without asking Google
  GET /files/{id}            {id, name, mime_type, kind, modified, owner, size, web_link}
  GET /files/{id}/preview    the file to look at: itself, or a Google file as PDF
  GET /files/{id}/original   the file itself, or a Google file as Office (Docs .docx, Sheets .xlsx, Slides .pptx)
  GET /search?q=&limit=      {results: [{id, name, mime_type, kind, modified, owner, size}]}, best match first
  GET /recent?limit=         the same, last modified first (no folders)

Only read-only Drive calls are made, although the token may also carry Gmail or Calendar scopes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

_MODULE = "_hermes_drive_core"

#: Response headers worth passing back: what a viewer needs to read the body, a part at a time.
PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")

#: Tests put an httpx transport here (a fake Drive).
transport: httpx.AsyncBaseTransport | None = None


def _core():
    """`drive.py`, loaded once by path: Hermes imports this file apart from the plugin package."""
    if _MODULE not in sys.modules:
        spec = importlib.util.spec_from_file_location(_MODULE, Path(__file__).resolve().parent.parent / "drive.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE] = module
        spec.loader.exec_module(module)

    return sys.modules[_MODULE]


#: The access token, refreshed in memory (tests replace it).
cache = _core().TokenCache()


async def _token(force: bool = False) -> str:
    try:
        return await asyncio.to_thread(cache.get, force)
    except _core().Failure as failure:
        raise HTTPException(status_code=failure.status, detail=str(failure)) from None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True, transport=transport)


def _file_id(value: str) -> str:
    if not _core().FILE_ID.match(value):
        raise HTTPException(status_code=400, detail="That is not a Drive file id.")

    return value


def _failure(response: httpx.Response, body: bytes = b"") -> HTTPException:
    reason = ""

    try:
        error = httpx.Response(200, content=body or response.content).json().get("error", {})
        reason = (error.get("errors") or [{}])[0].get("reason", "") or error.get("status", "")
    except Exception:
        pass

    if reason == "exportSizeLimitExceeded":
        return HTTPException(status_code=413, detail="This Google file is too big for Drive to turn into a PDF (about 10 MB). Open it in Drive instead.")

    if response.status_code == 404:
        return HTTPException(status_code=404, detail="Drive has no such file, or this server's Google account can't see it.")

    if response.status_code in (401, 403):
        return HTTPException(status_code=502, detail=f"Google refused this request ({reason or response.status_code}).")

    return HTTPException(status_code=502, detail=f"Drive answered {response.status_code}.")


async def _send(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None, headers: dict[str, str] | None = None, stream: bool = False) -> httpx.Response:
    """One Drive call; if Google says the access token is no good, it is refreshed once and the call made again."""
    for attempt in (0, 1):
        token = await _token(force=attempt == 1)
        request = client.build_request("GET", f"{_core().drive_api()}{path}", params=params, headers={"Authorization": f"Bearer {token}", **(headers or {})})

        try:
            response = await client.send(request, stream=stream)
        except httpx.HTTPError as error:
            raise HTTPException(status_code=502, detail=f"Drive did not answer ({type(error).__name__}).") from None

        if response.status_code == 401 and attempt == 0:
            await response.aclose()
            cache.invalidate()
            continue

        return response

    raise AssertionError("unreachable")


async def _json(path: str, params: dict[str, Any] | None = None) -> Any:
    async with _client() as client:
        response = await _send(client, path, params)

    if response.status_code >= 400:
        raise _failure(response)

    return response.json()


async def _stream(request: Request, path: str, params: dict[str, Any], ranged: bool) -> StreamingResponse:
    """Drive's answer to `path`, streamed back as it arrives, with a `Range` passed on when the file has ranges."""
    extra = {"Range": request.headers["range"]} if ranged and "range" in request.headers else {}
    client = _client()
    response = await _send(client, path, params, extra, stream=True)

    if response.status_code >= 400:
        body = await response.aread()
        await response.aclose()
        await client.aclose()
        raise _failure(response, body)

    headers = {name: value for name, value in response.headers.items() if name.lower() in PASS_HEADERS}

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


async def _meta(file_id: str) -> dict[str, Any]:
    return await _json(f"/files/{file_id}", {"fields": _core().FILE_FIELDS, "supportsAllDrives": "true"})


async def _content(request: Request, file_id: str, export_as: int) -> StreamingResponse:
    """The file itself, or a Google file exported (`export_as` 0: PDF to look at, 1: Office for the original)."""
    core = _core()
    mime = str((await _meta(file_id)).get("mimeType") or "")

    if mime in core.GOOGLE_EXPORTS:
        return await _stream(request, f"/files/{file_id}/export", {"mimeType": core.GOOGLE_EXPORTS[mime][export_as]}, ranged=False)

    if mime.startswith("application/vnd.google-apps."):
        raise HTTPException(status_code=415, detail=f"Drive can't give a {core.kind_of(mime).lower()} as a file; open it in Drive.")

    return await _stream(request, f"/files/{file_id}", {"alt": "media", "supportsAllDrives": "true"}, ranged=True)


router = APIRouter()


@router.get("/status")
async def status() -> dict[str, Any]:
    credentials = _core().load_credentials()

    return {"configured": credentials is not None, "drive": bool(credentials and credentials.drive), "account": credentials.account if credentials else None}


@router.get("/files/{file_id}")
async def file(file_id: str) -> dict[str, Any]:
    item = await _meta(_file_id(file_id))

    return {**_core().summary(item), "web_link": item.get("webViewLink")}


@router.get("/files/{file_id}/preview")
async def preview(file_id: str, request: Request) -> StreamingResponse:
    return await _content(request, _file_id(file_id), 0)


@router.get("/files/{file_id}/original")
async def original(file_id: str, request: Request) -> StreamingResponse:
    return await _content(request, _file_id(file_id), 1)


async def _listing(params: dict[str, Any], limit: int) -> dict[str, Any]:
    found = await _json("/files", {**params, "pageSize": limit, "fields": _core().LIST_FIELDS, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})

    return {"results": [_core().summary(item) for item in found.get("files", [])[:limit]]}


@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    return await _listing({"q": _core().search_query(q)}, limit)


@router.get("/recent")
async def recent(limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    return await _listing({"q": _core().RECENT_QUERY, "orderBy": "modifiedTime desc"}, limit)
