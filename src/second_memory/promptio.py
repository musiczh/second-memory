from __future__ import annotations

from typing import Any


def compile_response_schema(session_id: str = "session-from-request", mode: str = "incremental") -> dict[str, Any]:
    """CompilePlan v2 contract shown to the host Agent.

    This intentionally uses example-rich JSON rather than relying on a particular
    model provider's JSON-Schema dialect.
    """
    return {
        "schema_version": 2,
        "session_id": session_id,
        "mode": mode,
        "raw_annotations": [{
            "raw_id": "raw-...",
            "summary": "一句话原料摘要",
            "importance": 1,
            "emotion": "可选情绪",
            "mentions": [{"text": "明确出现的对象", "kind": "person|organization|place|work|product|tool|project|task|object|concept|emotion", "target_id": "可选：已解析的现有实体 ID", "confidence": 0.95}],
            "occurrences": [{
                "action": "只描述发生事实的候选动作",
                "subject_role": "user|directly_affects_user",
                "started_at": "YYYY-MM-DD",
                "factuality": "occurred|ongoing|planned",
                "event_basis": "appointment|scheduled_commitment|incident|milestone|transaction|material_change",
                "standalone_reason": "去掉反思后，这件事为何仍值得进入时间线",
                "confidence": 0.95,
            }],
            "claims": [{"kind": "preference|goal|belief|plan|decision|feeling|method|insight", "text": "可进入洞察线程的认知声明"}],
        }],
        "node_actions": [{
            "action": "create|reinforce|refine|change|supersede|relate|archive|merge|split（已有节点仅补来源时使用 reinforce，且只返回 target_id/type/source_ids）",
            "ref": "new-node-ref（仅 create）",
            "target_id": "existing-node-id（非 create）",
            "type": "entity|event|statement|topic",
            "title": "节点标题",
            "aliases": ["别名"],
            "summary": "用于 index 的稳定短摘要",
            "source_ids": ["raw-..."],
            "content": {
                "summary": "与 action.summary 完全一致的短摘要",
                "detail": "按类型使用两个固定标签段落并至少四个实质句：entity 对象与关系／历史与现状；event 发生与背景／结果与关联；statement 洞察与依据／演进与影响；topic 组织视角／脉络与边界。明确写出 summary 中心概念并综合全部有效来源，不使用通用填充",
                "key_points": ["至少三个不重复且不少于八个非空白字符的具体关键点，其中至少两条复用 summary 中心概念"],
                "evidence": [{"source_id": "raw-...", "claim": "该来源支持的具体结论"}],
                "uncertainties": ["证据不足或冲突之处；没有则为空数组"],
            },
            "entity_kind": "person|organization|place|work|product|tool|project|task|object|concept|emotion（entity）",
            "event_kind": "事件分类（event）",
            "status": "planned|ongoing|occurred|cancelled|superseded（event）",
            "event_date": "YYYY-MM-DD（event）",
            "semantics": {
                "subject_role": "user|directly_affects_user（event）",
                "action": "与节点 title 相同、仅描述可观察发生的短语（event）",
                "event_basis": "appointment|scheduled_commitment|incident|milestone|transaction|material_change（event）",
                "standalone_reason": "去掉洞察后仍值得进入时间线的来源约束理由（event）",
                "object_refs": ["node-id|plan-local-ref"],
                "started_at": "YYYY-MM-DD 或带分钟的 ISO datetime（event，须匹配 time_precision）",
                "ended_at": None,
                "time_precision": "minute|day|week|month|year|range（minute 用 datetime；range 必须有 ended_at）",
                "factuality": "occurred|ongoing|planned",
                "location_ref": None,
                "confidence": 0.95,
                "evidence": [{"source_id": "raw-...", "claim": "证明事件实际发生或明确安排的原文事实"}],
            },
            "current_state": "当前洞察（statement）",
            "effective_date": "YYYY-MM-DD",
            "membership_mode": "replace（topic 必填；成员集合是完整替换）",
            "evolution": (
                [{"date": "YYYY-MM-DD", "state": "历史状态", "source_ids": ["raw-..."]}]
                if mode == "consolidate"
                else []
            ),
            "attrs": {
                "topic_contract": {
                    "topic_kind": "life_domain|cross_domain_pattern|longitudinal_arc（topic）",
                    "organizing_question": "全部成员共同回答的稳定问题（topic）",
                    "facet_relationship": "各侧面为何必须共同存在，才能构成高于单条洞察的组织结构（topic）",
                    "boundary_rule": "可用于判断新洞察能否加入的明确成员边界（topic）",
                    "facets": [{
                        "name": "主题维度",
                        "summary": "该维度如何回答组织问题",
                        "member_refs": ["raw-id|entity-id|event-id|statement-id|topic-ref"],
                    }],
                    "member_rationales": {
                        "member-id": {
                            "facet": "主题维度",
                            "reason": "包含准确 facet 名称，并解释成员自身证据对组织问题的独特贡献，不得发明桥接",
                            "supporting_excerpt": "从该成员可用注解、内容或演进复制的直接依据",
                        },
                    },
                    "exclusions": [{
                        "member_ref": "nearby-member-id",
                        "reason": "语义相近但不属于该主题的边界理由",
                        "nearby_excerpt": "从被排除成员复制的最相关依据，排除理由必须回应它",
                    }],
                },
                "topic_reading": {
                    "core_understanding": "综合全部成员后可独立阅读的核心理解",
                    "evolution": [{"date": "YYYY-MM-DD", "state": "该阶段的理解或变化", "source_ids": ["raw-..."]}],
                    "contradictions": [{"member_refs": ["statement-a", "statement-b"], "description": "证据之间的真实张力", "source_ids": ["raw-..."]}],
                    "open_questions": [{"question": "仍待回答的问题", "basis": "为什么现有证据不足", "source_ids": ["raw-..."]}],
                    "confidence": 0.8,
                },
            },
            "absorbed_ids": ["merge 时被吸收的同类型节点"],
            "replacements": [{
                "type": "statement",
                "title": "split 后节点",
                "summary": "摘要",
                "current_state": "状态",
                "source_ids": ["raw-..."],
                "content": {
                    "summary": "摘要",
                    "detail": "拆分后节点的完整综合",
                    "key_points": ["关键点"],
                    "evidence": [{"source_id": "raw-...", "claim": "来源支持的结论"}],
                    "uncertainties": [],
                },
            }],
        }],
        "out_edges": [{
            "source_ref": "raw-id|node-id|plan-local-ref",
            "target_ref": "raw-id|node-id|plan-local-ref",
            "type": "belongs_to|contains|related_to|works_with|supports|contradicts|其他明确关系",
            "note": "关系说明",
            "inferred": False,
            "attrs": {},
        }],
        "candidates": [{
            "candidate_id": "稳定候选 ID；已有候选必须原样复用",
            "kind": "merge|split|topic",
            "node_ids": ["node-id"],
            "title": "topic 候选的稳定标题",
            "topic_kind": "life_domain|cross_domain_pattern|longitudinal_arc（topic 候选）",
            "status": "pending|watching|rejected|materialized（topic 候选）",
            "reason": "为什么需要后续判断",
            "confidence": 0.0,
        }],
        "consolidation_memo": "仅 consolidate 更新的短记忆；其他模式原样返回",
    }


def search_response_schema() -> dict[str, Any]:
    return {
        "answer_markdown": "基于候选节点的归纳回答",
        "used_pages": ["entity-...|event-...|statement-...|topic-..."],
        "caveats": ["知识库内容是用户历史视角"],
    }


def review_response_schema() -> dict[str, Any]:
    return {
        "title": "回顾标题",
        "summary_markdown": "结构化回顾",
        "highlighted_pages": ["timeline-YYYY-MM-DD"],
    }


def recap_response_schema() -> dict[str, Any]:
    return {
        "title": "回顾标题",
        "recap_markdown": "面向用户的回顾与关联总结，串联本次记录与历史内容",
        "connections": ["与历史记录的具体关联点"],
        "patterns": ["反复出现的主题、情绪或项目"],
        "related_pages": ["entity-...|event-...|statement-...|topic-...|timeline-YYYY-MM-DD"],
        "suggestions": ["可行动建议或值得追问的问题"],
    }


def llm_request(task: str, agents_rules: str, context: dict[str, Any], instructions: str, response_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": task,
        "agents_rules": agents_rules,
        "context": context,
        "response_schema": response_schema,
        "instructions": instructions,
    }
