from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Channel, Settings
from app.models import ResolvedStream
from app.resolver import YouTubeResolver, _last_json_object


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_name="test",
        channels_path=tmp_path / "channels.json",
        access_key="",
        public_base_url="",
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


def make_channel() -> Channel:
    return Channel(
        id="test-news",
        name="Test News",
        group="News",
        short_name="Test",
        sources=("https://www.youtube.com/@example/live",),
    )


def test_command_requests_hls_and_deno(tmp_path: Path) -> None:
    channel = make_channel()
    resolver = YouTubeResolver(make_settings(tmp_path), (channel,))
    command = resolver._command(channel.sources[0], "default")
    joined = " ".join(command)

    assert "--js-runtimes deno" in joined
    assert "height<=720" in joined
    assert "protocol^=m3u8" in joined
    assert "--no-playlist" in command
    assert "--remote-components" not in command


def test_last_json_object_ignores_non_json_lines() -> None:
    result = _last_json_object('warning\n{"id":"abc","is_live":true}\n')
    assert result["id"] == "abc"


def test_resolver_reuses_cached_result(tmp_path: Path) -> None:
    channel = make_channel()
    resolver = YouTubeResolver(make_settings(tmp_path), (channel,))
    calls = 0

    async def fake_extract(*args, **kwargs) -> ResolvedStream:
        nonlocal calls
        calls += 1
        now = datetime.now(tz=UTC)
        return ResolvedStream(
            channel_id=channel.id,
            source=channel.sources[0],
            stream_url="https://manifest.googlevideo.com/master.m3u8",
            webpage_url=channel.sources[0],
            title="Live",
            video_id="abc",
            protocol="m3u8_native",
            height=720,
            headers={"User-Agent": "test"},
            resolved_at=now,
            expires_at=now + timedelta(hours=1),
        )

    resolver._extract = fake_extract  # type: ignore[method-assign]

    async def scenario() -> None:
        first = await resolver.resolve(channel.id)
        second = await resolver.resolve(channel.id)
        assert first is second

    asyncio.run(scenario())
    assert calls == 1
