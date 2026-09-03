from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from app.config import Channel
from app.fourgtv import FourGTVError, _stream_urls, daily_auth, resolve_fourgtv


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
    channel = Channel(
        id="test-news",
        name="Test News",
        group="News",
        short_name="Test",
        sources=("https://www.youtube.com/@example/live",),
        fourgtv_channel_id="31",
        fourgtv_asset_id="litv-ftv13",
    )

    stream = asyncio.run(resolve_fourgtv(channel))

    assert stream.source == "4GTV 官方快取直播"
    assert stream.expires_at is not None
    assert stream.expires_at.year == 2100
