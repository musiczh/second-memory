from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from . import frontmatter
from .config import load_config
from .promptio import llm_request, review_response_schema
from .utils import date_range_last_week, parse_date


def review_request(
    repo: Path,
    *,
    range_name: str | None = None,
    on_this_day: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    config = load_config(repo)
    pages = collect_timeline_pages(repo, range_name=range_name, on_this_day=on_this_day, start_date=start_date, end_date=end_date, max_days=int(config.get("review_max_days", 7)))
    instructions = "请根据 timeline_pages 做结构化回顾，指出反复出现的主题、情绪、项目和可行动建议。"
    return llm_request(
        "review",
        (repo / "AGENTS.md").read_text(encoding="utf-8"),
        {"timeline_pages": pages},
        instructions,
        review_response_schema(),
    )


def collect_timeline_pages(
    repo: Path,
    *,
    range_name: str | None,
    on_this_day: str | None,
    start_date: str | None,
    end_date: str | None,
    max_days: int,
) -> list[dict[str, Any]]:
    timeline = repo / "wiki" / "timeline"
    if not timeline.exists():
        return []
    if on_this_day:
        target = parse_date(on_this_day)
        return [page_payload(repo, path) for path in sorted(timeline.glob(f"*-{target:%m-%d}.md"))]
    start, end = resolve_range(range_name, start_date, end_date)
    if (end - start).days + 1 > max_days:
        raise ValueError(f"review range exceeds {max_days} days")
    pages = []
    for path in sorted(timeline.glob("*.md")):
        meta, _ = frontmatter.read_document(path)
        event = parse_date(str(meta.get("event_date")))
        if start <= event <= end:
            pages.append(page_payload(repo, path))
    return pages


def resolve_range(range_name: str | None, start_date: str | None, end_date: str | None) -> tuple[date, date]:
    if range_name in {None, "last-week"} and not (start_date or end_date):
        return date_range_last_week()
    if start_date and end_date:
        start = parse_date(start_date)
        end = parse_date(end_date)
        if start > end:
            raise ValueError("start-date must be <= end-date")
        return start, end
    raise ValueError("use --range last-week, --on-this-day, or --start-date with --end-date")


def page_payload(repo: Path, path: Path) -> dict[str, Any]:
    meta, body = frontmatter.read_document(path)
    return {
        "id": meta.get("id"),
        "event_date": meta.get("event_date"),
        "sources": meta.get("sources", []),
        "path": path.relative_to(repo).as_posix(),
        "body": body,
    }
