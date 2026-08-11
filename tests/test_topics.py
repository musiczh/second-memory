from __future__ import annotations

import unittest
from pathlib import Path

from second_memory.errors import ValidationError
from second_memory.models import Node, RawEntry
from second_memory.topics import member_contains_excerpt, validate_materialized_topic_contracts, validate_topic_plan

from tests.helpers import content, topic_attrs


class TopicContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raws = {
            f"raw-{index}": RawEntry(
                id=f"raw-{index}",
                title=f"第 {index} 条可验证原料记录",
                created=f"2026-05-{index + 1:02d}T08:00:00+08:00",
                event_date=f"2026-05-{index + 1:02d}",
                tags=[],
                path=Path(f"raw-{index}.md"),
                body=f"原料正文 {index}",
            )
            for index in range(12)
        }
        self.nodes = {
            f"statement-{index}": Node(
                id=f"statement-{index}",
                type="statement",
                title=f"洞察 {index}",
                summary=f"洞察 {index} 的稳定结论",
                path=Path(f"statement-{index}.md"),
                sources=[f"raw-{index}"],
                current_state=f"洞察 {index} 的稳定结论",
            )
            for index in range(12)
        }
        self.nodes["entity-place"] = Node(
            id="entity-place",
            type="entity",
            title="某地点",
            summary="用户长期记录中反复出现的稳定地点实体",
            path=Path("entity-place.md"),
            sources=["raw-0"],
            entity_kind="place",
        )

    def action(
        self,
        ref: str,
        members: list[str],
        exclusion: str,
        *,
        topic_kind: str = "life_domain",
    ) -> dict[str, object]:
        sources = sorted({
            source
            for member in members
            for source in ([member] if member in self.raws else self.nodes[member].sources)
        })
        summary = "稳定机制与行动反馈共同解释这一长期生活领域。"
        attrs = topic_attrs(
            members,
            exclusion,
            topic_kind=topic_kind,
            catalog=[*self.nodes.values(), *self.raws.values()],
        )
        reviewed = [exclusion, *sorted(
            node_id
            for node_id, node in self.nodes.items()
            if node.type == "statement" and node_id not in members and node_id != exclusion
        )]
        attrs["topic_contract"]["exclusions"] = [{
            "member_ref": node_id,
            "reason": f"{node_id} 虽然被反向审查，但不能直接回答当前主题的组织问题。",
            "nearby_excerpt": self.nodes[node_id].current_state,
        } for node_id in reviewed]
        return {
            "action": "create",
            "ref": ref,
            "type": "topic",
            "title": ref,
            "summary": summary,
            "source_ids": [],
            "membership_mode": "replace",
            "attrs": attrs,
            "content": content(
                summary,
                sources,
                node_type="topic",
                detail="成员洞察共同回答同一个跨记录组织问题：长期机制侧面解释稳定状态如何形成，行动反馈侧面解释具体尝试如何修正这一机制。两个侧面缺一不可，并且主题明确排除了仅有表面词汇相似、却不能回答该问题的洞察。",
                key_points=["成员回答同一个组织问题", "两个侧面具有必要关系", "排除边界阻止表面相似归并"],
            ),
        }

    @staticmethod
    def edges(ref: str, members: list[str]) -> list[dict[str, object]]:
        return [{"source_ref": ref, "target_ref": member, "type": "contains"} for member in members]

    def test_valid_topic_contract_passes(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        validate_topic_plan(
            [self.action("topic-valid", members, "statement-5")],
            self.edges("topic-valid", members),
            self.nodes,
            self.raws,
            replace_all=True,
        )

    def test_direct_entity_member_is_allowed_when_grounded(self) -> None:
        members = ["statement-0", "statement-1", "statement-2", "statement-3", "entity-place"]
        action = self.action("topic-entity", members, "statement-4")
        action["attrs"] = topic_attrs(members, "statement-4", catalog=list(self.nodes.values()))
        validate_topic_plan(
            [action],
            self.edges("topic-entity", members),
            self.nodes,
            self.raws,
            replace_all=True,
        )

    def test_direct_raw_member_is_allowed_when_grounded(self) -> None:
        members = ["statement-0", "statement-1", "statement-2", "statement-3", "raw-4"]
        action = self.action("topic-raw", members, "statement-5")
        validate_topic_plan(
            [action],
            self.edges("topic-raw", members),
            self.nodes,
            self.raws,
            replace_all=True,
        )

    def test_structural_topic_without_contract_is_rejected(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        action = self.action("topic-no-contract", members, "statement-5")
        action.pop("attrs")
        with self.assertRaisesRegex(ValidationError, "attrs.topic_contract"):
            validate_topic_plan(
                [action],
                self.edges("topic-no-contract", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_empty_exclusion_boundary_is_allowed(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        action = self.action("topic-no-exclusion", members, "statement-5")
        action["attrs"]["topic_contract"]["exclusions"] = []
        validate_topic_plan(
            [action],
            self.edges("topic-no-exclusion", members),
            self.nodes,
            self.raws,
            replace_all=True,
        )

    def test_topic_reading_is_required(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        action = self.action("topic-no-reading", members, "statement-5")
        action["attrs"].pop("topic_reading")
        with self.assertRaisesRegex(ValidationError, "attrs.topic_reading"):
            validate_topic_plan(
                [action],
                self.edges("topic-no-reading", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_topic_contradiction_must_reference_members_and_sources(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        action = self.action("topic-invalid-contradiction", members, "statement-5")
        action["attrs"]["topic_reading"]["contradictions"] = [{
            "member_refs": ["statement-0", "statement-outside"],
            "description": "两条证据对于同一行动机制形成了需要保留的真实张力。",
            "source_ids": ["raw-0", "raw-11"],
        }]
        with self.assertRaisesRegex(ValidationError, "contradiction must cite"):
            validate_topic_plan(
                [action],
                self.edges("topic-invalid-contradiction", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_topic_contradiction_sources_must_belong_to_cited_members(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        action = self.action("topic-mismatched-contradiction", members, "statement-5")
        action["attrs"]["topic_reading"]["contradictions"] = [{
            "member_refs": ["statement-0", "statement-1"],
            "description": "两条被引用洞察对于同一行动机制形成需要保留的真实张力。",
            "source_ids": ["raw-0", "raw-2"],
        }]
        with self.assertRaisesRegex(ValidationError, "contradiction sources must match"):
            validate_topic_plan(
                [action],
                self.edges("topic-mismatched-contradiction", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_member_excerpt_accepts_raw_annotations_and_event_semantics(self) -> None:
        original = self.raws["raw-0"]
        raw = RawEntry(
            id=original.id,
            title=original.title,
            created=original.created,
            event_date=original.event_date,
            tags=original.tags,
            path=original.path,
            body=original.body,
            annotations={
                "summary": "原料摘要",
                "mentions": [],
                "occurrences": [],
                "claims": [{"text": "稀缺会让即时满足挤压长期投入"}],
            },
        )
        event = Node(
            id="event-incident",
            type="event",
            title="发送周报时写错周会时间",
            summary="用户发送周报时写错了周会时间",
            path=Path("event-incident.md"),
            sources=["raw-1"],
            semantics={"standalone_reason": "发生了可验证的信息错误并直接影响周报准确性"},
        )
        self.assertTrue(member_contains_excerpt(raw.id, "稀缺会让即时满足挤压长期投入", {}, {raw.id: raw}))
        self.assertTrue(member_contains_excerpt(event.id, "发生了可验证的信息错误并直接影响周报准确性", {event.id: event}, self.raws))

    def test_empty_topic_refresh_requires_candidate_disposition(self) -> None:
        with self.assertRaisesRegex(ValidationError, "candidate dispositions"):
            validate_topic_plan([], [], self.nodes, self.raws, replace_all=True, candidates=[])

    def test_materialized_contract_rejects_redirected_member_drift(self) -> None:
        action = self.action(
            "topic-stale-contract",
            ["statement-0", "statement-1", "statement-2", "statement-3", "statement-4"],
            "statement-5",
        )
        topic = Node(
            id="topic-stale-contract",
            type="topic",
            title="主题合同漂移",
            summary=action["summary"],
            path=Path("topic-stale-contract.md"),
            sources=["raw-1", "raw-2", "raw-3", "raw-4"],
            attrs=action["attrs"],
            detail=action["content"]["detail"],
            key_points=action["content"]["key_points"],
            evidence=action["content"]["evidence"],
            out_edges=[
                {"target": statement_id, "type": "contains"}
                for statement_id in ["statement-1", "statement-2", "statement-3", "statement-4", "statement-5"]
            ],
        )
        nodes = {**self.nodes, topic.id: topic}
        with self.assertRaisesRegex(ValidationError, "facets must partition"):
            validate_materialized_topic_contracts(nodes, self.raws)

    def test_member_rationale_must_match_its_actual_facet(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        action = self.action("topic-wrong-facet", members, "statement-5")
        action["attrs"]["topic_contract"]["member_rationales"]["statement-0"]["facet"] = "行动反馈"
        with self.assertRaisesRegex(ValidationError, "specific facet and rationale"):
            validate_topic_plan(
                [action],
                self.edges("topic-wrong-facet", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_member_rationales_cannot_repeat_a_generic_template(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        action = self.action("topic-generic-rationales", members, "statement-5")
        for rationale in action["attrs"]["topic_contract"]["member_rationales"].values():
            rationale["reason"] = f"{rationale['facet']}：该洞察从独立记录中直接解释主题问题的一个必要维度。"
        with self.assertRaisesRegex(ValidationError, "distinct contributions"):
            validate_topic_plan(
                [action],
                self.edges("topic-generic-rationales", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_member_rationale_reason_must_name_its_facet(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        action = self.action("topic-rationale-missing-facet", members, "statement-5")
        action["attrs"]["topic_contract"]["member_rationales"]["statement-0"]["reason"] = (
            "该成员自身证据能够直接回答组织问题，但理由没有标明所属侧面。"
        )
        with self.assertRaisesRegex(ValidationError, "specific facet and rationale"):
            validate_topic_plan(
                [action],
                self.edges("topic-rationale-missing-facet", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_member_rationale_requires_excerpt_from_its_statement(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        action = self.action("topic-invented-excerpt", members, "statement-5")
        action["attrs"]["topic_contract"]["member_rationales"]["statement-0"]["supporting_excerpt"] = "原洞察中完全不存在的财务仓位策略"
        with self.assertRaisesRegex(ValidationError, "specific facet and rationale"):
            validate_topic_plan(
                [action],
                self.edges("topic-invented-excerpt", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_member_rationale_cannot_use_title_only_as_support(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        self.nodes["statement-0"].title = "宽泛但长度足够的洞察标题"
        action = self.action("topic-title-only", members, "statement-5")
        action["attrs"]["topic_contract"]["member_rationales"]["statement-0"]["supporting_excerpt"] = self.nodes["statement-0"].title
        with self.assertRaisesRegex(ValidationError, "specific facet and rationale"):
            validate_topic_plan(
                [action],
                self.edges("topic-title-only", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_member_rationale_can_be_grounded_in_statement_evolution(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        self.nodes["statement-0"].evolution = [{"date": "2026-04-01", "state": "白天缺少自由会把补偿需求推迟到夜晚", "sources": ["raw-0"]}]
        action = self.action("topic-evolution-grounded", members, "statement-5")
        action["attrs"]["topic_contract"]["member_rationales"]["statement-0"]["supporting_excerpt"] = "白天缺少自由会把补偿需求推迟到夜晚"
        validate_topic_plan(
            [action],
            self.edges("topic-evolution-grounded", members),
            self.nodes,
            self.raws,
            replace_all=True,
        )

    def test_exclusion_requires_nearby_excerpt_from_excluded_statement(self) -> None:
        members = [f"statement-{index}" for index in range(5)]
        action = self.action("topic-invented-exclusion", members, "statement-5")
        action["attrs"]["topic_contract"]["exclusions"][0]["nearby_excerpt"] = "被排除洞察中不存在的相似证据"
        with self.assertRaisesRegex(ValidationError, "invalid exclusion boundary"):
            validate_topic_plan(
                [action],
                self.edges("topic-invented-exclusion", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_topics_with_forty_percent_overlap_are_rejected(self) -> None:
        first = ["statement-0", "statement-1", "statement-2", "statement-3", "statement-4"]
        second = ["statement-3", "statement-4", "statement-5", "statement-6", "statement-7"]
        actions = [
            self.action("topic-first", first, "statement-8"),
            self.action("topic-second", second, "statement-9"),
        ]
        with self.assertRaisesRegex(ValidationError, "overlap too much"):
            validate_topic_plan(
                actions,
                [*self.edges("topic-first", first), *self.edges("topic-second", second)],
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_statement_in_three_distinct_topics_is_rejected(self) -> None:
        memberships = {
            "topic-first": ["statement-0", "statement-1", "statement-2", "statement-3", "statement-4"],
            "topic-second": ["statement-0", "statement-5", "statement-6", "statement-7", "statement-8"],
            "topic-third": ["statement-0", "statement-2", "statement-9", "statement-10", "statement-11"],
        }
        actions = [
            self.action(ref, members, "statement-11" if ref != "topic-third" else "statement-8")
            for ref, members in memberships.items()
        ]
        edges = [edge for ref, members in memberships.items() for edge in self.edges(ref, members)]
        with self.assertRaisesRegex(ValidationError, "at most two topics"):
            validate_topic_plan(actions, edges, self.nodes, self.raws, replace_all=True)

    def test_longitudinal_topic_requires_fourteen_day_span(self) -> None:
        members = ["statement-0", "statement-1", "statement-2", "statement-3", "statement-4"]
        with self.assertRaisesRegex(ValidationError, "fourteen-day"):
            validate_topic_plan(
                [self.action("topic-short-arc", members, "statement-5", topic_kind="longitudinal_arc")],
                self.edges("topic-short-arc", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_container_summary_is_rejected(self) -> None:
        members = ["statement-0", "statement-1", "statement-2", "statement-3", "statement-4"]
        action = self.action("topic-container", members, "statement-5")
        action["summary"] = "整理这些洞察形成一个主题"
        action["content"]["summary"] = action["summary"]
        with self.assertRaisesRegex(ValidationError, "must state knowledge"):
            validate_topic_plan(
                [action],
                self.edges("topic-container", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_topic_sources_must_be_derived_from_members(self) -> None:
        members = ["statement-0", "statement-1", "statement-2", "statement-3", "statement-4"]
        action = self.action("topic-explicit-source", members, "statement-5")
        action["source_ids"] = ["raw-0"]
        with self.assertRaisesRegex(ValidationError, "sources must be derived"):
            validate_topic_plan(
                [action],
                self.edges("topic-explicit-source", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )

    def test_topic_action_rejects_raw_belongs_to_edge(self) -> None:
        members = ["statement-0", "statement-1", "statement-2", "statement-3", "statement-4"]
        edges = [
            *self.edges("topic-raw-link", members),
            {"source_ref": "raw-0", "target_ref": "topic-raw-link", "type": "belongs_to"},
        ]
        with self.assertRaisesRegex(ValidationError, "incident contains edges only"):
            validate_topic_plan(
                [self.action("topic-raw-link", members, "statement-5")],
                edges,
                self.nodes,
                self.raws,
                replace_all=False,
            )

    def test_knowledge_summary_about_organizational_ability_is_allowed(self) -> None:
        members = ["statement-0", "statement-1", "statement-2", "statement-3", "statement-4"]
        action = self.action("topic-organization", members, "statement-5")
        action["summary"] = "组织能力会通过责任边界与反馈机制持续形成。"
        action["content"]["summary"] = action["summary"]
        validate_topic_plan(
            [action],
            self.edges("topic-organization", members),
            self.nodes,
            self.raws,
            replace_all=True,
        )

    def test_knowledge_summary_about_organizing_user_feedback_is_allowed(self) -> None:
        members = ["statement-0", "statement-1", "statement-2", "statement-3", "statement-4"]
        action = self.action("topic-user-feedback", members, "statement-5")
        action["summary"] = "组织用户反馈的关键在于建立可追踪的事实与责任闭环。"
        action["content"]["summary"] = action["summary"]
        validate_topic_plan(
            [action],
            self.edges("topic-user-feedback", members),
            self.nodes,
            self.raws,
            replace_all=True,
        )

    def test_container_summary_starting_with_collect_is_rejected(self) -> None:
        members = ["statement-0", "statement-1", "statement-2", "statement-3", "statement-4"]
        action = self.action("topic-collect", members, "statement-5")
        action["summary"] = "收纳这些洞察并形成一个方便浏览的主题容器。"
        action["content"]["summary"] = action["summary"]
        with self.assertRaisesRegex(ValidationError, "must state knowledge"):
            validate_topic_plan(
                [action],
                self.edges("topic-collect", members),
                self.nodes,
                self.raws,
                replace_all=True,
            )


if __name__ == "__main__":
    unittest.main()
