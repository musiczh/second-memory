from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NODE_TYPES = {"entity", "event", "statement", "topic"}
ENTITY_KINDS = {
    "person",
    "organization",
    "place",
    "work",
    "product",
    "tool",
    "project",
    "task",
    "object",
    "concept",
    "emotion",
}
EVENT_STATUSES = {"planned", "ongoing", "occurred", "cancelled", "superseded"}
EVENT_SUBJECT_ROLES = {"user", "directly_affects_user"}
EVENT_FACTUALITIES = {"planned", "ongoing", "occurred"}
EVENT_TIME_PRECISIONS = {"minute", "day", "week", "month", "year", "range"}
EVENT_BASES = {"appointment", "scheduled_commitment", "incident", "milestone", "transaction", "material_change"}
EVENT_BASIS_LABELS = {
    "appointment": "会面／咨询",
    "scheduled_commitment": "明确排期",
    "incident": "具体事件",
    "milestone": "里程碑",
    "transaction": "交易",
    "material_change": "重要变化",
}
PLANNED_EVENT_BASES = {"appointment", "scheduled_commitment", "milestone"}
EVENT_CONFIDENCE_THRESHOLD = 0.8
CONTENT_DETAIL_MINIMUMS = {"entity": 120, "event": 140, "statement": 160, "topic": 240}
CONTENT_DETAIL_LABELS = {
    "entity": ("对象与关系：", "历史与现状："),
    "event": ("发生与背景：", "结果与关联："),
    "statement": ("洞察与依据：", "演进与影响："),
    "topic": ("组织视角：", "脉络与边界："),
}
CONTENT_MIN_PARAGRAPHS = 2
CONTENT_MIN_SENTENCES = 4
CONTENT_MIN_KEY_POINTS = 3
CONTENT_MIN_KEY_POINT_CHARS = 8
PLAN_MODES = {"incremental", "rebuild", "consolidate", "topics"}
NODE_ACTIONS = {"create", "reinforce", "refine", "change", "supersede", "relate", "archive", "merge", "split"}
SYMMETRIC_EDGE_TYPES = {"related_to", "similar_to", "works_with"}


@dataclass(frozen=True)
class RawEntry:
    id: str
    title: str
    created: str
    event_date: str
    tags: list[str]
    path: Path
    body: str
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass
class Node:
    id: str
    type: str
    title: str
    summary: str
    path: Path
    sources: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    entity_kind: str | None = None
    event_kind: str | None = None
    status: str | None = None
    event_date: str | None = None
    current_state: str | None = None
    evolution: list[dict[str, Any]] = field(default_factory=list)
    attrs: dict[str, Any] = field(default_factory=dict)
    out_edges: list[dict[str, Any]] = field(default_factory=list)
    backrefs: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    key_points: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    semantics: dict[str, Any] = field(default_factory=dict)
    body: str = ""


# Existing retrieval/review code historically used Page. Keep the public import
# while making every compiled page a typed graph node in v2.
Page = Node


@dataclass(frozen=True)
class CompilePlan:
    schema_version: int
    session_id: str
    mode: str
    raw_annotations: list[dict[str, Any]]
    node_actions: list[dict[str, Any]]
    out_edges: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    consolidation_memo: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CompilePlan":
        return cls(
            schema_version=int(value.get("schema_version", 0)),
            session_id=str(value.get("session_id", "")),
            mode=str(value.get("mode", "")),
            raw_annotations=list(value.get("raw_annotations", [])),
            node_actions=list(value.get("node_actions", [])),
            out_edges=list(value.get("out_edges", [])),
            candidates=list(value.get("candidates", [])),
            consolidation_memo=str(value.get("consolidation_memo", "")),
        )
