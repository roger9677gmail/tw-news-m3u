from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

LOGGER = logging.getLogger("youtube_m3u")
ROOT = Path(__file__).resolve().parent
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
SEGMENT_RE = re.compile(r"^seg-[0-9]{6}\.ts$")
HLS_URI_RE = re.compile(r'URI="([^"]+)"')
MAX_HLS_ITEMS_PER_REQUEST = 50
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
SAFE_UPSTREAM_RESPONSE_HEADERS = {
    "accept-ranges",
    "cache-control",
    "content-length",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
}


@dataclass(frozen=True, slots=True)
class Settings:
    access_key: str
    public_base_url: str
    data_dir: Path
    max_height: int = 720
    resolver_timeout_seconds: int = 50
    startup_timeout_seconds: int = 35
    idle_timeout_seconds: int = 150
    hls_timeout_seconds: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        access_key = os.getenv("ACCESS_KEY", "").strip()
        if len(access_key) < 20:
            raise RuntimeError("ACCESS_KEY 至少需要 20 個字元")
        public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if not public_base_url.startswith(("https://", "http://")):
            raise RuntimeError("PUBLIC_BASE_URL 必須是完整的 HTTP/HTTPS 網址")
        return cls(
            access_key=access_key,
            public_base_url=public_base_url,
            data_dir=Path(os.getenv("DATA_DIR", "/var/lib/youtube-m3u")).expanduser(),
            max_height=max(144, min(int(os.getenv("MAX_HEIGHT", "720")), 720)),
            resolver_timeout_seconds=max(
                20, min(int(os.getenv("RESOLVER_TIMEOUT_SECONDS", "50")), 120)
            ),
            startup_timeout_seconds=max(
                10, min(int(os.getenv("STARTUP_TIMEOUT_SECONDS", "35")), 90)
            ),
            idle_timeout_seconds=max(
                60, min(int(os.getenv("IDLE_TIMEOUT_SECONDS", "150")), 900)
            ),
            hls_timeout_seconds=max(
                5, min(int(os.getenv("HLS_TIMEOUT_SECONDS", "20")), 60)
            ),
        )


def utcnow() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_youtube_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("YouTube 網址不可空白")
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in YOUTUBE_HOSTS:
        raise ValueError("只接受 youtube.com 或 youtu.be 的 HTTPS 網址")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("YouTube 網址格式不正確")
    return value


def validate_hls_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("M3U8 網址不可空白")
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        raise ValueError("只接受公開的 HTTPS M3U8 網址")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("M3U8 網址格式不正確")
    return value


def validate_optional_header(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if "\r" in text or "\n" in text:
        raise ValueError(f"{label} 格式不正確")
    if len(text) > 500:
        raise ValueError(f"{label} 過長")
    return text


async def ensure_public_url(value: str) -> str:
    value = validate_hls_url(value)
    host = urllib.parse.urlsplit(value).hostname or ""
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ValueError("來源網域無法解析") from exc
    if not records:
        raise ValueError("來源網域無法解析")
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global:
            raise ValueError("來源必須是公開網際網路位址")
    return value


class Catalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def _write(self, items: list[dict[str, Any]]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._read()]

    def get(self, item_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list() if item.get("id") == item_id), None)

    def add(self, item: dict[str, Any]) -> None:
        with self._lock:
            items = self._read()
            prior = next((x for x in items if x.get("id") == item["id"]), None)
            if prior:
                item["added_at"] = prior.get("added_at") or item["added_at"]
            items = [x for x in items if x.get("id") != item["id"]]
            items.append(item)
            self._write(items)

    def remove(self, item_id: str) -> bool:
        with self._lock:
            items = self._read()
            remaining = [item for item in items if item.get("id") != item_id]
            if len(remaining) == len(items):
                return False
            self._write(remaining)
            return True


class ResolverProtocol(Protocol):
    async def inspect(self, url: str) -> dict[str, Any]: ...

    async def media(self, url: str) -> dict[str, Any]: ...


class YouTubeResolver:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _base_command(self, url: str) -> list[str]:
        selector = (
            f"best[height<={self.settings.max_height}][vcodec^=avc][acodec^=mp4a]/"
            f"best[height<={self.settings.max_height}][ext=mp4][acodec!=none]/"
            f"best[height<={self.settings.max_height}][acodec!=none]"
        )
        return [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-config",
            "--no-playlist",
            "--skip-download",
            "--quiet",
            "--no-warnings",
            "--socket-timeout",
            "20",
            "--retries",
            "1",
            "--extractor-retries",
            "1",
            "--remote-components",
            "ejs:github",
            "--js-runtimes",
            "node",
            "--format",
            selector,
            "--dump-single-json",
            url,
        ]

    async def _extract(self, url: str) -> dict[str, Any]:
        url = validate_youtube_url(url)
        process = await asyncio.create_subprocess_exec(
            *self._base_command(url),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.settings.resolver_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise RuntimeError("YouTube 解析逾時") from exc
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            lines = [line.strip() for line in message.splitlines() if line.strip()]
            raise RuntimeError(" | ".join(lines[-5:])[-900:] or "YouTube 解析失敗")
        try:
            info = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("YouTube 沒有回傳可用資料") from exc
        if not isinstance(info, dict):
            raise RuntimeError("YouTube 回傳資料格式不正確")
        return info

    async def inspect(self, url: str) -> dict[str, Any]:
        info = await self._extract(url)
        video_id = str(info.get("id") or "")
        if not ITEM_ID_RE.fullmatch(video_id):
            video_id = hashlib.sha256(url.encode()).hexdigest()[:20]
        title = str(info.get("title") or video_id).replace("\n", " ").strip()[:180]
        return {
            "id": video_id,
            "video_id": video_id,
            "title": title,
            "url": validate_youtube_url(url),
            "is_live": bool(info.get("is_live") or info.get("live_status") == "is_live"),
            "duration": info.get("duration") if isinstance(info.get("duration"), (int, float)) else None,
            "added_at": utcnow(),
        }

    async def media(self, url: str) -> dict[str, Any]:
        info = await self._extract(url)
        media_url = info.get("url")
        if not isinstance(media_url, str) or not media_url.startswith(("https://", "http://")):
            downloads = info.get("requested_downloads")
            if isinstance(downloads, list):
                media_url = next(
                    (
                        item.get("url")
                        for item in downloads
                        if isinstance(item, dict)
                        and isinstance(item.get("url"), str)
                        and item["url"].startswith(("https://", "http://"))
                    ),
                    None,
                )
        if not isinstance(media_url, str):
            raise RuntimeError("YouTube 沒有提供可播放的影片網址")
        headers = info.get("http_headers")
        safe_headers: dict[str, str] = {}
        if isinstance(headers, dict):
            for key, value in headers.items():
                key_text = str(key).strip()
                value_text = str(value).replace("\r", " ").replace("\n", " ").strip()
                if key_text.lower() in {"user-agent", "referer", "origin"} and value_text:
                    safe_headers[key_text] = value_text
        return {
            "url": media_url,
            "headers": safe_headers,
            "is_live": bool(info.get("is_live") or info.get("live_status") == "is_live"),
        }


class HlsGatewayProtocol(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def inspect(
        self, title: str, url: str, headers: dict[str, str]
    ) -> dict[str, Any]: ...

    async def fetch_manifest(
        self, item: dict[str, Any], target_url: str
    ) -> tuple[str, str]: ...

    async def open_asset(
        self, item: dict[str, Any], target_url: str, byte_range: str
    ) -> tuple[httpx.Response, str]: ...


class HlsGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.hls_timeout_seconds),
                follow_redirects=False,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    def _request_headers(
        self, saved_headers: dict[str, Any], byte_range: str = ""
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, video/*, */*",
            "User-Agent": "tw-news-m3u-authorized-hls/1.0",
        }
        for key, value in saved_headers.items():
            if key.lower() not in {"user-agent", "referer", "origin"}:
                continue
            clean = validate_optional_header(value, key)
            if clean:
                headers[key] = clean
        if byte_range:
            headers["Range"] = byte_range
        return headers

    async def _open(
        self,
        url: str,
        headers: dict[str, str],
        *,
        method: str = "GET",
    ) -> tuple[httpx.AsyncClient, httpx.Response, str]:
        if self.client is None:
            await self.start()
        assert self.client is not None
        current = url
        for _ in range(5):
            await ensure_public_url(current)
            request = self.client.build_request(method, current, headers=headers)
            try:
                response = await self.client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise RuntimeError("無法連線到 HLS 來源") from exc
            if response.status_code not in {301, 302, 303, 307, 308}:
                return self.client, response, current
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise RuntimeError("HLS 來源重新導向不完整")
            current = urllib.parse.urljoin(current, location)
        raise RuntimeError("HLS 來源重新導向次數過多")

    async def _manifest(
        self, url: str, headers: dict[str, str]
    ) -> tuple[str, str]:
        _, response, final_url = await self._open(url, headers)
        try:
            if response.status_code >= 400:
                raise RuntimeError(f"HLS 來源回應 HTTP {response.status_code}")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_MANIFEST_BYTES:
                raise RuntimeError("HLS 播放清單過大")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_MANIFEST_BYTES:
                    raise RuntimeError("HLS 播放清單過大")
                chunks.append(chunk)
            body = b"".join(chunks).decode("utf-8-sig", errors="replace")
        finally:
            await response.aclose()
        if not body.lstrip().startswith("#EXTM3U"):
            raise RuntimeError("來源不是有效的 M3U8/HLS 播放清單")
        return body, final_url

    async def inspect(
        self, title: str, url: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        url = validate_hls_url(url)
        title = title.replace("\r", " ").replace("\n", " ").strip()[:180]
        if not title:
            raise ValueError("節目名稱不可空白")
        if len(url) > 5000:
            raise ValueError("M3U8 網址過長")
        clean_headers: dict[str, str] = {}
        for key in ("User-Agent", "Referer", "Origin"):
            value = validate_optional_header(headers.get(key, ""), key)
            if value:
                clean_headers[key] = value
        item_id = "hls-" + hashlib.sha256(url.encode()).hexdigest()[:20]
        request_headers = self._request_headers(clean_headers)
        body, final_url = await self._manifest(url, request_headers)
        live_probe = body
        if "#EXT-X-STREAM-INF" in body:
            variant = next(
                (
                    line.strip()
                    for line in body.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ),
                "",
            )
            if variant:
                try:
                    live_probe, _ = await self._manifest(
                        urllib.parse.urljoin(final_url, variant), request_headers
                    )
                except Exception:
                    LOGGER.info("Unable to inspect HLS child playlist for %s", item_id)
        return {
            "id": item_id,
            "source_type": "hls",
            "title": title,
            "url": url,
            "headers": clean_headers,
            "is_live": not (
                "#EXT-X-ENDLIST" in live_probe
                or "#EXT-X-PLAYLIST-TYPE:VOD" in live_probe.upper()
            ),
            "added_at": utcnow(),
        }

    async def fetch_manifest(
        self, item: dict[str, Any], target_url: str
    ) -> tuple[str, str]:
        saved = item.get("headers") if isinstance(item.get("headers"), dict) else {}
        return await self._manifest(target_url, self._request_headers(saved))

    async def open_asset(
        self, item: dict[str, Any], target_url: str, byte_range: str
    ) -> tuple[httpx.Response, str]:
        saved = item.get("headers") if isinstance(item.get("headers"), dict) else {}
        _, response, final_url = await self._open(
            target_url, self._request_headers(saved, byte_range=byte_range)
        )
        return response, final_url


def _encode_hls_target(item_id: str, target_url: str, access_key: str) -> tuple[str, str]:
    validate_hls_url(target_url)
    encoded = base64.urlsafe_b64encode(target_url.encode()).decode().rstrip("=")
    signature = hmac.new(
        access_key.encode(), f"{item_id}\0{target_url}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    return encoded, signature


def _decode_hls_target(
    item_id: str, encoded: str, signature: str, access_key: str
) -> str:
    if not encoded or len(encoded) > 8000 or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise ValueError("HLS 代理網址格式不正確")
    try:
        padding = "=" * (-len(encoded) % 4)
        target_url = base64.urlsafe_b64decode(encoded + padding).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("HLS 代理網址格式不正確") from exc
    expected = hmac.new(
        access_key.encode(), f"{item_id}\0{target_url}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        raise ValueError("HLS 代理簽章錯誤")
    return validate_hls_url(target_url)


def _hls_relay_url(
    settings: Settings, item_id: str, target_url: str, access_key: str
) -> str:
    encoded, signature = _encode_hls_target(item_id, target_url, settings.access_key)
    base = f"{settings.public_base_url}/hls/{item_id}/asset"
    query = urllib.parse.urlencode({"u": encoded, "sig": signature, "key": access_key})
    return f"{base}?{query}"


def rewrite_hls_manifest(
    body: str,
    source_url: str,
    item_id: str,
    settings: Settings,
    access_key: str,
) -> str:
    def relay(value: str) -> str:
        target = urllib.parse.urljoin(source_url, value.strip())
        return _hls_relay_url(settings, item_id, target, access_key)

    output: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            output.append(relay(stripped))
            continue
        if "URI=\"" in line:
            line = HLS_URI_RE.sub(lambda match: f'URI="{relay(match.group(1))}"', line)
        output.append(line)
    return "\n".join(output) + "\n"


@dataclass(slots=True)
class RunningStream:
    process: asyncio.subprocess.Process
    directory: Path
    started_at: float
    last_access: float
    log_handle: Any


class StreamManagerProtocol(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def ensure(self, item: dict[str, Any]) -> Path: ...

    async def stop(self, item_id: str, *, remove_files: bool = False) -> None: ...

    def status(self, item_id: str) -> str: ...

    def touch(self, item_id: str) -> None: ...


class StreamManager:
    def __init__(self, settings: Settings, resolver: ResolverProtocol) -> None:
        self.settings = settings
        self.resolver = resolver
        self.root = settings.data_dir / "streams"
        self.root.mkdir(parents=True, exist_ok=True)
        self._running: dict[str, RunningStream] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._cleaner: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._cleaner = asyncio.create_task(self._cleanup_loop())

    async def close(self) -> None:
        if self._cleaner:
            self._cleaner.cancel()
            try:
                await self._cleaner
            except asyncio.CancelledError:
                pass
        for item_id in list(self._running):
            await self.stop(item_id)

    def status(self, item_id: str) -> str:
        current = self._running.get(item_id)
        if not current:
            return "idle"
        return "streaming" if current.process.returncode is None else "error"

    def touch(self, item_id: str) -> None:
        current = self._running.get(item_id)
        if current:
            current.last_access = time.monotonic()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            for item_id, current in list(self._running.items()):
                if (
                    current.process.returncode is not None
                    or now - current.last_access > self.settings.idle_timeout_seconds
                ):
                    await self.stop(item_id)

    async def stop(self, item_id: str, *, remove_files: bool = False) -> None:
        current = self._running.pop(item_id, None)
        directory = self.root / item_id
        if current:
            if current.process.returncode is None:
                current.process.terminate()
                try:
                    await asyncio.wait_for(current.process.wait(), timeout=5)
                except TimeoutError:
                    current.process.kill()
                    await current.process.wait()
            current.log_handle.close()
        if remove_files and directory.exists():
            shutil.rmtree(directory)

    async def ensure(self, item: dict[str, Any]) -> Path:
        item_id = str(item["id"])
        lock = self._locks.setdefault(item_id, asyncio.Lock())
        async with lock:
            current = self._running.get(item_id)
            manifest = self.root / item_id / "index.m3u8"
            if current and current.process.returncode is None and manifest.exists():
                current.last_access = time.monotonic()
                return manifest

            # The e2-micro test VM is deliberately limited to one active stream.
            for other_id in list(self._running):
                if other_id != item_id:
                    await self.stop(other_id, remove_files=True)
            await self.stop(item_id, remove_files=True)

            media = await self.resolver.media(str(item["url"]))
            directory = self.root / item_id
            directory.mkdir(parents=True, exist_ok=True)
            log_path = directory / "ffmpeg.log"
            log_handle = log_path.open("ab", buffering=0)
            command = self._ffmpeg_command(media, directory)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
            )
            now = time.monotonic()
            self._running[item_id] = RunningStream(
                process=process,
                directory=directory,
                started_at=now,
                last_access=now,
                log_handle=log_handle,
            )

            deadline = now + self.settings.startup_timeout_seconds
            while time.monotonic() < deadline:
                if manifest.exists() and manifest.stat().st_size > 20:
                    return manifest
                if process.returncode is not None:
                    break
                await asyncio.sleep(0.5)

            await self.stop(item_id)
            error = "FFmpeg 未能建立播放清單"
            if log_path.exists():
                text = log_path.read_text(encoding="utf-8", errors="replace")
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if lines:
                    error = " | ".join(lines[-5:])[-900:]
            raise RuntimeError(error)

    def _ffmpeg_command(self, media: dict[str, Any], directory: Path) -> list[str]:
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-re",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ]
        headers = media.get("headers") or {}
        user_agent = headers.get("User-Agent") or headers.get("user-agent")
        if user_agent:
            command.extend(["-user_agent", str(user_agent)])
        extra_headers = [
            f"{key}: {value}"
            for key, value in headers.items()
            if key.lower() in {"referer", "origin"}
        ]
        if extra_headers:
            command.extend(["-headers", "\r\n".join(extra_headers) + "\r\n"])
        command.extend(
            [
                "-i",
                str(media["url"]),
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-dn",
                "-sn",
                "-c",
                "copy",
                "-f",
                "hls",
                "-hls_time",
                "4",
                "-hls_list_size",
                "12",
                "-hls_flags",
                "delete_segments+append_list+independent_segments",
                "-hls_segment_filename",
                str(directory / "seg-%06d.ts"),
                str(directory / "index.m3u8"),
            ]
        )
        return command


def _provided_key(request: Request) -> str:
    key = request.query_params.get("key", "")
    if key:
        return key
    authorization = request.headers.get("authorization", "")
    return authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""


def _require_access(request: Request, settings: Settings) -> str:
    provided = _provided_key(request)
    if not provided or not hmac.compare_digest(provided, settings.access_key):
        raise HTTPException(status_code=401, detail="播放權杖錯誤")
    return provided


def _with_key(url: str, key: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}key={urllib.parse.quote(key, safe='')}"


def create_app(
    *,
    settings: Settings | None = None,
    catalog: Catalog | None = None,
    resolver: ResolverProtocol | None = None,
    streams: StreamManagerProtocol | None = None,
    hls_gateway: HlsGatewayProtocol | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    catalog = catalog or Catalog(settings.data_dir / "catalog.json")
    resolver = resolver or YouTubeResolver(settings)
    streams = streams or StreamManager(settings, resolver)
    hls_gateway = hls_gateway or HlsGateway(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await streams.start()
        await hls_gateway.start()
        try:
            yield
        finally:
            await hls_gateway.close()
            await streams.close()

    app = FastAPI(
        title="途播授權串流管理站",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(ROOT / "index.html", media_type="text/html")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "items": len(catalog.list()), "max_height": settings.max_height}

    @app.get("/api/config")
    async def api_config() -> dict[str, Any]:
        return {
            "app_name": "途播授權串流管理站",
            "public_base_url": settings.public_base_url,
            "max_height": settings.max_height,
        }

    @app.get("/api/items")
    async def list_items(request: Request) -> dict[str, Any]:
        _require_access(request, settings)
        items = catalog.list()
        for item in items:
            item["state"] = streams.status(str(item["id"]))
        return {"items": items}

    @app.post("/api/items")
    async def add_items(request: Request) -> JSONResponse:
        _require_access(request, settings)
        try:
            payload = await request.json()
            if payload.get("rights_confirmed") is not True:
                raise ValueError("請確認你擁有影片的使用與轉播權利")
            raw_urls = payload.get("urls")
            if not isinstance(raw_urls, list) or not raw_urls:
                raise ValueError("請至少輸入一個 YouTube 網址")
            urls = [validate_youtube_url(str(value)) for value in raw_urls[:10]]
        except (ValueError, TypeError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

        results: list[dict[str, Any]] = []
        for url in urls:
            try:
                item = await resolver.inspect(url)
                catalog.add(item)
            except Exception as exc:
                LOGGER.warning("Unable to add %s: %s", url, exc)
                results.append({"url": url, "ok": False, "error": str(exc)})
            else:
                results.append({"url": url, "ok": True, "item": item})
        return JSONResponse(
            status_code=200 if any(result["ok"] for result in results) else 502,
            content={"ok": any(result["ok"] for result in results), "results": results},
        )

    @app.post("/api/hls-items")
    async def add_hls_items(request: Request) -> JSONResponse:
        _require_access(request, settings)
        try:
            payload = await request.json()
            if payload.get("rights_confirmed") is not True:
                raise ValueError("請確認你擁有串流的使用與轉播權利")
            raw_items = payload.get("items")
            if not isinstance(raw_items, list) or not raw_items:
                raise ValueError("請至少輸入一個名稱與 M3U8 網址")
            if len(raw_items) > MAX_HLS_ITEMS_PER_REQUEST:
                raise ValueError(f"一次最多加入 {MAX_HLS_ITEMS_PER_REQUEST} 個來源")
            raw_headers = payload.get("headers") or {}
            if not isinstance(raw_headers, dict):
                raise ValueError("來源標頭格式不正確")
            headers = {
                "User-Agent": validate_optional_header(
                    raw_headers.get("user_agent", ""), "User-Agent"
                ),
                "Referer": validate_optional_header(
                    raw_headers.get("referer", ""), "Referer"
                ),
                "Origin": validate_optional_header(
                    raw_headers.get("origin", ""), "Origin"
                ),
            }
            headers = {key: value for key, value in headers.items() if value}
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

        results: list[dict[str, Any]] = []
        for index, raw_item in enumerate(raw_items, start=1):
            try:
                if not isinstance(raw_item, dict):
                    raise ValueError("格式不正確")
                title = str(raw_item.get("title") or "")
                url = str(raw_item.get("url") or "")
                item = await hls_gateway.inspect(title, url, headers)
                catalog.add(item)
            except Exception as exc:
                LOGGER.warning("Unable to add HLS item %s: %s", index, exc)
                results.append(
                    {
                        "index": index,
                        "title": str(raw_item.get("title") or "")[:180]
                        if isinstance(raw_item, dict)
                        else "",
                        "ok": False,
                        "error": str(exc),
                    }
                )
            else:
                results.append({"index": index, "ok": True, "item": item})
        ok = any(result["ok"] for result in results)
        return JSONResponse(
            status_code=200 if ok else 502,
            content={"ok": ok, "results": results},
        )

    @app.post("/api/items/{item_id}/probe")
    async def probe_item(item_id: str, request: Request) -> JSONResponse:
        _require_access(request, settings)
        item = catalog.get(item_id)
        if not item:
            raise HTTPException(status_code=404, detail="找不到影片")
        try:
            if item.get("source_type") == "hls":
                saved_headers = (
                    item.get("headers") if isinstance(item.get("headers"), dict) else {}
                )
                info = await hls_gateway.inspect(
                    str(item.get("title") or item_id), str(item["url"]), saved_headers
                )
            else:
                info = await resolver.inspect(str(item["url"]))
        except Exception as exc:
            return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})
        return JSONResponse({"ok": True, "title": info["title"], "is_live": info["is_live"]})

    @app.delete("/api/items/{item_id}")
    async def delete_item(item_id: str, request: Request) -> JSONResponse:
        _require_access(request, settings)
        await streams.stop(item_id, remove_files=True)
        if not catalog.remove(item_id):
            raise HTTPException(status_code=404, detail="找不到影片")
        return JSONResponse({"ok": True, "deleted": item_id})

    @app.api_route("/live.m3u", methods=["GET", "HEAD"])
    async def live_m3u(request: Request) -> Response:
        key = _require_access(request, settings)
        lines = ["#EXTM3U", "# 僅限已取得使用與轉播權利的串流內容"]
        for item in catalog.list():
            title = str(item.get("title") or item["id"]).replace("\n", " ").replace(",", "，")
            if item.get("source_type") == "hls":
                kind = "授權 HLS 直播" if item.get("is_live") else "授權 HLS 隨選"
                url = _with_key(
                    f"{settings.public_base_url}/hls/{item['id']}/manifest.m3u8", key
                )
                tvg_id = str(item["id"])
            else:
                kind = "YouTube 直播" if item.get("is_live") else "YouTube 影片"
                url = _with_key(
                    f"{settings.public_base_url}/stream/{item['id']}/index.m3u8", key
                )
                tvg_id = f"yt-{item['id']}"
            lines.append(
                f'#EXTINF:-1 tvg-id="{tvg_id}" group-title="{kind}",{title}'
            )
            lines.append(url)
        body = "\n".join(lines) + "\n"
        return Response(
            content="" if request.method == "HEAD" else body,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="youtube.m3u"'},
        )

    @app.get("/import-to-tubo")
    async def import_to_tubo(request: Request) -> RedirectResponse:
        key = _require_access(request, settings)
        playlist = _with_key(f"{settings.public_base_url}/live.m3u", key)
        target = "tubo://import?" + urllib.parse.urlencode(
            {"url": playlist, "name": "授權串流"}
        )
        return RedirectResponse(target, status_code=302)

    @app.api_route("/hls/{item_id}/manifest.m3u8", methods=["GET", "HEAD"])
    async def hls_manifest(item_id: str, request: Request) -> Response:
        key = _require_access(request, settings)
        item = catalog.get(item_id)
        if not item or item.get("source_type") != "hls":
            raise HTTPException(status_code=404, detail="找不到 HLS 串流")
        try:
            body, final_url = await hls_gateway.fetch_manifest(item, str(item["url"]))
            rewritten = rewrite_hls_manifest(
                body, final_url, item_id, settings, key
            )
        except Exception as exc:
            LOGGER.warning("Unable to relay HLS manifest %s: %s", item_id, exc)
            raise HTTPException(status_code=502, detail=f"HLS 串流無法讀取：{exc}") from exc
        return Response(
            content="" if request.method == "HEAD" else rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    @app.api_route("/hls/{item_id}/asset", methods=["GET", "HEAD"])
    async def hls_asset(item_id: str, request: Request) -> Response:
        key = _require_access(request, settings)
        item = catalog.get(item_id)
        if not item or item.get("source_type") != "hls":
            raise HTTPException(status_code=404, detail="找不到 HLS 串流")
        try:
            target_url = _decode_hls_target(
                item_id,
                request.query_params.get("u", ""),
                request.query_params.get("sig", ""),
                settings.access_key,
            )
            upstream, final_url = await hls_gateway.open_asset(
                item, target_url, request.headers.get("range", "")
            )
        except Exception as exc:
            LOGGER.warning("Unable to open HLS asset %s: %s", item_id, exc)
            raise HTTPException(status_code=502, detail=f"HLS 分段無法讀取：{exc}") from exc

        if upstream.status_code >= 400:
            status_code = upstream.status_code
            await upstream.aclose()
            raise HTTPException(status_code=502, detail=f"HLS 來源回應 HTTP {status_code}")

        content_type = upstream.headers.get("content-type", "").lower()
        path = urllib.parse.urlsplit(final_url).path.lower()
        is_manifest = path.endswith((".m3u8", ".m3u")) or "mpegurl" in content_type
        if is_manifest:
            await upstream.aclose()
            try:
                body, resolved_url = await hls_gateway.fetch_manifest(item, final_url)
                rewritten = rewrite_hls_manifest(
                    body, resolved_url, item_id, settings, key
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"HLS 子播放清單無法讀取：{exc}"
                ) from exc
            return Response(
                content="" if request.method == "HEAD" else rewritten,
                media_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store"},
            )

        response_headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower() in SAFE_UPSTREAM_RESPONSE_HEADERS
        }
        if request.method == "HEAD":
            await upstream.aclose()
            return Response(status_code=upstream.status_code, headers=response_headers)
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream.aclose),
        )

    @app.api_route("/stream/{item_id}/index.m3u8", methods=["GET", "HEAD"])
    async def stream_manifest(item_id: str, request: Request) -> Response:
        key = _require_access(request, settings)
        item = catalog.get(item_id)
        if not item:
            raise HTTPException(status_code=404, detail="找不到影片")
        try:
            manifest = await streams.ensure(item)
        except Exception as exc:
            LOGGER.warning("Unable to start %s: %s", item_id, exc)
            raise HTTPException(status_code=502, detail=f"無法啟動影片：{exc}") from exc
        streams.touch(item_id)
        text = manifest.read_text(encoding="utf-8", errors="replace")
        output: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                name = Path(stripped).name
                if not SEGMENT_RE.fullmatch(name):
                    raise HTTPException(status_code=502, detail="播放分段格式不正確")
                stripped = _with_key(
                    f"{settings.public_base_url}/stream/{item_id}/{name}", key
                )
            output.append(stripped if stripped else line)
        body = "\n".join(output) + "\n"
        return Response(
            content="" if request.method == "HEAD" else body,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    @app.api_route("/stream/{item_id}/{segment_name}", methods=["GET", "HEAD"])
    async def stream_segment(item_id: str, segment_name: str, request: Request) -> Response:
        _require_access(request, settings)
        if not catalog.get(item_id) or not SEGMENT_RE.fullmatch(segment_name):
            raise HTTPException(status_code=404, detail="找不到影片分段")
        streams.touch(item_id)
        path = settings.data_dir / "streams" / item_id / segment_name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="影片分段尚未產生，請重新載入")
        if request.method == "HEAD":
            return Response(media_type="video/mp2t", headers={"Cache-Control": "private, max-age=60"})
        return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "private, max-age=60"})

    return app


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
app = create_app()
