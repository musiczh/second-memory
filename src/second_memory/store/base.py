from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Storage(Protocol):
    repo: Path

    def commit_paths(self, message: str, paths: list[str]) -> str | None: ...

    def current_commit(self) -> str | None: ...
