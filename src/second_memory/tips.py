from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

# Usage tips surfaced through the CLI envelope so the host agent can pass a short
# suggestion on to the user. Each tip has a stable id; once shown it is recorded in
# .kb/tips.json and never shown again. When every tip has been seen, no tip is emitted.
TIPS: list[dict[str, str]] = [
    {
        "id": "scheduled-capture",
        "text": "可以使用定时任务，每天自动总结聊天中你认为重要的内容并存进第二记忆，增强 AI 记忆。",
    },
    {
        "id": "scheduled-review",
        "text": "可以使用定时任务，定时做总结、回顾，让你的记忆能够回响。",
    },
]


def _tips_path(repo: Path) -> Path:
    return repo / ".kb" / "tips.json"


def _read_seen(repo: Path) -> set[str]:
    path = _tips_path(repo)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("seen", []))
    except (ValueError, OSError):
        # A missing or corrupt state file just means nothing has been shown yet.
        return set()


def _write_seen(repo: Path, seen: set[str]) -> None:
    path = _tips_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seen": sorted(seen)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def next_tip(repo: Path) -> dict[str, Any] | None:
    """Return a random not-yet-shown tip and mark it seen, or None when all are shown."""
    seen = _read_seen(repo)
    remaining = [tip for tip in TIPS if tip["id"] not in seen]
    if not remaining:
        return None
    tip = random.choice(remaining)
    _write_seen(repo, seen | {tip["id"]})
    return tip
