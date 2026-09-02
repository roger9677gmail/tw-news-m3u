#!/usr/bin/env python3
"""Run the static generator with additional YouTube player-client fallbacks."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Channel, load_settings
from app.resolver import YouTubeResolver
from scripts import generate_static_m3u as generator


PROFILES = (
    "android_vr",
    "tv",
    "web_embedded",
    "ios",
    "web_safari",
    "default",
)


class StaticResolver(YouTubeResolver):
    def _command(self, source: str, profile: str) -> list[str]:
        if profile in {"default", "web_safari"}:
            return super()._command(source, profile)
        command = super()._command(source, "default")
        command[-1:-1] = ["--extractor-args", f"youtube:player_client={profile}"]
        return command


async def _resolve_one(resolver: StaticResolver, channel: Channel) -> generator.ChannelResult:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + resolver.settings.resolver_timeout_seconds
    errors: list[str] = []

    for source in channel.sources:
        for profile in PROFILES:
            remaining = deadline - loop.time()
            if remaining <= 1:
                errors.append("channel resolution deadline reached")
                break
            try:
                stream = await resolver._extract(
                    channel,
                    source,
                    profile,
                    timeout_seconds=min(30.0, remaining),
                )
                return generator.ChannelResult(channel=channel, stream=stream)
            except Exception as exc:
                errors.append(f"{source} [{profile}]: {generator._one_line(exc, 240)}")

    return generator.ChannelResult(
        channel=channel,
        stream=None,
        error=generator._one_line(" || ".join(errors), 900),
    )


async def resolve_all(channels: tuple[Channel, ...]) -> list[generator.ChannelResult]:
    resolver = StaticResolver(load_settings(), channels)
    return list(await asyncio.gather(*(_resolve_one(resolver, channel) for channel in channels)))


generator.resolve_all = resolve_all
raise SystemExit(generator.main())
