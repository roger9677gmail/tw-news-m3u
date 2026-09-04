from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Channel, Settings
from app.karaoke import KaraokeSong
from app.main import _media_content_type, _proxy_media_url, create_app


def settings(tmp_path: Path, *, key: str = "test-secret") -> Settings:
    return Settings(
        app_name="測試新聞 M3U",
        channels_path=tmp_path / "channels.json",
        access_key=key,
        public_base_url="https://relay.example",
        max_height=720,
        resolver_ttl_seconds=900,
        resolver_failure_ttl_seconds=90,
        resolver_timeout_seconds=60,
        resolver_max_concurrency=2,
        media_token_ttl_seconds=21600,
        upstream_timeout_seconds=25.0,
        max_token_entries=30000,
        log_level="WARNING",
    )


def channels() -> tuple[Channel, ...]:
    return (
        Channel(
            id="test-news",
            name="測試新聞",
            group="綜合新聞",
            short_name="測試",
            sources=("https://www.youtube.com/@example/live",),
        ),
    )


def test_health_and_config_do_not_require_key(tmp_path: Path) -> None:
    app = create_app(settings=settings(tmp_path), channels=channels())
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["channels"] == 1

        config = client.get("/api/config")
        assert config.status_code == 200
        assert config.json()["access_required"] is True
        assert config.json()["public_base_url"] == "https://relay.example"


def test_experimental_masked_segment_is_forwarded_as_mpeg_ts() -> None:
    import httpx

    request = httpx.Request(
        "GET", "http://4gtv.cnlive.club/ts/channel.123.jpeg"
    )
    response = httpx.Response(
        206,
        headers={"Content-Type": "text/html; charset=UTF-8"},
        request=request,
    )

    assert _media_content_type(response) == "video/mp2t"


def test_experimental_masked_segment_proxy_url_uses_ts_extension() -> None:
    url = _proxy_media_url(
        "https://relay.example",
        "secret",
        "tvbs-news",
        "media-token",
        "http://4gtv.cnlive.club/ts/channel.123.jpeg",
    )

    assert "/media/tvbs-news/media-token.ts?" in url


def test_ts_segment_with_incorrect_text_type_is_forwarded_as_mpeg_ts() -> None:
    import httpx

    request = httpx.Request("GET", "http://38.64.72.148/hls/news/segment.ts")
    response = httpx.Response(
        206,
        headers={"Content-Type": "text/vnd.trolltech.linguist"},
        request=request,
    )

    assert _media_content_type(response) == "video/mp2t"


def test_dashboard_supports_batch_mp4_selection(tmp_path: Path) -> None:
    app = create_app(settings=settings(tmp_path), channels=channels())
    with TestClient(app) as client:
        page = client.get("/")
        script = client.get("/static/app.js")

        assert page.status_code == 200
        assert 'id="karaoke-file" type="file" accept="video/mp4,.mp4" multiple' in page.text
        assert 'id="karaoke-title-input"' not in page.text
        assert "Array.from(els.karaokeFile.files || [])" in script.text
        assert "titleFromFileName(file.name)" in script.text


def test_m3u_requires_key_and_contains_fixed_relay_url(tmp_path: Path) -> None:
    app = create_app(settings=settings(tmp_path), channels=channels())
    with TestClient(app) as client:
        assert client.get("/live.m3u").status_code == 401

        response = client.get("/live.m3u?key=test-secret")
        assert response.status_code == 200
        assert response.text.startswith("#EXTM3U")
        assert '#EXTINF:-1 tvg-id="test-news"' in response.text
        assert (
            "https://relay.example/hls/test-news/master.m3u8?key=test-secret"
            in response.text
        )


def test_status_requires_key(tmp_path: Path) -> None:
    app = create_app(settings=settings(tmp_path), channels=channels())
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401
        response = client.get("/api/status?key=test-secret")
        assert response.status_code == 200
        assert response.json()["channels"][0]["state"] == "idle"


def test_key_is_optional_when_not_configured(tmp_path: Path) -> None:
    app = create_app(settings=settings(tmp_path, key=""), channels=channels())
    with TestClient(app) as client:
        response = client.get("/live.m3u")
        assert response.status_code == 200
        assert "?key=" not in response.text


def test_full_manifest_and_segment_relay(tmp_path: Path) -> None:
    import asyncio
    import gzip
    import urllib.parse
    from datetime import UTC, datetime, timedelta

    import httpx

    from app.models import ResolvedStream, ResolverStatus

    channel_set = channels()

    class FakeResolver:
        def __init__(self) -> None:
            self.invalidated = False
            self.status = ResolverStatus(state="online")

        def channel(self, channel_id: str) -> Channel:
            if channel_id != "test-news":
                raise KeyError(channel_id)
            return channel_set[0]

        async def resolve(self, channel_id: str, *, force: bool = False) -> ResolvedStream:
            now = datetime.now(tz=UTC)
            return ResolvedStream(
                channel_id=channel_id,
                source=channel_set[0].sources[0],
                stream_url="https://manifest.googlevideo.com/master.m3u8",
                webpage_url=channel_set[0].sources[0],
                title="測試直播",
                video_id="abc123",
                protocol="m3u8_native",
                height=720,
                headers={"User-Agent": "relay-test"},
                resolved_at=now,
                expires_at=now + timedelta(hours=1),
            )

        def invalidate(self, channel_id: str) -> None:
            self.invalidated = True

        def status_snapshot(self) -> dict[str, ResolverStatus]:
            return {"test-news": self.status}

    segment_bytes = b"\x00\x01fake-mpeg-ts-data"
    compressed_segment = gzip.compress(segment_bytes)

    class StaticAsyncStream(httpx.AsyncByteStream):
        def __init__(self, content: bytes) -> None:
            self.content = content

        async def __aiter__(self):
            yield self.content

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("user-agent") == "relay-test"
        assert request.headers.get("accept-encoding") == "identity"
        path = request.url.path
        if path == "/master.m3u8":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/vnd.apple.mpegurl"},
                stream=StaticAsyncStream(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nvariant.m3u8\n"),
                request=request,
            )
        if path == "/variant.m3u8":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/x-mpegURL"},
                stream=StaticAsyncStream(b"#EXTM3U\n#EXTINF:4.0,\nsegment-001.ts\n"),
                request=request,
            )
        if path == "/segment-001.ts":
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "video/mp2t",
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(compressed_segment)),
                },
                stream=StaticAsyncStream(compressed_segment),
                request=request,
            )
        return httpx.Response(404, request=request)

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        settings=settings(tmp_path),
        channels=channel_set,
        resolver=FakeResolver(),  # type: ignore[arg-type]
        http_client=async_client,
    )

    with TestClient(app) as client:
        master = client.get("/hls/test-news/master.m3u8?key=test-secret")
        assert master.status_code == 200
        assert "manifest.googlevideo.com" not in master.text
        master_media_url = next(
            line for line in master.text.splitlines() if line.startswith("https://relay.example/media/")
        )

        parsed_master = urllib.parse.urlparse(master_media_url)
        assert parsed_master.path.endswith(".m3u8")
        variant = client.get(parsed_master.path + "?" + parsed_master.query)
        assert variant.status_code == 200
        assert "segment-001.ts" not in variant.text
        segment_url = next(
            line for line in variant.text.splitlines() if line.startswith("https://relay.example/media/")
        )

        parsed_segment = urllib.parse.urlparse(segment_url)
        assert parsed_segment.path.endswith(".ts")
        segment = client.get(parsed_segment.path + "?" + parsed_segment.query)
        assert segment.status_code == 200
        assert segment.content == segment_bytes
        assert segment.headers["content-type"].startswith("video/mp2t")

    asyncio.run(async_client.aclose())


def test_master_reselects_source_after_any_upstream_error(tmp_path: Path) -> None:
    import asyncio
    from datetime import UTC, datetime

    import httpx

    from app.models import ResolvedStream, ResolverStatus

    channel_set = channels()

    class FailoverResolver:
        def __init__(self) -> None:
            self.invalidated = False
            self.calls: list[bool] = []

        def channel(self, channel_id: str) -> Channel:
            if channel_id != "test-news":
                raise KeyError(channel_id)
            return channel_set[0]

        async def resolve(self, channel_id: str, *, force: bool = False) -> ResolvedStream:
            self.calls.append(force)
            now = datetime.now(tz=UTC)
            path = "working.m3u8" if force else "failed.m3u8"
            return ResolvedStream(
                channel_id=channel_id,
                source="test",
                stream_url=f"https://manifest.googlevideo.com/{path}",
                webpage_url="https://www.youtube.com/@example/live",
                title="Test",
                video_id="test",
                protocol="m3u8_native",
                height=720,
                headers={"User-Agent": "test"},
                resolved_at=now,
                expires_at=None,
            )

        def invalidate(self, channel_id: str) -> None:
            self.invalidated = True

        def status_snapshot(self) -> dict[str, ResolverStatus]:
            return {"test-news": ResolverStatus()}

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/failed.m3u8":
            return httpx.Response(502, content=b"temporary failure", request=request)
        if request.url.path == "/working.m3u8":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/vnd.apple.mpegurl"},
                content=b"#EXTM3U\n#EXTINF:4,\nsegment.ts\n",
                request=request,
            )
        return httpx.Response(404, request=request)

    resolver = FailoverResolver()
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        settings=settings(tmp_path),
        channels=channel_set,
        resolver=resolver,  # type: ignore[arg-type]
        http_client=async_client,
    )

    with TestClient(app) as client:
        response = client.get("/hls/test-news/master.m3u8?key=test-secret")

    assert response.status_code == 200
    assert response.text.startswith("#EXTM3U")
    assert resolver.invalidated is True
    assert resolver.calls == [False, True]
    asyncio.run(async_client.aclose())


def test_direct_tubo_import_uses_custom_app_scheme(tmp_path: Path) -> None:
    import urllib.parse

    app = create_app(settings=settings(tmp_path), channels=channels())
    with TestClient(app) as client:
        response = client.get(
            "/import-to-tubo?key=test-secret", follow_redirects=False
        )
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("tubo://import?")
        parsed = urllib.parse.urlparse(location)
        query = urllib.parse.parse_qs(parsed.query)
        assert query["url"] == ["https://relay.example/live.m3u?key=test-secret"]
        assert query["name"] == ["測試新聞 M3U"]


def test_karaoke_upload_playlist_playback_and_delete(tmp_path: Path) -> None:
    class FakeKaraokeStore:
        enabled = True

        def __init__(self) -> None:
            self.songs: list[KaraokeSong] = []

        async def list_songs(self) -> list[KaraokeSong]:
            return list(self.songs)

        async def create_upload(
            self, *, file_name: str, size_bytes: int, origin: str
        ) -> dict[str, str]:
            assert file_name == "sample.mp4"
            assert size_bytes == 1234
            assert origin == "https://relay.example"
            return {
                "upload_id": "a" * 24,
                "upload_url": "https://storage.example/upload/session",
            }

        async def complete_upload(
            self, *, upload_id: str, title: str, file_name: str
        ) -> KaraokeSong:
            assert upload_id == "a" * 24
            song = KaraokeSong(
                id=upload_id,
                title=title,
                original_file=file_name,
                size_bytes=1234,
                created_at="2026-09-03T12:00:00+00:00",
            )
            self.songs = [song]
            return song

        async def delete_song(self, song_id: str) -> bool:
            before = len(self.songs)
            self.songs = [song for song in self.songs if song.id != song_id]
            return len(self.songs) != before

        async def read_asset(
            self, song_id: str, asset_name: str
        ) -> tuple[bytes, str]:
            assert song_id == "a" * 24
            if asset_name == "index.m3u8":
                return (
                    b"#EXTM3U\n#EXTINF:6.0,\nsegment-00000.ts\n#EXT-X-ENDLIST\n",
                    "application/vnd.apple.mpegurl",
                )
            assert asset_name == "segment-00000.ts"
            return b"fake-karaoke-segment", "video/mp2t"

    store = FakeKaraokeStore()
    app = create_app(
        settings=settings(tmp_path),
        channels=channels(),
        karaoke_store=store,
    )
    with TestClient(app) as client:
        assert client.get("/api/karaoke/songs").status_code == 401

        rights_missing = client.post(
            "/api/karaoke/uploads?key=test-secret",
            json={"file_name": "sample.mp4", "size_bytes": 1234},
        )
        assert rights_missing.status_code == 400

        upload = client.post(
            "/api/karaoke/uploads?key=test-secret",
            json={
                "file_name": "sample.mp4",
                "size_bytes": 1234,
                "rights_confirmed": True,
            },
        )
        assert upload.status_code == 200
        assert upload.json()["upload_id"] == "a" * 24

        completed = client.post(
            f"/api/karaoke/uploads/{'a' * 24}/complete?key=test-secret",
            json={"title": "KTV 系統測試", "file_name": "sample.mp4"},
        )
        assert completed.status_code == 200
        assert completed.json()["song"]["title"] == "KTV 系統測試"

        playlist = client.get("/live.m3u?key=test-secret")
        assert 'group-title="KTV 點歌"' in playlist.text
        assert (
            f"https://relay.example/karaoke/{'a' * 24}/index.m3u8?key=test-secret"
            in playlist.text
        )

        manifest = client.get(
            f"/karaoke/{'a' * 24}/index.m3u8?key=test-secret"
        )
        assert manifest.status_code == 200
        assert "segment-00000.ts" in manifest.text
        assert (
            f"https://relay.example/karaoke/{'a' * 24}/segment-00000.ts?key=test-secret"
            in manifest.text
        )

        segment = client.get(
            f"/karaoke/{'a' * 24}/segment-00000.ts?key=test-secret"
        )
        assert segment.status_code == 200
        assert segment.content == b"fake-karaoke-segment"
        assert segment.headers["content-type"].startswith("video/mp2t")

        deleted = client.delete(
            f"/api/karaoke/songs/{'a' * 24}?key=test-secret"
        )
        assert deleted.status_code == 200
        assert store.songs == []
