from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from second_memory import frontmatter
from second_memory.compiler import (
    apply_response,
    build_compile_request,
    normalize_plan_candidates,
    normalize_retained_candidates,
    validate_compile_plan,
)
from second_memory.errors import ValidationError
from second_memory.graph import (
    compiler_policy_detail_node_ids,
    cross_node_duplicate_detail_node_ids,
    node_content_quality_issues,
    nonspecific_shared_entity_evidence_node_ids,
)
from second_memory.models import ENTITY_KINDS, CompilePlan, Node, RawEntry
from second_memory.wiki import build_wiki_model, render_html

from tests.helpers import RepositoryTestCase


RAW_ID = "raw-20260805-0900-semantics"


def content(*, source_id: str = RAW_ID, node_type: str = "statement") -> dict[str, Any]:
    sections = {
        "entity": (
            "对象与关系：Second Memory 是本次语义契约验证所针对的稳定项目对象，用户直接维护它的编译规则与知识图谱结构。原料能够确认项目名称和验证关系，没有把测试结论混入实体定义。",
            "历史与现状：当前记录说明该项目正在接受 V2.3 事件发生性、详情质量和来源追溯验证。已有信息足以说明它与用户工作的关系，跨时区验证仍属于尚未覆盖的范围。",
        ),
        "event": (
            "发生与背景：用户在 2026 年 8 月 5 日上午完成了 V2.3 语义契约验证，执行对象是 Second Memory 的编译层。原料明确给出完成动作和时间范围，能够与附带认识分开记录。",
            "结果与关联：本次验证形成了可以独立回顾的完成结果，覆盖内容质量、事件发生性和来源追溯三方面。测试所得认识可另建洞察，但事件页只保留执行经过、完成状态和未覆盖的跨时区边界。",
        ),
        "statement": (
            "洞察与依据：V2.3 语义契约已经完成验证，原料明确记录了用户执行验证的时间、对象和结果。当前认识同时覆盖内容质量、事件发生性与来源追溯，不把测试之外的推断写入节点。",
            "演进与影响：这项认识为后续编译回归提供了可复用判断，后续变化应继续围绕验证结果追加历史。它会影响事件与详情的验收方式，但跨时区场景仍未验证，因此适用边界需要保留。",
        ),
    }
    return {
        "summary": "郑焕完成了语义契约验证",
        "detail": "\n\n".join(sections[node_type]),
        "key_points": ["语义契约覆盖内容质量", "语义契约校验事件发生性", "语义契约校验来源追溯"],
        "evidence": [{"source_id": source_id, "claim": "原料明确说明郑焕完成了验证"}],
        "uncertainties": ["尚未覆盖跨时区事件"],
    }


def event_semantics(*, source_id: str = RAW_ID) -> dict[str, Any]:
    return {
        "subject_role": "user",
        "action": "完成 V2.1 语义契约验证",
        "event_basis": "milestone",
        "standalone_reason": "验证工作已经完成并形成明确结果，即使不考虑相关认识，也值得作为项目里程碑回顾。",
        "object_refs": ["Second Memory"],
        "started_at": "2026-08-05T09:00:00+08:00",
        "ended_at": "2026-08-05T10:00:00+08:00",
        "time_precision": "minute",
        "factuality": "occurred",
        "location_ref": None,
        "confidence": 0.95,
        "evidence": [{"source_id": source_id, "claim": "原料直接记录了完成动作"}],
    }


def extraction_channels(node_type: str) -> dict[str, list[dict[str, Any]]]:
    return {
        "mentions": [{"text": "Second Memory", "kind": "project", "confidence": 0.99}] if node_type == "entity" else [],
        "occurrences": [{
            "action": "完成 V2.1 语义契约验证",
            "subject_role": "user",
            "started_at": "2026-08-05T09:00:00+08:00",
            "factuality": "occurred",
            "event_basis": "milestone",
            "standalone_reason": "验证工作已经完成并形成明确结果，即使不考虑相关认识，也值得作为项目里程碑回顾。",
            "confidence": 0.95,
        }] if node_type == "event" else [],
        "claims": [{"kind": "insight", "text": "语义契约已经完成验证"}] if node_type == "statement" else [],
    }


def create_action(node_type: str = "statement") -> dict[str, Any]:
    action: dict[str, Any] = {
        "action": "create",
        "ref": "semantic-node",
        "type": node_type,
        "title": "V2.1 语义契约验证",
        "summary": "郑焕完成了语义契约验证",
        "source_ids": [RAW_ID],
        "content": content(node_type=node_type),
    }
    if node_type == "statement":
        action.update(current_state="语义契约已经完成验证", effective_date="2026-08-05")
    elif node_type == "entity":
        action["entity_kind"] = "project"
    elif node_type == "event":
        action.update(
            title="完成 V2.1 语义契约验证",
            event_kind="completion",
            status="occurred",
            event_date="2026-08-05",
            semantics=event_semantics(),
        )
    return action


def compile_plan(action: dict[str, Any]) -> CompilePlan:
    channels = extraction_channels(str(action["type"]))
    if action["type"] == "event":
        semantics = action.get("semantics", {})
        occurrence = channels["occurrences"][0]
        if semantics.get("subject_role") in {"user", "directly_affects_user"}:
            occurrence["subject_role"] = semantics["subject_role"]
        if semantics.get("factuality") in {"occurred", "ongoing", "planned"}:
            occurrence["factuality"] = semantics["factuality"]
        if semantics.get("event_basis"):
            occurrence["event_basis"] = semantics["event_basis"]
        if semantics.get("standalone_reason"):
            occurrence["standalone_reason"] = semantics["standalone_reason"]
        if semantics.get("started_at"):
            occurrence["started_at"] = semantics["started_at"]
        if semantics.get("action"):
            occurrence["action"] = semantics["action"]
    return CompilePlan.from_dict({
        "schema_version": 2,
        "session_id": "session-semantics",
        "mode": "incremental",
        "raw_annotations": [{
            "raw_id": RAW_ID,
            "summary": "完成 V2.1 语义契约验证",
            "importance": 5,
            "emotion": "平静",
            **channels,
        }],
        "node_actions": [action],
        "out_edges": [{
            "source_ref": RAW_ID,
            "target_ref": str(action["ref"]),
            "type": "belongs_to",
        }],
        "candidates": [],
        "consolidation_memo": "",
    })


class EntityKindsTest(unittest.TestCase):
    def test_v21_entity_kinds_include_durable_referents(self) -> None:
        self.assertTrue({
            "organization",
            "place",
            "work",
            "product",
            "tool",
            "task",
            "object",
        }.issubset(ENTITY_KINDS))


class CrossNodeDetailQualityTest(unittest.TestCase):
    @staticmethod
    def node(node_id: str, detail: str, evidence_claim: str, *, node_type: str = "statement") -> Node:
        return Node(
            id=node_id,
            type=node_type,
            title=node_id,
            summary=f"{node_id} 的独立结论",
            path=Path(f"/{node_id}.md"),
            sources=[RAW_ID],
            current_state=f"{node_id} 的独立状态",
            detail=detail,
            key_points=[f"{node_id} 关键点一", f"{node_id} 关键点二", f"{node_id} 关键点三"],
            evidence=[{"source_id": RAW_ID, "claim": evidence_claim}],
        )

    def test_detects_reused_long_sentence_across_nodes(self) -> None:
        repeated = "这一整段说明没有引用任何节点自己的来源事实，只是反复宣称未来还需要继续观察和补充，因此不能作为多个节点共享的详情模板"
        nodes = {
            "statement-a": self.node("statement-a", f"洞察与依据：{repeated}。甲节点另有自己的事实。", "甲节点证据"),
            "statement-b": self.node("statement-b", f"洞察与依据：{repeated}。乙节点另有自己的事实。", "乙节点证据"),
        }

        self.assertEqual({"statement-a", "statement-b"}, cross_node_duplicate_detail_node_ids(nodes))

    def test_ignores_short_terms_and_verbatim_evidence_claims(self) -> None:
        evidence = "原料逐字记录了这一项具有充分长度的共同事实，因此两个节点都可以在详情中引用同一句来源证据而不被当成模板"
        nodes = {
            "statement-a": self.node("statement-a", f"洞察与依据：{evidence}。甲节点据此形成独立判断。", evidence),
            "statement-b": self.node("statement-b", f"洞察与依据：{evidence}。乙节点据此形成另一判断。", evidence),
        }

        self.assertEqual(set(), cross_node_duplicate_detail_node_ids(nodes))

    def test_only_allowlisted_prefix_plus_exact_claim_is_exempt(self) -> None:
        evidence = "原料逐字记录了这一项具有充分长度的共同事实，因此两个节点都可以引用这条完全相同的直接来源证据"
        exempt = {
            "statement-a": self.node("statement-a", f"洞察与依据：其直接依据是{evidence}。甲节点据此形成独立判断。", evidence),
            "statement-b": self.node("statement-b", f"洞察与依据：它参与{evidence}。乙节点据此形成另一判断。", evidence),
        }
        repeated_with_suffix = f"其直接依据是{evidence}，并进一步证明了一个并不存在于原始 claim 中的泛化结论"
        not_exempt = {
            "statement-a": self.node("statement-a", f"洞察与依据：{repeated_with_suffix}。甲节点另有事实。", evidence),
            "statement-b": self.node("statement-b", f"洞察与依据：{repeated_with_suffix}。乙节点另有事实。", evidence),
        }

        self.assertEqual(set(), cross_node_duplicate_detail_node_ids(exempt))
        self.assertEqual({"statement-a", "statement-b"}, cross_node_duplicate_detail_node_ids(not_exempt))

    def test_detects_a_and_b_entity_templates_at_twenty_four_char_threshold(self) -> None:
        def sentence(prefix: str, length: int) -> str:
            return prefix + "政" * (length - len(prefix))

        a_sentences = [sentence("甲组", length) for length in (35, 30, 19)]
        b_sentences = [sentence("乙组", length) for length in (32, 24, 25)]
        self.assertEqual([35, 30, 19], [len(value) for value in a_sentences])
        self.assertEqual([32, 24, 25], [len(value) for value in b_sentences])
        nodes = {
            **{
                f"entity-a-{index}": self.node(
                    f"entity-a-{index}",
                    "对象与关系：" + "。".join([*a_sentences, f"甲组实体 {index} 的独立事实"]),
                    f"甲组实体 {index} 的独立证据",
                    node_type="entity",
                )
                for index in range(6)
            },
            **{
                f"entity-b-{index}": self.node(
                    f"entity-b-{index}",
                    "对象与关系：" + "。".join([*b_sentences, f"乙组实体 {index} 的独立事实"]),
                    f"乙组实体 {index} 的独立证据",
                    node_type="entity",
                )
                for index in range(5)
            },
        }

        self.assertEqual(set(nodes), cross_node_duplicate_detail_node_ids(nodes))

    def test_repeated_twenty_three_character_sentence_stays_below_threshold(self) -> None:
        repeated = "短" * 23
        nodes = {
            "entity-a": self.node("entity-a", f"对象与关系：{repeated}。甲实体的独立事实。", "甲实体证据", node_type="entity"),
            "entity-b": self.node("entity-b", f"对象与关系：{repeated}。乙实体的独立事实。", "乙实体证据", node_type="entity"),
        }

        self.assertEqual(set(), cross_node_duplicate_detail_node_ids(nodes))

    def test_single_node_compiler_policy_detail_is_weak_without_banning_normal_node_facts(self) -> None:
        weak = self.node(
            "entity-policy",
            "对象与关系：当前节点只确认来源明确写出的对象关系。后续若出现新的实质信息，再更新这一综合。",
            "来源明确写出了对象关系",
            node_type="entity",
        )
        factual = self.node(
            "entity-factual",
            "对象与关系：该图谱节点连接原始记录和经过验证的实体页面，用户可以沿边回溯来源。",
            "图谱节点连接原始记录和实体页面",
            node_type="entity",
        )

        self.assertEqual({"entity-policy"}, compiler_policy_detail_node_ids({weak.id: weak, factual.id: factual}))


class EntityEvidenceSpecificityTest(unittest.TestCase):
    @staticmethod
    def entity(node_id: str, title: str, claim: str, *, aliases: list[str] | None = None) -> Node:
        return Node(
            id=node_id,
            type="entity",
            title=title,
            aliases=aliases or [],
            summary=f"{title} 的实体摘要",
            path=Path(f"/{node_id}.md"),
            sources=[RAW_ID],
            evidence=[{"source_id": RAW_ID, "claim": claim}],
        )

    def test_shared_claim_flags_only_entities_not_directly_named(self) -> None:
        claim = "房地产、消费、半导体、股票账户和资产配置均在这条原料中被直接讨论。"
        explicit_titles = ["房地产", "消费", "半导体", "股票账户", "资产配置"]
        weak_titles = ["全球央行", "铜", "齐俊杰", "发布指标排查", "埋点与日志查询平台", "跨端代码库"]
        nodes = {
            f"entity-{index}": self.entity(f"entity-{index}", title, claim)
            for index, title in enumerate([*explicit_titles, *weak_titles])
        }

        self.assertEqual(
            {f"entity-{index}" for index in range(len(explicit_titles), len(explicit_titles) + len(weak_titles))},
            nonspecific_shared_entity_evidence_node_ids(nodes),
        )

    def test_alias_and_single_entity_claim_do_not_create_false_positive(self) -> None:
        shared_claim = "OpenAI 与房地产都在同一条来源中被直接点名。"
        nodes = {
            "entity-openai": self.entity("entity-openai", "OpenAI 公司（美国）", shared_claim, aliases=["OpenAI"]),
            "entity-property": self.entity("entity-property", "房地产", shared_claim),
            "entity-sole": self.entity("entity-sole", "未在自己的唯一 claim 中点名的对象", "这里只记录了一段没有实体名的说明。"),
        }

        self.assertEqual(set(), nonspecific_shared_entity_evidence_node_ids(nodes))


class CompilePlanSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = RawEntry(
            id=RAW_ID,
            title="语义契约验证",
            created="2026-08-05T09:00:00+08:00",
            event_date="2026-08-05",
            tags=["test"],
            path=Path("/tmp/semantic-raw.md"),
            body="郑焕在上午完成了 V2.1 语义契约验证。",
        )

    def validate(self, action: dict[str, Any]) -> None:
        validate_compile_plan(compile_plan(action), [self.raw], {RAW_ID: self.raw})

    def test_create_requires_content(self) -> None:
        action = create_action()
        del action["content"]

        with self.assertRaisesRegex(ValidationError, "content"):
            self.validate(action)

    def test_content_requires_every_fixed_field(self) -> None:
        for field in ["summary", "detail", "key_points", "evidence", "uncertainties"]:
            with self.subTest(field=field):
                action = create_action()
                del action["content"][field]

                with self.assertRaisesRegex(ValidationError, field):
                    self.validate(action)

    def test_content_evidence_requires_source_id_and_claim(self) -> None:
        for field in ["source_id", "claim"]:
            with self.subTest(field=field):
                action = create_action()
                del action["content"]["evidence"][0][field]

                with self.assertRaisesRegex(ValidationError, field):
                    self.validate(action)

    def test_content_evidence_source_must_be_in_compile_scope(self) -> None:
        action = create_action()
        action["content"]["evidence"][0]["source_id"] = "raw-outside-scope"

        with self.assertRaisesRegex(ValidationError, "source"):
            self.validate(action)

    def test_content_requires_multiple_substantive_paragraphs_and_three_points(self) -> None:
        action = create_action()
        action["content"]["detail"] = "同一段内容" * 40
        with self.assertRaisesRegex(ValidationError, "paragraph"):
            self.validate(action)

        action = create_action()
        action["content"]["key_points"] = ["重复点", "重复点", "另一个点"]
        with self.assertRaisesRegex(ValidationError, "key_points"):
            self.validate(action)

    def test_content_rejects_repeated_padding_and_wrong_type_sections(self) -> None:
        action = create_action("statement")
        action["content"]["detail"] = (
            "洞察与依据：" + "同义填充内容" * 40
            + "\n\n演进与影响：" + "另一组填充内容" * 40
        )
        with self.assertRaisesRegex(ValidationError, "sentences|repeated|filler"):
            self.validate(action)
        issues = node_content_quality_issues(
            "statement",
            action["summary"],
            action["content"]["detail"],
            action["content"]["key_points"],
        )
        self.assertTrue(any("repeated template" in issue for issue in issues))

        action = create_action("event")
        action["content"]["detail"] = content(node_type="statement")["detail"]
        with self.assertRaisesRegex(ValidationError, "type-specific sections"):
            self.validate(action)

        action = create_action("entity")
        action["content"]["key_points"] = ["点1", "点2", "点3"]
        with self.assertRaisesRegex(ValidationError, "key point"):
            self.validate(action)

    def test_event_requires_semantics(self) -> None:
        action = create_action("event")
        del action["semantics"]

        with self.assertRaisesRegex(ValidationError, "semantics"):
            self.validate(action)

    def test_event_rejects_semantics_without_action(self) -> None:
        action = create_action("event")
        del action["semantics"]["action"]

        with self.assertRaisesRegex(ValidationError, "action"):
            self.validate(action)

    def test_event_rejects_raw_event_date_as_its_only_time_anchor(self) -> None:
        action = create_action("event")
        action["semantics"]["started_at"] = None
        action["semantics"]["ended_at"] = None
        action["semantics"]["time_precision"] = "day"

        with self.assertRaisesRegex(ValidationError, "time|started_at|ended_at"):
            self.validate(action)

    def test_event_rejects_low_confidence(self) -> None:
        action = create_action("event")
        action["semantics"]["confidence"] = 0.0

        with self.assertRaisesRegex(ValidationError, "confidence"):
            self.validate(action)

    def test_event_rejects_non_user_related_subject(self) -> None:
        action = create_action("event")
        action["semantics"]["subject_role"] = "third_party"

        with self.assertRaisesRegex(ValidationError, "subject_role"):
            self.validate(action)

    def test_event_rejects_unsupported_time_precision(self) -> None:
        action = create_action("event")
        action["semantics"]["time_precision"] = "second"

        with self.assertRaisesRegex(ValidationError, "time_precision"):
            self.validate(action)

    def test_event_rejects_unsupported_factuality(self) -> None:
        action = create_action("event")
        action["semantics"]["factuality"] = "hypothetical"

        with self.assertRaisesRegex(ValidationError, "factuality"):
            self.validate(action)

    def test_valid_user_related_events_pass_all_fact_gates(self) -> None:
        for subject_role in ["user", "directly_affects_user"]:
            with self.subTest(subject_role=subject_role):
                action = create_action("event")
                action["semantics"]["subject_role"] = subject_role

                self.validate(action)

    def test_event_rejects_title_action_mismatch(self) -> None:
        for title, semantic_action, event_basis in [
            ("第 19 次心理咨询重构靠谱标准", "参加第 19 次心理咨询", "appointment"),
            ("参加第 19 次心理咨询", "参加第 19 次心理咨询并重构靠谱标准", "appointment"),
        ]:
            with self.subTest(title=title):
                action = create_action("event")
                action["title"] = title
                action["semantics"].update(action=semantic_action, event_basis=event_basis)
                with self.assertRaisesRegex(ValidationError, "occurrence contract|title"):
                    self.validate(action)

    def test_event_accepts_appointment_with_plan_as_noun(self) -> None:
        action = create_action("event")
        action["title"] = "参加项目计划会"
        action["event_kind"] = "meeting"
        action["semantics"].update(
            action="参加项目计划会",
            event_basis="appointment",
            standalone_reason="用户实际参加了有明确时间边界的项目会议，去掉会议所得认识后仍值得独立回顾。",
        )

        self.validate(action)

    def test_event_contract_does_not_lexically_reject_legitimate_appointment_names(self) -> None:
        for title in ["参加认知科学研讨会", "参加启发式算法研讨会", "参加价值观培训会", "参加情绪管理课程"]:
            with self.subTest(title=title):
                action = create_action("event")
                action["title"] = title
                action["event_kind"] = "learning_appointment"
                action["semantics"].update(
                    action=title,
                    event_basis="appointment",
                    standalone_reason="用户实际参加了有明确时间边界的正式活动，去掉活动所得认识后仍值得独立回顾。",
                )
                self.validate(action)

    def test_event_accepts_consequential_weekly_report_error(self) -> None:
        action = create_action("event")
        action["title"] = "发送周报时把周会时间写错"
        action["event_kind"] = "work_error"
        action["semantics"].update(
            action="发送周报时把周会时间写错",
            event_basis="incident",
            standalone_reason="周报中的具体时间错误已经实际发生并会影响同事获取会议信息，去掉反思后仍值得回顾。",
        )

        self.validate(action)

    def test_event_accepts_bounded_consultation_occurrence_without_insight_title(self) -> None:
        action = create_action("event")
        action["title"] = "参加第 19 次心理咨询"
        action["event_kind"] = "consultation"
        action["semantics"].update(
            action="参加第 19 次心理咨询",
            event_basis="appointment",
            standalone_reason="用户实际参加了有明确次数与时间边界的咨询会谈，脱离会谈所得洞察后仍可独立回顾。",
        )

        self.validate(action)

    def test_planned_event_rejects_incident_basis(self) -> None:
        action = create_action("event")
        action["status"] = "planned"
        action["semantics"].update(factuality="planned", event_basis="incident")

        with self.assertRaisesRegex(ValidationError, "planned event"):
            self.validate(action)

    def test_empty_channels_allow_zero_node_compile(self) -> None:
        plan = CompilePlan.from_dict({
            "schema_version": 2,
            "session_id": "session-empty",
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": RAW_ID,
                "summary": "一次没有耐久节点价值的普通聊天",
                "importance": 1,
                "emotion": "",
                "mentions": [],
                "occurrences": [],
                "claims": [],
            }],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": "",
        })

        validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw})

    def test_event_semantics_requires_every_fixed_field(self) -> None:
        required = [
            "subject_role",
            "action",
            "event_basis",
            "standalone_reason",
            "object_refs",
            "started_at",
            "ended_at",
            "time_precision",
            "factuality",
            "location_ref",
            "confidence",
            "evidence",
        ]
        for field in required:
            with self.subTest(field=field):
                action = create_action("event")
                del action["semantics"][field]

                with self.assertRaisesRegex(ValidationError, field):
                    self.validate(action)

    def test_node_type_requires_its_matching_extraction_channel(self) -> None:
        for node_type, channel in [
            ("entity", "mentions"),
            ("event", "occurrences"),
            ("statement", "claims"),
        ]:
            with self.subTest(node_type=node_type, channel=channel):
                plan = compile_plan(create_action(node_type))
                plan.raw_annotations[0][channel] = []

                with self.assertRaisesRegex(ValidationError, channel):
                    validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw})

    def test_extraction_channels_reject_invalid_items(self) -> None:
        invalid_items = [
            ("mentions", [{}], "mention"),
            ("occurrences", [None], "occurrence"),
            ("claims", [""], "claim"),
        ]
        for channel, value, error in invalid_items:
            with self.subTest(channel=channel):
                plan = compile_plan(create_action({
                    "mentions": "entity",
                    "occurrences": "event",
                    "claims": "statement",
                }[channel]))
                plan.raw_annotations[0][channel] = value

                with self.assertRaisesRegex(ValidationError, error):
                    validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw})

    def test_event_occurrence_must_match_event_action(self) -> None:
        plan = compile_plan(create_action("event"))
        plan.raw_annotations[0]["occurrences"][0]["action"] = "完成厨房设备验证"

        with self.assertRaisesRegex(ValidationError, "matching raw annotation occurrences"):
            validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw})

    def test_event_occurrence_must_match_full_minute_anchor(self) -> None:
        plan = compile_plan(create_action("event"))
        plan.raw_annotations[0]["occurrences"][0]["started_at"] = "2026-08-05T20:00:00+08:00"

        with self.assertRaisesRegex(ValidationError, "matching raw annotation occurrences"):
            validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw})

    def test_event_occurrence_rejects_conflicting_material_facts(self) -> None:
        conflicts = [
            ("完成五公里跑步", "完成十公里跑步", "milestone"),
            ("支付100元", "支付900元", "transaction"),
            ("购买 iPhone 15", "购买 iPhone 16", "transaction"),
        ]
        for occurrence_action, event_action, event_basis in conflicts:
            with self.subTest(occurrence_action=occurrence_action, event_action=event_action):
                action = create_action("event")
                action["title"] = event_action
                action["semantics"].update(action=event_action, event_basis=event_basis)
                plan = compile_plan(action)
                plan.raw_annotations[0]["occurrences"][0]["action"] = occurrence_action

                with self.assertRaisesRegex(ValidationError, "matching raw annotation occurrences"):
                    validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw})

    def test_event_update_cannot_reuse_target_for_conflicting_version(self) -> None:
        existing = Node(
            id="event-existing-phone-purchase",
            type="event",
            title="购买 iPhone 15",
            summary="用户购买 iPhone 15",
            path=Path("/tmp/event-existing-phone-purchase.md"),
            sources=[RAW_ID],
            event_kind="purchase",
            status="occurred",
            event_date="2026-08-05",
            semantics={**event_semantics(), "action": "购买 iPhone 15", "event_basis": "transaction"},
        )
        plan = compile_plan(create_action("event"))
        action = plan.node_actions[0]
        action.update(action="change", target_id=existing.id, title="购买 iPhone 16")
        action.pop("ref")
        action["semantics"].update(action="购买 iPhone 16", event_basis="transaction")
        plan.raw_annotations[0]["occurrences"][0]["action"] = "购买 iPhone 16"
        plan.raw_annotations[0]["occurrences"][0]["event_basis"] = "transaction"
        plan.out_edges[0]["target_ref"] = existing.id

        with self.assertRaisesRegex(ValidationError, "matching raw annotation occurrences"):
            validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw}, {existing.id: existing})

    def test_event_occurrence_rejects_explicit_negation_mismatch(self) -> None:
        for occurrence_action, event_action, event_basis in [
            ("未完成跑步训练", "完成跑步训练", "milestone"),
            ("未能支付100元", "支付100元", "transaction"),
        ]:
            with self.subTest(occurrence_action=occurrence_action):
                action = create_action("event")
                action["title"] = event_action
                action["semantics"].update(action=event_action, event_basis=event_basis)
                plan = compile_plan(action)
                plan.raw_annotations[0]["occurrences"][0]["action"] = occurrence_action

                with self.assertRaisesRegex(ValidationError, "matching raw annotation occurrences"):
                    validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw})

    def test_belongs_to_must_match_source_grounded_node_action(self) -> None:
        plan = compile_plan(create_action())
        plan.out_edges[0]["target_ref"] = "unmatched-node-ref"

        with self.assertRaisesRegex(ValidationError, "belongs_to"):
            validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw})

    def source_only_entity_plan(self, existing: Node) -> CompilePlan:
        return CompilePlan.from_dict({
            "schema_version": 2,
            "session_id": "session-source-only-entity",
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": RAW_ID,
                "summary": "本条原料再次明确提到 Second Memory 项目",
                "importance": 3,
                "emotion": "平静",
                "mentions": [{
                    "text": "Second Memory",
                    "kind": "project",
                    "target_id": existing.id,
                    "confidence": 0.99,
                }],
                "occurrences": [],
                "claims": [],
            }],
            "node_actions": [{
                "action": "reinforce",
                "target_id": existing.id,
                "type": "entity",
                "source_ids": [RAW_ID],
            }],
            "out_edges": [{
                "source_ref": RAW_ID,
                "target_ref": existing.id,
                "type": "belongs_to",
            }],
            "candidates": [],
            "consolidation_memo": "",
        })

    def test_source_only_reinforce_accepts_existing_entity_mention(self) -> None:
        existing = Node(
            id="entity-second-memory",
            type="entity",
            title="Second Memory",
            summary="用户持续维护的个人知识库项目",
            path=Path("/tmp/entity-second-memory.md"),
            sources=["raw-existing"],
            entity_kind="project",
        )
        validate_compile_plan(
            self.source_only_entity_plan(existing),
            [self.raw],
            {RAW_ID: self.raw},
            {existing.id: existing},
        )

    def test_source_only_reinforce_rejects_content_or_mutable_fields(self) -> None:
        existing = Node(
            id="entity-second-memory",
            type="entity",
            title="Second Memory",
            summary="用户持续维护的个人知识库项目",
            path=Path("/tmp/entity-second-memory.md"),
            sources=["raw-existing"],
            entity_kind="project",
        )
        plan = self.source_only_entity_plan(existing)
        plan.node_actions[0]["summary"] = "不应由来源补强动作改写的摘要"
        with self.assertRaisesRegex(ValidationError, "source-only reinforce cannot mutate"):
            validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw}, {existing.id: existing})

    def test_source_only_reinforce_requires_matching_belongs_to_edge(self) -> None:
        existing = Node(
            id="entity-second-memory",
            type="entity",
            title="Second Memory",
            summary="用户持续维护的个人知识库项目",
            path=Path("/tmp/entity-second-memory.md"),
            sources=["raw-existing"],
            entity_kind="project",
        )
        plan = self.source_only_entity_plan(existing)
        plan.out_edges.clear()
        with self.assertRaisesRegex(ValidationError, "belongs_to"):
            validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw}, {existing.id: existing})

    def test_source_only_reinforce_is_incremental_or_rebuild_only(self) -> None:
        existing = Node(
            id="entity-second-memory",
            type="entity",
            title="Second Memory",
            summary="用户持续维护的个人知识库项目",
            path=Path("/tmp/entity-second-memory.md"),
            sources=["raw-existing"],
            entity_kind="project",
        )
        plan = replace(
            self.source_only_entity_plan(existing),
            mode="consolidate",
            raw_annotations=[],
            out_edges=[],
        )
        with self.assertRaisesRegex(ValidationError, "incremental or rebuild"):
            validate_compile_plan(plan, [], {RAW_ID: self.raw}, {existing.id: existing})

    def test_source_only_reinforce_rejects_legacy_sources_alias(self) -> None:
        existing = Node(
            id="entity-second-memory",
            type="entity",
            title="Second Memory",
            summary="用户持续维护的个人知识库项目",
            path=Path("/tmp/entity-second-memory.md"),
            sources=["raw-existing"],
            entity_kind="project",
        )
        plan = self.source_only_entity_plan(existing)
        plan.node_actions[0]["sources"] = plan.node_actions[0].pop("source_ids")
        with self.assertRaisesRegex(ValidationError, "source_ids only"):
            validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw}, {existing.id: existing})

    def test_retained_candidates_follow_merge_redirects_and_drop_resolved_structures(self) -> None:
        canonical = Node(
            id="statement-canonical",
            type="statement",
            title="合并后的洞察",
            summary="合并后的稳定结论",
            path=Path("/tmp/statement-canonical.md"),
            sources=[RAW_ID],
            current_state="合并后的稳定结论",
        )
        peer = Node(
            id="statement-peer",
            type="statement",
            title="仍需观察的洞察",
            summary="仍需观察的稳定结论",
            path=Path("/tmp/statement-peer.md"),
            sources=[RAW_ID],
            current_state="仍需观察的稳定结论",
        )
        retained = normalize_retained_candidates(
            [
                {
                    "candidate_id": "candidate-resolved-merge",
                    "kind": "merge",
                    "node_ids": [canonical.id, "statement-absorbed"],
                    "reason": "已经通过结构合并完成处置",
                    "confidence": 0.9,
                },
                {
                    "candidate_id": "candidate-still-live",
                    "kind": "topic",
                    "node_ids": ["statement-absorbed", peer.id],
                    "title": "仍需观察的主题",
                    "topic_kind": "life_domain",
                    "status": "watching",
                    "reason": "合并后仍有两个存活成员需要继续观察",
                    "confidence": 0.6,
                },
            ],
            {"statement-absorbed": canonical.id},
            {canonical.id: canonical, peer.id: peer},
            set(),
        )

        self.assertEqual(["candidate-still-live"], [item["candidate_id"] for item in retained])
        self.assertEqual(
            [canonical.id, peer.id],
            retained[0]["node_ids"],
        )

    def test_echoed_candidate_for_split_target_is_disposed_before_redirect(self) -> None:
        replacement = Node(
            id="statement-replacement-a",
            type="statement",
            title="拆分后的第一条洞察",
            summary="拆分后的第一条稳定结论",
            path=Path("/tmp/statement-replacement-a.md"),
            sources=[RAW_ID],
            current_state="拆分后的第一条稳定结论",
        )
        normalized = normalize_plan_candidates(
            [{
                "candidate_id": "candidate-before-split",
                "kind": "topic",
                "node_ids": ["statement-original", replacement.id],
                "title": "拆分前的观察主题",
                "topic_kind": "life_domain",
                "status": "watching",
                "reason": "该候选依赖尚未拆分的原始洞察",
                "confidence": 0.5,
            }],
            {},
            {"statement-original": replacement.id},
            {replacement.id: replacement},
            {"statement-original"},
        )

        self.assertEqual([], normalized)

    def test_relate_and_archive_cannot_mutate_semantic_fields(self) -> None:
        existing = Node(
            id="statement-existing",
            type="statement",
            title="现有洞察",
            summary="现有摘要",
            path=Path("/tmp/statement-existing.md"),
            sources=[RAW_ID],
            current_state="现有状态",
        )
        forbidden_values = {
            "summary": "被越权修改的摘要",
            "current_state": "被越权修改的状态",
            "content": content(),
        }
        for operation in ["relate", "archive"]:
            for field, value in forbidden_values.items():
                with self.subTest(operation=operation, field=field):
                    plan = CompilePlan.from_dict({
                        "schema_version": 2,
                        "session_id": "session-semantic-mutation",
                        "mode": "incremental",
                        "raw_annotations": [],
                        "node_actions": [{
                            "action": operation,
                            "target_id": existing.id,
                            field: value,
                        }],
                        "out_edges": [],
                        "candidates": [],
                        "consolidation_memo": "",
                    })

                    with self.assertRaisesRegex(ValidationError, "cannot mutate"):
                        validate_compile_plan(plan, [], {}, {existing.id: existing})

    def test_consolidate_rejects_non_empty_raw_annotations(self) -> None:
        annotation = compile_plan(create_action()).raw_annotations[0]
        plan = CompilePlan.from_dict({
            "schema_version": 2,
            "session_id": "session-consolidate",
            "mode": "consolidate",
            "raw_annotations": [annotation],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": "",
        })

        with self.assertRaisesRegex(ValidationError, "consolidate raw_annotations"):
            validate_compile_plan(plan, [self.raw], {RAW_ID: self.raw})

    def test_event_rejects_time_format_inconsistent_with_precision(self) -> None:
        invalid_values = [
            ("minute", "2026-08-05", None),
            ("minute", "2026-08-05T09:00:00+08:00", "2026-08-05"),
            ("day", "2026-08-05T09:00:00+08:00", None),
            ("range", "2026-08-05", None),
        ]
        for precision, started_at, ended_at in invalid_values:
            with self.subTest(precision=precision, started_at=started_at):
                action = create_action("event")
                action["semantics"].update(
                    time_precision=precision,
                    started_at=started_at,
                    ended_at=ended_at,
                )

                with self.assertRaisesRegex(ValidationError, "time|precision|range|ended_at"):
                    self.validate(action)

    def test_event_rejects_end_before_start(self) -> None:
        action = create_action("event")
        action["semantics"].update(
            time_precision="range",
            started_at="2026-08-05",
            ended_at="2026-08-04",
        )

        with self.assertRaisesRegex(ValidationError, "ended_at"):
            self.validate(action)


class SemanticProjectionTest(RepositoryTestCase):
    backend = "plain"

    def semantic_plan(self, action: dict[str, Any], raw_id: str) -> dict[str, Any]:
        request = build_compile_request(self.repo, mode="incremental")
        normalized_action = copy.deepcopy(action)
        channels = extraction_channels(str(normalized_action["type"]))
        if normalized_action["type"] == "entity":
            channels["mentions"][0]["text"] = normalized_action["title"]
        elif normalized_action["type"] == "event":
            semantics = normalized_action["semantics"]
            channels["occurrences"][0].update({
                "action": semantics["action"],
                "subject_role": semantics["subject_role"],
                "started_at": semantics["started_at"],
                "factuality": semantics["factuality"],
            })
        elif normalized_action["type"] == "statement":
            channels["claims"][0]["text"] = normalized_action["current_state"]
        normalized_action["source_ids"] = [raw_id]
        normalized_action["content"]["evidence"] = [{
            "source_id": raw_id,
            "claim": "原料直接支持这条综合结论",
        }]
        if normalized_action.get("semantics"):
            normalized_action["semantics"]["evidence"] = [{
                "source_id": raw_id,
                "claim": "原料直接记录了用户的完成动作",
            }]
        return {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": raw_id,
                "summary": "V2.1 语义投影验证",
                "importance": 5,
                "emotion": "平静",
                **channels,
            }],
            "node_actions": [normalized_action],
            "out_edges": [{
                "source_ref": raw_id,
                "target_ref": str(normalized_action["ref"]),
                "type": "belongs_to",
            }],
            "candidates": [],
            "consolidation_memo": request["context"]["consolidation_memo"],
        }

    def test_node_markdown_renders_rich_content(self) -> None:
        raw_id = self.add("语义投影", "原料直接支持这条综合结论。", "2026-08-05")
        action = create_action("entity")
        result = apply_response(self.repo, self.semantic_plan(action, raw_id), command="compile")
        node_id = result["updated_pages"][0]
        _, body = frontmatter.read_document(self.repo / "wiki" / "entities" / f"{node_id}.md")

        self.assertIn("Second Memory 是本次语义契约验证所针对的稳定项目对象", body)
        self.assertIn("语义契约覆盖内容质量", body)
        self.assertIn("原料直接支持这条综合结论", body)
        self.assertIn("尚未覆盖跨时区事件", body)

    def test_timeline_does_not_project_statement_evolution(self) -> None:
        raw_id = self.add("认知变化", "我已经形成一条新的工程判断。", "2026-08-05")
        action = create_action("statement")

        apply_response(self.repo, self.semantic_plan(action, raw_id), command="compile")

        self.assertEqual([], list((self.repo / "wiki" / "timeline").glob("*.md")))

    def test_v2_wiki_exposes_node_body_and_uses_insight_copy(self) -> None:
        raw_id = self.add("工程判断", "我决定先验证语义边界。", "2026-08-05")
        action = create_action("statement")
        result = apply_response(self.repo, self.semantic_plan(action, raw_id), command="compile")
        model = build_wiki_model(self.repo)
        node = next(item for item in model["nodes"] if item["id"] == result["updated_pages"][0])

        self.assertTrue(node["body_html"])
        self.assertIn("V2.3 语义契约已经完成验证", node["body_html"])

        html = render_html(model)
        self.assertIn("洞察", html)
        self.assertNotIn("陈述", html)

    def test_event_details_render_semantic_refs_times_and_evidence(self) -> None:
        raw_id = self.add("火锅晚餐", "今天我去海底捞吃了一顿火锅。", "2026-08-05")
        request = build_compile_request(self.repo, mode="incremental")
        place_summary = "海底捞是本次晚餐发生的地点"
        event_summary = "郑焕今天去海底捞吃了一顿火锅"
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": raw_id,
                "summary": event_summary,
                "importance": 3,
                "emotion": "满足",
                "mentions": [{"text": "海底捞", "kind": "place", "confidence": 0.99}],
                "occurrences": [{"action": "去海底捞吃火锅", "subject_role": "user", "started_at": "2026-08-05", "factuality": "occurred", "event_basis": "incident", "standalone_reason": "用户实际完成了一次有明确日期和地点的就餐活动，脱离感受后仍可独立回顾。", "confidence": 0.99}],
                "claims": [],
            }],
            "node_actions": [
                {
                    "action": "create",
                    "ref": "place",
                    "type": "entity",
                    "title": "海底捞",
                    "summary": place_summary,
                    "source_ids": [raw_id],
                    "entity_kind": "place",
                    "content": {
                        "summary": place_summary,
                        "detail": "对象与关系：海底捞是原料中明确出现的餐饮地点，也是本次火锅晚餐发生的位置。它在这条记录中承担稳定地点锚点的作用，使就餐事件能够指向具体场所，而不是停留在泛化的餐饮描述。\n\n历史与现状：郑焕在 2026 年 8 月 5 日亲自前往该地点并完成火锅晚餐。当前来源只支持这一次到访，不足以推断长期偏好或更多消费历史，因此节点只整理已确认的地点关系和本次关联。",
                        "key_points": ["本次晚餐的明确地点", "海底捞与用户存在实际到访关系", "海底捞地点当前仅有一条来源"],
                        "evidence": [{"source_id": raw_id, "claim": "原料明确提到郑焕去了海底捞"}],
                        "uncertainties": [],
                    },
                },
                {
                    "action": "create",
                    "ref": "dinner",
                    "type": "event",
                    "title": "去海底捞吃火锅",
                    "summary": event_summary,
                    "source_ids": [raw_id],
                    "event_kind": "experience",
                    "status": "occurred",
                    "event_date": "2026-08-05",
                    "content": {
                        "summary": event_summary,
                        "detail": "发生与背景：郑焕在 2026 年 8 月 5 日亲自去了海底捞，并在那里吃了一顿火锅。这是一件具有明确日期、执行者、地点和已完成结果的就餐活动；即使不附加当时的感受，也能作为当天生活时间线中的独立经历。\n\n结果与关联：事件的直接结果是用户实际完成了火锅晚餐，地点实体为海底捞。原料没有提供同行人、具体时间、菜品或消费金额，因此详情不补写这些信息，只保留发生事实、地点关系和可回顾边界。",
                        "key_points": ["用户亲自前往海底捞", "实际完成一顿火锅晚餐", "发生日期为 2026 年 8 月 5 日"],
                        "evidence": [{"source_id": raw_id, "claim": "原料明确记录今天去海底捞吃火锅"}],
                        "uncertainties": [],
                    },
                    "semantics": {
                        "subject_role": "user",
                        "action": "去海底捞吃火锅",
                        "event_basis": "incident",
                        "standalone_reason": "用户实际完成了一次有明确日期和地点的就餐活动，脱离感受后仍可独立回顾。",
                        "object_refs": ["place"],
                        "started_at": "2026-08-05",
                        "ended_at": "2026-08-05",
                        "time_precision": "range",
                        "factuality": "occurred",
                        "location_ref": "place",
                        "confidence": 0.99,
                        "evidence": [{"source_id": raw_id, "claim": "原料明确记录用户实际完成该动作"}],
                    },
                },
            ],
            "out_edges": [
                {"source_ref": raw_id, "target_ref": "place", "type": "belongs_to"},
                {"source_ref": raw_id, "target_ref": "dinner", "type": "belongs_to"},
                {"source_ref": "dinner", "target_ref": "place", "type": "location"},
            ],
            "candidates": [],
            "consolidation_memo": "",
        }

        result = apply_response(self.repo, plan, command="compile")
        model = build_wiki_model(self.repo)
        event = next(node for node in model["nodes"] if node["type"] == "event")
        place = next(node for node in model["nodes"] if node["type"] == "entity")

        self.assertEqual(place["id"], event["semantics"]["location_ref"])
        self.assertEqual([place["id"]], event["semantics"]["object_refs"])
        self.assertEqual(2, len(result["updated_pages"]))

        _, markdown = frontmatter.read_document(self.repo / "wiki" / "events" / f"{event['id']}.md")
        for expected in [
            "与用户关系：user",
            "开始时间：2026-08-05",
            "结束时间：2026-08-05",
            "时间精度：range",
            "地点：",
            "相关对象：",
            "海底捞",
            "### 事件事实证据",
            "原料明确记录用户实际完成该动作",
        ]:
            with self.subTest(surface="markdown", expected=expected):
                self.assertIn(expected, markdown)

        html = render_html(model)
        for expected in [
            "与用户关系",
            "user",
            "开始时间",
            "结束时间",
            "地点",
            "相关对象",
            "海底捞",
            "事件事实证据",
            "原料明确记录用户实际完成该动作",
        ]:
            with self.subTest(surface="wiki", expected=expected):
                self.assertIn(expected, html)
