from __future__ import annotations

import json
import os
import re
from collections import defaultdict, deque
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from . import frontmatter
from .errors import ValidationError
from .models import (
    CONTENT_DETAIL_LABELS,
    CONTENT_DETAIL_MINIMUMS,
    CONTENT_MIN_KEY_POINT_CHARS,
    CONTENT_MIN_KEY_POINTS,
    CONTENT_MIN_PARAGRAPHS,
    CONTENT_MIN_SENTENCES,
    ENTITY_KINDS,
    EVENT_BASES,
    EVENT_BASIS_LABELS,
    EVENT_CONFIDENCE_THRESHOLD,
    EVENT_FACTUALITIES,
    EVENT_STATUSES,
    EVENT_SUBJECT_ROLES,
    EVENT_TIME_PRECISIONS,
    NODE_TYPES,
    PLANNED_EVENT_BASES,
    SYMMETRIC_EDGE_TYPES,
    Node,
    RawEntry,
)
from .resolver import deterministic_node_id, normalize_name
from .utils import now_local, parse_date, parse_temporal_anchor, relpath

NODE_DIRECTORIES = {
    "entity": "entities",
    "event": "events",
    "statement": "statements",
    "topic": "topics",
}

def node_relative_path(node: Node) -> Path:
    return Path("wiki") / NODE_DIRECTORIES[node.type] / f"{node.id}.md"


def load_nodes(repo: Path) -> tuple[dict[str, Node], dict[str, str]]:
    nodes: dict[str, Node] = {}
    wiki = repo / "wiki"
    if not wiki.exists():
        return nodes, {}
    for directory in NODE_DIRECTORIES.values():
        root = wiki / directory
        if not root.exists():
            continue
        for path in sorted(root.glob("*.md")):
            meta, body = frontmatter.read_document(path)
            node_id = str(meta.get("id", ""))
            node_type = str(meta.get("type", ""))
            if not node_id or node_type not in NODE_TYPES:
                continue
            content = dict(meta.get("content", {}))
            node = Node(
                id=node_id,
                type=node_type,
                title=str(meta.get("title") or node_id),
                summary=str(meta.get("summary") or first_body_line(body)),
                path=path,
                sources=sorted(set(str(value) for value in meta.get("sources", []))),
                aliases=sorted(set(str(value) for value in meta.get("aliases", []))),
                entity_kind=meta.get("entity_kind"),
                event_kind=meta.get("event_kind"),
                status=meta.get("status"),
                event_date=meta.get("event_date"),
                current_state=meta.get("current_state") or (first_body_line(body) if node_type == "statement" else None),
                evolution=list(meta.get("evolution", [])),
                attrs=dict(meta.get("attrs", {})),
                out_edges=list(meta.get("out_edges", [])),
                backrefs=list(meta.get("backrefs", [])),
                detail=str(content.get("detail", "")),
                key_points=normalize_strings(content.get("key_points", [])),
                evidence=normalize_evidence(content.get("evidence", [])),
                uncertainties=normalize_strings(content.get("uncertainties", [])),
                semantics=dict(meta.get("semantics", {})),
                body=body,
            )
            nodes[node.id] = node
    return nodes, {}


def apply_node_actions(
    nodes: dict[str, Node],
    actions: list[dict[str, Any]],
    *,
    mode: str,
    raw_entries: dict[str, RawEntry],
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    refs: dict[str, str] = {}
    redirects: dict[str, str] = {}
    affected: set[str] = set()
    existing_ids = set(nodes)

    for action in actions:
        if str(action.get("action")) != "create":
            continue
        node_type = str(action.get("type", ""))
        if node_type not in NODE_TYPES:
            raise ValidationError(f"invalid node type: {node_type}")
        if node_type == "topic" and mode not in {"consolidate", "topics"}:
            raise ValidationError("topics can only be created by consolidate or topics refresh")
        ref = str(action.get("ref", ""))
        if not ref or ref in refs:
            raise ValidationError("create action requires a unique plan-local ref")
        if ref in existing_ids or ref in raw_entries:
            raise ValidationError("create plan-local ref cannot reuse an existing node or raw ID")
        title = required_text(action, "title")
        sources = source_ids(action)
        earliest = min(sources) if sources else ref
        node_id = deterministic_node_id(node_type, title, earliest, existing_ids)
        existing_ids.add(node_id)
        refs[ref] = node_id

    for action in actions:
        if str(action.get("action")) != "create":
            continue
        ref = str(action["ref"])
        node_type = str(action["type"])
        sources = source_ids(action)
        resolved_action = resolve_action_refs(action, refs)
        node = new_node(refs[ref], node_type, resolved_action, sources, raw_entries)
        validate_node_shape(node)
        nodes[node.id] = node
        affected.add(node.id)

    for action in actions:
        operation = str(action.get("action", ""))
        if operation == "create":
            continue
        if operation in {"merge", "split"}:
            if mode != "consolidate":
                raise ValidationError(f"{operation} actions are not allowed outside consolidate mode")
            if operation == "merge":
                merge_nodes(nodes, resolve_action_refs(action, refs), raw_entries, redirects, affected)
            else:
                split_node(nodes, resolve_action_refs(action, refs), raw_entries, redirects, affected)
            continue
        target_id = str(action.get("target_id", ""))
        if target_id not in nodes:
            raise ValidationError(f"unknown target_id: {target_id}")
        update_node(nodes[target_id], resolve_action_refs(action, refs), raw_entries)
        validate_node_shape(nodes[target_id])
        affected.add(target_id)
    return refs, redirects, affected


def new_node(node_id: str, node_type: str, action: dict[str, Any], sources: list[str], raw_entries: dict[str, RawEntry] | None = None) -> Node:
    content = dict(action.get("content", {}))
    node = Node(
        id=node_id,
        type=node_type,
        title=required_text(action, "title"),
        summary=required_text(action, "summary"),
        path=Path(),
        sources=sources,
        aliases=sorted(set(str(value) for value in action.get("aliases", []))),
        entity_kind=action.get("entity_kind"),
        event_kind=action.get("event_kind"),
        status=action.get("status"),
        event_date=action.get("event_date"),
        current_state=action.get("current_state"),
        evolution=normalize_evolution(list(action.get("evolution", []))),
        attrs=sanitize_attrs(action.get("attrs", {})),
        detail=str(content.get("detail", "")).strip(),
        key_points=normalize_strings(content.get("key_points", [])),
        evidence=normalize_evidence(content.get("evidence", [])),
        uncertainties=normalize_strings(content.get("uncertainties", [])),
        semantics=normalize_semantics(action.get("semantics", {})),
    )
    if node.type == "statement" and node.current_state and not node.evolution:
        node.evolution = [{"date": action_date(action, sources, raw_entries or {}), "state": node.current_state, "sources": sources}]
    if node.type == "event":
        changed_on = action_change_date(action, sources, raw_entries or {})
        node.attrs["date_history"] = [{"event_date": node.event_date, "changed_on": changed_on, "sources": sources}]
        node.attrs["status_history"] = [{"status": node.status, "date": changed_on, "sources": sources}]
    return node


def update_node(node: Node, action: dict[str, Any], raw_entries: dict[str, RawEntry]) -> None:
    operation = str(action.get("action"))
    if operation == "relate":
        return
    if operation == "archive":
        node.attrs["archived"] = True
        return
    incoming_sources = source_ids(action)
    prior_sources = list(node.sources)
    if operation == "reinforce" and "content" not in action:
        node.sources = sorted(set(node.sources) | set(incoming_sources))
        return
    if node.type == "topic" and action.get("membership_mode") == "replace":
        node.sources = []
    else:
        node.sources = sorted(set(node.sources) | set(incoming_sources))
    node.aliases = sorted(set(node.aliases) | {str(value) for value in action.get("aliases", [])})
    if action.get("title"):
        old_title = node.title
        node.title = str(action["title"])
        if normalize_name(old_title) != normalize_name(node.title):
            node.aliases = sorted(set(node.aliases) | {old_title})
    if action.get("summary"):
        node.summary = str(action["summary"])
    if action.get("content"):
        content = dict(action["content"])
        node.summary = str(content["summary"])
        node.detail = str(content["detail"]).strip()
        node.key_points = normalize_strings(content["key_points"])
        node.evidence = normalize_evidence(content["evidence"])
        node.uncertainties = normalize_strings(content["uncertainties"])
    if action.get("attrs"):
        node.attrs.update(sanitize_attrs(action["attrs"]))
    if node.type == "entity" and action.get("entity_kind"):
        node.entity_kind = str(action["entity_kind"])
    if node.type == "event":
        if action.get("semantics"):
            node.semantics = normalize_semantics(action["semantics"])
        if action.get("event_kind"):
            node.event_kind = str(action["event_kind"])
        if action.get("event_date") and str(action["event_date"]) != node.event_date:
            changed_on = action_change_date(action, incoming_sources, raw_entries)
            history = list(node.attrs.get("date_history", []))
            if node.event_date and (not history or history[-1].get("event_date") != node.event_date):
                history.append({"event_date": node.event_date, "changed_on": changed_on, "sources": prior_sources})
            node.event_date = str(action["event_date"])
            history.append({"event_date": node.event_date, "changed_on": changed_on, "sources": incoming_sources})
            node.attrs["date_history"] = dedupe_dicts_preserve_order(history)
        incoming_status = "superseded" if operation == "supersede" else action.get("status")
        if incoming_status and str(incoming_status) != node.status:
            history = list(node.attrs.get("status_history", []))
            if node.status and (not history or history[-1].get("status") != node.status):
                history.append({"status": node.status, "date": action_change_date(action, incoming_sources, raw_entries), "sources": prior_sources})
            node.status = str(incoming_status)
            history.append({"status": node.status, "date": action_change_date(action, incoming_sources, raw_entries), "sources": incoming_sources})
            node.attrs["status_history"] = dedupe_dicts_preserve_order(history)
    if node.type == "statement":
        incoming_state = action.get("current_state")
        if incoming_state and str(incoming_state) != node.current_state:
            node.current_state = str(incoming_state)
            node.evolution.append({"date": action_change_date(action, incoming_sources, raw_entries), "state": node.current_state, "sources": incoming_sources})
        node.evolution = normalize_evolution([*node.evolution, *list(action.get("evolution", []))])


def merge_nodes(
    nodes: dict[str, Node],
    action: dict[str, Any],
    raw_entries: dict[str, RawEntry],
    redirects: dict[str, str],
    affected: set[str],
) -> None:
    canonical_id = str(action.get("target_id", ""))
    absorbed = [str(value) for value in action.get("absorbed_ids", [])]
    if canonical_id not in nodes or not absorbed:
        raise ValidationError("merge requires an existing target_id and absorbed_ids")
    canonical = nodes[canonical_id]
    for node_id in absorbed:
        if node_id == canonical_id or node_id not in nodes:
            raise ValidationError(f"invalid absorbed id: {node_id}")
        other = nodes[node_id]
        if other.type != canonical.type:
            raise ValidationError("merge nodes must have the same type")
        canonical.sources = sorted(set(canonical.sources) | set(other.sources))
        canonical.aliases = sorted(set(canonical.aliases) | set(other.aliases) | {other.title})
        canonical.evolution = normalize_evolution([*canonical.evolution, *other.evolution])
        canonical.out_edges.extend(other.out_edges)
        redirects[node_id] = canonical_id
        del nodes[node_id]
    update_node(canonical, action, raw_entries)
    validate_node_shape(canonical)
    affected.add(canonical_id)


def split_node(
    nodes: dict[str, Node],
    action: dict[str, Any],
    raw_entries: dict[str, RawEntry],
    redirects: dict[str, str],
    affected: set[str],
) -> None:
    target_id = str(action.get("target_id", ""))
    replacements = list(action.get("replacements", []))
    if target_id not in nodes or len(replacements) < 2:
        raise ValidationError("split requires an existing target_id and at least two replacements")
    original = nodes.pop(target_id)
    created: list[str] = []
    occupied = set(nodes)
    for replacement in replacements:
        node_type = str(replacement.get("type") or original.type)
        if node_type not in NODE_TYPES:
            raise ValidationError(f"invalid split replacement type: {node_type}")
        title = required_text(replacement, "title")
        sources = source_ids(replacement) or list(original.sources)
        node_id = deterministic_node_id(node_type, title, min(sources) if sources else target_id, occupied)
        occupied.add(node_id)
        replacement = {**replacement, "summary": replacement.get("summary") or original.summary}
        nodes[node_id] = new_node(node_id, node_type, replacement, sources, raw_entries)
        validate_node_shape(nodes[node_id])
        created.append(node_id)
        affected.add(node_id)
    redirects[target_id] = created[0]


def resolve_edges(
    nodes: dict[str, Node],
    raw_ids: set[str],
    refs: dict[str, str],
    incoming: list[dict[str, Any]],
    redirects: dict[str, str],
) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for node in nodes.values():
        for edge in node.out_edges:
            combined.append({"source_ref": node.id, **edge})
    combined.extend(incoming)
    resolved: list[dict[str, Any]] = []
    for raw_edge in combined:
        source = resolve_ref(str(raw_edge.get("source_ref") or raw_edge.get("source") or ""), refs, redirects)
        target = resolve_ref(str(raw_edge.get("target_ref") or raw_edge.get("target") or ""), refs, redirects)
        edge_type = str(raw_edge.get("type", ""))
        if not source or not target or not edge_type:
            raise ValidationError("every edge requires source_ref, target_ref, and type")
        if source == target:
            raise ValidationError("self-referential edges are not allowed")
        if target not in nodes and not (edge_type == "contains" and target in raw_ids):
            raise ValidationError(f"edge target does not exist: {target}")
        if source not in nodes and source not in raw_ids:
            raise ValidationError(f"edge source does not exist: {source}")
        if edge_type == "belongs_to" and source not in raw_ids:
            raise ValidationError("belongs_to edges must originate from raw entries")
        if edge_type == "contains" and (source not in nodes or nodes[source].type != "topic"):
            raise ValidationError("contains edges must originate from topics")
        if edge_type in SYMMETRIC_EDGE_TYPES and source in nodes and target in nodes and source > target:
            source, target = target, source
        resolved.append({
            "source": source,
            "target": target,
            "type": edge_type,
            "note": str(raw_edge.get("note", "")),
            "inferred": bool(raw_edge.get("inferred", False)),
            "attrs": dict(raw_edge.get("attrs", {})),
        })
    deduped = dedupe_edges(resolved)
    derive_node_sources(nodes, raw_ids, deduped)
    validate_graph(nodes, raw_ids, deduped)
    attach_edges(nodes, deduped)
    return deduped


def derive_node_sources(nodes: dict[str, Node], raw_ids: set[str], edges: list[dict[str, Any]]) -> None:
    sources = {node_id: set(node.sources) for node_id, node in nodes.items()}
    for edge in edges:
        source = str(edge["source"])
        target = str(edge["target"])
        if edge["type"] == "belongs_to" and source in raw_ids and target in nodes:
            sources[target].add(source)

    entity_relation_types = {"about", "involves", "instance_of"}
    changed = True
    while changed:
        changed = False
        for edge in edges:
            source = str(edge["source"])
            target = str(edge["target"])
            if (
                edge["type"] in entity_relation_types
                and source in nodes
                and nodes[source].type in {"event", "statement"}
                and target in nodes
                and nodes[target].type == "entity"
            ):
                expanded = sources[target] | sources[source]
                if expanded != sources[target]:
                    sources[target] = expanded
                    changed = True
    contains = {
        node.id: {
            str(edge["target"])
            for edge in edges
            if edge["type"] == "contains" and edge["source"] == node.id
        }
        for node in nodes.values()
        if node.type == "topic"
    }
    topic_cache: dict[str, set[str]] = {}

    def topic_sources(topic_id: str, visiting: set[str]) -> set[str]:
        if topic_id in topic_cache:
            return topic_cache[topic_id]
        if topic_id in visiting:
            raise ValidationError("topic contains graph has a cycle")
        next_visiting = {*visiting, topic_id}
        values: set[str] = set()
        for member_id in contains.get(topic_id, set()):
            if member_id in raw_ids:
                values.add(member_id)
            elif member_id in nodes and nodes[member_id].type == "topic":
                values.update(topic_sources(member_id, next_visiting))
            elif member_id in nodes:
                values.update(sources[member_id])
        topic_cache[topic_id] = values
        return values

    for node_id, node in nodes.items():
        if node.type == "topic":
            derived = topic_sources(node_id, set())
            sources[node_id] = derived if "topic_contract" in node.attrs else sources[node_id] | derived
    for node_id, values in sources.items():
        nodes[node_id].sources = sorted(values)


def validate_graph(nodes: dict[str, Node], raw_ids: set[str], edges: list[dict[str, Any]]) -> None:
    contains: dict[str, list[str]] = defaultdict(list)
    directed: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge["type"] == "contains" and edge["source"] in nodes:
            contains[str(edge["source"])].append(str(edge["target"]))
        if (
            edge["type"] not in SYMMETRIC_EDGE_TYPES
            and edge["source"] in nodes
            and edge["target"] in nodes
        ):
            directed[str(edge["source"])].append(str(edge["target"]))

    colors: dict[str, int] = {}
    for start_node in sorted(nodes):
        if colors.get(start_node, 0):
            continue
        stack: list[tuple[str, int]] = [(start_node, 0)]
        path: list[str] = []
        path_positions: dict[str, int] = {}
        while stack:
            node_id, child_index = stack[-1]
            if colors.get(node_id, 0) == 0:
                colors[node_id] = 1
                path_positions[node_id] = len(path)
                path.append(node_id)
            children = directed.get(node_id, [])
            if child_index < len(children):
                child = children[child_index]
                stack[-1] = (node_id, child_index + 1)
                color = colors.get(child, 0)
                if color == 0:
                    stack.append((child, 0))
                elif color == 1:
                    cycle = [*path[path_positions[child] :], child]
                    raise ValidationError("directed graph has a cycle: " + " -> ".join(cycle))
                continue
            stack.pop()
            path.pop()
            path_positions.pop(node_id, None)
            colors[node_id] = 2

    for node in nodes.values():
        if node.type != "event":
            continue
        for object_ref in node.semantics.get("object_refs", []):
            if str(object_ref) not in nodes:
                raise ValidationError(f"event {node.id} has dangling object_ref: {object_ref}")
        location_ref = str(node.semantics.get("location_ref") or "")
        if location_ref:
            location = nodes.get(location_ref)
            if not location or location.type != "entity" or location.entity_kind != "place":
                raise ValidationError(f"event {node.id} location_ref must target a place entity")

    for node in nodes.values():
        if node.type != "topic":
            continue
        targets = set(contains.get(node.id, []))
        if "topic_contract" in node.attrs:
            statements = {target for target in targets if target in nodes and nodes[target].type == "statement"}
            if len(targets) < 5 or len(statements) < 2:
                raise ValidationError(f"topic {node.id} requires at least five direct members and two statements")
        else:
            statements = {target for target in targets if target in nodes and nodes[target].type == "statement"}
            if len(targets) < 5 or len(statements) < 2:
                raise ValidationError(f"legacy topic {node.id} requires at least five contained nodes and two statements")
        depths = {node.id: 1}
        queue = deque([node.id])
        while queue:
            parent = queue.popleft()
            child_depth = depths[parent] + 1
            for child in contains.get(parent, []):
                if child not in nodes or nodes[child].type != "topic" or child_depth <= depths.get(child, 0):
                    continue
                if child_depth > 3:
                    raise ValidationError(f"topic {node.id} exceeds maximum depth 3")
                depths[child] = child_depth
                queue.append(child)
        validate_node_shape(node)


def attach_edges(nodes: dict[str, Node], edges: list[dict[str, Any]]) -> None:
    for node in nodes.values():
        node.out_edges = []
        node.backrefs = []
    for edge in edges:
        source = str(edge["source"])
        target = str(edge["target"])
        payload = {key: edge[key] for key in ["target", "type", "note", "inferred", "attrs"]}
        if source in nodes:
            nodes[source].out_edges.append(payload)
        if target in nodes:
            nodes[target].backrefs.append({"source": source, "type": edge["type"], "note": edge["note"]})
    for node in nodes.values():
        node.out_edges = sorted(dedupe_dicts(node.out_edges), key=edge_sort_key)
        node.backrefs = sorted(dedupe_dicts(node.backrefs), key=lambda edge: (str(edge["type"]), str(edge["source"])))


def render_graph(repo: Path, wiki_root: Path, nodes: dict[str, Node], edges: list[dict[str, Any]], raw_entries: dict[str, RawEntry]) -> tuple[str, dict[str, int]]:
    for directory in [*NODE_DIRECTORIES.values(), "timeline"]:
        (wiki_root / directory).mkdir(parents=True, exist_ok=True)
    for node in sorted(nodes.values(), key=lambda item: item.id):
        relative = node_relative_path(node)
        path = wiki_root / relative.relative_to("wiki")
        node.path = repo / relative
        meta: dict[str, Any] = {
            "id": node.id,
            "type": node.type,
            "title": node.title,
            "aliases": sorted(set(node.aliases)),
            "summary": node.summary,
            "sources": sorted(set(node.sources)),
            "out_edges": node.out_edges,
            "backrefs": node.backrefs,
            "attrs": node.attrs,
            "content": {
                "summary": node.summary,
                "detail": node.detail,
                "key_points": node.key_points,
                "evidence": node.evidence,
                "uncertainties": node.uncertainties,
            },
        }
        if node.entity_kind:
            meta["entity_kind"] = node.entity_kind
        if node.event_kind:
            meta["event_kind"] = node.event_kind
        if node.status:
            meta["status"] = node.status
        if node.event_date:
            meta["event_date"] = node.event_date
        if node.current_state:
            meta["current_state"] = node.current_state
        if node.evolution:
            meta["evolution"] = normalize_evolution(node.evolution)
        if node.semantics:
            meta["semantics"] = node.semantics
        frontmatter.write_document(path, meta, render_node_body(node, nodes, raw_entries))
    render_timeline(wiki_root, nodes)
    index = render_index(repo, nodes, raw_entries)
    counts = {node_type: sum(1 for node in nodes.values() if node.type == node_type) for node_type in sorted(NODE_TYPES)}
    counts["edges"] = len(edges)
    return index, counts


def render_node_body(node: Node, nodes: dict[str, Node], raw_entries: dict[str, RawEntry]) -> str:
    lines = [f"# {node.title}", "", node.summary, "", "## 详细内容", "", node.detail or node.summary]
    if node.key_points:
        lines.extend(["", "## 关键点", ""])
        lines.extend(f"- {value}" for value in node.key_points)
    if node.evidence:
        lines.extend(["", "## 证据", ""])
        for item in node.evidence:
            raw_id = str(item.get("source_id", ""))
            claim = str(item.get("claim", ""))
            raw = raw_entries.get(raw_id)
            if raw:
                target = os.path.relpath(raw.path, node.path.parent)
                lines.append(f"- {claim}（[{raw.title}]({target})，{raw_id}）")
            else:
                lines.append(f"- {claim}（{raw_id}）")
    if node.uncertainties:
        lines.extend(["", "## 不确定信息", ""])
        lines.extend(f"- {value}" for value in node.uncertainties)
    if node.type == "entity":
        lines.extend(["", "## 实体信息", "", f"- 类型：{node.entity_kind or '未分类'}"])
    if node.type == "event":
        lines.extend(["", "## 事件", "", f"- 类型：{node.event_kind or '未分类'}", f"- 状态：{node.status or 'planned'}", f"- 日期：{node.event_date or '未指定'}"])
        semantics = node.semantics
        lines.extend([
            f"- 动作／变化：{semantics.get('action', '未提供')}",
            f"- 时间线类别：{EVENT_BASIS_LABELS.get(str(semantics.get('event_basis', '')), semantics.get('event_basis', '未提供'))}",
            f"- 独立回顾价值：{semantics.get('standalone_reason', '未提供')}",
            f"- 与用户关系：{semantics.get('subject_role', '未提供')}",
            f"- 事实性：{semantics.get('factuality', '未提供')}",
            f"- 开始时间：{semantics.get('started_at', '未提供')}",
            f"- 结束时间：{semantics.get('ended_at') or '未提供'}",
            f"- 时间精度：{semantics.get('time_precision', '未提供')}",
            f"- 置信度：{semantics.get('confidence', '未提供')}",
        ])
        location_ref = str(semantics.get("location_ref") or "")
        if location_ref:
            location = nodes.get(location_ref)
            location_text = f"[{location.title}](../{NODE_DIRECTORIES[location.type]}/{location.id}.md)" if location else location_ref
            lines.append(f"- 地点：{location_text}")
        object_refs = [str(value) for value in semantics.get("object_refs", [])]
        if object_refs:
            objects = []
            for object_ref in object_refs:
                target = nodes.get(object_ref)
                objects.append(f"[{target.title}](../{NODE_DIRECTORIES[target.type]}/{target.id}.md)" if target else object_ref)
            lines.append("- 相关对象：" + "、".join(objects))
        event_evidence = normalize_evidence(semantics.get("evidence", []))
        if event_evidence:
            lines.extend(["", "### 事件事实证据", ""])
            for item in event_evidence:
                raw_id = item["source_id"]
                raw = raw_entries.get(raw_id)
                if raw:
                    target = os.path.relpath(raw.path, node.path.parent)
                    lines.append(f"- {item['claim']}（[{raw.title}]({target})，{raw_id}）")
                else:
                    lines.append(f"- {item['claim']}（{raw_id}）")
    if node.type == "statement":
        lines.extend(["", "## 当前洞察", "", node.current_state or node.summary])
        if node.evolution:
            lines.extend(["", "## 认知演进", ""])
            for item in normalize_evolution(node.evolution):
                sources = ", ".join(item.get("sources", []))
                suffix = f"（{sources}）" if sources else ""
                lines.append(f"- {item.get('date') or '未指定日期'}：{item.get('state', '')}{suffix}")
    if node.out_edges:
        lines.extend(["", "## 关系", ""])
        for edge in node.out_edges:
            target = str(edge["target"])
            title = nodes[target].title if target in nodes else raw_entries[target].title if target in raw_entries else target
            if target in nodes:
                lines.append(f"- {edge['type']} → [{title}](../{NODE_DIRECTORIES[nodes[target].type]}/{target}.md)")
            elif target in raw_entries:
                raw_target = os.path.relpath(raw_entries[target].path, node.path.parent)
                lines.append(f"- {edge['type']} → [{title}]({raw_target})")
            else:
                lines.append(f"- {edge['type']} → {target}")
    if node.backrefs:
        lines.extend(["", "## 反向关联", ""])
        for edge in node.backrefs:
            source = str(edge["source"])
            if source in nodes:
                lines.append(f"- {edge['type']} ← [{nodes[source].title}](../{NODE_DIRECTORIES[nodes[source].type]}/{source}.md)")
            else:
                raw = raw_entries.get(source)
                label = raw.title if raw else source
                target = os.path.relpath(raw.path, node.path.parent) if raw else ""
                lines.append(f"- {edge['type']} ← [{label}]({target})" if target else f"- {edge['type']} ← {label}")
    lines.extend(["", "## 来源", ""])
    for raw_id in sorted(set(node.sources)):
        raw = raw_entries.get(raw_id)
        if raw:
            target = os.path.relpath(raw.path, node.path.parent)
            lines.append(f"- [{raw.title}]({target})（{raw_id}）")
        else:
            lines.append(f"- {raw_id}")
    return "\n".join(lines).rstrip() + "\n"


def render_timeline(wiki_root: Path, nodes: dict[str, Node]) -> None:
    status_labels = {
        "planned": "计划中",
        "ongoing": "进行中",
        "occurred": "已发生",
        "cancelled": "已取消",
        "superseded": "已替代",
    }
    entries: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for node in nodes.values():
        if node.type == "event" and node.event_date:
            status = node.status or "planned"
            entries[node.event_date][node.id] = {"node": node, "text": f"{node.title}［{status_labels.get(status, status)}］", "sources": node.sources}
            for history in node.attrs.get("date_history", []):
                historical_date = str(history.get("event_date", ""))[:10]
                if historical_date and historical_date != node.event_date:
                    entries[historical_date][node.id] = {"node": node, "text": f"{node.title}［日期已调整］", "sources": history.get("sources", node.sources)}
    for event_date, by_node in sorted(entries.items()):
        sources = sorted({source for item in by_node.values() for source in item["sources"]})
        meta = {"id": f"timeline-{event_date}", "type": "timeline", "event_date": event_date, "sources": sources}
        lines = [f"# {event_date}", ""]
        for node_id, item in sorted(by_node.items()):
            node = item["node"]
            lines.append(f"- [{item['text']}](../{NODE_DIRECTORIES[node.type]}/{node_id}.md)")
        frontmatter.write_document(wiki_root / "timeline" / f"{event_date}.md", meta, "\n".join(lines) + "\n")


def render_index(repo: Path, nodes: dict[str, Node], raw_entries: dict[str, RawEntry]) -> str:
    lines = ["# 知识库索引", "", "| ID | 类型 | 标题 | 摘要 | 路径 | 最近来源 |", "|-|-|-|-|-|-|"]
    for node in sorted(nodes.values(), key=lambda item: item.id):
        path = node_relative_path(node).as_posix()
        display_type = "洞察" if node.type == "statement" else {"entity": "实体", "event": "事件", "topic": "主题"}.get(node.type, node.type)
        recent = []
        for raw_id in sorted(node.sources)[-3:]:
            raw = raw_entries.get(raw_id)
            if raw:
                recent.append(f"[{raw_id}]({relpath(raw.path, repo)})")
            else:
                recent.append(raw_id)
        lines.append(f"| {node.id} | {display_type} | {escape_cell(node.title)} | {escape_cell(node.summary)} | [{path}]({path}) | {'、'.join(recent)} |")
    if not nodes:
        return "# 知识库索引\n\n暂无编译节点。\n"
    return "\n".join(lines) + "\n"


def validate_node_shape(node: Node) -> None:
    content_issues = node_content_quality_issues(node.type, node.summary, node.detail, node.key_points)
    if content_issues:
        raise ValidationError(f"node {node.id} has weak synthesized content: {content_issues[0]}")
    if not node.evidence:
        raise ValidationError(f"node {node.id} requires source-grounded evidence")
    for item in node.evidence:
        source_id = str(item.get("source_id", ""))
        if not source_id or not str(item.get("claim", "")).strip():
            raise ValidationError(f"invalid evidence for {node.id}")
        if node.type != "topic" or node.sources:
            if source_id not in node.sources:
                raise ValidationError(f"evidence source is not attached to {node.id}: {source_id}")
    if node.type == "entity" and node.entity_kind not in ENTITY_KINDS:
        raise ValidationError(f"invalid entity_kind for {node.id}")
    if node.type == "event":
        if not node.event_kind or node.status not in EVENT_STATUSES or not node.event_date:
            raise ValidationError(f"event {node.id} requires event_kind, event_date, and valid status")
        try:
            parse_date(node.event_date)
        except ValueError as exc:
            raise ValidationError(f"invalid event_date for {node.id}: {node.event_date}") from exc
        semantics = node.semantics
        contract_issues = event_contract_issues(node.title, semantics)
        if contract_issues:
            raise ValidationError(f"event {node.id} violates occurrence contract: {contract_issues[0]}")
        if semantics.get("subject_role") not in EVENT_SUBJECT_ROLES:
            raise ValidationError(f"event {node.id} is not user-centered")
        if not str(semantics.get("action", "")).strip():
            raise ValidationError(f"event {node.id} requires a concrete action or state change")
        started_at = str(semantics.get("started_at", ""))
        try:
            start_value, start_has_time = parse_temporal_anchor(started_at)
        except ValueError as exc:
            raise ValidationError(f"event {node.id} has invalid started_at") from exc
        if start_value.date().isoformat() != node.event_date:
            raise ValidationError(f"event {node.id} requires a matching temporal anchor")
        time_precision = semantics.get("time_precision")
        if time_precision not in EVENT_TIME_PRECISIONS:
            raise ValidationError(f"event {node.id} has invalid time_precision")
        if time_precision == "minute" and not start_has_time:
            raise ValidationError(f"event {node.id} minute precision requires datetime")
        if time_precision in {"day", "week", "month", "year"} and start_has_time:
            raise ValidationError(f"event {node.id} date precision cannot contain a time")
        ended_at = semantics.get("ended_at")
        if time_precision == "range" and not ended_at:
            raise ValidationError(f"event {node.id} range precision requires ended_at")
        if ended_at:
            try:
                end_value, _ = parse_temporal_anchor(str(ended_at))
                if end_value < start_value:
                    raise ValidationError(f"event {node.id} ended_at precedes started_at")
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"event {node.id} has invalid ended_at") from exc
        factuality = str(semantics.get("factuality", ""))
        if factuality not in EVENT_FACTUALITIES:
            raise ValidationError(f"event {node.id} has invalid factuality")
        confidence = semantics.get("confidence")
        if not isinstance(confidence, (int, float)) or not EVENT_CONFIDENCE_THRESHOLD <= confidence <= 1:
            raise ValidationError(f"event {node.id} confidence is below threshold")
        if node.status in {"planned", "ongoing", "occurred"} and node.status != factuality:
            raise ValidationError(f"event {node.id} status does not match factuality")
        event_evidence = normalize_evidence(semantics.get("evidence", []))
        if not event_evidence:
            raise ValidationError(f"event {node.id} requires factual evidence")
        content_sources = {item["source_id"] for item in node.evidence}
        if any(item["source_id"] not in content_sources for item in event_evidence):
            raise ValidationError(f"event evidence must be covered by node content evidence: {node.id}")
    if node.type == "statement" and not node.current_state:
        raise ValidationError(f"statement {node.id} requires current_state")


def action_date(action: dict[str, Any], sources: list[str], raw_entries: dict[str, RawEntry]) -> str:
    explicit = str(action.get("effective_date") or action.get("event_date") or "")
    if explicit:
        return explicit[:10]
    dates = [raw_entries[source].event_date for source in sources if source in raw_entries]
    return min(dates) if dates else now_local().date().isoformat()


def action_change_date(action: dict[str, Any], sources: list[str], raw_entries: dict[str, RawEntry]) -> str:
    explicit = str(action.get("effective_date") or "")
    if explicit:
        return explicit[:10]
    dates = [raw_entries[source].event_date for source in sources if source in raw_entries]
    return min(dates) if dates else now_local().date().isoformat()


def source_ids(action: dict[str, Any]) -> list[str]:
    return sorted(set(str(value) for value in action.get("source_ids", action.get("sources", []))))


def normalize_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def normalize_evidence(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        item = {
            "source_id": str(value.get("source_id", "")).strip(),
            "claim": str(value.get("claim", "")).strip(),
        }
        key = (item["source_id"], item["claim"])
        if all(key) and key not in seen:
            normalized.append(item)
            seen.add(key)
    return normalized


def normalize_semantics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "subject_role": str(value.get("subject_role", "")),
        "action": str(value.get("action", "")).strip(),
        "event_basis": str(value.get("event_basis", "")),
        "standalone_reason": str(value.get("standalone_reason", "")).strip(),
        "object_refs": normalize_strings(value.get("object_refs", [])),
        "started_at": str(value.get("started_at", "")),
        "ended_at": str(value.get("ended_at")) if value.get("ended_at") else None,
        "time_precision": str(value.get("time_precision", "")),
        "factuality": str(value.get("factuality", "")),
        "location_ref": str(value.get("location_ref")) if value.get("location_ref") else None,
        "confidence": value.get("confidence"),
        "evidence": normalize_evidence(value.get("evidence", [])),
    }


def node_content_quality_issues(node_type: str, summary: str, detail: str, key_points: Any) -> list[str]:
    issues: list[str] = []
    compact_detail = "".join(str(detail).split())
    minimum = CONTENT_DETAIL_MINIMUMS.get(node_type, 120)
    if len(compact_detail) < minimum:
        issues.append(f"detail requires at least {minimum} non-whitespace characters for {node_type}")
    paragraphs = [value.strip() for value in re.split(r"\n\s*\n", str(detail)) if value.strip()]
    if len(paragraphs) < CONTENT_MIN_PARAGRAPHS or any(len("".join(value.split())) < 30 for value in paragraphs):
        issues.append(f"detail requires at least {CONTENT_MIN_PARAGRAPHS} substantive paragraphs")
    labels = CONTENT_DETAIL_LABELS.get(node_type, ())
    if labels and (
        len(paragraphs) < len(labels)
        or any(not paragraphs[index].startswith(label) for index, label in enumerate(labels))
    ):
        issues.append(f"detail requires type-specific sections for {node_type}: " + "、".join(labels))
    sentences = [
        "".join(value.split())
        for value in re.split(r"[。！？!?；;\n]+", str(detail))
        if len("".join(value.split())) >= 12
    ]
    if len(set(sentences)) < CONTENT_MIN_SENTENCES:
        issues.append(f"detail requires at least {CONTENT_MIN_SENTENCES} distinct substantive sentences")
    if _looks_like_repeated_content(paragraphs, sentences):
        issues.append("detail cannot use repeated template content")
    compact_summary = "".join(str(summary).split())
    if compact_detail and compact_summary and compact_detail == compact_summary:
        issues.append("detail cannot repeat summary verbatim")
    normalized_points = ["".join(str(value).split()) for value in key_points or [] if str(value).strip()]
    if len(set(normalized_points)) < CONTENT_MIN_KEY_POINTS:
        issues.append(f"key_points requires at least {CONTENT_MIN_KEY_POINTS} distinct items")
    if any(len(value) < CONTENT_MIN_KEY_POINT_CHARS or re.fullmatch(r"(?:关键)?点\d+", value) for value in normalized_points):
        issues.append(f"every key point requires at least {CONTENT_MIN_KEY_POINT_CHARS} non-whitespace characters")
    return issues


def _semantic_compact(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", str(value)).casefold()


def _looks_like_repeated_content(paragraphs: list[str], sentences: list[str]) -> bool:
    labels = tuple(label for values in CONTENT_DETAIL_LABELS.values() for label in values)
    compact_paragraphs = []
    for paragraph in paragraphs:
        value = paragraph
        label = next((candidate for candidate in labels if value.startswith(candidate)), None)
        if label:
            value = value[len(label):]
        compact_paragraphs.append("".join(value.split()))
    for value in compact_paragraphs:
        for width in range(3, min(24, len(value) // 3 + 1)):
            unit = value[:width]
            repeated = (unit * ((len(value) + width - 1) // width))[:len(value)]
            matches = sum(left == right for left, right in zip(value, repeated, strict=True))
            if value and matches / len(value) >= 0.8:
                return True
    if len(compact_paragraphs) >= 2:
        left = set(_ngrams(compact_paragraphs[0], 3))
        right = set(_ngrams(compact_paragraphs[1], 3))
        if left and right and len(left & right) / len(left | right) >= 0.82:
            return True
    comparable_sentences = [
        re.sub(r"(?:第?[一二三四五六七八九十百]+|\d+)", "#", _semantic_compact(value))
        for value in sentences
    ]
    near_duplicate_pairs = sum(
        SequenceMatcher(None, left, right).ratio() >= 0.72
        for index, left in enumerate(comparable_sentences)
        for right in comparable_sentences[index + 1:]
    )
    if near_duplicate_pairs >= 2:
        return True
    return False


def _ngrams(value: str, width: int) -> list[str]:
    return [value[index:index + width] for index in range(max(0, len(value) - width + 1))]


CROSS_NODE_DETAIL_SENTENCE_MIN_CHARS = 24
COMPILER_POLICY_DETAIL_PATTERNS = (
    r"(?:当前)?节点(?:只|仅)(?:确认|保留)",
    r"(?:当前)?节点不把",
    r"后续(?:若|如果)出现新的?实质信息",
    r"后续实质变化需要",
)


def cross_node_duplicate_detail_groups(nodes: dict[str, Node]) -> dict[str, set[str]]:
    occurrences: dict[str, set[str]] = defaultdict(set)
    labels = tuple(label for values in CONTENT_DETAIL_LABELS.values() for label in values)
    for node in nodes.values():
        evidence_claims = {
            _semantic_compact(item.get("claim", ""))
            for item in node.evidence
            if isinstance(item, dict) and item.get("claim")
        }
        fragments: set[str] = set()
        paragraphs = [value.strip() for value in re.split(r"\n\s*\n", node.detail) if value.strip()]
        for paragraph in paragraphs:
            value = paragraph
            label = next((candidate for candidate in labels if value.startswith(candidate)), None)
            if label:
                value = value[len(label):]
            compact_paragraph = _semantic_compact(value)
            if len(compact_paragraph) >= 72 and compact_paragraph not in evidence_claims:
                fragments.add("paragraph:" + compact_paragraph)
            for sentence in re.split(r"[。！？!?；;\n]+", value):
                compact_sentence = _semantic_compact(sentence)
                evidence_sentence = compact_sentence
                for prefix in ("其直接依据是", "它参与"):
                    compact_prefix = _semantic_compact(prefix)
                    if evidence_sentence.startswith(compact_prefix):
                        evidence_sentence = evidence_sentence[len(compact_prefix):]
                        break
                if len(compact_sentence) < CROSS_NODE_DETAIL_SENTENCE_MIN_CHARS or evidence_sentence in evidence_claims:
                    continue
                fragments.add("sentence:" + compact_sentence)
        for fragment in fragments:
            occurrences[fragment].add(node.id)
    return {
        fragment: node_ids
        for fragment, node_ids in occurrences.items()
        if len(node_ids) >= 2
    }


def cross_node_duplicate_detail_node_ids(nodes: dict[str, Node]) -> set[str]:
    return {
        node_id
        for node_ids in cross_node_duplicate_detail_groups(nodes).values()
        for node_id in node_ids
    }


def compiler_policy_detail_node_ids(nodes: dict[str, Node]) -> set[str]:
    patterns = tuple(re.compile(pattern) for pattern in COMPILER_POLICY_DETAIL_PATTERNS)
    return {
        node.id
        for node in nodes.values()
        if any(pattern.search(_semantic_compact(node.detail)) for pattern in patterns)
    }


def _entity_name_forms(node: Node) -> set[str]:
    forms: set[str] = set()
    for value in [node.title, *node.aliases]:
        text = str(value).strip()
        if not text:
            continue
        compact = _semantic_compact(text)
        if compact:
            forms.add(compact)
        without_qualifier = re.sub(r"[（(【\[].*?[）)】\]]", "", text).strip()
        compact_without_qualifier = _semantic_compact(without_qualifier)
        if compact_without_qualifier:
            forms.add(compact_without_qualifier)
        for part in re.split(r"[／/|｜、]", without_qualifier):
            compact_part = _semantic_compact(part)
            if len(compact_part) >= 3:
                forms.add(compact_part)
    return forms


def nonspecific_shared_entity_evidence_node_ids(nodes: dict[str, Node]) -> set[str]:
    claims: dict[str, set[str]] = defaultdict(set)
    for node in nodes.values():
        if node.type != "entity":
            continue
        for item in node.evidence:
            if not isinstance(item, dict):
                continue
            claim = _semantic_compact(item.get("claim", ""))
            if claim:
                claims[claim].add(node.id)

    weak: set[str] = set()
    for claim, node_ids in claims.items():
        if len(node_ids) < 2:
            continue
        for node_id in node_ids:
            node = nodes[node_id]
            if not any(form in claim for form in _entity_name_forms(node)):
                weak.add(node_id)
    return weak


def event_contract_issues(title: str, semantics: Any) -> list[str]:
    if not isinstance(semantics, dict):
        return ["event semantics is missing"]
    issues: list[str] = []
    basis = str(semantics.get("event_basis", ""))
    if basis not in EVENT_BASES:
        issues.append("event_basis is not a timeline-worthy occurrence category")
    reason = "".join(str(semantics.get("standalone_reason", "")).split())
    if len(reason) < 20:
        issues.append("standalone_reason must explain independent timeline value")
    factuality = str(semantics.get("factuality", ""))
    if factuality == "planned" and basis not in PLANNED_EVENT_BASES:
        issues.append("planned event requires an appointment, scheduled commitment, or milestone")
    action = str(semantics.get("action", "")).strip()
    if title and action and normalize_name(title) != normalize_name(action):
        issues.append("event title must equal semantics.action after normalization")
    return issues


def resolve_action_refs(action: dict[str, Any], refs: dict[str, str]) -> dict[str, Any]:
    resolved = dict(action)
    if isinstance(action.get("semantics"), dict):
        semantics = dict(action["semantics"])
        semantics["object_refs"] = [refs.get(str(value), str(value)) for value in semantics.get("object_refs", [])]
        if semantics.get("location_ref"):
            semantics["location_ref"] = refs.get(str(semantics["location_ref"]), str(semantics["location_ref"]))
        resolved["semantics"] = semantics
    if isinstance(action.get("replacements"), list):
        resolved["replacements"] = [resolve_action_refs(value, refs) for value in action["replacements"]]
    if isinstance(action.get("attrs"), dict):
        attrs = dict(action["attrs"])
        contract = attrs.get("topic_contract")
        if isinstance(contract, dict):
            contract = dict(contract)
            facets = []
            for value in contract.get("facets", []):
                facet = dict(value) if isinstance(value, dict) else value
                if isinstance(facet, dict):
                    key = "member_refs" if "member_refs" in facet else "statement_refs"
                    if isinstance(facet.get(key), list):
                        facet[key] = [refs.get(str(item), str(item)) for item in facet[key]]
                facets.append(facet)
            contract["facets"] = facets
            rationales = contract.get("member_rationales")
            if isinstance(rationales, dict):
                contract["member_rationales"] = {
                    refs.get(str(key), str(key)): value
                    for key, value in rationales.items()
                }
            exclusions = []
            for value in contract.get("exclusions", []):
                item = dict(value) if isinstance(value, dict) else value
                if isinstance(item, dict):
                    for key in ("member_ref", "member_id", "statement_id"):
                        if item.get(key):
                            item[key] = refs.get(str(item[key]), str(item[key]))
                exclusions.append(item)
            contract["exclusions"] = exclusions
            attrs["topic_contract"] = contract
        reading = attrs.get("topic_reading")
        if isinstance(reading, dict):
            reading = dict(reading)
            contradictions = []
            for value in reading.get("contradictions", []):
                item = dict(value) if isinstance(value, dict) else value
                if isinstance(item, dict) and isinstance(item.get("member_refs"), list):
                    item["member_refs"] = [refs.get(str(member), str(member)) for member in item["member_refs"]]
                contradictions.append(item)
            reading["contradictions"] = contradictions
            attrs["topic_reading"] = reading
        resolved["attrs"] = attrs
    return resolved


def sanitize_attrs(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key not in {"date_history", "status_history"}}


def required_text(item: dict[str, Any], key: str) -> str:
    value = str(item.get(key, "")).strip()
    if not value:
        raise ValidationError(f"{key} is required")
    return value


def resolve_ref(value: str, refs: dict[str, str], redirects: dict[str, str]) -> str:
    resolved = refs.get(value, value)
    seen: set[str] = set()
    while resolved in redirects and resolved not in seen:
        seen.add(resolved)
        resolved = redirects[resolved]
    return resolved


def normalize_evolution(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in items:
        state = str(item.get("state", "")).strip()
        if not state:
            continue
        normalized.append({
            "date": str(item.get("date", ""))[:10],
            "state": state,
            "sources": sorted(set(str(value) for value in item.get("sources", item.get("source_ids", [])))),
            "note": str(item.get("note", "")),
        })
    # Evolution is an append-only log. Sorting by date/state would reorder two
    # changes recorded on the same day and make a later projection rewrite
    # history instead of preserving it.
    return dedupe_dicts_preserve_order(normalized)


def dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        unique[key] = item
    return [unique[key] for key in sorted(unique)]


def dedupe_dicts_preserve_order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def dedupe_edges(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        key = str(item["source"]), str(item["target"]), str(item["type"])
        if key in unique:
            previous = unique[key]
            merged_attrs = {**dict(previous.get("attrs", {})), **dict(item.get("attrs", {}))}
            item = {
                **previous,
                **item,
                "note": str(item.get("note") or previous.get("note", "")),
                "inferred": bool(previous.get("inferred", False) and item.get("inferred", False)),
                "attrs": merged_attrs,
            }
        unique[key] = item
    return [unique[key] for key in sorted(unique)]


def edge_sort_key(edge: dict[str, Any]) -> tuple[str, str, str]:
    return str(edge.get("type", "")), str(edge.get("target", "")), str(edge.get("note", ""))


def escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def first_body_line(body: str) -> str:
    for line in body.splitlines():
        if line.lstrip().startswith("#"):
            continue
        stripped = line.strip(" -#")
        if stripped:
            return stripped[:160]
    return ""
