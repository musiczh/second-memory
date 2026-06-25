from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Storage(Protocol):
    repo: Path

    def add_paths(self, paths: list[str]) -> None: ...

    def commit_all(self, message: str) -> str | None: ...

    def current_commit(self) -> str | None: ...
