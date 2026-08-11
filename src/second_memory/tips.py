from __future__ import annotations

from collections.abc import Iterable


TIPS: tuple[dict[str, str], ...] = (
    {
        "id": "scheduled-capture",
        "text": "可以使用定时任务，每天自动总结聊天中你认为重要的内容并存进第二记忆，增强 AI 记忆。",
    },
    {
        "id": "scheduled-review",
        "text": "可以使用定时任务，定时做总结、回顾，让你的记忆能够回响。",
    },
)


def next_tip(seen_ids: Iterable[str]) -> tuple[dict[str, str] | None, list[str]]:
    """Return the next unseen tip and the manifest state to persist with the apply."""
    seen = {str(value) for value in seen_ids}
    for tip in TIPS:
        if tip["id"] not in seen:
            return dict(tip), sorted({*seen, tip["id"]})
    return None, sorted(seen)
