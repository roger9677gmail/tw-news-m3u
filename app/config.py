from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CHANNEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


@dataclass(frozen=True, slots=True)
class Channel:
    id: str
    name: str
    group: str
    short_name: str
    sources: tuple[str, ...]
    fourgtv_channel_id: str | None = None
    fourgtv_asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str
    channels_path: Path
    access_key: str
    public_base_url: str
    max_height: int
    resolver_ttl_seconds: int
    resolver_failure_ttl_seconds: int
    resolver_timeout_seconds: int
    resolver_max_concurrency: int
    media_token_ttl_seconds: int
    upstream_timeout_seconds: float
    max_token_entries: int
    log_level: str

    @property
    def access_required(self) -> bool:
        return bool(self.access_key)


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必須是整數") from exc
    return max(minimum, min(value, maximum))


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必須是數字") from exc
    return max(minimum, min(value, maximum))


def load_settings() -> Settings:
    channels_path = Path(os.getenv("CHANNELS_FILE", str(ROOT / "channels.json"))).expanduser()
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    access_key = os.getenv("ACCESS_KEY", "").strip()

    return Settings(
        app_name=os.getenv("APP_NAME", "台灣新聞直播 M3U").strip() or "台灣新聞直播 M3U",
        channels_path=channels_path,
        access_key=access_key,
        public_base_url=public_base_url,
        max_height=_int_env("MAX_HEIGHT", 720, 144, 2160),
        resolver_ttl_seconds=_int_env("RESOLVER_TTL_SECONDS", 900, 60, 7200),
        resolver_failure_ttl_seconds=_int_env("RESOLVER_FAILURE_TTL_SECONDS", 90, 10, 1800),
        resolver_timeout_seconds=_int_env("RESOLVER_TIMEOUT_SECONDS", 75, 20, 300),
        resolver_max_concurrency=_int_env("MAX_RESOLVER_CONCURRENCY", 2, 1, 8),
        media_token_ttl_seconds=_int_env("MEDIA_TOKEN_TTL_SECONDS", 21600, 600, 86400),
        upstream_timeout_seconds=_float_env("UPSTREAM_TIMEOUT_SECONDS", 25.0, 5.0, 120.0),
        max_token_entries=_int_env("MAX_TOKEN_ENTRIES", 30000, 1000, 200000),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )


def _require_text(item: dict[str, Any], field: str, index: int) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"channels.json 第 {index + 1} 筆缺少 {field}")
    return value.strip()


def load_channels(path: Path) -> tuple[Channel, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到頻道設定檔：{path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"頻道設定檔 JSON 格式錯誤：{exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise RuntimeError("channels.json 必須是非空白陣列")

    channels: list[Channel] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RuntimeError(f"channels.json 第 {index + 1} 筆必須是物件")

        channel_id = _require_text(item, "id", index)
        if not CHANNEL_ID_RE.fullmatch(channel_id):
            raise RuntimeError(f"頻道 id 格式不正確：{channel_id}")
        if channel_id in seen_ids:
            raise RuntimeError(f"頻道 id 重複：{channel_id}")
        seen_ids.add(channel_id)

        source_values = item.get("sources")
        if not isinstance(source_values, list) or not source_values:
            raise RuntimeError(f"頻道 {channel_id} 至少需要一個 sources 網址")
        sources: list[str] = []
        for source in source_values:
            if not isinstance(source, str) or not source.startswith(("https://", "http://")):
                raise RuntimeError(f"頻道 {channel_id} 有不合法來源網址")
            sources.append(source.strip())

        fourgtv = item.get("fourgtv")
        fourgtv_channel_id: str | None = None
        fourgtv_asset_id: str | None = None
        if fourgtv is not None:
            if not isinstance(fourgtv, dict):
                raise RuntimeError(f"頻道 {channel_id} 的 fourgtv 必須是物件")
            raw_channel_id = fourgtv.get("channel_id")
            raw_asset_id = fourgtv.get("asset_id")
            if not isinstance(raw_channel_id, str) or not raw_channel_id.strip():
                raise RuntimeError(f"頻道 {channel_id} 缺少 fourgtv.channel_id")
            if not isinstance(raw_asset_id, str) or not raw_asset_id.strip():
                raise RuntimeError(f"頻道 {channel_id} 缺少 fourgtv.asset_id")
            fourgtv_channel_id = raw_channel_id.strip()
            fourgtv_asset_id = raw_asset_id.strip()

        channels.append(
            Channel(
                id=channel_id,
                name=_require_text(item, "name", index),
                group=_require_text(item, "group", index),
                short_name=str(item.get("short_name") or item["name"]).strip(),
                sources=tuple(sources),
                fourgtv_channel_id=fourgtv_channel_id,
                fourgtv_asset_id=fourgtv_asset_id,
            )
        )

    return tuple(channels)
