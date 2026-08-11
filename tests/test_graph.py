from __future__ import annotations

import unittest
from pathlib import Path

from second_memory.errors import ValidationError
from second_memory.graph import apply_node_actions, resolve_action_refs, resolve_edges
from second_memory.models import Node, RawEntry

from tests.helpers import content


def statement(node_id: str) -> Node:
    return Node(
        id=node_id,
        type="statement",
        title=node_id,
        summary=node_id,
        path=Path(),
        sources=[f"raw-{node_id}"],
        current_state=node_id,
    )


class GraphTest(unittest.TestCase):
    def test_symmetric_edges_are_canonical_and_deduplicated(self) -> None:
        nodes = {"entity-a": Node("entity-a", "entity", "A", "A", Path(), entity_kind="person"), "entity-b": Node("entity-b", "entity", "B", "B", Path(), entity_kind="person")}
        edges = resolve_edges(
            nodes,
            set(),
            {},
            [
                {"source_ref": "entity-b", "target_ref": "entity-a", "type": "related_to"},
                {"source_ref": "entity-a", "target_ref": "entity-b", "type": "related_to"},
            ],
            {},
        )
        self.assertEqual(1, len(edges))
        self.assertEqual(("entity-a", "entity-b"), (edges[0]["source"], edges[0]["target"]))

    def test_topic_threshold_and_cycle_are_rejected(self) -> None:
        nodes = {f"statement-{index}": statement(f"statement-{index}") for index in range(4)}
        nodes["topic-a"] = Node("topic-a", "topic", "A", "A", Path())
        edges = [{"source_ref": "topic-a", "target_ref": node_id, "type": "contains"} for node_id in sorted(nodes) if node_id != "topic-a"]
        with self.assertRaisesRegex(ValidationError, "five contained nodes"):
            resolve_edges(nodes, set(), {}, edges, {})

        nodes["statement-4"] = statement("statement-4")
        nodes["topic-b"] = Node("topic-b", "topic", "B", "B", Path())
        cycle_edges = [
            *[{"source_ref": "topic-a", "target_ref": f"statement-{index}", "type": "contains"} for index in range(5)],
            *[{"source_ref": "topic-b", "target_ref": f"statement-{index}", "type": "contains"} for index in range(5)],
            {"source_ref": "topic-a", "target_ref": "topic-b", "type": "contains"},
            {"source_ref": "topic-b", "target_ref": "topic-a", "type": "contains"},
        ]
        with self.assertRaisesRegex(ValidationError, "cycle"):
            resolve_edges(nodes, set(), {}, cycle_edges, {})

    def test_incremental_merge_is_rejected(self) -> None:
        nodes = {"statement-a": statement("statement-a"), "statement-b": statement("statement-b")}
        with self.assertRaisesRegex(ValidationError, "not allowed"):
            apply_node_actions(nodes, [{"action": "merge", "target_id": "statement-a", "absorbed_ids": ["statement-b"]}], mode="incremental", raw_entries={})

    def test_non_symmetric_directed_cycle_is_rejected(self) -> None:
        nodes = {"statement-a": statement("statement-a"), "statement-b": statement("statement-b")}
        edges = [
            {"source_ref": "statement-a", "target_ref": "statement-b", "type": "supports"},
            {"source_ref": "statement-b", "target_ref": "statement-a", "type": "supports"},
        ]

        with self.assertRaisesRegex(ValidationError, "directed graph has a cycle"):
            resolve_edges(nodes, set(), {}, edges, {})

    def test_long_directed_chain_does_not_use_python_recursion(self) -> None:
        nodes = {f"statement-{index:04d}": statement(f"statement-{index:04d}") for index in range(1200)}
        edges = [
            {
                "source_ref": f"statement-{index:04d}",
                "target_ref": f"statement-{index + 1:04d}",
                "type": "supports",
            }
            for index in range(1199)
        ]

        resolved = resolve_edges(nodes, set(), {}, edges, {})

        self.assertEqual(1199, len(resolved))

    def test_topic_can_contain_raw_and_derives_its_source(self) -> None:
        nodes = {
            "statement-a": statement("statement-a"),
            "statement-b": statement("statement-b"),
        }
        topic_content = content(
            "多个来源共同揭示稳定机制与行动反馈之间的长期关系",
            ["raw-direct-a", "raw-direct-b", "raw-direct-c"],
            node_type="topic",
        )
        nodes["topic-mixed"] = Node(
            id="topic-mixed",
            type="topic",
            title="混合来源主题",
            summary=topic_content["summary"],
            path=Path(),
            attrs={"topic_contract": {}},
            detail=topic_content["detail"],
            key_points=topic_content["key_points"],
            evidence=topic_content["evidence"],
        )
        raw_ids = {"raw-direct-a", "raw-direct-b", "raw-direct-c"}
        edges = [
            {"source_ref": "topic-mixed", "target_ref": "statement-a", "type": "contains"},
            {"source_ref": "topic-mixed", "target_ref": "statement-b", "type": "contains"},
            *[
                {"source_ref": "topic-mixed", "target_ref": raw_id, "type": "contains"}
                for raw_id in sorted(raw_ids)
            ],
        ]

        resolved = resolve_edges(nodes, raw_ids, {}, edges, {})

        self.assertEqual(raw_ids | {"raw-statement-a", "raw-statement-b"}, set(nodes["topic-mixed"].sources))
        self.assertEqual(5, len(resolved))
        self.assertEqual("topic-mixed", nodes["statement-a"].backrefs[0]["source"])

    def test_entity_sources_expand_only_through_explicit_relations(self) -> None:
        entity = Node(
            id="entity-consultation",
            type="entity",
            title="心理咨询",
            summary="用户持续参与的心理咨询对象",
            path=Path(),
            sources=["raw-direct"],
            entity_kind="concept",
        )
        event = Node(
            id="event-consultation",
            type="event",
            title="参加心理咨询",
            summary="用户参加了一次心理咨询",
            path=Path(),
            sources=["raw-event"],
            semantics={"object_refs": [entity.id]},
        )
        insight = Node(
            id="statement-consultation",
            type="statement",
            title="咨询中的长期洞察",
            summary="咨询记录形成了新的长期洞察",
            path=Path(),
            sources=["raw-insight"],
            current_state="咨询记录形成了新的长期洞察",
        )
        unrelated = Node(
            id="statement-shared-source",
            type="statement",
            title="共享来源但无显式关系",
            summary="共享来源不应推导实体关系",
            path=Path(),
            sources=["raw-direct", "raw-unrelated"],
            current_state="共享来源不应推导实体关系",
        )
        nodes = {node.id: node for node in [entity, event, insight, unrelated]}

        resolve_edges(
            nodes,
            {"raw-direct", "raw-event", "raw-insight", "raw-unrelated"},
            {},
            [{"source_ref": insight.id, "target_ref": entity.id, "type": "about"}],
            {},
        )

        self.assertEqual({"raw-direct", "raw-insight"}, set(entity.sources))

    def test_plan_local_topic_refs_resolve_in_contract_and_reading(self) -> None:
        action = {
            "attrs": {
                "topic_contract": {
                    "facets": [{"name": "子主题", "member_refs": ["child-topic", "statement-a"]}],
                    "member_rationales": {
                        "child-topic": {"facet": "子主题"},
                        "statement-a": {"facet": "子主题"},
                    },
                    "exclusions": [{"member_ref": "nearby-topic"}],
                },
                "topic_reading": {
                    "contradictions": [{"member_refs": ["child-topic", "statement-a"]}],
                },
            },
        }

        resolved = resolve_action_refs(
            action,
            {"child-topic": "topic-child-stable", "nearby-topic": "topic-nearby-stable"},
        )

        contract = resolved["attrs"]["topic_contract"]
        self.assertEqual(["topic-child-stable", "statement-a"], contract["facets"][0]["member_refs"])
        self.assertIn("topic-child-stable", contract["member_rationales"])
        self.assertEqual("topic-nearby-stable", contract["exclusions"][0]["member_ref"])
        self.assertEqual(
            ["topic-child-stable", "statement-a"],
            resolved["attrs"]["topic_reading"]["contradictions"][0]["member_refs"],
        )


if __name__ == "__main__":
    unittest.main()
