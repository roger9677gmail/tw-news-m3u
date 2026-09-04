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
from .karaoke import (
    DisabledKaraokeStore,
    GCSKaraokeStore,
    KaraokeError,
    KaraokeSong,
    KaraokeStore,
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


def _proxy_media_url(
    base_url: str,
    access_key: str,
    channel_id: str,
    token: str,
    source_url: str,
) -> str:
    parsed = urllib.parse.urlparse(source_url)
    suffix = Path(parsed.path).suffix.lower()
    if parsed.hostname == "4gtv.cnlive.club" and suffix == ".jpeg":
        suffix = ".ts"
    if suffix not in {
        ".m3u8",
        ".m3u",
        ".ts",
        ".m2ts",
        ".m4s",
        ".mp4",
        ".aac",
        ".ac3",
        ".ec3",
        ".vtt",
        ".webvtt",
        ".key",
    }:
        suffix = ".bin"
    media_name = urllib.parse.quote(token, safe="") + suffix
    path = (
        f"{base_url}/media/{urllib.parse.quote(channel_id, safe='')}/{media_name}"
    )
    return _append_key(path, access_key)


def _playlist_url(request: Request, settings: Settings, access_key: str) -> str:
    return _append_key(f"{_base_url(request, settings)}/live.m3u", access_key)


def _m3u(
    channels: tuple[Channel, ...],
    request: Request,
    settings: Settings,
    access_key: str,
    karaoke_songs: list[KaraokeSong] | None = None,
) -> str:
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
    for song in karaoke_songs or []:
        name = song.title.replace("\n", " ").replace(",", "，")
        stream_url = _append_key(
            f"{base}/karaoke/{urllib.parse.quote(song.id, safe='')}/index.m3u8",
            access_key,
        )
        lines.append(
            f'#EXTINF:-1 tvg-id="ktv-{song.id}" group-title="KTV 點歌",{name}'
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
                "source": status.source,
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
        "accept-encoding",
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
    # Compressed MPEG-TS is legal HTTP but several small IPTV origins apply
    # Brotli to full segment responses only. Request identity so the relay
    # never forwards compressed media bytes without a matching encoding.
    headers["Accept-Encoding"] = "identity"
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
        if response.headers.get("content-encoding"):
            headers.pop("content-length", None)
        headers["Cache-Control"] = "private, max-age=20"
    return headers


def _media_content_type(response: httpx.Response) -> str | None:
    content_type = response.headers.get("content-type")
    parsed = urllib.parse.urlparse(str(response.url))
    # Some IPTV origins let the web server identify `.ts` as a Qt translation
    # file (`text/vnd.trolltech.linguist`). HLS clients need the actual MPEG-TS
    # media type regardless of that incorrect upstream header.
    if parsed.path.lower().endswith((".ts", ".m2ts")):
        return "video/mp2t"
    # This experimental 4GTV-compatible relay publishes MPEG-TS segments with
    # a .jpeg suffix and text/html header. Correct it before forwarding so HLS
    # players do not reject valid transport-stream bytes as a web page.
    if (
        parsed.hostname == "4gtv.cnlive.club"
        and parsed.path.lower().endswith(".jpeg")
    ):
        return "video/mp2t"
    return content_type


async def _close_upstream(response: httpx.Response) -> None:
    await response.aclose()


async def _stream_body(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        # `aiter_bytes` transparently decodes any compression an upstream
        # applies despite Accept-Encoding: identity.
        async for chunk in response.aiter_bytes():
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
            proxy_url=lambda cid, token, source_url: _proxy_media_url(
                base, access_key, cid, token, source_url
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

    content_type = _media_content_type(upstream)
    response_headers = _response_headers(upstream, manifest=False)
    if content_type:
        response_headers["content-type"] = content_type
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
    karaoke_store: KaraokeStore | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    channels = channels or load_channels(settings.channels_path)
    resolver = resolver or YouTubeResolver(
        settings, channels, http_client=http_client
    )
    owns_client = http_client is None
    karaoke_backend: KaraokeStore
    if karaoke_store is not None:
        karaoke_backend = karaoke_store
    elif settings.karaoke_enabled:
        karaoke_backend = GCSKaraokeStore(
            bucket_name=settings.karaoke_bucket,
            project_id=settings.gcp_project_id,
            prefix=settings.karaoke_prefix,
            max_upload_bytes=settings.karaoke_max_upload_bytes,
            ffmpeg_timeout_seconds=settings.karaoke_ffmpeg_timeout_seconds,
        )
    else:
        karaoke_backend = DisabledKaraokeStore()

    _configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.channels = channels
        app.state.resolver = resolver
        app.state.karaoke_store = karaoke_backend
        app.state.token_store = MediaTokenStore(
            ttl_seconds=settings.media_token_ttl_seconds,
            max_entries=settings.max_token_entries,
        )
        app.state.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout_seconds, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
            http2=True,
        )
        if isinstance(resolver, YouTubeResolver):
            resolver.set_http_client(app.state.http_client)
        yield
        if owns_client:
            await app.state.http_client.aclose()
            if isinstance(resolver, YouTubeResolver):
                resolver.set_http_client(None)

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
        song_count = 0
        if karaoke_backend.enabled:
            try:
                song_count = len(await karaoke_backend.list_songs())
            except Exception:
                LOGGER.exception("Unable to read karaoke catalog for config")
        return {
            "app_name": settings.app_name,
            "access_required": settings.access_required,
            "public_base_url": _base_url(request, settings),
            "max_height": settings.max_height,
            "channel_count": len(channels),
            "karaoke_enabled": karaoke_backend.enabled,
            "karaoke_song_count": song_count,
            "karaoke_max_upload_bytes": settings.karaoke_max_upload_bytes,
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
                "source": stream.source,
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

    @app.get("/api/karaoke/songs")
    async def karaoke_songs(request: Request) -> dict[str, Any]:
        _require_access(request, settings)
        if not karaoke_backend.enabled:
            return {"enabled": False, "songs": []}
        try:
            songs = await karaoke_backend.list_songs()
        except KaraokeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"enabled": True, "songs": [song.as_dict() for song in songs]}

    @app.post("/api/karaoke/uploads")
    async def create_karaoke_upload(request: Request) -> JSONResponse:
        _require_access(request, settings)
        if not karaoke_backend.enabled:
            raise HTTPException(status_code=503, detail="尚未啟用卡拉 OK 儲存空間")
        try:
            payload = await request.json()
            if payload.get("rights_confirmed") is not True:
                raise KaraokeError("請先確認你擁有影片的使用與上傳權利")
            result = await karaoke_backend.create_upload(
                file_name=str(payload.get("file_name") or ""),
                size_bytes=int(payload.get("size_bytes") or 0),
                origin=_base_url(request, settings),
            )
        except (KaraokeError, TypeError, ValueError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except Exception:
            LOGGER.exception("Unable to create karaoke upload session")
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": "無法建立雲端上傳連結，請稍後重試"},
            )
        return JSONResponse({"ok": True, **result})

    @app.post("/api/karaoke/uploads/{upload_id}/complete")
    async def complete_karaoke_upload(upload_id: str, request: Request) -> JSONResponse:
        _require_access(request, settings)
        if not karaoke_backend.enabled:
            raise HTTPException(status_code=503, detail="尚未啟用卡拉 OK 儲存空間")
        try:
            payload = await request.json()
            song = await karaoke_backend.complete_upload(
                upload_id=upload_id,
                title=str(payload.get("title") or ""),
                file_name=str(payload.get("file_name") or ""),
            )
        except (KaraokeError, TypeError, ValueError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except Exception:
            LOGGER.exception("Unable to complete karaoke upload %s", upload_id)
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": "雲端轉檔失敗，請稍後重試"},
            )
        return JSONResponse({"ok": True, "song": song.as_dict()})

    @app.delete("/api/karaoke/songs/{song_id}")
    async def delete_karaoke_song(song_id: str, request: Request) -> JSONResponse:
        _require_access(request, settings)
        if not karaoke_backend.enabled:
            raise HTTPException(status_code=503, detail="尚未啟用卡拉 OK 儲存空間")
        try:
            deleted = await karaoke_backend.delete_song(song_id)
        except KaraokeError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        except Exception:
            LOGGER.exception("Unable to delete karaoke song %s", song_id)
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": "雲端刪除失敗，請稍後重試"},
            )
        if not deleted:
            raise HTTPException(status_code=404, detail="找不到歌曲")
        return JSONResponse({"ok": True, "deleted": song_id})

    @app.api_route("/live.m3u", methods=["GET", "HEAD"])
    @app.api_route("/live.txt", methods=["GET", "HEAD"])
    async def live_playlist(request: Request) -> Response:
        key = _require_access(request, settings)
        songs: list[KaraokeSong] = []
        if karaoke_backend.enabled:
            try:
                songs = await karaoke_backend.list_songs()
            except Exception:
                LOGGER.exception("Unable to include karaoke catalog in playlist")
        body = _m3u(channels, request, settings, key, songs)
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

        first_error: HTTPException | None = None
        try:
            response = await _proxy_response(
                app=app,
                request=request,
                channel_id=channel_id,
                url=stream.stream_url,
                headers=stream.headers,
                access_key=key,
            )
        except HTTPException as exc:
            if exc.status_code != 502:
                raise
            first_error = exc
            response = None
        else:
            if response.status_code < 400:
                return response

        # Cached official URLs and experimental fallbacks can disappear at any
        # time. Re-resolve on every upstream error, including DNS/connection
        # failures and 5xx responses, instead of leaving the player spinning.
        resolver.invalidate(channel_id)
        try:
            fresh = await resolver.resolve(channel_id, force=True)
        except ResolveError:
            if response is None and first_error is not None:
                raise first_error
            assert response is not None
            return response
        return await _proxy_response(
            app=app,
            request=request,
            channel_id=channel_id,
            url=fresh.stream_url,
            headers=fresh.headers,
            access_key=key,
        )

    @app.api_route(
        "/karaoke/{song_id}/index.m3u8", methods=["GET", "HEAD"]
    )
    async def karaoke_manifest(song_id: str, request: Request) -> Response:
        key = _require_access(request, settings)
        if not karaoke_backend.enabled:
            raise HTTPException(status_code=404, detail="卡拉 OK 功能未啟用")
        try:
            body, _ = await karaoke_backend.read_asset(song_id, "index.m3u8")
            manifest = body.decode("utf-8")
        except (KaraokeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        base = _base_url(request, settings)
        rewritten: list[str] = []
        for line in manifest.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                asset = urllib.parse.quote(Path(stripped).name, safe="")
                stripped = _append_key(f"{base}/karaoke/{song_id}/{asset}", key)
            rewritten.append(stripped if stripped else line)
        content = "" if request.method == "HEAD" else "\n".join(rewritten) + "\n"
        return Response(
            content=content,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "private, max-age=60"},
        )

    @app.api_route("/karaoke/{song_id}/{asset_name}", methods=["GET", "HEAD"])
    async def karaoke_asset(song_id: str, asset_name: str, request: Request) -> Response:
        _require_access(request, settings)
        if not karaoke_backend.enabled:
            raise HTTPException(status_code=404, detail="卡拉 OK 功能未啟用")
        try:
            body, content_type = await karaoke_backend.read_asset(song_id, asset_name)
        except KaraokeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=b"" if request.method == "HEAD" else body,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.api_route("/media/{channel_id}/{token_with_suffix}", methods=["GET", "HEAD"])
    async def proxied_media(
        channel_id: str, token_with_suffix: str, request: Request
    ) -> Response:
        key = _require_access(request, settings)
        _safe_channel_id(channel_id, resolver)
        # URL-safe token values never contain a dot. The suffix lets strict
        # HLS/FFmpeg clients identify the resource before reading its headers.
        token = token_with_suffix.split(".", 1)[0]
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
