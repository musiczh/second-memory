from __future__ import annotations

import json
from pathlib import Path

from second_memory import frontmatter
from second_memory.compiler import add_raw, apply_response, build_compile_request, load_manifest
from second_memory.wiki import TEMPLATE_PATH, build_wiki_model, render_html, render_markdown

from tests.helpers import RepositoryTestCase, content, event_semantics


class WikiTest(RepositoryTestCase):
    def _write_raw(self, raw_id: str, title: str, event_date: str) -> None:
        frontmatter.write_document(self.repo / "raw" / "2026" / "08" / f"{raw_id}.md", {
            "id": raw_id,
            "type": "raw",
            "title": title,
            "created": f"{event_date}T09:00:00+08:00",
            "event_date": event_date,
            "tags": ["v2.4"],
        }, f"{title}正文")

    def _write_node(self, node_type: str, node_id: str, meta: dict[str, object]) -> None:
        directories = {"entity": "entities", "event": "events", "statement": "statements", "topic": "topics"}
        frontmatter.write_document(self.repo / "wiki" / directories[node_type] / f"{node_id}.md", {
            "id": node_id,
            "type": node_type,
            "title": node_id,
            "summary": f"{node_id} 摘要",
            **meta,
        }, f"# {node_id}\n\n{node_id} 正文")

    def _write_edges(self, edges: list[dict[str, object]]) -> None:
        manifest = load_manifest(self.repo)
        manifest["edges"] = edges
        (self.repo / ".kb" / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_markdown_renderer_escapes_html_and_unsafe_links(self) -> None:
        rendered = render_markdown("# 标题\n\n<script>alert(1)</script>\n\n[危险](javascript:alert(1)) [安全](https://example.com)")

        self.assertIn("<h1>标题</h1>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertIn('href="https://example.com"', rendered)

    def test_source_template_explains_that_generated_output_is_required(self) -> None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")

        self.assertIn("此文件尚未注入知识库数据", template)
        self.assertIn("second-memory wiki --json", template)
        self.assertIn(".view { opacity: 1; }", template)
        self.assertIn("function renderTopicMembers", template)
        self.assertIn("主题组织问题", template)
        self.assertIn("排除边界", template)
        self.assertNotIn("animation: enter", template)

    def test_v24_entity_source_groups_require_explicit_paths_and_raw_incoming_topics(self) -> None:
        raw_ids = ["raw-direct", "raw-event-edge", "raw-event-semantics", "raw-insight", "raw-shared"]
        for index, raw_id in enumerate(raw_ids, start=1):
            self._write_raw(raw_id, raw_id, f"2026-08-{index:02d}")
        self._write_node("entity", "entity-focus", {
            "entity_kind": "concept",
            "sources": raw_ids,
        })
        self._write_node("event", "event-edge", {
            "sources": ["raw-event-edge"],
            "semantics": {"object_refs": [], "location_ref": None},
        })
        self._write_node("event", "event-semantics", {
            "sources": ["raw-event-semantics"],
            "semantics": {"object_refs": ["entity-focus"], "location_ref": None},
        })
        self._write_node("statement", "statement-linked", {
            "sources": ["raw-insight"],
            "current_state": "显式关联洞察",
        })
        self._write_node("statement", "statement-shared-only", {
            "sources": ["raw-shared"],
            "current_state": "只共享来源但没有显式关系",
        })
        self._write_node("topic", "topic-raw-member", {
            "sources": ["raw-shared"],
        })
        self._write_edges([
            {"source": "raw-direct", "target": "entity-focus", "type": "belongs_to"},
            {"source": "event-edge", "target": "entity-focus", "type": "about"},
            {"source": "statement-linked", "target": "entity-focus", "type": "involves"},
            {"source": "topic-raw-member", "target": "raw-shared", "type": "contains"},
        ])

        model = build_wiki_model(self.repo)
        entity = next(node for node in model["nodes"] if node["id"] == "entity-focus")

        self.assertEqual(["raw-direct"], [item["id"] for item in entity["source_groups"]["direct"]])
        self.assertEqual(
            ["raw-event-edge"],
            [item["id"] for item in entity["source_groups"]["via_events"]],
        )
        self.assertEqual(["raw-insight"], [item["id"] for item in entity["source_groups"]["via_insights"]])
        self.assertNotIn(
            "raw-shared",
            {
                item["id"]
                for group in entity["source_groups"].values()
                for item in group
            },
        )
        incoming = model["raws"]["raw-shared"]["incoming"]
        self.assertEqual(1, len(incoming))
        self.assertEqual("contains", incoming[0]["type"])
        self.assertEqual("topic-raw-member", incoming[0]["other"]["id"])

    def test_v24_topic_first_views_and_reading_support_new_and_legacy_member_refs(self) -> None:
        self._write_raw("raw-member", "Raw 成员", "2026-08-01")
        self._write_raw("raw-evidence", "主题证据", "2026-08-02")
        self._write_node("entity", "entity-member", {
            "entity_kind": "concept",
            "sources": ["raw-evidence"],
        })
        self._write_node("statement", "statement-member", {
            "sources": ["raw-evidence"],
            "current_state": "旧合同洞察成员",
        })
        self._write_node("topic", "topic-v24", {
            "sources": ["raw-member", "raw-evidence"],
            "attrs": {
                "topic_reading": {
                    "core_understanding": "核心综合理解",
                    "evolution": [{"date": "2026-08-02", "state": "形成阶段理解", "source_ids": ["raw-evidence"]}],
                    "contradictions": [{"member_refs": ["entity-member", "raw-member"], "description": "两类证据存在张力", "source_ids": ["raw-member"]}],
                    "open_questions": [{"question": "后续是否持续？", "basis": "当前证据跨度不足", "source_ids": ["raw-evidence"]}],
                    "confidence": 0.82,
                },
                "topic_contract": {
                    "topic_kind": "life_domain",
                    "organizing_question": "如何形成稳定理解？",
                    "facet_relationship": "对象和洞察共同形成结构。",
                    "boundary_rule": "必须直接提供独立证据。",
                    "facets": [
                        {"name": "新成员", "summary": "兼容全节点。", "member_refs": ["raw-member", "entity-member"]},
                        {"name": "旧成员", "summary": "兼容旧字段。", "statement_refs": ["statement-member"]},
                    ],
                    "member_rationales": {
                        "raw-member": {"facet": "新成员", "reason": "提供原始证据。", "supporting_excerpt": "Raw 成员正文"},
                        "statement-member": {"facet": "旧成员", "reason": "保留旧合同兼容。", "supporting_excerpt": "旧合同洞察成员"},
                    },
                    "exclusions": [{"member_id": "raw-evidence", "reason": "只作证据，不作成员。", "nearby_excerpt": "主题证据"}],
                },
            },
            "content": {
                "detail": "组织视角：主题详情。\n\n脉络与边界：主题边界。",
                "key_points": ["主题关键点"],
                "evidence": [{"source_id": "raw-evidence", "claim": "主题形成有直接证据"}],
                "uncertainties": [],
            },
        })
        self._write_edges([
            {"source": "topic-v24", "target": "raw-member", "type": "contains"},
            {"source": "topic-v24", "target": "entity-member", "type": "contains"},
            {"source": "topic-v24", "target": "statement-member", "type": "contains"},
        ])
        frontmatter.write_document(self.repo / "wiki" / "timeline" / "2026-08-02.md", {
            "id": "timeline-2026-08-02",
            "type": "timeline",
            "event_date": "2026-08-02",
            "sources": ["raw-evidence"],
        }, "- 09:00 时间线事件 -> entity-member")

        html = render_html(build_wiki_model(self.repo))
        static_html = html.split('<script type="application/json"', 1)[0]

        self.assertLess(static_html.index("主题优先阅读"), static_html.index("时间线投影"))
        ordered_labels = ["<h4>核心理解", "<h4>演变", "<h4>矛盾", "<h4>开放问题", "<h4>证据", "<h4>成员合同"]
        positions = [static_html.index(label) for label in ordered_labels]
        self.assertEqual(sorted(positions), positions)
        self.assertIn("Raw 成员", static_html)
        self.assertIn("旧合同洞察成员", static_html)
        self.assertIn("只作证据，不作成员。", static_html)
        self.assertIn('<a href="#/topics" data-tab="topics">主题</a>', html)
        self.assertIn('|| "topics"', html)
        self.assertIn("Array.isArray(facet.member_refs)", html)
        self.assertIn("item.member_ref || item.member_id || item.statement_id", html)
        self.assertIn("if (rawById[id])", html)

    def test_v2_model_exposes_nodes_history_edges_raw_and_health(self) -> None:
        raw_id = str(add_raw(self.repo, "Second Memory 规划", "原文包含 </script><script>alert(1)</script>", "2026-08-05", ["memory"])["raw_id"])
        request = build_compile_request(self.repo, mode="incremental")
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": raw_id,
                "summary": "确定本地优先",
                "importance": 5,
                "emotion": "坚定",
                "mentions": [{"text": "Second Memory", "kind": "project", "confidence": 0.99}],
                "occurrences": [{
                    "action": "完成 v2 验收",
                    "subject_role": "user",
                    "started_at": "2026-08-12",
                    "factuality": "planned",
                    "event_basis": "milestone",
                    "standalone_reason": "该事项具有明确结果和时间边界，脱离附带解释后仍值得进入时间线回顾。",
                    "confidence": 0.95,
                }],
                "claims": [{"kind": "decision", "text": "继续使用 Markdown 与 Git，不引入数据库"}],
            }],
            "node_actions": [
                {
                    "action": "create",
                    "ref": "project",
                    "type": "entity",
                    "title": "Second Memory",
                    "summary": "本地知识库项目",
                    "entity_kind": "project",
                    "source_ids": [raw_id],
                    "content": content("本地知识库项目", raw_id, node_type="entity"),
                },
                {
                    "action": "create",
                    "ref": "milestone",
                    "type": "event",
                    "title": "完成 v2 验收",
                    "summary": "完成图谱验收",
                    "event_kind": "milestone",
                    "status": "planned",
                    "event_date": "2026-08-12",
                    "source_ids": [raw_id],
                    "content": content("完成图谱验收", raw_id, node_type="event"),
                    "semantics": event_semantics(
                        raw_id,
                        action="完成 v2 验收",
                        started_at="2026-08-12",
                        factuality="planned",
                    ),
                },
                {
                    "action": "create",
                    "ref": "decision",
                    "type": "statement",
                    "title": "存储技术选型",
                    "summary": "继续使用 Markdown 与 Git",
                    "current_state": "不引入数据库",
                    "source_ids": [raw_id],
                    "content": content("继续使用 Markdown 与 Git", raw_id),
                    "effective_date": "2026-08-05",
                },
            ],
            "out_edges": [
                {"source_ref": raw_id, "target_ref": "project", "type": "belongs_to", "note": "项目", "inferred": False, "attrs": {}},
                {"source_ref": raw_id, "target_ref": "milestone", "type": "belongs_to", "note": "里程碑", "inferred": False, "attrs": {}},
                {"source_ref": raw_id, "target_ref": "decision", "type": "belongs_to", "note": "决定", "inferred": False, "attrs": {}},
                {"source_ref": "decision", "target_ref": "project", "type": "applies_to", "note": "项目决策", "inferred": False, "attrs": {}},
            ],
            "candidates": [{"kind": "merge", "node_ids": ["project"], "reason": "待更多证据"}],
            "consolidation_memo": request["context"]["consolidation_memo"],
        }
        result = apply_response(self.repo, plan, command="compile")
        model = build_wiki_model(self.repo)

        self.assertEqual(3, model["counts"]["nodes"])
        self.assertEqual(1, model["counts"]["entity"])
        self.assertEqual(1, model["counts"]["event"])
        self.assertEqual(1, model["counts"]["statement"])
        self.assertEqual(4, model["counts"]["edges"])
        self.assertEqual("clean", model["health"]["transaction"]["state"])
        self.assertEqual("idle", model["health"]["rebuild"]["phase"])
        self.assertEqual(1, model["counts"]["timeline"])

        event = next(node for node in model["nodes"] if node["type"] == "event")
        statement = next(node for node in model["nodes"] if node["type"] == "statement")
        self.assertEqual("2026-08-12", event["date_history"][0]["event_date"])
        self.assertEqual("planned", event["status_history"][0]["status"])
        self.assertEqual("milestone", event["semantics"]["event_basis"])
        self.assertEqual([], model["health"]["semantic_quality"]["weak_detail"])
        self.assertEqual([], model["health"]["semantic_quality"]["weak_evidence"])
        self.assertEqual([], model["health"]["semantic_quality"]["weak_event_contract"])
        self.assertEqual("不引入数据库", statement["evolution"][0]["state"])
        self.assertTrue(statement["outgoing"])

        raw = model["raws"][raw_id]
        self.assertEqual(3, len(raw["belongs_to"]))
        self.assertEqual("确定本地优先", raw["annotations"]["summary"])
        self.assertIn("&lt;/script&gt;&lt;script&gt;", raw["body_html"])

        html = render_html(model)
        static_html = html.split('<script type="application/json"', 1)[0]
        self.assertNotIn("__WIKI_DATA__", html)
        self.assertNotIn("</script><script>alert(1)</script>", html)
        self.assertIn("知识库编译结果", static_html)
        self.assertIn("Second Memory", static_html)
        self.assertIn("完成 v2 验收", static_html)
        self.assertNotIn("此文件尚未注入知识库数据", static_html)
        self.assertIn("认知演进", html)
        self.assertIn("独立回顾价值", html)
        self.assertIn("detailHtml(node.detail)", html)
        self.assertIn("显式关系", html)
        self.assertIn("raw replay progress", html)
        self.assertIn(str(result["session_id"]), html)

    def test_v1_compiled_pages_remain_browsable_before_rebuild(self) -> None:
        raw_id = "raw-20260805-0900-legacy"
        raw_path = self.repo / "raw" / "2026" / "08" / "legacy.md"
        frontmatter.write_document(raw_path, {
            "id": raw_id,
            "type": "raw",
            "title": "旧记录",
            "created": "2026-08-05T09:00:00+08:00",
            "event_date": "2026-08-05",
            "tags": ["legacy"],
        }, "旧知识库正文")
        frontmatter.write_document(self.repo / "wiki" / "entities" / "entity-legacy.md", {
            "id": "entity-legacy",
            "type": "entity",
            "title": "旧实体",
            "summary": "v1 实体",
            "entity_kind": "concept",
            "sources": [raw_id],
        }, "旧实体正文")
        frontmatter.write_document(self.repo / "wiki" / "topics" / "topic-legacy.md", {
            "id": "topic-legacy",
            "type": "topic",
            "title": "旧主题",
            "summary": "v1 主题",
            "sources": [raw_id],
            "attrs": {"topic_contract": {
                "topic_kind": "life_domain",
                "organizing_question": "跨记录主题如何维持稳定且可验证的组织边界？",
                "facet_relationship": "机制侧面解释结构如何形成，反馈侧面解释结构如何被持续修正。",
                "boundary_rule": "只有直接回答组织问题的洞察才能成为成员。",
                "facets": [{"name": "长期机制", "summary": "解释稳定结构如何形成。", "statement_refs": ["statement-example"]}],
                "member_rationales": {"statement-example": {"facet": "长期机制", "reason": "解释主题的稳定结构。", "supporting_excerpt": "可验证的成员依据片段"}},
                "exclusions": [{"statement_id": "statement-nearby", "reason": "只在表面相近但不回答组织问题。", "nearby_excerpt": "相近但被排除的依据片段"}],
            }},
        }, "旧主题正文")
        frontmatter.write_document(self.repo / "wiki" / "timeline" / "2026-08-05.md", {
            "id": "timeline-2026-08-05",
            "type": "timeline",
            "event_date": "2026-08-05",
            "sources": [raw_id],
        }, "- 09:00 旧记录 -> entity-legacy, topic-legacy")
        manifest = load_manifest(self.repo)
        manifest["schema"] = 1
        manifest["kb_version"] = "1.0.0"
        (self.repo / ".kb" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        model = build_wiki_model(self.repo)

        self.assertEqual(1, model["counts"]["entity"])
        self.assertEqual(1, model["counts"]["topic"])
        self.assertTrue(model["health"]["version_drift"])
        self.assertEqual(["entity-legacy", "topic-legacy"], [ref["id"] for ref in model["timeline"][0]["entries"][0]["refs"]])
        self.assertTrue(next(node for node in model["nodes"] if node["id"] == "entity-legacy")["body_html"])
        topic = next(node for node in model["nodes"] if node["id"] == "topic-legacy")
        self.assertEqual("跨记录主题如何维持稳定且可验证的组织边界？", topic["attrs"]["topic_contract"]["organizing_question"])
        html = render_html(model)
        static_html = html.split('<script type="application/json"', 1)[0]
        self.assertIn("跨记录主题如何维持稳定且可验证的组织边界？", static_html)
        self.assertIn("只有直接回答组织问题的洞察才能成为成员。", static_html)
        self.assertIn("life_domain", static_html)
        self.assertIn("主题侧面", static_html)
        self.assertIn("成员依据", static_html)
        self.assertIn("排除边界", static_html)
