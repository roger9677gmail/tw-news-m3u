from __future__ import annotations

import time

import pytest

from app.hls import (
    MediaTokenStore,
    UnsafeUpstreamURL,
    rewrite_hls_manifest,
    validate_upstream_url,
)


def test_validate_upstream_url_allowlist() -> None:
    assert validate_upstream_url("https://manifest.googlevideo.com/api/manifest/hls_playlist/test")
    assert validate_upstream_url("https://rr1---sn.example.googlevideo.com/videoplayback?id=1")
    with pytest.raises(UnsafeUpstreamURL):
        validate_upstream_url("https://example.com/private")
    with pytest.raises(UnsafeUpstreamURL):
        validate_upstream_url("file:///etc/passwd")


def test_token_store_is_channel_scoped() -> None:
    store = MediaTokenStore(ttl_seconds=600, max_entries=100)
    url = f"https://manifest.googlevideo.com/live.m3u8?expire={int(time.time()) + 3600}"
    token = store.register("news-a", url, {"User-Agent": "test"})

    assert store.get("news-a", token) is not None
    assert store.get("news-b", token) is None
    assert len(store) == 1


def test_rewrite_manifest_rewrites_lines_and_uri_attributes() -> None:
    manifest = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",URI=\"audio/index.m3u8\"
#EXT-X-KEY:METHOD=AES-128,URI=\"keys/live.key\"
#EXT-X-STREAM-INF:BANDWIDTH=2200000,RESOLUTION=1280x720
video/index.m3u8
"""
    store = MediaTokenStore(ttl_seconds=600, max_entries=100)

    rewritten = rewrite_hls_manifest(
        manifest,
        manifest_url="https://manifest.googlevideo.com/hls/master.m3u8",
        channel_id="tvbs-news",
        headers={"User-Agent": "test"},
        token_store=store,
        proxy_url=lambda channel, token: f"https://relay.example/media/{channel}/{token}?key=secret",
    ).decode()

    assert rewritten.startswith("#EXTM3U\n")
    assert "audio/index.m3u8" not in rewritten
    assert "keys/live.key" not in rewritten
    assert "video/index.m3u8" not in rewritten
    assert rewritten.count("https://relay.example/media/tvbs-news/") == 3
    assert len(store) == 3


def test_rewrite_manifest_does_not_proxy_untrusted_host() -> None:
    manifest = b"#EXTM3U\nhttps://evil.example/segment.ts\n"
    store = MediaTokenStore(ttl_seconds=600)
    rewritten = rewrite_hls_manifest(
        manifest,
        manifest_url="https://manifest.googlevideo.com/master.m3u8",
        channel_id="news",
        headers={},
        token_store=store,
        proxy_url=lambda channel, token: f"/media/{channel}/{token}",
    ).decode()
    assert "https://evil.example/segment.ts" in rewritten
    assert len(store) == 0


def test_rewrite_master_manifest_limits_video_height_but_keeps_audio() -> None:
    manifest = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,AUDIO="audio"
video-720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,AUDIO="audio"
video-1080.m3u8
"""
    store = MediaTokenStore(ttl_seconds=600)
    rewritten = rewrite_hls_manifest(
        manifest,
        manifest_url="https://manifest.googlevideo.com/master.m3u8",
        channel_id="news",
        headers={},
        token_store=store,
        proxy_url=lambda channel, token: f"/media/{channel}/{token}",
        max_height=720,
    ).decode()

    assert "RESOLUTION=1280x720" in rewritten
    assert "RESOLUTION=1920x1080" not in rewritten
    assert "video-720.m3u8" not in rewritten
    assert "video-1080.m3u8" not in rewritten
    assert rewritten.count("/media/news/") == 2
