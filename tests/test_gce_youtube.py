from __future__ import annotations

import importlib.util
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).parents[1] / "gce-youtube" / "app.py"
os.environ.setdefault("ACCESS_KEY", "test-import-key-that-is-long-enough")
os.environ.setdefault("PUBLIC_BASE_URL", "https://relay.example")
os.environ.setdefault("DATA_DIR", str(Path(tempfile.gettempdir()) / "gce-youtube-import-test"))
SPEC = importlib.util.spec_from_file_location("gce_youtube_app", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeResolver:
    async def inspect(self, url: str):
        return {
            "id": "BaW_jenozKc",
            "video_id": "BaW_jenozKc",
            "title": "授權測試影片",
            "url": url,
            "is_live": False,
            "duration": 10,
            "added_at": "2026-09-04T00:00:00Z",
        }

    async def media(self, url: str):
        return {"url": "https://example.invalid/video.mp4", "headers": {}, "is_live": False}


class FakeStreams:
    async def start(self):
        return None

    async def close(self):
        return None

    async def ensure(self, item):
        raise AssertionError("not used")

    async def stop(self, item_id: str, *, remove_files: bool = False):
        return None

    def status(self, item_id: str):
        return "idle"

    def touch(self, item_id: str):
        return None


class FakeHlsGateway:
    async def start(self):
        return None

    async def close(self):
        return None

    async def inspect(self, title: str, url: str, headers: dict[str, str]):
        MODULE.validate_hls_url(url)
        if not title.strip():
            raise ValueError("節目名稱不可空白")
        return {
            "id": "hls-0123456789abcdef",
            "source_type": "hls",
            "title": title.strip(),
            "url": url,
            "headers": headers,
            "is_live": True,
            "added_at": "2026-09-04T00:00:00Z",
        }

    async def fetch_manifest(self, item, target_url):
        return "#EXTM3U\n#EXT-X-TARGETDURATION:6\nsegment.ts\n", target_url

    async def open_asset(self, item, target_url, byte_range):
        raise AssertionError("not used")


def test_admin_catalog_and_playlist(tmp_path: Path) -> None:
    settings = MODULE.Settings(
        access_key="test-key-that-is-long-enough",
        public_base_url="https://relay.example",
        data_dir=tmp_path,
    )
    catalog = MODULE.Catalog(tmp_path / "catalog.json")
    app = MODULE.create_app(
        settings=settings,
        catalog=catalog,
        resolver=FakeResolver(),
        streams=FakeStreams(),
        hls_gateway=FakeHlsGateway(),
    )
    with TestClient(app) as client:
        assert client.get("/api/items").status_code == 401
        bad = client.post(
            "/api/items?key=test-key-that-is-long-enough",
            json={"urls": ["https://example.com/video"], "rights_confirmed": True},
        )
        assert bad.status_code == 400

        added = client.post(
            "/api/items?key=test-key-that-is-long-enough",
            json={
                "urls": ["https://www.youtube.com/watch?v=BaW_jenozKc"],
                "rights_confirmed": True,
            },
        )
        assert added.status_code == 200
        assert added.json()["ok"] is True

        playlist = client.get("/live.m3u?key=test-key-that-is-long-enough")
        assert playlist.status_code == 200
        assert "授權測試影片" in playlist.text
        assert (
            "https://relay.example/stream/BaW_jenozKc/index.m3u8"
            "?key=test-key-that-is-long-enough"
        ) in playlist.text

        deleted = client.delete(
            "/api/items/BaW_jenozKc?key=test-key-that-is-long-enough"
        )
        assert deleted.status_code == 200
        assert "授權測試影片" not in client.get(
            "/live.m3u?key=test-key-that-is-long-enough"
        ).text


def test_youtube_url_validation() -> None:
    assert MODULE.validate_youtube_url("https://youtu.be/BaW_jenozKc")
    for value in (
        "http://youtube.com/watch?v=x",
        "https://evil.example/video",
        "file:///etc/passwd",
    ):
        try:
            MODULE.validate_youtube_url(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted unsafe URL: {value}")


def test_hls_catalog_playlist_probe_and_delete(tmp_path: Path) -> None:
    settings = MODULE.Settings(
        access_key="test-key-that-is-long-enough",
        public_base_url="https://relay.example",
        data_dir=tmp_path,
    )
    catalog = MODULE.Catalog(tmp_path / "catalog.json")
    app = MODULE.create_app(
        settings=settings,
        catalog=catalog,
        resolver=FakeResolver(),
        streams=FakeStreams(),
        hls_gateway=FakeHlsGateway(),
    )
    with TestClient(app) as client:
        denied = client.post(
            "/api/hls-items?key=test-key-that-is-long-enough",
            json={
                "items": [
                    {"title": "授權頻道", "url": "https://media.example/live.m3u8"}
                ],
                "rights_confirmed": False,
            },
        )
        assert denied.status_code == 400

        added = client.post(
            "/api/hls-items?key=test-key-that-is-long-enough",
            json={
                "items": [
                    {"title": "授權頻道", "url": "https://media.example/live.m3u8"}
                ],
                "headers": {"referer": "https://media.example/"},
                "rights_confirmed": True,
            },
        )
        assert added.status_code == 200
        assert added.json()["results"][0]["ok"] is True

        playlist = client.get("/live.m3u?key=test-key-that-is-long-enough")
        assert "授權頻道" in playlist.text
        assert "授權 HLS 直播" in playlist.text
        assert (
            "https://relay.example/hls/hls-0123456789abcdef/manifest.m3u8"
            "?key=test-key-that-is-long-enough"
        ) in playlist.text

        probe = client.post(
            "/api/items/hls-0123456789abcdef/probe"
            "?key=test-key-that-is-long-enough"
        )
        assert probe.status_code == 200
        assert probe.json()["title"] == "授權頻道"

        deleted = client.delete(
            "/api/items/hls-0123456789abcdef?key=test-key-that-is-long-enough"
        )
        assert deleted.status_code == 200
        assert "授權頻道" not in client.get(
            "/live.m3u?key=test-key-that-is-long-enough"
        ).text


def test_hls_manifest_rewrite_and_signature(tmp_path: Path) -> None:
    settings = MODULE.Settings(
        access_key="test-key-that-is-long-enough",
        public_base_url="https://relay.example",
        data_dir=tmp_path,
    )
    body = (
        "#EXTM3U\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="keys/key.bin"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=1200000\n"
        "variants/main.m3u8?token=abc\n"
    )
    rewritten = MODULE.rewrite_hls_manifest(
        body,
        "https://cdn.example/root/master.m3u8",
        "hls-test",
        settings,
        settings.access_key,
    )
    urls = [
        part
        for line in rewritten.splitlines()
        for part in ([line] if line.startswith("https://") else [])
    ]
    assert len(urls) == 1
    query = parse_qs(urlsplit(urls[0]).query)
    decoded = MODULE._decode_hls_target(
        "hls-test", query["u"][0], query["sig"][0], settings.access_key
    )
    assert decoded == "https://cdn.example/root/variants/main.m3u8?token=abc"
    assert "keys%2Fkey.bin" not in rewritten
    assert rewritten.count("/hls/hls-test/asset?") == 2


def test_hls_validation_blocks_private_network() -> None:
    assert MODULE.validate_hls_url("https://media.example/live.m3u8")
    for value in ("http://media.example/live.m3u8", "file:///tmp/live.m3u8"):
        try:
            MODULE.validate_hls_url(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted unsafe HLS URL: {value}")
    try:
        asyncio.run(MODULE.ensure_public_url("https://127.0.0.1/live.m3u8"))
    except ValueError:
        pass
    else:
        raise AssertionError("accepted a private-network HLS URL")
