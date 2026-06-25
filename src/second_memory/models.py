from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RawEntry:
    id: str
    title: str
    created: str
    event_date: str
    tags: list[str]
    path: Path
    body: str


@dataclass(frozen=True)
class Page:
    id: str
    type: str
    title: str
    summary: str
    path: Path
    sources: list[str] = field(default_factory=list)
    body: str = ""
    entity_kind: str | None = None
    aliases: list[str] = field(default_factory=list)
