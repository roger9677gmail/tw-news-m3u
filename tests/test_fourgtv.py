from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from app.config import Channel
from app.fourgtv import (
    FourGTVError,
    _stream_urls,
    cache_from_client_responses,
    daily_auth,
    install_runtime_cache,
    refresh_plan,
    resolve_fourgtv,
)


def mapped_channel() -> Channel:
    return Channel(
        id="test-news",
        name="Test News",
        group="News",
        short_name="Test",
        sources=("https://www.youtube.com/@example/live",),
        fourgtv_channel_id="31",
        fourgtv_asset_id="litv-ftv13",
    )


def test_daily_auth_matches_public_ios_protocol() -> None:
    assert daily_auth(date(2026, 9, 3)) == (
        "bl13L74BMGfKslLIwMWWS4h14g+7vseJnT4C2SsSmO9RGkZiHYbLprz8F7YM3mgR"
        "Wld65bovoiY6yi6tn105bg=="
    )


def test_stream_urls_accepts_https_hls_values() -> None:
    assert _stream_urls(
        {
            "Success": True,
            "Data": {
                "flstURLs": [
                    "https://4gtvfreemobile-cds.cdn.hinet.net/live/index.m3u8",
                    "javascript:alert(1)",
                ]
            },
        }
    ) == ["https://4gtvfreemobile-cds.cdn.hinet.net/live/index.m3u8"]


def test_stream_urls_rejects_failed_api_response() -> None:
    with pytest.raises(FourGTVError, match="invalid params"):
        _stream_urls({"Success": False, "Message": "invalid params"})


def test_resolve_uses_mounted_secret_cache(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "streams.json"
    cache.write_text(
        json.dumps(
            {
                "channels": {
                    "test-news": {
                        "url": "https://4gtvfreemobile-cds.cdn.hinet.net/live/index.m3u8?expires=4102444800"
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FOURGTV_CACHE_FILE", str(cache))
    channel = mapped_channel()

    stream = asyncio.run(resolve_fourgtv(channel))

    assert stream.source == "4GTV 官方快取直播"
    assert stream.expires_at is not None
    assert stream.expires_at.year == 2100


def test_just_refreshed_runtime_cache_overrides_secret_mount(
    tmp_path, monkeypatch
) -> None:
    mounted = tmp_path / "streams.json"
    mounted.write_text(
        json.dumps(
            {
                "channels": {
                    "test-news": {
                        "url": "https://4gtvfreemobile-mozai.4gtv.tv/old/index.m3u8?expires=4102444800"
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FOURGTV_CACHE_FILE", str(mounted))
    fresh_url = (
        "https://4gtvfreemobile-cds.cdn.hinet.net/fresh/index.m3u8"
        "?expires=4102444800"
    )
    install_runtime_cache(
        {"channels": {"test-news": {"url": fresh_url}}}
    )
    try:
        stream = asyncio.run(resolve_fourgtv(mapped_channel()))
        assert stream.stream_url == fresh_url
        assert stream.source == "4GTV 官方快取直播"
    finally:
        install_runtime_cache({"channels": {}})


def test_refresh_plan_and_client_response_cache() -> None:
    channel = mapped_channel()
    plan = refresh_plan((channel,))
    request = plan["requests"][0]
    encryption_key = request["headers"]["fsENC_KEY"]

    assert request["channel_id"] == channel.id
    assert request["body"]["clsAPP_IDENTITY_VALIDATE_ARUS"]["fsENC_KEY"] == encryption_key

    cache = cache_from_client_responses(
        (channel,),
        {
            "responses": [
                {
                    "channel_id": channel.id,
                    "payload": {
                        "Success": True,
                        "Data": {
                            "flstURLs": [
                                "https://4gtvfreemobile-cds.cdn.hinet.net/live/index.m3u8?expires=4102444800",
                                "https://4gtvfreemobile-mozai.4gtv.tv/live/index.m3u8?expires=4102444800",
                            ]
                        },
                    },
                }
            ]
        },
    )

    assert cache["channels"][channel.id]["url"].startswith(
        "https://4gtvfreemobile-cds.cdn.hinet.net/"
    )
