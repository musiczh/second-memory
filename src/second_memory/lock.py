from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType


class RepoLock:
    def __init__(self, repo: Path) -> None:
        self.path = repo / ".kb" / "lock"
        self._handle = None

    def __enter__(self) -> "RepoLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handle:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
