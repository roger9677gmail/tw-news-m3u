from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Channel, Settings
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
    return compact[-limit:] or "未知錯誤"


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
    url = info.get("url")
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
    def __init__(self, settings: Settings, channels: tuple[Channel, ...]) -> None:
        self.settings = settings
        self.channels = {channel.id: channel for channel in channels}
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

            for source in channel.sources:
                for profile in ("default", "web_safari"):
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

                    self._cache[channel_id] = stream
                    cached_until = min(
                        stream.resolved_at + timedelta(seconds=self.settings.resolver_ttl_seconds),
                        (stream.expires_at - timedelta(minutes=2))
                        if stream.expires_at
                        else stream.resolved_at
                        + timedelta(seconds=self.settings.resolver_ttl_seconds),
                    )
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
                        source,
                        profile,
                        stream.height or "?",
                    )
                    return stream

            message = _clean_error(" || ".join(errors), 900)
            status.state = "error"
            status.error = message
            status.cached_until = now + timedelta(
                seconds=self.settings.resolver_failure_ttl_seconds
            )
            LOGGER.warning("Unable to resolve %s: %s", channel_id, message)
            raise ResolveError(message)

    def _command(self, source: str, profile: str) -> list[str]:
        max_height = self.settings.max_height
        selector = (
            f"best[protocol^=m3u8][vcodec!=none][acodec!=none][height<={max_height}]/"
            f"best[protocol^=m3u8][height<={max_height}]/"
            "worst[protocol^=m3u8][vcodec!=none][acodec!=none]/"
            "worst[protocol^=m3u8]"
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
        if profile == "web_safari":
            command.extend(["--extractor-args", "youtube:player_client=web_safari"])
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
