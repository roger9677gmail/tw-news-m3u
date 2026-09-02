from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ResolvedStream:
    channel_id: str
    source: str
    stream_url: str
    webpage_url: str
    title: str
    video_id: str
    protocol: str
    height: int | None
    headers: Mapping[str, str]
    resolved_at: datetime
    expires_at: datetime | None


@dataclass(slots=True)
class ResolverStatus:
    state: str = "idle"
    title: str | None = None
    height: int | None = None
    source: str | None = None
    webpage_url: str | None = None
    resolved_at: datetime | None = None
    expires_at: datetime | None = None
    error: str | None = None
    attempted_at: datetime | None = None
    cached_until: datetime | None = None
    extra: dict[str, str] = field(default_factory=dict)
