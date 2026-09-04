from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import Channel, HLSFallback, Settings
from .fourgtv import resolve_fourgtv
from .hls import validate_upstream_url
from .models import ResolvedStream, ResolverStatus

LOGGER = logging.getLogger(__name__)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class ResolveError(RuntimeError):
    """The configured public livestream could not be resolved."""


def utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


def _expiry_from_url(url: str) -> datetime | None:
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        value = values.get("expire", [None])[0]
        if value is not None and str(value).isdigit():
            return datetime.fromtimestamp(int(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return None


def _clean_error(value: str, limit: int = 650) -> str:
    text = ANSI_RE.sub("", value or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = " | ".join(lines[-6:])
    compact = compact.replace("\r", " ").replace("\n", " ")
    if len(compact) > limit:
        # Keep both the extractor's reason at the start and its remediation at
        # the end. Keeping only the suffix used to hide errors such as
        # "Sign in to confirm you're not a bot" behind the cookie help URL.
        head = max(1, (limit - 5) // 2)
        tail = max(1, limit - head - 5)
        compact = f"{compact[:head]} ... {compact[-tail:]}"
    return compact or "未知錯誤"


def _last_json_object(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            result = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
    raise ResolveError("yt-dlp 沒有回傳可解析的直播資料")


def _selected_stream(info: dict[str, Any]) -> tuple[str | None, str, int | None]:
    # YouTube live formats normally expose video and audio as separate media
    # playlists. The common manifest_url is the HLS master playlist that ties
    # them together; proxying the selected variant URL would lose audio.
    manifest_url = info.get("manifest_url")
    url = manifest_url if isinstance(manifest_url, str) else info.get("url")
    protocol = str(info.get("protocol") or "")
    height = info.get("height")

    if not isinstance(url, str):
        requested = info.get("requested_downloads")
        if isinstance(requested, list):
            for item in requested:
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    url = item["url"]
                    protocol = str(item.get("protocol") or protocol)
                    height = item.get("height", height)
                    break

    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None, protocol, None

    parsed_height: int | None
    try:
        parsed_height = int(height) if height is not None else None
    except (TypeError, ValueError):
        parsed_height = None
    return url, protocol, parsed_height


class YouTubeResolver:
    def __init__(
        self,
        settings: Settings,
        channels: tuple[Channel, ...],
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.channels = {channel.id: channel for channel in channels}
        self._http_client = http_client
        self._cache: dict[str, ResolvedStream] = {}
        self._status: dict[str, ResolverStatus] = {
            channel.id: ResolverStatus() for channel in channels
        }
        self._locks: dict[str, asyncio.Lock] = {
            channel.id: asyncio.Lock() for channel in channels
        }
        self._extract_semaphore = asyncio.Semaphore(settings.resolver_max_concurrency)

    def channel(self, channel_id: str) -> Channel:
        try:
            return self.channels[channel_id]
        except KeyError as exc:
            raise KeyError(f"找不到頻道：{channel_id}") from exc

    def set_http_client(self, client: httpx.AsyncClient | None) -> None:
        self._http_client = client

    def invalidate(self, channel_id: str) -> None:
        self._cache.pop(channel_id, None)
        status = self._status.get(channel_id)
        if status:
            status.cached_until = None

    def status_snapshot(self) -> dict[str, ResolverStatus]:
        return {key: value for key, value in self._status.items()}

    def _cached_is_usable(self, stream: ResolvedStream, now: datetime) -> bool:
        status = self._status[stream.channel_id]
        if status.cached_until is None or status.cached_until <= now:
            return False
        if stream.expires_at and stream.expires_at <= now + timedelta(minutes=2):
            return False
        return True

    async def resolve(self, channel_id: str, *, force: bool = False) -> ResolvedStream:
        channel = self.channel(channel_id)
        now = utcnow()
        cached = self._cache.get(channel_id)
        if not force and cached and self._cached_is_usable(cached, now):
            return cached

        async with self._locks[channel_id]:
            now = utcnow()
            cached = self._cache.get(channel_id)
            if not force and cached and self._cached_is_usable(cached, now):
                return cached

            status = self._status[channel_id]
            if (
                not force
                and status.state == "error"
                and status.cached_until
                and status.cached_until > now
            ):
                raise ResolveError(status.error or "頻道目前無法解析")

            status.state = "resolving"
            status.attempted_at = now
            status.error = None
            errors: list[str] = []
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.settings.resolver_timeout_seconds

            if channel.fourgtv_channel_id and channel.fourgtv_asset_id:
                try:
                    stream = await resolve_fourgtv(channel)
                    remaining = deadline - loop.time()
                    if remaining <= 1:
                        raise ResolveError("4GTV 解析後已達整體逾時")
                    await self._health_check_stream(
                        stream, timeout_seconds=min(10.0, remaining)
                    )
                except Exception as exc:
                    errors.append(f"4GTV 官方來源：{_clean_error(str(exc), 280)}")
                else:
                    return self._store_success(channel_id, stream, "HLS 健康檢查")

            if self.settings.experimental_hls_enabled:
                for fallback in channel.experimental_hls:
                    remaining = deadline - loop.time()
                    if remaining <= 1:
                        errors.append("已達整體解析逾時")
                        break
                    try:
                        stream = await self._resolve_experimental_hls(
                            channel,
                            fallback,
                            timeout_seconds=min(10.0, remaining),
                        )
                    except Exception as exc:
                        errors.append(
                            f"實驗性備援 {fallback.name}：{_clean_error(str(exc), 280)}"
                        )
                        continue
                    return self._store_success(channel_id, stream, "HLS 健康檢查")

            for source in channel.sources:
                for profile in ("mweb", "web_safari", "default"):
                    remaining = deadline - loop.time()
                    if remaining <= 1:
                        errors.append("已達整體解析逾時")
                        break
                    try:
                        stream = await self._extract(
                            channel, source, profile, timeout_seconds=min(35.0, remaining)
                        )
                    except Exception as exc:  # Continue to configured fallback source.
                        errors.append(f"{source} [{profile}]：{_clean_error(str(exc), 280)}")
                        continue

                    return self._store_success(channel_id, stream, profile)

            message = _clean_error(" || ".join(errors), 900)
            status.state = "error"
            status.error = message
            status.cached_until = now + timedelta(
                seconds=self.settings.resolver_failure_ttl_seconds
            )
            LOGGER.warning("Unable to resolve %s: %s", channel_id, message)
            raise ResolveError(message)

    async def _probe_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        *,
        limit: int,
    ) -> tuple[str, int, str, bytes]:
        current_url = validate_upstream_url(url)
        for _ in range(6):
            request = client.build_request("GET", current_url, headers=headers)
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                validate_upstream_url(str(response.url))
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ResolveError("上游重新導向缺少網址")
                    current_url = validate_upstream_url(
                        urllib.parse.urljoin(str(response.url), location)
                    )
                    continue

                chunks: list[bytes] = []
                total = 0
                if response.is_stream_consumed:
                    chunks.append(response.content)
                    total = len(response.content)
                else:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > limit:
                            raise ResolveError("上游健康檢查內容過大")
                        chunks.append(chunk)
                if total > limit:
                    raise ResolveError("上游健康檢查內容過大")
                return (
                    str(response.url),
                    response.status_code,
                    response.headers.get("content-type", ""),
                    b"".join(chunks),
                )
            finally:
                await response.aclose()
        raise ResolveError("上游重新導向次數過多")

    async def _probe_hls_with_client(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> None:
        current_url = url
        for _ in range(3):
            manifest_url, status, _, body = await self._probe_request(
                client, current_url, headers, limit=512 * 1024
            )
            if status < 200 or status >= 300:
                raise ResolveError(f"播放清單回應 HTTP {status}")
            if not body.lstrip().startswith(b"#EXTM3U"):
                raise ResolveError("上游沒有回傳 HLS 播放清單")

            first_uri = next(
                (
                    line.strip()
                    for line in body.decode("utf-8", errors="replace").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ),
                None,
            )
            if not first_uri:
                raise ResolveError("HLS 播放清單沒有影音網址")
            next_url = validate_upstream_url(
                urllib.parse.urljoin(manifest_url, first_uri)
            )

            if b"#EXT-X-STREAM-INF:" in body:
                current_url = next_url
                continue

            segment_headers = {**headers, "Range": "bytes=0-2047"}
            _, segment_status, _, segment = await self._probe_request(
                client, next_url, segment_headers, limit=64 * 1024
            )
            if segment_status not in {200, 206} or not segment:
                raise ResolveError(f"第一個影音分段回應 HTTP {segment_status}")
            prefix = segment.lstrip()[:32].lower()
            if prefix.startswith((b"<!doctype html", b"<html")):
                raise ResolveError("第一個影音分段回傳錯誤網頁")
            return

        raise ResolveError("HLS 播放清單層級過深")

    async def _health_check_stream(
        self,
        stream: ResolvedStream,
        *,
        timeout_seconds: float,
    ) -> None:
        headers = dict(stream.headers)
        async with asyncio.timeout(timeout_seconds):
            if self._http_client is not None:
                await self._probe_hls_with_client(
                    self._http_client, stream.stream_url, headers
                )
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        timeout_seconds, connect=min(5.0, timeout_seconds)
                    ),
                    http2=True,
                ) as client:
                    await self._probe_hls_with_client(
                        client, stream.stream_url, headers
                    )

    async def _resolve_experimental_hls(
        self,
        channel: Channel,
        fallback: HLSFallback,
        *,
        timeout_seconds: float,
    ) -> ResolvedStream:
        headers = {
            "Accept": "*/*",
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
            ),
        }

        async with asyncio.timeout(timeout_seconds):
            if self._http_client is not None:
                await self._probe_hls_with_client(
                    self._http_client, fallback.url, headers
                )
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds)),
                    http2=True,
                ) as client:
                    await self._probe_hls_with_client(client, fallback.url, headers)

        resolved_at = utcnow()
        return ResolvedStream(
            channel_id=channel.id,
            source=f"實驗性備援：{fallback.name}",
            stream_url=fallback.url,
            webpage_url=fallback.source_page,
            title=channel.name,
            video_id=f"experimental-{channel.id}",
            protocol="m3u8_native",
            height=None,
            headers=headers,
            resolved_at=resolved_at,
            expires_at=_expiry_from_url(fallback.url),
        )

    def _store_success(
        self, channel_id: str, stream: ResolvedStream, profile: str
    ) -> ResolvedStream:
        self._cache[channel_id] = stream
        cached_until = min(
            stream.resolved_at + timedelta(seconds=self.settings.resolver_ttl_seconds),
            (stream.expires_at - timedelta(minutes=2))
            if stream.expires_at
            else stream.resolved_at
            + timedelta(seconds=self.settings.resolver_ttl_seconds),
        )
        status = self._status[channel_id]
        status.state = "online"
        status.title = stream.title
        status.height = stream.height
        status.source = stream.source
        status.webpage_url = stream.webpage_url
        status.resolved_at = stream.resolved_at
        status.expires_at = stream.expires_at
        status.cached_until = cached_until
        status.error = None
        LOGGER.info(
            "Resolved %s via %s (%s, %sp)",
            channel_id,
            stream.source,
            profile,
            stream.height or "?",
        )
        return stream

    def _command(self, source: str, profile: str) -> list[str]:
        max_height = self.settings.max_height
        selector = (
            f"best*[protocol^=m3u8][vcodec!=none][height<={max_height}]/"
            "worst*[protocol^=m3u8][vcodec!=none]"
        )
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-config",
            "--quiet",
            "--no-warnings",
            "--no-playlist",
            "--skip-download",
            "--socket-timeout",
            "20",
            "--retries",
            "1",
            "--extractor-retries",
            "1",
            "--js-runtimes",
            "deno",
            "--format",
            selector,
            "--dump-single-json",
        ]
        if profile != "default":
            command.extend(["--extractor-args", f"youtube:player_client={profile}"])
        command.append(source)
        return command

    async def _extract(
        self,
        channel: Channel,
        source: str,
        profile: str,
        *,
        timeout_seconds: float,
    ) -> ResolvedStream:
        command = self._command(source, profile)
        async with self._extract_semaphore:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_seconds
                )
            except asyncio.CancelledError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.communicate()
                raise
            except TimeoutError as exc:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.communicate()
                raise ResolveError("解析逾時") from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise ResolveError(_clean_error(stderr or stdout))

        info = _last_json_object(stdout)
        live_status = info.get("live_status")
        if info.get("is_live") is not True and live_status != "is_live":
            raise ResolveError(f"來源目前不是直播（live_status={live_status!r}）")

        stream_url, protocol, height = _selected_stream(info)
        if not stream_url:
            raise ResolveError("沒有取得可播放的 HLS 網址")
        if "m3u8" not in protocol.lower() and ".m3u8" not in stream_url.lower():
            raise ResolveError(f"取得的格式不是 HLS（protocol={protocol or 'unknown'}）")

        raw_headers = info.get("http_headers")
        headers: dict[str, str] = {}
        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                if not isinstance(key, str) or not isinstance(value, (str, int, float)):
                    continue
                if key.lower() in {"cookie", "authorization", "proxy-authorization"}:
                    continue
                headers[key] = str(value)
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0 Mobile Safari/537.36",
        )

        resolved_at = utcnow()
        return ResolvedStream(
            channel_id=channel.id,
            source=source,
            stream_url=stream_url,
            webpage_url=str(info.get("webpage_url") or source),
            title=str(info.get("title") or channel.name),
            video_id=str(info.get("id") or ""),
            protocol=protocol,
            height=height,
            headers=headers,
            resolved_at=resolved_at,
            expires_at=_expiry_from_url(stream_url),
        )
