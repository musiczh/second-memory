from __future__ import annotations

import unicodedata
from collections.abc import Iterable

from .models import Node
from .utils import short_hash, slugify


def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def deterministic_node_id(node_type: str, title: str, earliest_source: str, existing_ids: Iterable[str]) -> str:
    prefix = f"{node_type}-"
    base = prefix + slugify(title, fallback=node_type)
    occupied = set(existing_ids)
    if base not in occupied:
        return base
    hashed = f"{base}-{short_hash(node_type, normalize_name(title), earliest_source, length=8)}"
    if hashed not in occupied:
        return hashed
    suffix = 2
    while f"{hashed}-{suffix}" in occupied:
        suffix += 1
    return f"{hashed}-{suffix}"


class Resolver:
    """Deterministic exact/name/alias/lexical resolver used before Agent choice."""

    def __init__(self, nodes: Iterable[Node]) -> None:
        self.nodes = sorted(nodes, key=lambda node: node.id)
        self.by_id = {node.id: node for node in self.nodes}
        self.by_name: dict[str, list[Node]] = {}
        for node in self.nodes:
            for value in [node.title, *node.aliases]:
                key = normalize_name(value)
                if key:
                    self.by_name.setdefault(key, []).append(node)

    def resolve(self, query: str, *, limit: int = 5) -> list[dict[str, object]]:
        if query in self.by_id:
            return [self._payload(self.by_id[query], "exact_id", 100)]
        normalized = normalize_name(query)
        if normalized in self.by_name:
            return [self._payload(node, "title_or_alias", 90) for node in self.by_name[normalized]][:limit]
        terms = lexical_terms(normalized)
        scored: list[tuple[int, Node]] = []
        for node in self.nodes:
            haystack = normalize_name(" ".join([
                node.id,
                node.title,
                *node.aliases,
                node.summary,
                node.detail,
                *node.key_points,
                str(node.semantics.get("action", "")),
                *node.semantics.get("object_refs", []),
            ]))
            score = sum(3 if term in normalize_name(node.title) else 1 for term in terms if term in haystack)
            if score:
                scored.append((score, node))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return [self._payload(node, "lexical", score) for score, node in scored[:limit]]

    @staticmethod
    def _payload(node: Node, match: str, score: int) -> dict[str, object]:
        return {
            "id": node.id,
            "type": node.type,
            "title": node.title,
            "aliases": node.aliases,
            "summary": node.summary,
            "entity_kind": node.entity_kind,
            "event_kind": node.event_kind,
            "content": {
                "detail": node.detail,
                "key_points": node.key_points,
            },
            "semantics": node.semantics,
            "match": match,
            "score": score,
        }


def lexical_terms(normalized: str) -> list[str]:
    terms = [term for term in normalized.replace("-", " ").split() if term]
    expanded: list[str] = []
    for term in terms:
        if len(term) >= 4 and any("\u4e00" <= char <= "\u9fff" for char in term):
            expanded.extend(term[index : index + 2] for index in range(len(term) - 1))
        else:
            expanded.append(term)
    return list(dict.fromkeys(expanded))
