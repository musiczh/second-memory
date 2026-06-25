from __future__ import annotations

import json
from pathlib import Path

from ..utils import now_local, short_hash


class PlainFSStorage:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def add_paths(self, paths: list[str]) -> None:
        return None

    def current_commit(self) -> str | None:
        return None

    def commit_all(self, message: str) -> str | None:
        history = self.repo / ".kb" / "history.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "id": short_hash(message, now_local().isoformat()),
            "created": now_local().isoformat(),
            "message": message,
        }
        with history.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry["id"]
