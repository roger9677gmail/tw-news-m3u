#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_channels  # noqa: E402
from app.fourgtv import resolve_fourgtv  # noqa: E402


async def build_cache() -> dict[str, object]:
    channels: dict[str, dict[str, str | None]] = {}
    for channel in load_channels(ROOT / "channels.json"):
        if not channel.fourgtv_channel_id:
            continue
        stream = await resolve_fourgtv(channel)
        channels[channel.id] = {
            "url": stream.stream_url,
            "expires_at": stream.expires_at.isoformat() if stream.expires_at else None,
        }
        print(f"resolved {channel.id}", file=sys.stderr)
    if not channels:
        raise RuntimeError("沒有建立任何 4GTV 頻道快取")
    return {
        "generated_at": datetime.now(tz=UTC).replace(microsecond=0).isoformat(),
        "channels": channels,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    content = json.dumps(asyncio.run(build_cache()), ensure_ascii=False, separators=(",", ":"))
    if args.output:
        args.output.write_text(content, encoding="utf-8")
        print(f"wrote {len(json.loads(content)['channels'])} channels", file=sys.stderr)
    else:
        print(content)


if __name__ == "__main__":
    main()
