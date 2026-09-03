from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from google.api_core.exceptions import GoogleAPIError, NotFound
from google.cloud import storage

LOGGER = logging.getLogger(__name__)
SONG_ID_RE = re.compile(r"^[a-f0-9]{24}$")
ASSET_RE = re.compile(r"^(?:index\.m3u8|segment-\d{5}\.ts)$")


class KaraokeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KaraokeSong:
    id: str
    title: str
    original_file: str
    size_bytes: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class KaraokeStore(Protocol):
    enabled: bool

    async def list_songs(self) -> list[KaraokeSong]: ...

    async def create_upload(
        self, *, file_name: str, size_bytes: int, origin: str
    ) -> dict[str, str]: ...

    async def complete_upload(
        self, *, upload_id: str, title: str, file_name: str
    ) -> KaraokeSong: ...

    async def delete_song(self, song_id: str) -> bool: ...

    async def read_asset(self, song_id: str, asset_name: str) -> tuple[bytes, str]: ...


class DisabledKaraokeStore:
    enabled = False

    async def list_songs(self) -> list[KaraokeSong]:
        return []

    async def create_upload(
        self, *, file_name: str, size_bytes: int, origin: str
    ) -> dict[str, str]:
        raise KaraokeError("尚未設定卡拉 OK 儲存空間")

    async def complete_upload(
        self, *, upload_id: str, title: str, file_name: str
    ) -> KaraokeSong:
        raise KaraokeError("尚未設定卡拉 OK 儲存空間")

    async def delete_song(self, song_id: str) -> bool:
        raise KaraokeError("尚未設定卡拉 OK 儲存空間")

    async def read_asset(self, song_id: str, asset_name: str) -> tuple[bytes, str]:
        raise KaraokeError("尚未設定卡拉 OK 儲存空間")


def _clean_file_name(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip()
    if not name or len(name) > 180 or not name.lower().endswith(".mp4"):
        raise KaraokeError("請選擇 MP4 影片檔")
    return name


def _clean_title(value: str, fallback: str) -> str:
    title = " ".join(str(value or "").split()).strip()
    if not title:
        title = Path(fallback).stem
    if not title or len(title) > 120:
        raise KaraokeError("歌曲名稱必須介於 1 到 120 個字元")
    return title


def _validate_song_id(value: str) -> str:
    if not SONG_ID_RE.fullmatch(value):
        raise KaraokeError("歌曲識別碼格式錯誤")
    return value


def _validate_asset_name(value: str) -> str:
    if not ASSET_RE.fullmatch(value):
        raise KaraokeError("影音檔案名稱格式錯誤")
    return value


class GCSKaraokeStore:
    enabled = True

    def __init__(
        self,
        *,
        bucket_name: str,
        project_id: str,
        prefix: str = "karaoke",
        max_upload_bytes: int = 600 * 1024 * 1024,
        ffmpeg_timeout_seconds: int = 3300,
        client: storage.Client | None = None,
    ) -> None:
        self.bucket_name = bucket_name
        self.prefix = prefix.strip("/") or "karaoke"
        self.max_upload_bytes = max_upload_bytes
        self.ffmpeg_timeout_seconds = ffmpeg_timeout_seconds
        self.client = client or storage.Client(project=project_id or None)
        self.bucket = self.client.bucket(bucket_name)
        self._lock = asyncio.Lock()

    @property
    def _catalog_name(self) -> str:
        return f"{self.prefix}/catalog.json"

    def _incoming_name(self, upload_id: str) -> str:
        return f"{self.prefix}/incoming/{upload_id}.mp4"

    def _song_prefix(self, song_id: str) -> str:
        return f"{self.prefix}/songs/{song_id}/"

    def _read_catalog_sync(self) -> list[KaraokeSong]:
        blob = self.bucket.blob(self._catalog_name)
        if not blob.exists(client=self.client):
            return []
        try:
            payload = json.loads(blob.download_as_text(encoding="utf-8"))
            rows = payload.get("songs", [])
            return [KaraokeSong(**row) for row in rows if isinstance(row, dict)]
        except (ValueError, TypeError) as exc:
            raise KaraokeError(f"歌曲目錄格式錯誤：{exc}") from exc

    def _write_catalog_sync(self, songs: list[KaraokeSong]) -> None:
        payload = {
            "version": 1,
            "updated_at": datetime.now(tz=UTC).isoformat(),
            "songs": [song.as_dict() for song in songs],
        }
        self.bucket.blob(self._catalog_name).upload_from_string(
            json.dumps(payload, ensure_ascii=False, indent=2),
            content_type="application/json; charset=utf-8",
        )

    async def list_songs(self) -> list[KaraokeSong]:
        songs = await asyncio.to_thread(self._read_catalog_sync)
        return sorted(songs, key=lambda song: (song.title.casefold(), song.created_at))

    async def create_upload(
        self, *, file_name: str, size_bytes: int, origin: str
    ) -> dict[str, str]:
        _clean_file_name(file_name)
        if size_bytes < 1 or size_bytes > self.max_upload_bytes:
            limit_mb = self.max_upload_bytes // (1024 * 1024)
            raise KaraokeError(f"影片大小必須小於 {limit_mb} MB")
        upload_id = uuid.uuid4().hex[:24]
        blob = self.bucket.blob(self._incoming_name(upload_id))
        upload_url = await asyncio.to_thread(
            blob.create_resumable_upload_session,
            content_type="video/mp4",
            size=size_bytes,
            origin=origin,
            client=self.client,
        )
        return {"upload_id": upload_id, "upload_url": upload_url}

    def _transcode_sync(self, source: Path, output_dir: Path) -> None:
        playlist = output_dir / "index.m3u8"
        segment_pattern = output_dir / "segment-%05d.ts"
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-hls_time",
            "6",
            "-hls_playlist_type",
            "vod",
            "-hls_flags",
            "independent_segments",
            "-hls_segment_filename",
            str(segment_pattern),
            str(playlist),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.ffmpeg_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise KaraokeError("影片轉檔逾時") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "ffmpeg 失敗").strip()[-1200:]
            raise KaraokeError(f"影片轉檔失敗：{detail}") from exc

    def _upload_outputs_sync(self, song_id: str, output_dir: Path) -> None:
        files = sorted(output_dir.iterdir())
        if not files or not (output_dir / "index.m3u8").is_file():
            raise KaraokeError("轉檔完成但找不到 HLS 播放清單")
        for path in files:
            content_type = (
                "application/vnd.apple.mpegurl"
                if path.suffix == ".m3u8"
                else "video/mp2t"
            )
            self.bucket.blob(self._song_prefix(song_id) + path.name).upload_from_filename(
                str(path), content_type=content_type
            )

    async def complete_upload(
        self, *, upload_id: str, title: str, file_name: str
    ) -> KaraokeSong:
        upload_id = _validate_song_id(upload_id)
        file_name = _clean_file_name(file_name)
        title = _clean_title(title, file_name)
        incoming = self.bucket.blob(self._incoming_name(upload_id))

        async with self._lock:
            try:
                await asyncio.to_thread(incoming.reload, client=self.client)
            except NotFound as exc:
                raise KaraokeError("找不到已上傳的 MP4，請重新選檔") from exc
            except GoogleAPIError as exc:
                raise KaraokeError(f"無法讀取已上傳的 MP4：{exc}") from exc
            size = int(incoming.size or 0)
            if size < 1 or size > self.max_upload_bytes:
                raise KaraokeError("找不到完整影片，或影片大小超過限制")

            with tempfile.TemporaryDirectory(prefix="karaoke-") as temp_path:
                temp_dir = Path(temp_path)
                source = temp_dir / "source.mp4"
                output_dir = temp_dir / "hls"
                output_dir.mkdir()
                await asyncio.to_thread(incoming.download_to_filename, str(source))
                await asyncio.to_thread(self._transcode_sync, source, output_dir)
                await asyncio.to_thread(self._upload_outputs_sync, upload_id, output_dir)

            song = KaraokeSong(
                id=upload_id,
                title=title,
                original_file=file_name,
                size_bytes=size,
                created_at=datetime.now(tz=UTC).isoformat(),
            )
            songs = await asyncio.to_thread(self._read_catalog_sync)
            songs = [item for item in songs if item.id != song.id]
            songs.append(song)
            await asyncio.to_thread(self._write_catalog_sync, songs)
            await asyncio.to_thread(incoming.delete)
            return song

    async def delete_song(self, song_id: str) -> bool:
        song_id = _validate_song_id(song_id)
        async with self._lock:
            songs = await asyncio.to_thread(self._read_catalog_sync)
            remaining = [song for song in songs if song.id != song_id]
            if len(remaining) == len(songs):
                return False
            blobs = await asyncio.to_thread(
                lambda: list(self.client.list_blobs(self.bucket, prefix=self._song_prefix(song_id)))
            )
            for blob in blobs:
                await asyncio.to_thread(blob.delete)
            await asyncio.to_thread(self._write_catalog_sync, remaining)
            return True

    async def read_asset(self, song_id: str, asset_name: str) -> tuple[bytes, str]:
        song_id = _validate_song_id(song_id)
        asset_name = _validate_asset_name(asset_name)
        blob = self.bucket.blob(self._song_prefix(song_id) + asset_name)
        try:
            body = await asyncio.to_thread(blob.download_as_bytes)
        except NotFound as exc:
            raise KaraokeError("找不到歌曲影音檔") from exc
        except GoogleAPIError as exc:
            raise KaraokeError(f"無法讀取歌曲影音檔：{exc}") from exc
        content_type = blob.content_type or mimetypes.guess_type(asset_name)[0] or "application/octet-stream"
        return body, content_type
