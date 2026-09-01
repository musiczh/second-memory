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

    try:
        manifest_raw = load_manifest(repo).get("raw_hashes", {})
        raw_entries: dict[str, Any] = {}
        raw_summaries: dict[str, dict[str, Any]] = {}
        units: list[dict[str, Any]] = []
        raws: list[dict[str, Any]] = []
        selected_raw_ids = {str(raw.get("raw_id", "")) for raw in result.raws}
        for unit in result.units:
            units.append(_vector_unit_payload(unit))
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
                raws.append(raw)
            raw["best_score"] = max(float(raw["best_score"]), float(unit.score or 0.0))
            raw["matched_units"].append(unit.chunk_id)
    except Exception as error:
        return {
            "status": "corrupt",
            "reason": f"vector supplemental metadata is unavailable: {error}",
            "units": [],
            "raws": [],
        }
    payload["units"] = units
    payload["raws"] = raws
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
    if not isinstance(manifest_raw, dict):
        raise ValueError("vector Raw manifest catalog is invalid")
    info = manifest_raw.get(raw_id)
    if not isinstance(info, dict):
        raise ValueError(f"vector Raw manifest entry is invalid: {raw_id}")
    path = str(info.get("path", ""))
    if not path:
        raise ValueError(f"vector Raw manifest path is missing: {raw_id}")
    entry = read_raw_by_path(repo, path)
    if entry.id != raw_id:
        raise ValueError(f"vector Raw manifest path resolves to another Raw: {raw_id}: {entry.id}")
    entries[raw_id] = entry
    return entry


def search_level1(
    repo: Path,
    query: str,
    *,
    disabled_unit_types: set[str] | frozenset[str] = frozenset(),
    offline: bool = False,
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
        search_vectors(repo, query, disabled_unit_types=disabled_unit_types, offline=offline),
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
    instructions = (
        "请直接回答用户当前问题。candidate_pages、source_snippets、vector_units 与 vector_raws "
        "只提供个人化辅助证据，不是权威事实；先结合当前问题独立判断其相关性、时效性、可靠性和语境，"
        "再决定采用、保留不确定性、指出冲突或忽略。过去的观点、策略和归纳仍带有当时的主观性与时间属性，"
        "允许自然提到过去。若可靠且与当前问题直接相关的历史能补充稳定偏好、重复模式或判断演进，"
        "优先自然带出一句，让回答体现连续理解。历史锚点应按证据融入回答理由、再次出现的模式或前后变化，"
        "不必显式使用时间词，也不依赖「你已经／你之前／你过去」等固定句式，不得固定套用同一句式；"
        "除非用户明确要求回顾，"
        "通常最多一个历史锚点，不枚举历史。用户明确不要历史、证据低相关、"
        "历史与当前事实冲突且尚未核实，或历史只是在复述当前输入时，不提过去。"
        "answer_markdown 直接给出最终用户态答案；除非用户明确询问检索或调试，"
        "不得在面向用户的回答中提及检索、向量、召回、命中、分数、Raw、原料、候选、编译层、噪声，"
        "也不得解释采用或拒绝证据的过程。没有相关证据时也正常回答，不解释为何未使用。"
        "来源追溯和证据限制只写入 used_pages 与 caveats。"
    )
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
