from __future__ import annotations

from pathlib import Path
from typing import Any

from .compiler import list_index_pages, load_manifest, read_raw_by_path, read_text
from .config import load_config
from .models import NODE_TYPES
from .promptio import llm_request, search_response_schema
from .search import phrase_coverage_bonus, query_terms, rg_hits
from .vectors import VectorSearchResult, VectorUnit, search_vectors


def vector_supplement(repo: Path, result: VectorSearchResult) -> dict[str, Any]:
    """Serialize bounded, source-addressable vector evidence for search clients."""
    payload: dict[str, Any] = {
        "status": result.status,
        "reason": result.reason,
        "units": [],
        "raws": [],
    }
    if result.status != "ready":
        return payload

    manifest_raw = load_manifest(repo).get("raw_hashes", {})
    raw_entries: dict[str, Any] = {}
    raw_summaries: dict[str, dict[str, Any]] = {}
    selected_raw_ids = {str(raw.get("raw_id", "")) for raw in result.raws}
    for unit in result.units:
        serialized = _vector_unit_payload(unit)
        payload["units"].append(serialized)
        if unit.raw_id not in selected_raw_ids:
            continue
        raw = raw_summaries.get(unit.raw_id)
        if raw is None:
            entry = _vector_raw_entry(repo, manifest_raw, raw_entries, unit.raw_id)
            raw = {
                "raw_id": unit.raw_id,
                "title": entry.title if entry is not None else "",
                "event_date": entry.event_date if entry is not None else None,
                "best_score": float(unit.score or 0.0),
                "matched_units": [],
            }
            raw_summaries[unit.raw_id] = raw
            payload["raws"].append(raw)
        raw["best_score"] = max(float(raw["best_score"]), float(unit.score or 0.0))
        raw["matched_units"].append(unit.chunk_id)
    return payload


def _vector_unit_payload(unit: VectorUnit) -> dict[str, Any]:
    return {
        "chunk_id": unit.chunk_id,
        "raw_id": unit.raw_id,
        "kind": unit.kind,
        "segment_index": unit.segment_index,
        "section_index": unit.section_index,
        "start": unit.start,
        "end": unit.end,
        "score": unit.score,
        "snippet": unit.text,
    }


def _vector_raw_entry(
    repo: Path,
    manifest_raw: Any,
    entries: dict[str, Any],
    raw_id: str,
) -> Any | None:
    if raw_id in entries:
        return entries[raw_id]
    info = manifest_raw.get(raw_id, {}) if isinstance(manifest_raw, dict) else {}
    path = str(info.get("path", "")) if isinstance(info, dict) else ""
    if not path:
        return None
    entry = read_raw_by_path(repo, path)
    entries[raw_id] = entry
    return entry


def search_level1(
    repo: Path,
    query: str,
    *,
    disabled_unit_types: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
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
    keyword_result = {"query": query, "candidates": candidates[:10], "hits": rg_hits(repo, query)}
    keyword_result["supplemental_raw"] = vector_supplement(
        repo,
        search_vectors(repo, query, disabled_unit_types=disabled_unit_types),
    )
    return keyword_result


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
    supplemental = l1["supplemental_raw"]
    instructions = "请只基于 candidate_pages 与必要命中片段，归纳这些历史记录能为当前问题提供的个人上下文。"
    return llm_request(
        "search_l2",
        read_text(repo / "AGENTS.md"),
        {
            "query": query,
            "level1": l1,
            "candidate_pages": pages,
            "source_snippets": snippets,
            "vector_units": supplemental["units"],
            "vector_raws": supplemental["raws"],
        },
        instructions,
        search_response_schema(),
    )
