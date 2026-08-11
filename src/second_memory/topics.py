from __future__ import annotations

import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .models import Node, RawEntry

TOPIC_KINDS = {"life_domain", "cross_domain_pattern", "longitudinal_arc"}
TOPIC_MIN_MEMBERS = 5
TOPIC_MIN_STATEMENTS = 2
TOPIC_MIN_FACETS = 2
TOPIC_MIN_CAPTURE_SESSIONS = 3
TOPIC_LONGITUDINAL_DAYS = 14
TOPIC_MAX_MEMBERSHIPS = 2
TOPIC_MAX_OVERLAP_RATIO = 0.4
TOPIC_CANDIDATE_STATUSES = {"pending", "watching", "rejected", "materialized"}


def validate_topic_plan(
    actions: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    nodes: dict[str, Node],
    raw_entries: dict[str, RawEntry],
    *,
    replace_all: bool,
    candidates: list[dict[str, Any]] | None = None,
) -> None:
    topic_actions: dict[str, dict[str, Any]] = {}
    for action in actions:
        operation = str(action.get("action", ""))
        if operation == "create" and action.get("type") == "topic":
            key = str(action.get("ref", ""))
        else:
            target_id = str(action.get("target_id", ""))
            target = nodes.get(target_id)
            if not target or target.type != "topic":
                continue
            if operation not in {"reinforce", "refine", "change"}:
                raise ValidationError("topic updates support reinforce, refine, or change only")
            key = target_id
        if not key or key in topic_actions:
            raise ValidationError("each topic action requires a unique ref or target_id")
        if action.get("membership_mode") != "replace":
            raise ValidationError("topic actions require membership_mode=replace")
        topic_actions[key] = action

    if replace_all:
        non_topic_actions = [
            action
            for action in actions
            if not (action.get("action") == "create" and action.get("type") == "topic")
        ]
        if non_topic_actions:
            raise ValidationError("topics refresh accepts create topic actions only")

    contains_by_topic: dict[str, list[dict[str, Any]]] = {key: [] for key in topic_actions}
    topic_refs = {*topic_actions, *(node.id for node in nodes.values() if node.type == "topic")}
    for edge in edges:
        source = str(edge.get("source_ref", ""))
        target = str(edge.get("target_ref", ""))
        if (source in topic_refs or target in topic_refs) and not (
            edge.get("type") == "contains" and source in topic_actions
        ):
            raise ValidationError("topic actions may emit incident contains edges only")
        if edge.get("type") != "contains":
            if replace_all:
                raise ValidationError("topics refresh accepts contains edges only")
            continue
        if source not in topic_actions:
            raise ValidationError("contains edges require a matching topic action")
        contains_by_topic[source].append(edge)

    proposed_members: dict[str, set[str]] = {}
    virtual_nodes = dict(nodes)
    for key, action in topic_actions.items():
        virtual_nodes[key] = topic_node_from_action(key, action, nodes.get(key))
        member_edges = contains_by_topic[key]
        member_ids = {str(edge.get("target_ref", "")) for edge in member_edges}
        if len(member_ids) != len(member_edges):
            raise ValidationError(f"topic {key} contains duplicate members")
        proposed_members[key] = member_ids

    for key, action in topic_actions.items():
        validate_topic_contract(
            action,
            proposed_members[key],
            virtual_nodes,
            raw_entries,
            memberships=proposed_members,
        )

    if not topic_actions:
        if replace_all and candidates is not None and recurring_topic_evidence(nodes, raw_entries):
            topic_candidates = [
                item
                for item in candidates
                if item.get("kind") == "topic" and item.get("status") in TOPIC_CANDIDATE_STATUSES
            ]
            if not topic_candidates:
                raise ValidationError("topics refresh must return topics or explicit topic candidate dispositions")
        return

    memberships: dict[str, set[str]] = {}
    if not replace_all:
        for node in nodes.values():
            if node.type != "topic":
                continue
            memberships[node.id] = {
                str(edge.get("target"))
                for edge in node.out_edges
                if edge.get("type") == "contains"
            }
    memberships.update(proposed_members)
    validate_topic_overlap(memberships)


def topic_node_from_action(key: str, action: dict[str, Any], existing: Node | None) -> Node:
    content = action.get("content", {}) if isinstance(action.get("content"), dict) else {}
    return Node(
        id=key,
        type="topic",
        title=str(action.get("title") or (existing.title if existing else key)),
        summary=str(action.get("summary") or content.get("summary") or (existing.summary if existing else "")),
        path=existing.path if existing else Path(),
        sources=[],
        aliases=list(action.get("aliases", existing.aliases if existing else [])),
        attrs=dict(action.get("attrs", existing.attrs if existing else {})),
        detail=str(content.get("detail") or (existing.detail if existing else "")),
        key_points=list(content.get("key_points", existing.key_points if existing else [])),
        evidence=list(content.get("evidence", existing.evidence if existing else [])),
        uncertainties=list(content.get("uncertainties", existing.uncertainties if existing else [])),
    )


def validate_topic_contract(
    action: dict[str, Any],
    member_ids: set[str],
    nodes: dict[str, Node],
    raw_entries: dict[str, RawEntry],
    *,
    memberships: dict[str, set[str]] | None = None,
) -> None:
    label = str(action.get("ref") or action.get("target_id") or "topic")
    if action.get("source_ids", action.get("sources", [])):
        raise ValidationError(f"topic {label} sources must be derived from contained members")
    if len(member_ids) < TOPIC_MIN_MEMBERS:
        raise ValidationError(f"topic {label} requires at least five direct members")
    unknown = sorted(member_id for member_id in member_ids if member_id not in nodes and member_id not in raw_entries)
    if unknown:
        raise ValidationError(f"topic {label} contains unknown members: " + ", ".join(unknown))
    statement_ids = {member_id for member_id in member_ids if member_id in nodes and nodes[member_id].type == "statement"}
    if len(statement_ids) < TOPIC_MIN_STATEMENTS:
        raise ValidationError(f"topic {label} requires at least two direct statements")

    attrs = action.get("attrs")
    contract = attrs.get("topic_contract") if isinstance(attrs, dict) else None
    if not isinstance(contract, dict):
        raise ValidationError(f"topic {label} requires attrs.topic_contract")
    topic_kind = str(contract.get("topic_kind", ""))
    if topic_kind not in TOPIC_KINDS:
        raise ValidationError(f"topic {label} has invalid topic_kind")
    if len(str(contract.get("organizing_question", "")).strip()) < 12:
        raise ValidationError(f"topic {label} requires a durable organizing_question")
    if len(str(contract.get("facet_relationship", "")).strip()) < 30:
        raise ValidationError(f"topic {label} requires a cross-facet relationship")
    if len(str(contract.get("boundary_rule", "")).strip()) < 20:
        raise ValidationError(f"topic {label} requires an explicit membership boundary")

    facets = contract.get("facets")
    if not isinstance(facets, list) or len(facets) < TOPIC_MIN_FACETS:
        raise ValidationError(f"topic {label} requires at least two facets")
    facet_names: set[str] = set()
    facet_members: list[str] = []
    facet_for_member: dict[str, str] = {}
    for facet in facets:
        if not isinstance(facet, dict):
            raise ValidationError(f"topic {label} has invalid facet")
        name = str(facet.get("name", "")).strip()
        summary = str(facet.get("summary", "")).strip()
        refs = [str(value) for value in facet.get("member_refs", facet.get("statement_refs", []))]
        if not name or name in facet_names or len(summary) < 12 or len(set(refs)) < 2:
            raise ValidationError(f"topic {label} facets require unique names, summaries, and two members")
        facet_names.add(name)
        facet_members.extend(refs)
        facet_for_member.update({ref: name for ref in refs})
    if len(facet_members) != len(set(facet_members)) or set(facet_members) != member_ids:
        raise ValidationError(f"topic {label} facets must partition the complete member set")

    rationales = contract.get("member_rationales")
    if not isinstance(rationales, dict) or set(str(value) for value in rationales) != member_ids:
        raise ValidationError(f"topic {label} member_rationales must cover every member exactly")
    rationale_reasons: list[str] = []
    for member_id, value in rationales.items():
        if not isinstance(value, dict):
            raise ValidationError(f"topic {label} has invalid member rationale: {member_id}")
        reason = str(value.get("reason", "")).strip()
        excerpt = str(value.get("supporting_excerpt", "")).strip()
        facet_name = str(value.get("facet", "")).strip()
        if (
            facet_name != facet_for_member.get(str(member_id))
            or len(reason) < 12
            or normalize_excerpt(facet_name) not in normalize_excerpt(reason)
            or not member_contains_excerpt(str(member_id), excerpt, nodes, raw_entries)
        ):
            raise ValidationError(f"topic {label} requires a specific facet and rationale for {member_id}")
        rationale_reasons.append(reason)
    if len(set(rationale_reasons)) != len(rationale_reasons):
        raise ValidationError(f"topic {label} member rationales must describe distinct contributions")

    exclusions = contract.get("exclusions")
    if not isinstance(exclusions, list):
        raise ValidationError(f"topic {label} exclusions must be an array")
    for item in exclusions:
        member_id = str(item.get("member_ref") or item.get("member_id") or item.get("statement_id") or "") if isinstance(item, dict) else ""
        reason = str(item.get("reason", "")).strip() if isinstance(item, dict) else ""
        nearby_excerpt = str(item.get("nearby_excerpt", "")).strip() if isinstance(item, dict) else ""
        if (
            member_id in member_ids
            or (member_id not in nodes and member_id not in raw_entries)
            or len(reason) < 12
            or not member_contains_excerpt(member_id, nearby_excerpt, nodes, raw_entries)
        ):
            raise ValidationError(f"topic {label} has an invalid exclusion boundary")

    member_sources = topic_member_sources(label, member_ids, nodes, raw_entries, memberships or {})
    capture_sessions = {source for source in member_sources if source in raw_entries}
    if len(capture_sessions) < TOPIC_MIN_CAPTURE_SESSIONS:
        raise ValidationError(f"topic {label} requires three independent raw capture sessions")
    if topic_kind == "longitudinal_arc":
        dates = sorted(
            date.fromisoformat(raw_entries[source].event_date[:10])
            for source in member_sources
            if source in raw_entries and raw_entries[source].event_date
        )
        if len(dates) < 2 or (dates[-1] - dates[0]).days < TOPIC_LONGITUDINAL_DAYS:
            raise ValidationError(f"topic {label} longitudinal_arc requires a fourteen-day source span")

    summary = str(action.get("summary", "")).strip()
    container_prefixes = (
        "本主题", "该主题", "组织用户在", "组织用户从", "组织用户以", "组织这些", "组织相关", "组织洞察",
        "汇集用户", "汇集这些", "汇集相关", "汇集洞察", "整理用户", "整理这些", "整理相关", "整理洞察",
        "聚合用户", "聚合这些", "聚合相关", "聚合洞察", "收纳用户在", "收纳用户从", "收纳用户以",
        "收纳这些", "收纳相关", "收纳洞察", "收纳记录", "收纳内容",
    )
    if summary.startswith(container_prefixes):
        raise ValidationError(f"topic {label} summary must state knowledge, not describe a container")
    content = action.get("content", {})
    if len(str(content.get("detail", "")).strip()) < 80:
        raise ValidationError(f"topic {label} requires a graph-wide synthesis")
    key_points = [str(value).strip() for value in content.get("key_points", []) if str(value).strip()]
    if len(key_points) < 3:
        raise ValidationError(f"topic {label} requires at least three key points")
    evidence_sources = {
        str(item.get("source_id", ""))
        for item in content.get("evidence", [])
        if isinstance(item, dict) and str(item.get("claim", "")).strip()
    }
    if len(evidence_sources) < 3 or not evidence_sources <= member_sources:
        raise ValidationError(f"topic {label} evidence requires three member-grounded sources")
    validate_topic_reading(
        label,
        attrs,
        member_ids,
        member_sources,
        nodes,
        raw_entries,
        memberships or {},
    )


def validate_topic_reading(
    label: str,
    attrs: dict[str, Any],
    member_ids: set[str],
    member_sources: set[str],
    nodes: dict[str, Node],
    raw_entries: dict[str, RawEntry],
    memberships: dict[str, set[str]],
) -> None:
    reading = attrs.get("topic_reading")
    if not isinstance(reading, dict):
        raise ValidationError(f"topic {label} requires attrs.topic_reading")
    if len(str(reading.get("core_understanding", "")).strip()) < 30:
        raise ValidationError(f"topic {label} requires a substantive core_understanding")
    evolution = reading.get("evolution")
    if not isinstance(evolution, list) or not evolution:
        raise ValidationError(f"topic {label} requires source-grounded evolution")
    evolution_dates: list[date] = []
    for item in evolution:
        if not isinstance(item, dict):
            raise ValidationError(f"topic {label} evolution must be member-grounded")
        sources = source_list(item)
        try:
            evolution_dates.append(date.fromisoformat(str(item.get("date", ""))[:10]))
        except (TypeError, ValueError):
            raise ValidationError(f"topic {label} has invalid evolution date") from None
        if len(str(item.get("state", "")).strip()) < 12 or not sources or not sources <= member_sources:
            raise ValidationError(f"topic {label} evolution must be member-grounded")
    if evolution_dates != sorted(evolution_dates):
        raise ValidationError(f"topic {label} evolution must be chronological")
    contradictions = reading.get("contradictions")
    if not isinstance(contradictions, list):
        raise ValidationError(f"topic {label} contradictions must be an array")
    for item in contradictions:
        refs = {str(value) for value in item.get("member_refs", [])} if isinstance(item, dict) else set()
        sources = source_list(item)
        if len(refs) < 2 or not refs <= member_ids or len(str(item.get("description", "")).strip()) < 12 or not sources or not sources <= member_sources:
            raise ValidationError(f"topic {label} contradiction must cite members and sources")
        cited_sources = {
            member_id: source_ids_for_member(member_id, nodes, raw_entries, memberships)
            for member_id in refs
        }
        if not sources <= set().union(*cited_sources.values()) or any(
            not (sources & values) for values in cited_sources.values()
        ):
            raise ValidationError(f"topic {label} contradiction sources must match every cited member")
    questions = reading.get("open_questions")
    if not isinstance(questions, list):
        raise ValidationError(f"topic {label} open_questions must be an array")
    for item in questions:
        sources = source_list(item)
        if (
            not isinstance(item, dict)
            or len(str(item.get("question", "")).strip()) < 8
            or len(str(item.get("basis", "")).strip()) < 12
            or not sources
            or not sources <= member_sources
        ):
            raise ValidationError(f"topic {label} open question must be evidence-grounded")
    confidence = reading.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        raise ValidationError(f"topic {label} confidence must be between zero and one")


def source_list(item: Any) -> set[str]:
    if not isinstance(item, dict) or not isinstance(item.get("source_ids"), list):
        return set()
    return {str(value) for value in item["source_ids"] if str(value)}


def topic_member_sources(
    topic_id: str,
    member_ids: set[str],
    nodes: dict[str, Node],
    raw_entries: dict[str, RawEntry],
    memberships: dict[str, set[str]],
    *,
    cache: dict[str, set[str]] | None = None,
    visiting: set[str] | None = None,
) -> set[str]:
    cache = cache if cache is not None else {}
    visiting = visiting if visiting is not None else set()
    if topic_id in cache:
        return cache[topic_id]
    if topic_id in visiting:
        raise ValidationError("topic contains graph has a cycle")
    visiting.add(topic_id)
    sources: set[str] = set()
    for member_id in member_ids:
        sources.update(
            source_ids_for_member(
                member_id,
                nodes,
                raw_entries,
                memberships,
                cache=cache,
                visiting=visiting,
            )
        )
    visiting.remove(topic_id)
    cache[topic_id] = sources
    return sources


def source_ids_for_member(
    member_id: str,
    nodes: dict[str, Node],
    raw_entries: dict[str, RawEntry],
    memberships: dict[str, set[str]],
    *,
    cache: dict[str, set[str]] | None = None,
    visiting: set[str] | None = None,
) -> set[str]:
    if member_id in raw_entries:
        return {member_id}
    node = nodes[member_id]
    if node.type == "topic" and member_id in memberships:
        return topic_member_sources(
            member_id,
            memberships[member_id],
            nodes,
            raw_entries,
            memberships,
            cache=cache,
            visiting=visiting,
        )
    return {str(value) for value in node.sources}


def recurring_topic_evidence(nodes: dict[str, Node], raw_entries: dict[str, RawEntry]) -> bool:
    statements = [node for node in nodes.values() if node.type == "statement"]
    sources = {source for node in statements for source in node.sources if source in raw_entries}
    return len(statements) >= TOPIC_MIN_STATEMENTS and len(nodes) + len(raw_entries) >= TOPIC_MIN_MEMBERS and len(sources) >= TOPIC_MIN_CAPTURE_SESSIONS


def validate_topic_overlap(memberships: dict[str, set[str]]) -> None:
    counts: dict[str, int] = {}
    for members in memberships.values():
        for member in members:
            counts[member] = counts.get(member, 0) + 1
    overused = sorted(member for member, count in counts.items() if count > TOPIC_MAX_MEMBERSHIPS)
    if overused:
        raise ValidationError("members may belong to at most two topics: " + ", ".join(overused))

    topic_ids = sorted(memberships)
    for index, left_id in enumerate(topic_ids):
        left = memberships[left_id]
        for right_id in topic_ids[index + 1 :]:
            right = memberships[right_id]
            smaller = min(len(left), len(right))
            if smaller and len(left & right) / smaller >= TOPIC_MAX_OVERLAP_RATIO:
                raise ValidationError(f"topics overlap too much: {left_id}, {right_id}")


def validate_materialized_topic_contracts(
    nodes: dict[str, Node],
    raw_entries: dict[str, RawEntry],
) -> None:
    memberships = {
        node.id: {
            str(edge.get("target", ""))
            for edge in node.out_edges
            if edge.get("type") == "contains"
        }
        for node in nodes.values()
        if node.type == "topic" and "topic_contract" in node.attrs
    }
    for topic_id, member_ids in memberships.items():
        node = nodes[topic_id]
        validate_topic_contract(
            {
                "ref": node.id,
                "summary": node.summary,
                "attrs": node.attrs,
                "content": {
                    "detail": node.detail,
                    "key_points": node.key_points,
                    "evidence": node.evidence,
                },
            },
            member_ids,
            nodes,
            raw_entries,
            memberships=memberships,
        )
    validate_topic_overlap(memberships)


def member_contains_excerpt(
    member_id: str,
    excerpt: str,
    nodes: dict[str, Node],
    raw_entries: dict[str, RawEntry],
) -> bool:
    normalized_excerpt = normalize_excerpt(excerpt)
    if len(normalized_excerpt) < 8:
        return False
    if member_id in raw_entries:
        raw = raw_entries[member_id]
        annotation = raw.annotations if isinstance(raw.annotations, dict) else {}
        values = [raw.title, str(annotation.get("summary", "")), *raw.tags]
        for channel in ("mentions", "occurrences", "claims"):
            for item in annotation.get(channel, []):
                if not isinstance(item, dict):
                    continue
                values.extend(
                    str(item.get(key, ""))
                    for key in ("text", "title", "action", "current_state", "event_basis", "standalone_reason")
                )
    else:
        node = nodes[member_id]
        contract = node.attrs.get("topic_contract", {}) if isinstance(node.attrs, dict) else {}
        reading = node.attrs.get("topic_reading", {}) if isinstance(node.attrs, dict) else {}
        semantic_evidence = node.semantics.get("evidence", [])
        values = [
            node.summary,
            node.current_state or "",
            node.detail,
            *node.key_points,
            *(str(item.get("state", "")) for item in node.evolution),
            str(node.semantics.get("action", "")),
            str(node.semantics.get("event_basis", "")),
            str(node.semantics.get("standalone_reason", "")),
            *(
                str(item.get("claim") or item.get("text") or item)
                if isinstance(item, dict)
                else str(item)
                for item in semantic_evidence
            ),
            str(contract.get("organizing_question", "")),
            str(reading.get("core_understanding", "")),
        ]
    return any(normalized_excerpt in normalize_excerpt(value) for value in values)


def node_contains_excerpt(node: Node, excerpt: str) -> bool:
    return member_contains_excerpt(node.id, excerpt, {node.id: node}, {})


def normalize_excerpt(value: Any) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", str(value)).casefold()
        if not character.isspace()
    )
