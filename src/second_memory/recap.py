from __future__ import annotations

from pathlib import Path
from typing import Any

from . import frontmatter
from .compiler import list_compiled_pages, raw_lookup, read_text
from .config import load_config
from .models import Page, RawEntry
from .promptio import llm_request, recap_response_schema
from .utils import parse_date

# How many related history pages to surface so the request stays compact.
MAX_RELATED_PAGES = 8


def build_recap_request(repo: Path, focus_raw_ids: list[str], updated_pages: list[str]) -> dict[str, Any] | None:
    """Build an LLM request that reviews freshly saved content against the user's history.

    Returns None when there is no focus content (nothing was just saved).
    """
    load_config(repo)
    lookup = raw_lookup(repo)
    focus_entries = [lookup[raw_id] for raw_id in focus_raw_ids if raw_id in lookup]
    if not focus_entries:
        return None

    focus_set = set(focus_raw_ids)
    terms = focus_terms(focus_entries)
    pages = list_compiled_pages(repo)

    related = related_pages(repo, pages, focus_set, terms)
    on_this_day = on_this_day_pages(repo, pages, focus_entries)

    context = {
        "focus": [focus_payload(entry) for entry in focus_entries],
        "updated_pages": sorted(set(updated_pages)),
        "related_history": related,
        "on_this_day": on_this_day,
        "history_available": bool(related or on_this_day),
    }
    instructions = (
        "用户刚刚把 focus 中的内容存入第二记忆库。请基于 related_history 与 on_this_day 给用户做一次回顾与关联总结，"
        "而不仅仅确认入库：指出本次记录与历史记录的关联、反复出现的主题/情绪/项目，并给出可行动建议或值得追问的问题。"
        "只能引用提供的页面与原料，不要臆造历史；若 history_available 为 false，就说明这是相关主题的首次记录，"
        "给出一句轻量回顾与后续可关注的方向。"
    )
    return llm_request(
        "recap",
        read_text(repo / "AGENTS.md"),
        context,
        instructions,
        recap_response_schema(),
    )


def focus_terms(entries: list[RawEntry]) -> set[str]:
    terms: set[str] = set()
    for entry in entries:
        for token in entry.title.split():
            if token:
                terms.add(token.lower())
        for tag in entry.tags:
            if tag:
                terms.add(tag.lower())
    return terms


def related_pages(repo: Path, pages: list[Page], focus_set: set[str], terms: set[str]) -> list[dict[str, Any]]:
    scored: list[tuple[int, Page, list[str]]] = []
    for page in pages:
        if page.type not in {"entity", "event", "statement", "topic"}:
            continue
        meta, _ = frontmatter.read_document(page.path)
        prior_sources = [src for src in page.sources if src not in focus_set]
        created = str(meta.get("created") or "")
        updated = str(meta.get("updated") or "")
        # A page is history if it existed before this save: either it still carries
        # earlier sources, or its created timestamp predates this round's update.
        # (Incremental compile may overwrite an entity's sources with only the new raw,
        # so the timestamp gap is the reliable signal.)
        pre_existing = bool(prior_sources) or (created and updated and created != updated)
        if not pre_existing:
            continue
        score = 0
        if focus_set & set(page.sources):
            score += 5
        haystack = " ".join([page.title, page.summary, " ".join(page.aliases), page.body]).lower()
        score += sum(3 if term in page.title.lower() else 1 for term in terms if term in haystack)
        if score:
            scored.append((score, page, sorted(prior_sources)))
    scored.sort(key=lambda item: (-item[0], item[1].id))
    return [
        {
            "id": page.id,
            "type": page.type,
            "title": page.title,
            "summary": page.summary,
            "prior_sources": prior_sources,
            "body": page.body,
        }
        for _, page, prior_sources in scored[:MAX_RELATED_PAGES]
    ]


def on_this_day_pages(repo: Path, pages: list[Page], focus_entries: list[RawEntry]) -> list[dict[str, Any]]:
    timeline = repo / "wiki" / "timeline"
    if not timeline.exists():
        return []
    focus_dates = {entry.event_date for entry in focus_entries if entry.event_date}
    month_days = {entry.event_date[5:] for entry in focus_entries if len(entry.event_date) >= 10}
    results: list[dict[str, Any]] = []
    for page in pages:
        if page.type != "timeline":
            continue
        meta, body = frontmatter.read_document(page.path)
        event_date = str(meta.get("event_date", ""))
        if not event_date or event_date in focus_dates:
            continue  # skip today's just-written timeline; we want prior years/days
        if event_date[5:] in month_days:
            results.append({
                "id": page.id,
                "event_date": event_date,
                "sources": list(meta.get("sources", [])),
                "body": body,
            })
    results.sort(key=lambda item: str(item["event_date"]))
    return results


def focus_payload(entry: RawEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "title": entry.title,
        "event_date": entry.event_date,
        "tags": entry.tags,
        "body": entry.body,
    }
