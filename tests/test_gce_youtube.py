from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

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
