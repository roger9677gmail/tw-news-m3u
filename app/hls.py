from __future__ import annotations

import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping

ALLOWED_HOST_SUFFIXES = (
    "googlevideo.com",
    "youtube.com",
    "youtu.be",
    "googleusercontent.com",
)
URI_ATTRIBUTE_RE = re.compile(r'(?P<prefix>\bURI=)(?P<quote>["\'])(?P<uri>.*?)(?P=quote)')


class UnsafeUpstreamURL(ValueError):
    """The URL is outside the deliberately small media host allowlist."""


def url_expiry_epoch(url: str) -> float | None:
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        raw = query.get("expire", [None])[0]
        if raw is not None and str(raw).isdigit():
            return float(raw)
    except (OverflowError, OSError, ValueError):
        return None
    return None


def validate_upstream_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUpstreamURL("只允許 HTTP/HTTPS 媒體網址")
    if parsed.username or parsed.password:
        raise UnsafeUpstreamURL("不允許含帳號密碼的媒體網址")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUpstreamURL("媒體連接埠格式錯誤") from exc
    if port not in {None, 80, 443}:
        raise UnsafeUpstreamURL("不允許非標準媒體連接埠")

    host = parsed.hostname.lower().rstrip(".")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_HOST_SUFFIXES):
        raise UnsafeUpstreamURL(f"媒體主機不在允許清單：{host}")
    return url


@dataclass(frozen=True, slots=True)
class MediaToken:
    channel_id: str
    url: str
    headers: Mapping[str, str]
    expires_monotonic: float


class MediaTokenStore:
    """Small in-memory capability store; prevents the relay becoming an open proxy."""

    def __init__(self, ttl_seconds: int, max_entries: int = 30000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._tokens: dict[str, MediaToken] = {}
        self._reverse: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._last_purge = 0.0

    def _purge_locked(self, now: float, *, force: bool = False) -> None:
        if not force and now - self._last_purge < 30 and len(self._tokens) < self.max_entries:
            return
        self._last_purge = now
        expired = [token for token, item in self._tokens.items() if item.expires_monotonic <= now]
        for token in expired:
            item = self._tokens.pop(token, None)
            if item:
                key = (item.channel_id, item.url)
                if self._reverse.get(key) == token:
                    self._reverse.pop(key, None)

        if len(self._tokens) <= self.max_entries:
            return
        overflow = len(self._tokens) - self.max_entries
        oldest = sorted(self._tokens.items(), key=lambda pair: pair[1].expires_monotonic)[:overflow]
        for token, item in oldest:
            self._tokens.pop(token, None)
            key = (item.channel_id, item.url)
            if self._reverse.get(key) == token:
                self._reverse.pop(key, None)

    def register(self, channel_id: str, url: str, headers: Mapping[str, str]) -> str:
        validate_upstream_url(url)
        now = time.monotonic()
        ttl = float(self.ttl_seconds)
        wall_expiry = url_expiry_epoch(url)
        if wall_expiry is not None:
            ttl = min(ttl, max(60.0, wall_expiry - time.time() - 30.0))
        expires = now + ttl
        key = (channel_id, url)

        with self._lock:
            self._purge_locked(now)
            existing_token = self._reverse.get(key)
            if existing_token:
                existing = self._tokens.get(existing_token)
                if existing and existing.expires_monotonic > now + 30:
                    return existing_token

            token = secrets.token_urlsafe(18)
            item = MediaToken(
                channel_id=channel_id,
                url=url,
                headers=dict(headers),
                expires_monotonic=expires,
            )
            self._tokens[token] = item
            self._reverse[key] = token
            self._purge_locked(now, force=len(self._tokens) > self.max_entries)
            return token

    def get(self, channel_id: str, token: str) -> MediaToken | None:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            item = self._tokens.get(token)
            if not item or item.channel_id != channel_id or item.expires_monotonic <= now:
                return None
            return item

    def __len__(self) -> int:
        with self._lock:
            return len(self._tokens)


def is_hls_manifest(content_type: str | None, url: str, body: bytes | None = None) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type in {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "audio/mpegurl",
        "audio/x-mpegurl",
    }:
        return True
    parsed_url = urllib.parse.urlparse(url)
    path = parsed_url.path.lower()
    full = url.lower()
    if path.endswith((".m3u8", ".m3u")):
        return True
    if "/manifest/" in path or "hls_playlist" in full or "hls_variant" in full:
        return True
    return bool(body and body.lstrip().startswith(b"#EXTM3U"))


def _rewrite_uri(
    value: str,
    *,
    manifest_url: str,
    channel_id: str,
    headers: Mapping[str, str],
    token_store: MediaTokenStore,
    proxy_url: Callable[[str, str], str],
) -> str:
    stripped = value.strip()
    if not stripped or stripped.startswith(("data:", "skd:", "urn:")):
        return value
    absolute = urllib.parse.urljoin(manifest_url, stripped)
    try:
        token = token_store.register(channel_id, absolute, headers)
    except UnsafeUpstreamURL:
        # Do not turn an unexpected third-party URI into an open proxy.
        return value
    return proxy_url(channel_id, token)


def rewrite_hls_manifest(
    body: bytes,
    *,
    manifest_url: str,
    channel_id: str,
    headers: Mapping[str, str],
    token_store: MediaTokenStore,
    proxy_url: Callable[[str, str], str],
) -> bytes:
    text = body.decode("utf-8", errors="replace")
    output: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            output.append("")
            continue
        if not line.startswith("#"):
            output.append(
                _rewrite_uri(
                    line,
                    manifest_url=manifest_url,
                    channel_id=channel_id,
                    headers=headers,
                    token_store=token_store,
                    proxy_url=proxy_url,
                )
            )
            continue

        def replace_attribute(match: re.Match[str]) -> str:
            rewritten = _rewrite_uri(
                match.group("uri"),
                manifest_url=manifest_url,
                channel_id=channel_id,
                headers=headers,
                token_store=token_store,
                proxy_url=proxy_url,
            )
            quote = match.group("quote")
            return f"{match.group('prefix')}{quote}{rewritten}{quote}"

        output.append(URI_ATTRIBUTE_RE.sub(replace_attribute, raw_line))

    return ("\n".join(output) + "\n").encode("utf-8")


def iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
