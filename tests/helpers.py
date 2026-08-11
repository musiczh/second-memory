from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from second_memory.compiler import add_raw, apply_response, build_compile_request, initialize, read_pending


def content(
    summary: str,
    source_ids: str | list[str],
    *,
    node_type: str = "statement",
    detail: str | None = None,
    key_points: list[str] | None = None,
    uncertainties: list[str] | None = None,
) -> dict[str, Any]:
    sources = [source_ids] if isinstance(source_ids, str) else source_ids
    supplied_detail = detail or summary
    compact_summary = "".join(summary.split()).strip("。！？!?；;")
    marker = compact_summary if len(compact_summary) <= 16 else compact_summary[:8] + compact_summary[-8:]
    sections = {
        "entity": (
            f"对象与关系：{supplied_detail}指向原料中可稳定识别的对象，并说明用户如何在当前记录里接触、使用或提及它。关于「{marker}」的现有事实限定了对象名称、类别和与用户的直接关系，没有把相关观点误写成对象属性。",
            f"历史与现状：围绕「{marker}」的已知记录说明了它在当前知识图谱中的位置，以及不同来源补充的经历、用途或关联。当前综合只保留来源能够确认的「{marker}」历史，缺少的信息继续留作不确定项。",
        ),
        "event": (
            f"发生与背景：{supplied_detail}是在明确时间锚点下发生的具体事项，原料能够确认「{marker}」的参与者、动作及其发生背景。这里仅描述「{marker}」可观察的经过，不把当时形成的解释或感受并入事件事实。",
            f"结果与关联：「{marker}」形成了可以单独回顾的结果或状态变化，并说明它为何值得进入时间线。相关洞察可以通过关系与「{marker}」连接，但事件详情只保留其直接结果、后续影响和仍需核实的边界。",
        ),
        "statement": (
            f"洞察与依据：{supplied_detail}是原料支持的当前认识，来自用户对「{marker}」相关经历、判断或选择的直接记录。现有证据说明「{marker}」在什么情境下成立，也保留了它与相近观点之间的差别。多个来源涉及「{marker}」时还需区分共同结论与单次记录，避免用最新表述覆盖先前脉络。",
            f"演进与影响：围绕「{marker}」的历史更新说明认识如何形成、被强化或发生变化。「{marker}」的当前状态会影响后续判断与行动，但来源没有证明的因果和适用范围不会被写成确定结论。后续信息只有在真正改变「{marker}」时才追加演进，重复证据只强化已有认识。",
        ),
        "topic": (
            f"组织视角：{supplied_detail}通过一个稳定问题组织多个独立洞察，并说明「{marker}」的各侧面如何共同回答该问题。成员关系来自「{marker}」洞察本身的内容与依据，而不是共同日期、情绪或批次标签。每个成员还要给出对该组织问题的具体贡献，防止「{marker}」退化为宽泛摘要。",
            f"脉络与边界：围绕「{marker}」的跨来源记录呈现了长期结构、现实反馈与变化方向。该主题同时说明哪些相近洞察仍在「{marker}」边界之外，避免为了覆盖率强行合并不相关内容。跨时间关系只有在来源真实支持「{marker}」变化时才成立，单次共现不能替代纵向证据。",
        ),
    }
    synthesis = "\n\n".join(sections[node_type])
    points = key_points or [f"当前核心结论：{summary}", f"来源事实支持：{summary}", f"适用边界说明：{summary}"]
    return {
        "summary": summary,
        "detail": synthesis,
        "key_points": points,
        "evidence": [{"source_id": source_id, "claim": summary} for source_id in sources],
        "uncertainties": uncertainties or [],
    }


def event_semantics(
    source_id: str,
    *,
    action: str,
    started_at: str,
    factuality: str,
    subject_role: str = "user",
    object_refs: list[str] | None = None,
    ended_at: str | None = None,
    time_precision: str = "day",
    location_ref: str | None = None,
    confidence: float = 0.95,
    event_basis: str = "milestone",
    standalone_reason: str = "该事项具有明确结果和时间边界，脱离附带解释后仍值得进入时间线回顾。",
) -> dict[str, Any]:
    return {
        "subject_role": subject_role,
        "action": action,
        "event_basis": event_basis,
        "standalone_reason": standalone_reason,
        "object_refs": object_refs or [],
        "started_at": started_at,
        "ended_at": ended_at,
        "time_precision": time_precision,
        "factuality": factuality,
        "location_ref": location_ref,
        "confidence": confidence,
        "evidence": [{"source_id": source_id, "claim": action}],
    }


def topic_attrs(
    member_ids: list[str],
    exclusion_id: str,
    *,
    topic_kind: str = "life_domain",
    catalog: list[Any] | None = None,
) -> dict[str, Any]:
    catalog_by_id = {
        str(item.get("id") if isinstance(item, dict) else item.id): item
        for item in (catalog or [])
    }

    def excerpt(node_id: str) -> str:
        item = catalog_by_id.get(node_id)
        if isinstance(item, dict):
            candidates = (item.get("current_state"), item.get("summary"), item.get("title"))
        else:
            candidates = (
                getattr(item, "current_state", None),
                getattr(item, "summary", None),
                getattr(item, "title", ""),
            )
        return next((str(value) for value in candidates if len("".join(str(value).split())) >= 8), "")

    midpoint = max(2, len(member_ids) // 2)
    facets = [
        {
            "name": "长期机制",
            "summary": "这些洞察共同解释主题长期形成与维持的机制。",
            "member_refs": member_ids[:midpoint],
        },
        {
            "name": "行动反馈",
            "summary": "这些洞察共同解释主题如何通过行动与反馈发生变化。",
            "member_refs": member_ids[midpoint:],
        },
    ]
    rationales = {
        member_id: {
            "facet": facet_name,
            "reason": f"{facet_name}：{member_id} 从独立记录中解释该侧面对组织问题的具体贡献。",
            "supporting_excerpt": excerpt(member_id),
        }
        for index, member_id in enumerate(member_ids)
        for facet_name in ["长期机制" if index < midpoint else "行动反馈"]
    }
    sources: list[str] = []
    for member_id in member_ids:
        item = catalog_by_id.get(member_id)
        item_sources = (
            item.get("sources", [])
            if isinstance(item, dict)
            else getattr(item, "sources", [])
        )
        sources.extend(str(source) for source in item_sources)
        if member_id.startswith("raw-") and not item_sources:
            sources.append(member_id)
    sources = sorted(set(sources))
    return {
        "topic_contract": {
            "topic_kind": topic_kind,
            "organizing_question": "用户如何在这一稳定领域中识别机制并通过行动反馈持续调整？",
            "facet_relationship": "长期机制解释稳定结构如何形成，行动反馈解释结构如何被现实尝试修正，两者共同形成可持续调整的闭环。",
            "boundary_rule": "只有直接回答组织问题并对其中一个侧面提供独立证据的洞察才能成为成员。",
            "facets": facets,
            "member_rationales": rationales,
            "exclusions": [{
                "member_ref": exclusion_id,
                "reason": "该洞察虽在同一知识库中出现，但并不回答本主题的核心问题。",
                "nearby_excerpt": excerpt(exclusion_id),
            }],
        },
        "topic_reading": {
            "core_understanding": "用户在这一长期领域中持续识别稳定机制，并通过现实行动和反馈调整自己的判断与做法。",
            "evolution": [{
                "date": "2026-01-01",
                "state": "现有来源共同形成了当前可验证的主题理解，后续变化仍需按来源继续补充。",
                "source_ids": sources[:3],
            }],
            "contradictions": [],
            "open_questions": [],
            "confidence": 0.8,
        },
    }


class RepositoryTestCase(unittest.TestCase):
    backend = "git"

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="second-memory-test-")
        self.repo = Path(self._temporary.name) / "knowledge-base"
        initialize(self.repo, "agent", "test", self.backend)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def add(self, title: str, body: str, event_date: str) -> str:
        return str(add_raw(self.repo, title, body, event_date, ["test"])["raw_id"])

    def pending_plan(self, *, states: dict[str, str] | None = None) -> dict[str, Any]:
        request = build_compile_request(self.repo, mode="incremental")
        raw_entries = request["context"]["raw_entries"]
        states = states or {}
        annotations = []
        actions = []
        edges = []
        for index, entry in enumerate(raw_entries):
            raw_id = str(entry["id"])
            ref = f"statement-{index}"
            state = states.get(raw_id, f"{entry['title']} 的当前状态")
            annotations.append({
                "raw_id": raw_id,
                "summary": state,
                "importance": 3,
                "emotion": "平静",
                "mentions": [],
                "occurrences": [],
                "claims": [{"kind": "insight", "text": state}],
            })
            actions.append({
                "action": "create",
                "ref": ref,
                "type": "statement",
                "title": str(entry["title"]),
                "summary": state,
                "source_ids": [raw_id],
                "content": content(state, raw_id),
                "current_state": state,
                "effective_date": str(entry["event_date"]),
            })
            edges.append({"source_ref": raw_id, "target_ref": ref, "type": "belongs_to", "note": "test", "inferred": False, "attrs": {}})
        return {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": annotations,
            "node_actions": actions,
            "out_edges": edges,
            "candidates": [],
            "consolidation_memo": request["context"]["consolidation_memo"],
        }

    def apply_pending(self, *, states: dict[str, str] | None = None) -> dict[str, Any]:
        self.assertTrue(read_pending(self.repo))
        return apply_response(self.repo, self.pending_plan(states=states), command="compile")
