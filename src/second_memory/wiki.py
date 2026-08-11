from __future__ import annotations

import html as html_lib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import frontmatter
from .compiler import (
    CONSOLIDATION_BATCH_SIZE,
    consolidation_state,
    load_manifest,
    manifest_drift,
    raw_lookup,
    read_pending,
    rebuild_state,
    version_drift,
)
from .config import KB_VERSION, load_config
from .graph import (
    compiler_policy_detail_node_ids,
    cross_node_duplicate_detail_node_ids,
    event_contract_issues,
    load_nodes,
    node_content_quality_issues,
    nonspecific_shared_entity_evidence_node_ids,
)
from .models import CONTENT_DETAIL_LABELS, NODE_TYPES, Node, RawEntry
from .transaction import transaction_state
from .utils import now_local

TEMPLATE_PATH = Path(__file__).parent / "templates" / "wiki.html"
DATA_PLACEHOLDER = "__WIKI_DATA__"
STATIC_START = "<!-- WIKI_STATIC_START -->"
STATIC_END = "<!-- WIKI_STATIC_END -->"
MAX_RELATED = 10
ENTITY_CONTEXT_EDGE_TYPES = {"involves", "about", "instance_of"}
# Topic candidates carry a lifecycle status; once materialized (promoted to a real
# topic node) or rejected they are resolved and must not surface as open governance
# items. Non-topic candidates (merge/split) have no status and stay unresolved.
RESOLVED_CANDIDATE_STATUSES = {"materialized", "rejected"}

_CODE = re.compile(r"`([^`]+?)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*")
_ITALIC = re.compile(r"\*(.+?)\*")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_ULIST = re.compile(r"^[-*]\s+(.*)$")
_OLIST = re.compile(r"^\d+\.\s+(.*)$")
_TIME_PREFIX = re.compile(r"^(\d{1,2}:\d{2})\s+(.*)$")
_TIMELINE_LINK = re.compile(r"^\s*-\s+\[([^\]]+)\]\(([^)]+)\)\s*$")


def _safe_href(url: str) -> str:
    lowered = url.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "#")):
        return url.replace('"', "%22")
    return ""


def _inline(text: str) -> str:
    text = _CODE.sub(lambda match: f"<code>{match.group(1)}</code>", text)

    def link_sub(match: re.Match[str]) -> str:
        href = _safe_href(match.group(2).strip())
        if not href:
            return match.group(1)
        return f'<a href="{href}" rel="noopener noreferrer" target="_blank">{match.group(1)}</a>'

    text = _LINK.sub(link_sub, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    return _ITALIC.sub(r"<em>\1</em>", text)


def render_markdown(text: str) -> str:
    """Render the small Markdown subset used by raw and compiled pages safely."""
    if not text.strip():
        return ""
    output: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag: str | None = None

    def flush_paragraph() -> None:
        if paragraph:
            output.append("<p>" + "<br>".join(_inline(html_lib.escape(line)) for line in paragraph) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            output.append(f"<{list_tag}>" + "".join(f"<li>{item}</li>" for item in list_items) + f"</{list_tag}>")
            list_items.clear()
        list_tag = None

    for raw_line in text.replace("\r\n", "\n").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        heading = _HEADING.match(stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            output.append(f"<h{level}>{_inline(html_lib.escape(heading.group(2)))}</h{level}>")
            continue
        ordered = _OLIST.match(stripped)
        unordered = None if ordered else _ULIST.match(stripped)
        if ordered or unordered:
            flush_paragraph()
            tag = "ol" if ordered else "ul"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            list_items.append(_inline(html_lib.escape((ordered or unordered).group(1))))
            continue
        flush_list()
        paragraph.append(stripped)

    flush_paragraph()
    flush_list()
    return "".join(output)


_DETAIL_LABELS = tuple(
    label
    for labels in CONTENT_DETAIL_LABELS.values()
    for label in labels
)


def render_detail(text: str) -> str:
    """Render validated detail labels as scan-friendly section headings."""
    blocks: list[str] = []
    for paragraph in [value.strip() for value in re.split(r"\n\s*\n", text) if value.strip()]:
        label = next((value for value in _DETAIL_LABELS if paragraph.startswith(value)), None)
        if not label:
            blocks.append(render_markdown(paragraph))
            continue
        body = paragraph[len(label):].strip()
        blocks.append(f'<h3 class="detail-section-label">{html_lib.escape(label.removesuffix("："))}</h3>')
        if body:
            blocks.append(render_markdown(body))
    return "".join(blocks)


def _parse_legacy_timeline_line(line: str) -> tuple[str, str, list[str]]:
    body = line.strip().removeprefix("-").strip()
    refs: list[str] = []
    if " -> " in body:
        body, _, raw_refs = body.rpartition(" -> ")
        refs = [value.strip() for value in raw_refs.split(",") if value.strip()]
    match = _TIME_PREFIX.match(body)
    if match:
        return match.group(1), match.group(2).strip(), refs
    return "", body, refs


def _reference(ref_id: str, nodes: dict[str, Node], raws: dict[str, RawEntry]) -> dict[str, str]:
    if ref_id in nodes:
        node = nodes[ref_id]
        return {"id": node.id, "title": node.title, "type": node.type}
    if ref_id in raws:
        raw = raws[ref_id]
        return {"id": raw.id, "title": raw.title, "type": "raw"}
    return {"id": ref_id, "title": ref_id, "type": "unknown"}


def _normalize_edges(nodes: dict[str, Node], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    source_edges = manifest.get("edges", [])
    if not isinstance(source_edges, list) or not source_edges:
        source_edges = [
            {"source": node.id, **edge}
            for node in nodes.values()
            for edge in node.out_edges
        ]
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for value in source_edges:
        if not isinstance(value, dict):
            continue
        source = str(value.get("source") or value.get("source_ref") or "")
        target = str(value.get("target") or value.get("target_ref") or "")
        edge_type = str(value.get("type") or "")
        if not source or not target or not edge_type:
            continue
        deduped[(source, target, edge_type)] = {
            "source": source,
            "target": target,
            "type": edge_type,
            "note": str(value.get("note") or ""),
            "inferred": bool(value.get("inferred", False)),
            "attrs": dict(value.get("attrs") or {}),
        }
    return [deduped[key] for key in sorted(deduped)]


def _timeline_model(
    repo: Path,
    nodes: dict[str, Node],
    raws: dict[str, RawEntry],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, str]]]]:
    timeline_root = repo / "wiki" / "timeline"
    appearances: dict[str, list[dict[str, str]]] = defaultdict(list)
    days: list[dict[str, Any]] = []
    if not timeline_root.exists():
        return days, appearances

    for path in sorted(timeline_root.glob("*.md"), reverse=True):
        meta, body = frontmatter.read_document(path)
        event_date = str(meta.get("event_date") or path.stem)
        day_sources = [str(value) for value in meta.get("sources", [])]
        entries: list[dict[str, Any]] = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            linked = _TIMELINE_LINK.match(stripped)
            if linked:
                text = linked.group(1).strip()
                ref_id = Path(linked.group(2)).stem
                time = ""
                ref_ids = [ref_id]
            else:
                time, text, ref_ids = _parse_legacy_timeline_line(stripped)
            refs = [_reference(ref_id, nodes, raws) for ref_id in ref_ids]
            source_overlap = sorted({
                raw_id
                for ref_id in ref_ids
                if ref_id in nodes
                for raw_id in nodes[ref_id].sources
                if raw_id in day_sources
            })
            entry_sources = source_overlap or [raw_id for raw_id in day_sources if raw_id in raws]
            raw_refs = [_reference(raw_id, nodes, raws) for raw_id in entry_sources]
            entries.append({"time": time, "text": text, "refs": refs, "raws": raw_refs})
            for ref in refs:
                if ref["type"] in NODE_TYPES:
                    appearances[ref["id"]].append({"date": event_date, "time": time, "text": text})
        entries.sort(key=lambda item: str(item["time"]), reverse=True)
        days.append({"id": str(meta.get("id") or f"timeline-{event_date}"), "date": event_date, "entries": entries})
    return days, appearances


def _related_nodes(node: Node, nodes: dict[str, Node], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scores: dict[str, int] = defaultdict(int)
    own_sources = set(node.sources)
    for other in nodes.values():
        if other.id != node.id:
            scores[other.id] += 3 * len(own_sources & set(other.sources))
    for edge in edges:
        if edge["source"] == node.id and edge["target"] in nodes:
            scores[edge["target"]] += 20
        if edge["target"] == node.id and edge["source"] in nodes:
            scores[edge["source"]] += 20
    related = []
    for other_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0])):
        if score <= 0:
            continue
        other = nodes[other_id]
        related.append({"id": other.id, "type": other.type, "title": other.title, "summary": other.summary})
        if len(related) == MAX_RELATED:
            break
    return related


def _entity_source_groups(
    entity: Node,
    nodes: dict[str, Node],
    raws: dict[str, RawEntry],
    edges: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    """Group entity sources by explicit provenance path without shared-source inference."""
    direct_ids = {
        edge["source"]
        for edge in edges
        if edge["target"] == entity.id
        and edge["type"] == "belongs_to"
        and edge["source"] in raws
    }
    event_ids = {
        node.id
        for node in nodes.values()
        if node.type == "event"
        and any(
            edge["source"] == node.id
            and edge["target"] == entity.id
            and edge["type"] in ENTITY_CONTEXT_EDGE_TYPES
            for edge in edges
        )
    }
    insight_ids = {
        edge["source"]
        for edge in edges
        if edge["target"] == entity.id
        and edge["type"] in ENTITY_CONTEXT_EDGE_TYPES
        and edge["source"] in nodes
        and nodes[edge["source"]].type == "statement"
    }

    def raw_refs(source_ids: set[str]) -> list[dict[str, str]]:
        return [
            _reference(raw_id, nodes, raws)
            for raw_id in sorted(source_ids)
            if raw_id in raws
        ]

    return {
        "direct": raw_refs(direct_ids),
        "via_events": raw_refs({raw_id for node_id in event_ids for raw_id in nodes[node_id].sources}),
        "via_insights": raw_refs({raw_id for node_id in insight_ids for raw_id in nodes[node_id].sources}),
    }


def _node_model(
    node: Node,
    nodes: dict[str, Node],
    raws: dict[str, RawEntry],
    edges: list[dict[str, Any]],
    appearances: dict[str, list[dict[str, str]]],
    manifest_schema: int,
) -> dict[str, Any]:
    meta, _ = frontmatter.read_document(node.path)
    outgoing = []
    incoming = []
    for edge in edges:
        if edge["source"] == node.id:
            outgoing.append({**edge, "other": _reference(edge["target"], nodes, raws)})
        if edge["target"] == node.id:
            incoming.append({**edge, "other": _reference(edge["source"], nodes, raws)})
    attrs = dict(node.attrs)
    return {
        "id": node.id,
        "type": node.type,
        "title": node.title,
        "summary": node.summary,
        "aliases": list(node.aliases),
        "entity_kind": node.entity_kind,
        "event_kind": node.event_kind,
        "status": node.status,
        "event_date": node.event_date,
        "current_state": node.current_state,
        "evolution": list(node.evolution),
        "status_history": list(attrs.get("status_history", [])),
        "date_history": list(attrs.get("date_history", [])),
        "detail": node.detail,
        "key_points": list(node.key_points),
        "evidence": [
            {**item, "source": _reference(str(item.get("source_id", "")), nodes, raws)}
            for item in node.evidence
        ],
        "uncertainties": list(node.uncertainties),
        "semantics": dict(node.semantics),
        "attrs": attrs,
        "created": str(meta.get("created") or ""),
        "updated": str(meta.get("updated") or ""),
        "body_html": render_markdown(node.body),
        "sources": [_reference(raw_id, nodes, raws) for raw_id in node.sources],
        "outgoing": outgoing,
        "incoming": incoming,
        "related": _related_nodes(node, nodes, edges),
        "source_groups": _entity_source_groups(node, nodes, raws, edges) if node.type == "entity" else {},
        "timeline_appearances": sorted(
            appearances.get(node.id, []),
            key=lambda item: (item["date"], item["time"]),
            reverse=True,
        ),
    }


def _raw_model(raw: RawEntry, nodes: dict[str, Node], edges: list[dict[str, Any]]) -> dict[str, Any]:
    belongs_to = [
        _reference(edge["target"], nodes, {raw.id: raw})
        for edge in edges
        if edge["source"] == raw.id and edge["type"] == "belongs_to"
    ]
    incoming = [
        {**edge, "other": _reference(edge["source"], nodes, {raw.id: raw})}
        for edge in edges
        if edge["target"] == raw.id
        and edge["type"] == "contains"
        and edge["source"] in nodes
        and nodes[edge["source"]].type == "topic"
    ]
    return {
        "id": raw.id,
        "title": raw.title,
        "created": raw.created,
        "event_date": raw.event_date,
        "tags": list(raw.tags),
        "annotations": dict(raw.annotations),
        "belongs_to": belongs_to,
        "incoming": incoming,
        "body_html": render_markdown(raw.body),
    }


def _open_candidates(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only unresolved candidates worth surfacing as governance items."""
    return [
        candidate
        for candidate in manifest.get("candidates", [])
        if str(candidate.get("status", "")) not in RESOLVED_CANDIDATE_STATUSES
    ]


def build_wiki_model(repo: Path) -> dict[str, Any]:
    load_config(repo)
    manifest = load_manifest(repo)
    nodes, _ = load_nodes(repo)
    raws = raw_lookup(repo)
    edges = _normalize_edges(nodes, manifest)
    timeline, appearances = _timeline_model(repo, nodes, raws)
    manifest_schema = int(manifest.get("schema", 1))
    open_candidates = _open_candidates(manifest)
    node_values = [
        _node_model(node, nodes, raws, edges, appearances, manifest_schema)
        for node in sorted(nodes.values(), key=lambda value: (value.type, value.title, value.id))
    ]
    raw_values = {
        raw.id: _raw_model(raw, nodes, edges)
        for raw in sorted(raws.values(), key=lambda value: (value.event_date, value.created, value.id), reverse=True)
    }
    consolidation = consolidation_state(manifest)
    pending = read_pending(repo)
    counts = {node_type: sum(1 for node in nodes.values() if node.type == node_type) for node_type in sorted(NODE_TYPES)}
    counts.update({
        "nodes": len(nodes),
        "edges": len(edges),
        "timeline": len(timeline),
        "raw": len(raws),
        "candidates": len(open_candidates),
        "redirects": len(manifest.get("redirects", {})),
    })
    def event_confidence(node: Node) -> float:
        value = node.semantics.get("confidence")
        return float(value) if isinstance(value, (int, float)) else 0.0

    weak_detail_ids = cross_node_duplicate_detail_node_ids(nodes) | compiler_policy_detail_node_ids(nodes)
    semantic_quality = {
        "missing_detail": sorted(node.id for node in nodes.values() if not node.detail.strip()),
        "weak_detail": sorted({
            node.id
            for node in nodes.values()
            if node_content_quality_issues(node.type, node.summary, node.detail, node.key_points)
        } | weak_detail_ids),
        "missing_evidence": sorted(node.id for node in nodes.values() if not node.evidence),
        "weak_evidence": sorted(nonspecific_shared_entity_evidence_node_ids(nodes)),
        "low_confidence_events": sorted(
            node.id
            for node in nodes.values()
            if node.type == "event" and event_confidence(node) < 0.8
        ),
        "weak_event_contract": sorted(
            node.id
            for node in nodes.values()
            if node.type == "event" and event_contract_issues(node.title, node.semantics)
        ),
    }
    return {
        "schema_version": 2,
        "generated_at": now_local().isoformat(),
        "repo": str(repo),
        "counts": counts,
        "health": {
            "manifest_schema": manifest_schema,
            "kb_version": KB_VERSION,
            "compiled_kb_version": manifest.get("kb_version"),
            "version_drift": version_drift(repo),
            "manifest_drift": manifest_drift(repo),
            "pending": len(pending),
            "pending_raw": pending,
            "consolidation_pending": len(consolidation["pending_raw"]),
            "consolidation_due": len(consolidation["pending_raw"]) >= CONSOLIDATION_BATCH_SIZE,
            "consolidation_memo": consolidation["memo"],
            "applied_session_id": manifest.get("applied_session_id"),
            "rebuild": rebuild_state(repo),
            "transaction": transaction_state(repo),
            "semantic_quality": semantic_quality,
        },
        "nodes": node_values,
        "timeline": timeline,
        "raws": raw_values,
        "edges": edges,
        "candidates": open_candidates,
        "redirects": dict(manifest.get("redirects", {})),
    }


def _embed_json(model: dict[str, Any]) -> str:
    payload = json.dumps(model, ensure_ascii=False)
    return payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def render_static_overview(model: dict[str, Any]) -> str:
    """Render a useful no-JavaScript view of the compiled knowledge base."""
    counts = model["counts"]
    health = model["health"]

    def escape(value: object) -> str:
        return html_lib.escape(str(value or ""), quote=True)

    def metric(node_type: str, label: str) -> str:
        return (
            f'<div class="metric {node_type}"><strong>{counts[node_type]}</strong>'
            f'<span>{label}</span></div>'
        )

    type_labels = {"entity": "实体", "event": "事件", "statement": "洞察", "topic": "主题"}
    ref_titles = {
        **{str(node["id"]): str(node["title"]) for node in model["nodes"]},
        **{str(raw_id): str(raw["title"]) for raw_id, raw in model["raws"].items()},
    }

    def refs_text(refs: object) -> str:
        if not isinstance(refs, list):
            return ""
        return "、".join(escape(ref_titles.get(str(ref_id), ref_id)) for ref_id in refs)

    def topic_reading_html(node: dict[str, Any]) -> str:
        reading = (node.get("attrs") or {}).get("topic_reading")
        if not isinstance(reading, dict):
            return ""
        evolution = "".join(
            f'<li><strong>{escape(item.get("date") or "未标日期")}</strong>：{escape(item.get("state"))}'
            f'{("<br>来源：" + refs_text(item.get("source_ids"))) if refs_text(item.get("source_ids")) else ""}</li>'
            for item in reading.get("evolution", [])
            if isinstance(item, dict)
        )
        contradictions = "".join(
            f'<li>{escape(item.get("description"))}'
            f'{("<br>相关成员：" + refs_text(item.get("member_refs"))) if refs_text(item.get("member_refs")) else ""}'
            f'{("<br>来源：" + refs_text(item.get("source_ids"))) if refs_text(item.get("source_ids")) else ""}</li>'
            for item in reading.get("contradictions", [])
            if isinstance(item, dict)
        )
        open_questions = "".join(
            f'<li><strong>{escape(item.get("question"))}</strong>'
            f'{("：" + escape(item.get("basis"))) if item.get("basis") else ""}'
            f'{("<br>来源：" + refs_text(item.get("source_ids"))) if refs_text(item.get("source_ids")) else ""}</li>'
            for item in reading.get("open_questions", [])
            if isinstance(item, dict)
        )
        evidence = "".join(
            f'<li>{escape(item.get("claim"))}：{escape((item.get("source") or {}).get("title"))}</li>'
            for item in node.get("evidence", [])
            if isinstance(item, dict)
        )
        return (
            '<div class="topic-reading">'
            f'<h4>核心理解</h4><p>{escape(reading.get("core_understanding"))}</p>'
            f'<p><strong>置信度：</strong>{escape(reading.get("confidence"))}</p>'
            f'<h4>演变</h4>{("<ul>" + evolution + "</ul>") if evolution else "<p>暂无演变记录。</p>"}'
            f'<h4>矛盾</h4>{("<ul>" + contradictions + "</ul>") if contradictions else "<p>暂无已识别矛盾。</p>"}'
            f'<h4>开放问题</h4>{("<ul>" + open_questions + "</ul>") if open_questions else "<p>暂无开放问题。</p>"}'
            f'<h4>证据</h4>{("<ul>" + evidence + "</ul>") if evidence else "<p>暂无结构化证据。</p>"}'
            "</div>"
        )

    def topic_contract_html(node: dict[str, Any]) -> str:
        contract = (node.get("attrs") or {}).get("topic_contract")
        if not isinstance(contract, dict):
            return ""
        facets = []
        for facet in contract.get("facets", []):
            if not isinstance(facet, dict):
                continue
            member_refs = facet.get("member_refs")
            if not isinstance(member_refs, list):
                member_refs = facet.get("statement_refs", [])
            if not isinstance(member_refs, list):
                member_refs = []
            members = "、".join(
                escape(ref_titles.get(str(node_id), node_id))
                for node_id in member_refs
            )
            facets.append(
                f'<li><strong>{escape(facet.get("name"))}</strong>：{escape(facet.get("summary"))}'
                f'{("<br>成员：" + members) if members else ""}</li>'
            )
        rationales = []
        for node_id, rationale in (contract.get("member_rationales") or {}).items():
            if not isinstance(rationale, dict):
                continue
            rationales.append(
                f'<li><strong>{escape(ref_titles.get(str(node_id), node_id))}</strong>'
                f'（{escape(rationale.get("facet"))}）：{escape(rationale.get("reason"))}'
                f'<br>依据：{escape(rationale.get("supporting_excerpt"))}</li>'
            )
        exclusions = []
        for item in contract.get("exclusions", []):
            if not isinstance(item, dict):
                continue
            statement_id = str(item.get("member_ref") or item.get("member_id") or item.get("statement_id") or "")
            exclusions.append(
                f'<li><strong>{escape(ref_titles.get(statement_id, statement_id))}</strong>：'
                f'{escape(item.get("reason"))}<br>相近依据：{escape(item.get("nearby_excerpt"))}</li>'
            )
        return (
            '<div class="topic-contract">'
            '<h4>成员合同</h4>'
            f'<p><strong>主题类型：</strong>{escape(contract.get("topic_kind"))}</p>'
            f'<p><strong>主题组织问题：</strong>{escape(contract.get("organizing_question"))}</p>'
            f'<p><strong>侧面关系：</strong>{escape(contract.get("facet_relationship"))}</p>'
            f'<p><strong>成员边界：</strong>{escape(contract.get("boundary_rule"))}</p>'
            f'{("<h4>主题侧面</h4><ul>" + "".join(facets) + "</ul>") if facets else ""}'
            f'{("<h4>成员依据</h4><ul>" + "".join(rationales) + "</ul>") if rationales else ""}'
            f'{("<h4>排除边界</h4><ul>" + "".join(exclusions) + "</ul>") if exclusions else ""}'
            "</div>"
        )

    def node_row(node: dict[str, Any]) -> str:
        reading = (node.get("attrs") or {}).get("reading")
        reading = reading if isinstance(reading, dict) else {}
        lead = reading.get("tldr") or node.get("current_state") or node.get("status") or node.get("summary") or ""
        narrative = reading.get("narrative") or node.get("detail") or node.get("summary") or ""
        highlights = reading.get("highlights") if isinstance(reading.get("highlights"), list) else []
        highlights_html = (
            '<ul class="row-highlights">'
            + "".join(f"<li>{escape(item)}</li>" for item in highlights)
            + "</ul>"
        ) if highlights else ""
        original_detail = node.get("detail") or ""
        original_fold = (
            f'<details class="raw-fold"><summary>完整综合（原始 detail）</summary>'
            f'<div class="prose">{render_detail(str(original_detail))}</div></details>'
        ) if (reading.get("narrative") and original_detail) else ""
        sources = "、".join(escape(source["title"]) for source in node.get("sources", []))
        return (
            '<details class="relation-row">'
            f'<summary>{escape(type_labels.get(node["type"], node["type"]))} · <strong>{escape(node["title"])}</strong></summary>'
            f'<p class="row-summary">{escape(lead)}</p>'
            f'{topic_reading_html(node)}'
            f'{topic_contract_html(node)}'
            f'<div class="prose">{render_detail(str(narrative))}</div>'
            f'{highlights_html}'
            f'{original_fold}'
            f'<div class="row-meta" style="text-align:left">{len(node.get("sources", []))} 来源'
            f'{(" · " + sources) if sources else ""}</div>'
            "</details>"
        )

    topics = [node for node in model["nodes"] if node["type"] == "topic"]
    topic_rows = [node_row(node) for node in topics]
    node_rows = [node_row(node) for node in model["nodes"] if node["type"] != "topic"]

    timeline_rows = []
    for day in model["timeline"]:
        entries = "".join(
            '<div class="timeline-entry">'
            f'<span class="entry-time">{escape(entry.get("time"))}</span>'
            f'<span class="entry-text">{escape(entry.get("text"))}</span>'
            "</div>"
            for entry in day.get("entries", [])
        )
        timeline_rows.append(
            f'<section class="day"><div class="day-date">{escape(day["date"])}</div>{entries}</section>'
        )

    raw_rows = []
    for raw in model["raws"].values():
        summary = raw.get("annotations", {}).get("summary") or "、".join(raw.get("tags", []))
        raw_rows.append(
            '<div class="list-row">'
            '<div><span class="type-label"><span class="type-dot raw"></span>原文</span></div>'
            f'<div><div class="row-title">{escape(raw["title"])}</div>'
            f'<div class="row-summary">{escape(summary)}</div></div>'
            f'<div class="row-meta">{escape(raw.get("event_date"))}</div></div>'
        )

    health_state = "需重建" if health["version_drift"] else "版本一致"
    rebuild = health["rebuild"]
    rebuild_progress = f'{rebuild["processed"]}/{rebuild["total"]}'
    return (
        '<div class="view static-export">'
        '<section class="hero"><p class="eyebrow">Pre-rendered Knowledge Base</p>'
        '<h1>知识库编译结果</h1>'
        f'<p class="lede">已从真实知识库副本预渲染 {counts["nodes"]} 个节点、'
        f'{counts["timeline"]} 天时间线和 {counts["raw"]} 条原始记录。'
        "即使当前查看器禁用 JavaScript，以下内容仍然可读。</p>"
        f'<span class="schema-pill">manifest schema {escape(health["manifest_schema"])} · '
        f'{escape(health_state)} · rebuild {escape(rebuild["phase"])} '
        f'{escape(rebuild_progress)}</span></section>'
        '<div class="metrics">'
        f'{metric("entity", "实体")}{metric("event", "事件")}'
        f'{metric("statement", "洞察")}{metric("topic", "主题")}'
        f'<div class="metric"><strong>{counts["edges"]}</strong><span>Edges</span></div>'
        f'<div class="metric"><strong>{counts["raw"]}</strong><span>Raw</span></div></div>'
        '<section class="section"><div class="section-title"><h2>主题优先阅读</h2>'
        f'<span>{counts["topic"]}</span></div><div class="list">{"".join(topic_rows)}</div></section>'
        '<section class="section"><div class="section-title"><h2>时间线投影</h2>'
        f'<span>{counts["timeline"]} 天</span></div><div class="spine">{"".join(timeline_rows)}</div></section>'
        '<section class="section"><div class="section-title"><h2>其他编译节点</h2>'
        f'<span>{counts["nodes"] - counts["topic"]}</span></div><div class="list">{"".join(node_rows)}</div></section>'
        '<section class="section"><div class="section-title"><h2>原始记录索引</h2>'
        f'<span>{counts["raw"]}</span></div><div class="list">{"".join(raw_rows)}</div></section>'
        "</div>"
    )


def render_html(model: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    start = template.index(STATIC_START)
    end = template.index(STATIC_END, start) + len(STATIC_END)
    static_block = f"{STATIC_START}\n{render_static_overview(model)}\n{STATIC_END}"
    template = template[:start] + static_block + template[end:]
    return template.replace(DATA_PLACEHOLDER, _embed_json(model))


def build_wiki_html(repo: Path, output_path: Path) -> dict[str, Any]:
    model = build_wiki_model(repo)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_html(model), encoding="utf-8")
    return {
        "output": str(output_path),
        "counts": model["counts"],
        "health": model["health"],
        "generated_at": model["generated_at"],
    }
