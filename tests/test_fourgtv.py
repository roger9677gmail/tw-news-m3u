from __future__ import annotations

from datetime import date

import pytest

from app.fourgtv import FourGTVError, _stream_urls, daily_auth


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
