from __future__ import annotations

from pathlib import Path
from typing import Any

from .compiler import list_index_pages, load_manifest, read_raw_by_path, read_text
from .config import load_config
from .models import NODE_TYPES
from .promptio import llm_request, search_response_schema
from .search import phrase_coverage_bonus, query_terms, rg_hits


def search_level1(repo: Path, query: str) -> dict[str, Any]:
    load_config(repo)
    terms = query_terms(query)
    candidates = []
    manifest_pages = load_manifest(repo).get("pages", {})
    compact_rows = [
        {
            "id": str(node_id),
            "type": str(value.get("type", "")),
            "title": str(value.get("title", node_id)),
            "summary": str(value.get("summary", "")),
            "path": str(value.get("path", "")),
            "aliases": [str(alias) for alias in value.get("aliases", [])],
            "sources": [str(source) for source in value.get("sources", [])],
        }
        for node_id, value in manifest_pages.items()
        if (
            isinstance(value, dict)
            and str(value.get("type", "")) in NODE_TYPES
            and value.get("title")
            and value.get("path")
        )
    ]
    if not compact_rows:
        compact_rows = [
            {
                "id": page.id,
                "type": page.type,
                "title": page.title,
                "summary": page.summary,
                "path": page.path.relative_to(repo).as_posix(),
                "aliases": page.aliases,
                "sources": page.sources,
            }
            for page in list_index_pages(repo)
        ]
    for page in compact_rows:
        haystack = " ".join([page["id"], page["title"], page["summary"], " ".join(page["aliases"])]).lower()
        score = sum(3 if term in page["title"].lower() else 1 for term in terms if term in haystack)
        score += phrase_coverage_bonus(query, page["title"], haystack)
        if score:
            candidates.append({
                "id": page["id"],
                "type": page["type"],
                "title": page["title"],
                "summary": page["summary"],
                "path": page["path"],
                "score": score,
                "sources": page["sources"],
            })
    candidates.sort(key=lambda item: (-int(item["score"]), str(item["id"])))
    return {"query": query, "candidates": candidates[:10], "hits": rg_hits(repo, query)}


def search_level2_request(repo: Path, query: str) -> dict[str, Any]:
    l1 = search_level1(repo, query)
    pages = []
    for candidate in l1["candidates"][:5]:
        path = repo / str(candidate["path"])
        pages.append({**candidate, "body": read_text(path)})
    manifest_raw = load_manifest(repo).get("raw_hashes", {})
    source_ids = []
    for candidate in l1["candidates"][:5]:
        source_ids.extend(candidate.get("sources", []))
    snippets = []
    for raw_id in list(dict.fromkeys(source_ids))[:5]:
        info = manifest_raw.get(str(raw_id), {})
        path = str(info.get("path", "")) if isinstance(info, dict) else ""
        if path:
            entry = read_raw_by_path(repo, path)
            snippets.append({"raw_id": entry.id, "title": entry.title, "event_date": entry.event_date, "snippet": entry.body[:500]})
    instructions = "请只基于 candidate_pages 与必要命中片段，归纳这些历史记录能为当前问题提供的个人上下文。"
    return llm_request(
        "search_l2",
        read_text(repo / "AGENTS.md"),
        {"query": query, "level1": l1, "candidate_pages": pages, "source_snippets": snippets},
        instructions,
        search_response_schema(),
    )
