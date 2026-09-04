from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import urllib.parse
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad
from curl_cffi import requests

from .config import Channel
from .models import ResolvedStream

LOGGER = logging.getLogger(__name__)

API_URL = "https://api2.4gtv.tv/App/GetChannelUrl2"
APP_USER_AGENT = (
    "%E5%9B%9B%E5%AD%A3%E7%B7%9A%E4%B8%8A/1 "
    "CFNetwork/1568.200.51 Darwin/24.1.0"
)
# Keep the public mobile-client protocol version configurable so a future
# official app update does not require changing request logic in multiple places.
APP_VERSION = os.getenv("FOURGTV_APP_VERSION", "3.2.17").strip() or "3.2.17"

# These values are part of 4GTV's public iOS client protocol.  They are not a
# user credential and the resulting authorization value changes every UTC day.
_AES_KEY = b"ilyB29ZdruuQjC45JhBBR7o2Z8WJ26Vg"
_AES_IV = b"JUMxvVMmszqUTeKn"
_ENCRYPTED_APP_SECRET = (
    "PyPJU25iI2IQCMWq7kblwh9sGCypqsxMp4sKjJo95SK43h08ff+j1nbWliTySSB+"
    "N67BnXrYv9DfwK+ue5wWkg=="
)


class FourGTVError(RuntimeError):
    """The official 4GTV mobile API did not return a usable live stream."""


def _app_secret() -> str:
    encrypted = base64.b64decode(_ENCRYPTED_APP_SECRET)
    decrypted = AES.new(_AES_KEY, AES.MODE_CBC, _AES_IV).decrypt(encrypted)
    return unpad(decrypted, AES.block_size).decode("ascii")


def daily_auth(day: date) -> str:
    digest = hashlib.sha512((day.strftime("%Y%m%d") + _app_secret()).encode()).digest()
    return base64.b64encode(digest).decode("ascii")


def _request_headers(encryption_key: str, now: datetime) -> dict[str, str]:
    return {
        "4GTV_AUTH": daily_auth(now.astimezone(UTC).date()),
        "fsDEVICE": "iOS",
        "fsVALUE": "",
        "fsVERSION": APP_VERSION,
        "fsENC_KEY": encryption_key,
        "User-Agent": APP_USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
    }


def _stream_urls(payload: Any) -> list[str]:
    if not isinstance(payload, dict) or payload.get("Success") is not True:
        message = (
            payload.get("ErrMessage") or payload.get("Message")
            if isinstance(payload, dict)
            else None
        )
        raise FourGTVError(str(message or "官方 API 拒絕直播請求"))
    data = payload.get("Data")
    values = data.get("flstURLs") if isinstance(data, dict) else None
    if not isinstance(values, list):
        raise FourGTVError("官方 API 沒有回傳直播網址")
    urls = [
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip().startswith("https://")
    ]
    if not urls:
        raise FourGTVError("官方 API 沒有可用的 HLS 網址")
    return urls


def _request_body(channel: Channel, encryption_key: str) -> dict[str, object]:
    return {
        "fnCHANNEL_ID": channel.fourgtv_channel_id,
        "fsDEVICE_TYPE": "mobile",
        "clsAPP_IDENTITY_VALIDATE_ARUS": {
            "fsVALUE": "",
            "fsENC_KEY": encryption_key,
        },
        "fsASSET_ID": channel.fourgtv_asset_id,
    }


def refresh_plan(channels: tuple[Channel, ...]) -> dict[str, object]:
    now = datetime.now(tz=UTC)
    requests_to_make: list[dict[str, object]] = []
    for channel in channels:
        if not channel.fourgtv_channel_id or not channel.fourgtv_asset_id:
            continue
        encryption_key = str(uuid.uuid4()).upper()
        requests_to_make.append(
            {
                "channel_id": channel.id,
                "url": API_URL,
                "headers": _request_headers(encryption_key, now),
                "body": _request_body(channel, encryption_key),
            }
        )
    return {"requests": requests_to_make}


def cache_from_client_responses(
    channels: tuple[Channel, ...], payload: Any
) -> dict[str, object]:
    from .hls import validate_upstream_url

    values = payload.get("responses") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise FourGTVError("更新內容缺少 responses")

    expected = {
        channel.id
        for channel in channels
        if channel.fourgtv_channel_id and channel.fourgtv_asset_id
    }
    result: dict[str, dict[str, str]] = {}
    for item in values:
        channel_id = item.get("channel_id") if isinstance(item, dict) else None
        response_payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(channel_id, str) or channel_id not in expected:
            raise FourGTVError("更新內容含有未知頻道")
        if channel_id in result:
            raise FourGTVError(f"頻道 {channel_id} 重複")
        # Prefer the first official CDN returned by 4GTV. The API currently
        # lists Hinet first and Mozai second; Mozai can return HTTP 403 even
        # while the Hinet URL from the same response remains playable.
        url = _stream_urls(response_payload)[0]
        validate_upstream_url(url)
        expiry = stream_expiry(url)
        if expiry is None or expiry <= datetime.now(tz=UTC):
            raise FourGTVError(f"頻道 {channel_id} 的直播網址已過期")
        result[channel_id] = {"url": url, "expires_at": expiry.isoformat()}

    missing = expected - result.keys()
    if missing:
        raise FourGTVError("更新內容缺少頻道：" + ", ".join(sorted(missing)))
    return {
        "generated_at": datetime.now(tz=UTC).replace(microsecond=0).isoformat(),
        "channels": result,
    }


def stream_expiry(url: str) -> datetime | None:
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        values = [
            int(raw)
            for key in ("expires", "expires1", "expire")
            for raw in query.get(key, [])
            if str(raw).isdigit()
        ]
        if values:
            return datetime.fromtimestamp(min(values), tz=UTC)
    except (OverflowError, OSError, ValueError):
        pass
    return None


def _cached_stream_url(channel: Channel, cache_path: Path) -> str:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        channels = payload.get("channels") if isinstance(payload, dict) else None
        entry = channels.get(channel.id) if isinstance(channels, dict) else None
        url = entry.get("url") if isinstance(entry, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        raise FourGTVError(f"無法讀取官方直播快取：{exc}") from exc
    if not isinstance(url, str) or not url.startswith("https://"):
        raise FourGTVError("官方直播快取缺少頻道網址")
    expiry = stream_expiry(url)
    if expiry and expiry <= datetime.now(tz=UTC):
        raise FourGTVError("官方直播快取已過期")
    return url


def _fetch_stream_url(channel: Channel, timeout_seconds: float) -> str:
    if not channel.fourgtv_channel_id or not channel.fourgtv_asset_id:
        raise FourGTVError("頻道未設定 4GTV 官方來源")

    encryption_key = str(uuid.uuid4()).upper()
    headers = _request_headers(encryption_key, datetime.now(tz=UTC))
    body = _request_body(channel, encryption_key)
    try:
        response = requests.post(
            API_URL,
            headers=headers,
            json=body,
            timeout=timeout_seconds,
            impersonate="safari_ios",
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise FourGTVError(f"官方 API 連線失敗：{exc}") from exc
    # Prefer the first official CDN returned by 4GTV. In current responses the
    # first Hinet URL remains playable while the later Mozai URL can return
    # HTTP 403.
    return _stream_urls(payload)[0]


async def resolve_fourgtv(channel: Channel, timeout_seconds: float = 15.0) -> ResolvedStream:
    cache_file = os.getenv("FOURGTV_CACHE_FILE", "").strip()
    source = "4GTV 官方行動直播"
    if cache_file:
        try:
            stream_url = await asyncio.to_thread(
                _cached_stream_url, channel, Path(cache_file)
            )
            source = "4GTV 官方快取直播"
        except FourGTVError as exc:
            LOGGER.warning("4GTV cache unavailable for %s: %s", channel.id, exc)
            stream_url = await asyncio.wait_for(
                asyncio.to_thread(_fetch_stream_url, channel, timeout_seconds),
                timeout=timeout_seconds + 2,
            )
    else:
        stream_url = await asyncio.wait_for(
            asyncio.to_thread(_fetch_stream_url, channel, timeout_seconds),
            timeout=timeout_seconds + 2,
        )
    resolved_at = datetime.now(tz=UTC).replace(microsecond=0)
    return ResolvedStream(
        channel_id=channel.id,
        source=source,
        stream_url=stream_url,
        webpage_url="https://www.4gtv.tv/channel_list.html",
        title=channel.name,
        video_id=channel.fourgtv_asset_id or "",
        protocol="m3u8_native",
        height=None,
        headers={"User-Agent": APP_USER_AGENT, "Accept": "*/*"},
        resolved_at=resolved_at,
        expires_at=stream_expiry(stream_url),
    )
