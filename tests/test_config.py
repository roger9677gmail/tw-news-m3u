from __future__ import annotations

import json

import pytest

from app.config import load_channels


def test_load_channels_parses_experimental_hls(tmp_path) -> None:
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "test-news",
                    "name": "Test News",
                    "group": "News",
                    "sources": ["https://www.youtube.com/@example/live"],
                    "experimental_hls": [
                        {
                            "name": "Public list",
                            "url": "http://4gtv.cnlive.club/channel/test/index.m3u8",
                            "source_page": "https://github.com/example/list",
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    channel = load_channels(path)[0]

    assert channel.experimental_hls[0].name == "Public list"
    assert channel.experimental_hls[0].url.endswith("/index.m3u8")
    assert channel.experimental_hls[0].source_page.startswith("https://github.com/")


def test_load_channels_requires_experimental_source_page(tmp_path) -> None:
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "test-news",
                    "name": "Test News",
                    "group": "News",
                    "sources": ["https://www.youtube.com/@example/live"],
                    "experimental_hls": [
                        {
                            "name": "Untraceable",
                            "url": "http://4gtv.cnlive.club/channel/test/index.m3u8",
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="缺少 source_page"):
        load_channels(path)
