from __future__ import annotations

import hmac
import logging
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Channel, Settings, load_channels, load_settings
from .cache_store import CacheStoreError, store_stream_cache
from .fourgtv import FourGTVError, cache_from_client_responses, refresh_plan
from .hls import (
    MediaTokenStore,
    UnsafeUpstreamURL,
    is_hls_manifest,
    iso_datetime,
    rewrite_hls_manifest,
    validate_upstream_url,
)
from .models import ResolvedStream
from .resolver import ResolveError, YouTubeResolver

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
PASSTHROUGH_REQUEST_HEADERS = {"range", "if-none-match", "if-modified-since"}
PASSTHROUGH_RESPONSE_HEADERS = {
    "accept-ranges",
    "content-length",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
}


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _base_url(request: Request, settings: Settings) -> str:
    if settings.public_base_url:
        return settings.public_base_url
    return str(request.base_url).rstrip("/")


def _append_key(url: str, key: str) -> str:
    if not key:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}key={urllib.parse.quote(key, safe='')}"


def _provided_key(request: Request) -> str:
    query_key = request.query_params.get("key", "")
    if query_key:
        return query_key
    header_key = request.headers.get("x-access-key", "")
    if header_key:
        return header_key
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _require_access(request: Request, settings: Settings) -> str:
    if not settings.access_required:
        return ""
    provided = _provided_key(request)
    if not provided or not hmac.compare_digest(provided, settings.access_key):
        raise HTTPException(status_code=401, detail="播放權杖錯誤")
    return provided


def _safe_channel_id(channel_id: str, resolver: YouTubeResolver) -> Channel:
    try:
        return resolver.channel(channel_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="找不到頻道") from exc


def _proxy_media_url(base_url: str, access_key: str, channel_id: str, token: str) -> str:
    path = f"{base_url}/media/{urllib.parse.quote(channel_id, safe='')}/{urllib.parse.quote(token, safe='')}"
    return _append_key(path, access_key)


def _playlist_url(request: Request, settings: Settings, access_key: str) -> str:
    return _append_key(f"{_base_url(request, settings)}/live.m3u", access_key)


def _m3u(channels: tuple[Channel, ...], request: Request, settings: Settings, access_key: str) -> str:
    base = _base_url(request, settings)
    lines = [
        "#EXTM3U",
        "# Taiwan News M3U — official streams are resolved on demand by this relay.",
    ]
    for channel in channels:
        name = channel.name.replace("\n", " ").replace(",", "，")
        group = channel.group.replace('"', "'")
        channel_id = channel.id.replace('"', "'")
        stream_url = _append_key(f"{base}/hls/{channel.id}/master.m3u8", access_key)
        lines.append(
            f'#EXTINF:-1 tvg-id="{channel_id}" group-title="{group}",{name}'
        )
        lines.append(stream_url)
    return "\n".join(lines) + "\n"


def _status_payload(channels: tuple[Channel, ...], resolver: YouTubeResolver) -> list[dict[str, Any]]:
    statuses = resolver.status_snapshot()
    output: list[dict[str, Any]] = []
    for channel in channels:
        status = statuses[channel.id]
        output.append(
            {
                "id": channel.id,
                "name": channel.name,
                "short_name": channel.short_name,
                "group": channel.group,
                "state": status.state,
                "title": status.title,
                "height": status.height,
                "webpage_url": status.webpage_url or channel.sources[0],
                "resolved_at": iso_datetime(status.resolved_at),
                "expires_at": iso_datetime(status.expires_at),
                "attempted_at": iso_datetime(status.attempted_at),
                "error": status.error,
            }
        )
    return output


def _upstream_headers(base: Mapping[str, str], request: Request) -> dict[str, str]:
    blocked = {
        "authorization",
        "connection",
        "content-length",
        "cookie",
        "host",
        "proxy-authorization",
        "transfer-encoding",
    }
    headers = {
        str(key): str(value)
        for key, value in base.items()
        if str(key).lower() not in blocked
    }
    for key, value in request.headers.items():
        if key.lower() in PASSTHROUGH_REQUEST_HEADERS:
            headers[key] = value
    headers.setdefault("Accept", "*/*")
    return headers


def _response_headers(response: httpx.Response, *, manifest: bool) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in response.headers.items():
        if key.lower() in PASSTHROUGH_RESPONSE_HEADERS:
            headers[key] = value
    if manifest:
        for key in list(headers):
            if key.lower() in {"content-length", "content-type"}:
                headers.pop(key, None)
        headers["Content-Type"] = "application/vnd.apple.mpegurl; charset=utf-8"
        headers["Cache-Control"] = "no-store, max-age=0"
    else:
        headers["Cache-Control"] = "private, max-age=20"
    return headers


async def _close_upstream(response: httpx.Response) -> None:
    await response.aclose()


async def _stream_body(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_raw():
            if chunk:
                yield chunk
    finally:
        await _close_upstream(response)


async def _fetch_upstream(
    client: httpx.AsyncClient,
    request: Request,
    url: str,
    headers: Mapping[str, str],
) -> httpx.Response:
    current_url = validate_upstream_url(url)
    request_headers = _upstream_headers(headers, request)

    for _ in range(6):
        upstream_request = client.build_request(
            "GET",
            current_url,
            headers=request_headers,
        )
        response = await client.send(upstream_request, stream=True, follow_redirects=False)
        try:
            validate_upstream_url(str(response.url))
        except UnsafeUpstreamURL:
            await response.aclose()
            raise

        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        next_url = urllib.parse.urljoin(str(response.url), location)
        try:
            current_url = validate_upstream_url(next_url)
        except UnsafeUpstreamURL:
            await response.aclose()
            raise
        await response.aclose()

    raise httpx.TooManyRedirects(
        "上游重新導向次數過多", request=upstream_request
    )


async def _read_limited(response: httpx.Response, limit: int = 4 * 1024 * 1024) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=502, detail="上游播放清單過大")
        chunks.append(chunk)
    return b"".join(chunks)


async def _proxy_response(
    *,
    app: FastAPI,
    request: Request,
    channel_id: str,
    url: str,
    headers: Mapping[str, str],
    access_key: str,
) -> Response:
    settings: Settings = app.state.settings
    token_store: MediaTokenStore = app.state.token_store
    client: httpx.AsyncClient = app.state.http_client
    base = _base_url(request, settings)

    try:
        upstream = await _fetch_upstream(client, request, url, headers)
    except (httpx.HTTPError, UnsafeUpstreamURL) as exc:
        raise HTTPException(status_code=502, detail=f"上游連線失敗：{exc}") from exc

    content_type = upstream.headers.get("content-type")
    if upstream.status_code >= 400:
        try:
            error_body = await _read_limited(upstream, limit=64 * 1024)
        finally:
            await upstream.aclose()
        return Response(
            content=b"" if request.method == "HEAD" else error_body[:2000],
            status_code=upstream.status_code,
            media_type="text/plain",
            headers={"Cache-Control": "no-store"},
        )

    likely_manifest = is_hls_manifest(content_type, str(upstream.url))

    if likely_manifest:
        try:
            body = await _read_limited(upstream)
        finally:
            await upstream.aclose()
        if upstream.status_code >= 400:
            return Response(
                content=body[:1000],
                status_code=upstream.status_code,
                media_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )
        rewritten = rewrite_hls_manifest(
            body,
            manifest_url=str(upstream.url),
            channel_id=channel_id,
            headers=headers,
            token_store=token_store,
            proxy_url=lambda cid, token: _proxy_media_url(
                base, access_key, cid, token
            ),
            max_height=settings.max_height,
        )
        if request.method == "HEAD":
            rewritten = b""
        return Response(
            content=rewritten,
            status_code=upstream.status_code,
            headers=_response_headers(upstream, manifest=True),
        )

    response_headers = _response_headers(upstream, manifest=False)
    if request.method == "HEAD":
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=response_headers)
    return StreamingResponse(
        _stream_body(upstream),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=content_type,
    )


def create_app(
    *,
    settings: Settings | None = None,
    channels: tuple[Channel, ...] | None = None,
    resolver: YouTubeResolver | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    channels = channels or load_channels(settings.channels_path)
    resolver = resolver or YouTubeResolver(settings, channels)
    owns_client = http_client is None

    _configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.channels = channels
        app.state.resolver = resolver
        app.state.token_store = MediaTokenStore(
            ttl_seconds=settings.media_token_ttl_seconds,
            max_entries=settings.max_token_entries,
        )
        app.state.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout_seconds, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
            http2=True,
        )
        yield
        if owns_client:
            await app.state.http_client.aclose()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.svg")
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "channels": len(channels),
            "access_required": settings.access_required,
        }

    @app.get("/api/config")
    async def api_config(request: Request) -> dict[str, Any]:
        return {
            "app_name": settings.app_name,
            "access_required": settings.access_required,
            "public_base_url": _base_url(request, settings),
            "max_height": settings.max_height,
            "channel_count": len(channels),
        }

    @app.get("/api/status")
    async def api_status(request: Request) -> dict[str, Any]:
        _require_access(request, settings)
        items = _status_payload(channels, resolver)
        counts = {
            state: sum(item["state"] == state for item in items)
            for state in ("idle", "resolving", "online", "error")
        }
        return {"summary": counts, "channels": items}

    @app.post("/api/channels/{channel_id}/probe")
    async def probe_channel(channel_id: str, request: Request) -> JSONResponse:
        _require_access(request, settings)
        _safe_channel_id(channel_id, resolver)
        try:
            stream = await resolver.resolve(channel_id, force=True)
        except ResolveError as exc:
            return JSONResponse(
                status_code=502,
                content={"ok": False, "channel_id": channel_id, "error": str(exc)},
            )
        return JSONResponse(
            {
                "ok": True,
                "channel_id": channel_id,
                "title": stream.title,
                "height": stream.height,
                "resolved_at": iso_datetime(stream.resolved_at),
                "expires_at": iso_datetime(stream.expires_at),
            }
        )

    @app.get("/api/fourgtv/refresh-plan")
    async def fourgtv_refresh_plan(request: Request) -> dict[str, object]:
        _require_access(request, settings)
        return refresh_plan(channels)

    @app.post("/api/fourgtv/refresh")
    async def fourgtv_refresh(request: Request) -> JSONResponse:
        _require_access(request, settings)
        try:
            payload = await request.json()
            cache = cache_from_client_responses(channels, payload)
            version = await store_stream_cache(cache)
        except (FourGTVError, CacheStoreError, ValueError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        for channel in channels:
            resolver.invalidate(channel.id)
        cached_channels = cache.get("channels")
        count = len(cached_channels) if isinstance(cached_channels, dict) else 0
        return JSONResponse({"ok": True, "channels": count, "version": version})

    @app.api_route("/live.m3u", methods=["GET", "HEAD"])
    @app.api_route("/live.txt", methods=["GET", "HEAD"])
    async def live_playlist(request: Request) -> Response:
        key = _require_access(request, settings)
        body = _m3u(channels, request, settings, key)
        content = "" if request.method == "HEAD" else body
        return Response(
            content=content,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Content-Disposition": 'inline; filename="taiwan-news.m3u"',
            },
        )

    @app.get("/import-to-tubo")
    async def import_to_tubo(request: Request) -> RedirectResponse:
        key = _require_access(request, settings)
        playlist = _playlist_url(request, settings, key)
        target = "tubo://import?" + urllib.parse.urlencode(
            {"url": playlist, "name": settings.app_name}
        )
        return RedirectResponse(target, status_code=302)

    @app.api_route("/hls/{channel_id}/master.m3u8", methods=["GET", "HEAD"])
    async def channel_master(channel_id: str, request: Request) -> Response:
        key = _require_access(request, settings)
        _safe_channel_id(channel_id, resolver)
        try:
            stream: ResolvedStream = await resolver.resolve(channel_id)
        except ResolveError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"{channel_id} 目前無法解析：{exc}",
            ) from exc

        response = await _proxy_response(
            app=app,
            request=request,
            channel_id=channel_id,
            url=stream.stream_url,
            headers=stream.headers,
            access_key=key,
        )
        if response.status_code not in {403, 404, 410}:
            return response

        # A cached upstream URL may have expired between manifest reloads.
        resolver.invalidate(channel_id)
        try:
            fresh = await resolver.resolve(channel_id, force=True)
        except ResolveError:
            return response
        return await _proxy_response(
            app=app,
            request=request,
            channel_id=channel_id,
            url=fresh.stream_url,
            headers=fresh.headers,
            access_key=key,
        )

    @app.api_route("/media/{channel_id}/{token}", methods=["GET", "HEAD"])
    async def proxied_media(channel_id: str, token: str, request: Request) -> Response:
        key = _require_access(request, settings)
        _safe_channel_id(channel_id, resolver)
        item = app.state.token_store.get(channel_id, token)
        if item is None:
            raise HTTPException(status_code=410, detail="媒體網址已過期，請重新載入頻道")
        return await _proxy_response(
            app=app,
            request=request,
            channel_id=channel_id,
            url=item.url,
            headers=item.headers,
            access_key=key,
        )

    return app


app = create_app()
