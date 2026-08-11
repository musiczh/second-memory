from __future__ import annotations

import json
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from second_memory import frontmatter
from second_memory.cli import app
from second_memory.compiler import (
    DEFAULT_GITIGNORE,
    apply_response,
    apply_rebuild_response,
    build_compile_request,
    build_consolidation_request,
    build_rebuild_request,
    build_topic_request,
    consolidation_state,
    create_session_id,
    determine_update_mode,
    finalize_rebuild,
    list_index_pages,
    load_manifest,
    manifest_drift,
    read_pending,
    rebuild_state,
    rebuild_workspace,
)
from second_memory.errors import StaleSessionError, ValidationError
from second_memory.retriever import search_level1, search_level2_request
from second_memory.reviewer import collect_timeline_pages
from second_memory.search import rg_hits
from second_memory.store.git_store import GitStorage
from second_memory.transaction import KnowledgeTransaction, recover_transaction, transaction_state
from second_memory.utils import sha256_text
from second_memory.wiki import build_wiki_model

from tests.helpers import RepositoryTestCase, content, event_semantics, topic_attrs


class CompileIntegrationTest(RepositoryTestCase):
    def test_zero_node_raw_compiles_without_polluting_graph_and_counts_for_consolidation(self) -> None:
        raw_id = self.add("普通聊天", "我今天和对象随口聊了几句，没有形成决定、承诺或结果。", "2026-08-05")
        request = build_compile_request(self.repo, mode="incremental")
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": raw_id,
                "summary": "一次没有独立结果的普通聊天",
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
        }

        result = apply_response(self.repo, plan, command="compile")

        self.assertEqual(0, result["counts"]["entity"] + result["counts"]["event"] + result["counts"]["statement"])
        self.assertEqual([], result["updated_pages"])
        self.assertEqual(1, result["consolidation_pending"])
        self.assertEqual([], read_pending(self.repo))
        self.assertIsNotNone(result["recap_request"])
        raw_path = next((self.repo / "raw").rglob("*.md"))
        meta, _ = frontmatter.read_document(raw_path)
        self.assertTrue(meta["compiled"])
        self.assertEqual([], meta["belongs_to"])

    def test_source_only_reinforce_expands_entity_sources_without_rewriting_content(self) -> None:
        first_raw = self.add("心理咨询记录", "我参加心理咨询，并记录了咨询过程。", "2026-08-01")
        first_request = build_compile_request(self.repo, mode="incremental")
        summary = "心理咨询是用户持续参与并反复记录的支持性活动"
        first_plan = {
            "schema_version": 2,
            "session_id": first_request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": first_raw,
                "summary": "首次记录心理咨询",
                "importance": 4,
                "emotion": "平静",
                "mentions": [{"text": "心理咨询", "kind": "concept", "confidence": 0.99}],
                "occurrences": [],
                "claims": [],
            }],
            "node_actions": [{
                "action": "create",
                "ref": "entity-consultation",
                "type": "entity",
                "title": "心理咨询",
                "summary": summary,
                "source_ids": [first_raw],
                "entity_kind": "concept",
                "content": content(summary, first_raw, node_type="entity"),
            }],
            "out_edges": [{"source_ref": first_raw, "target_ref": "entity-consultation", "type": "belongs_to"}],
            "candidates": [],
            "consolidation_memo": "",
        }
        first_result = apply_response(self.repo, first_plan, command="compile")
        entity_id = first_result["updated_pages"][0]
        before = next(node for node in list_index_pages(self.repo) if node.id == entity_id)

        second_raw = self.add("第二次心理咨询记录", "这次记录再次明确提到心理咨询。", "2026-08-08")
        second_request = build_compile_request(self.repo, mode="incremental")
        second_plan = {
            "schema_version": 2,
            "session_id": second_request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": second_raw,
                "summary": "再次记录心理咨询",
                "importance": 3,
                "emotion": "平静",
                "mentions": [{
                    "text": "心理咨询",
                    "kind": "concept",
                    "target_id": entity_id,
                    "confidence": 0.99,
                }],
                "occurrences": [],
                "claims": [],
            }],
            "node_actions": [{
                "action": "reinforce",
                "target_id": entity_id,
                "type": "entity",
                "source_ids": [second_raw],
            }],
            "out_edges": [{"source_ref": second_raw, "target_ref": entity_id, "type": "belongs_to"}],
            "candidates": [],
            "consolidation_memo": "",
        }
        apply_response(self.repo, second_plan, command="compile")

        after = next(node for node in list_index_pages(self.repo) if node.id == entity_id)
        self.assertEqual({first_raw, second_raw}, set(after.sources))
        self.assertEqual(before.summary, after.summary)
        self.assertEqual(before.detail, after.detail)
        self.assertEqual(before.key_points, after.key_points)

    def test_incremental_apply_renders_graph_and_preserves_raw(self) -> None:
        raw_id = self.add("技术选型", "我决定使用 Markdown 与 Git，不引入向量库。", "2026-08-05")
        raw_path = next((self.repo / "raw").rglob("*.md"))
        _, raw_body = frontmatter.read_document(raw_path)
        body_hash = sha256_text(raw_body)

        result = self.apply_pending(states={raw_id: "使用 Markdown 与 Git，不引入向量库"})

        self.assertEqual([], read_pending(self.repo))
        self.assertEqual(2, load_manifest(self.repo)["schema"])
        self.assertEqual(1, result["counts"]["statement"])
        self.assertEqual(1, result["counts"]["edges"])
        self.assertIn("recap_request", result)
        self.assertFalse(result["stale_session"])
        self.assertEqual([], manifest_drift(self.repo))
        raw_meta, after_body = frontmatter.read_document(raw_path)
        self.assertEqual(body_hash, sha256_text(after_body))
        self.assertEqual([result["updated_pages"][0]], raw_meta["belongs_to"])
        self.assertEqual(0o444, stat.S_IMODE(raw_path.stat().st_mode))
        self.assertEqual([], list((self.repo / "wiki" / "timeline").glob("*.md")))
        self.assertIn(result["updated_pages"][0], (self.repo / "index.md").read_text(encoding="utf-8"))
        self.assertEqual([], GitStorage(self.repo).status_porcelain())

    def test_change_reuses_statement_and_appends_evolution(self) -> None:
        first_raw = self.add("Second Memory 里程碑", "8 月 12 日前完成 CompilePlan v2。", "2026-08-05")
        first = self.apply_pending(states={first_raw: "里程碑为 2026-08-12"})
        statement_id = first["updated_pages"][0]
        second_raw = self.add("Second Memory 里程碑调整", "里程碑调整到 8 月 15 日。", "2026-08-06")
        request = build_compile_request(self.repo, mode="incremental")
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{"raw_id": second_raw, "summary": "里程碑调整到 8 月 15 日", "importance": 4, "emotion": "专注", "mentions": [], "occurrences": [], "claims": [{"kind": "plan", "text": "里程碑调整到 8 月 15 日"}]}],
            "node_actions": [{
                "action": "change",
                "target_id": statement_id,
                "summary": "Second Memory v2 当前里程碑为 8 月 15 日",
                "source_ids": [second_raw],
                "content": content("Second Memory v2 当前里程碑为 8 月 15 日", second_raw),
                "current_state": "里程碑为 2026-08-15",
                "effective_date": "2026-08-06",
            }],
            "out_edges": [{"source_ref": second_raw, "target_ref": statement_id, "type": "belongs_to", "note": "里程碑更新", "inferred": False, "attrs": {}}],
            "candidates": [],
            "consolidation_memo": "",
        }
        apply_response(self.repo, plan, command="compile")

        pages = list_index_pages(self.repo)
        self.assertEqual(1, len(pages))
        node = pages[0]
        self.assertEqual("里程碑为 2026-08-15", node.current_state)
        self.assertEqual({first_raw, second_raw}, set(node.sources))
        self.assertEqual(["里程碑为 2026-08-12", "里程碑为 2026-08-15"], [item["state"] for item in node.evolution])
        self.assertEqual(2, len(load_manifest(self.repo)["edges"]))
        self.assertEqual([], list((self.repo / "wiki" / "timeline").glob("*.md")))

    def test_same_day_statement_evolution_preserves_append_order(self) -> None:
        first_raw = self.add("同日初始洞察", "先形成旧状态。", "2026-08-05")
        first = self.apply_pending(states={first_raw: "旧状态"})
        statement_id = first["updated_pages"][0]
        second_raw = self.add("同日更新洞察", "同一天形成新状态。", "2026-08-05")
        request = build_compile_request(self.repo, mode="incremental")
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": second_raw,
                "summary": "同一天形成新状态",
                "importance": 4,
                "emotion": "平静",
                "mentions": [],
                "occurrences": [],
                "claims": [{"kind": "insight", "text": "新状态"}],
            }],
            "node_actions": [{
                "action": "refine",
                "target_id": statement_id,
                "summary": "同一天形成新状态",
                "source_ids": [second_raw],
                "content": content("同一天形成新状态", second_raw),
                "current_state": "新状态",
                "effective_date": "2026-08-05",
                "evolution": [],
            }],
            "out_edges": [{"source_ref": second_raw, "target_ref": statement_id, "type": "belongs_to"}],
            "candidates": [],
            "consolidation_memo": "",
        }

        apply_response(self.repo, plan, command="compile")

        statement = next(node for node in list_index_pages(self.repo) if node.id == statement_id)
        self.assertEqual(["旧状态", "新状态"], [item["state"] for item in statement.evolution])

    def test_stale_session_is_rejected_without_writes(self) -> None:
        self.add("记录一", "第一条", "2026-08-01")
        stale = self.pending_plan()
        self.add("记录二", "第二条", "2026-08-02")
        before = (self.repo / ".kb" / "manifest.json").read_text(encoding="utf-8")
        with self.assertRaises(StaleSessionError):
            apply_response(self.repo, stale, command="compile")
        self.assertEqual(before, (self.repo / ".kb" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(2, len(read_pending(self.repo)))

    def test_incremental_session_rejects_raw_metadata_change(self) -> None:
        self.add("元数据会话", "正文保持不变。", "2026-08-01")
        plan = self.pending_plan()
        raw_path = next((self.repo / "raw").rglob("*.md"))
        meta, body = frontmatter.read_document(raw_path)
        raw_path.chmod(0o644)
        meta["title"] = "会话创建后修改的标题"
        frontmatter.write_document(raw_path, meta, body)
        raw_path.chmod(0o444)

        with self.assertRaises(StaleSessionError):
            apply_response(self.repo, plan, command="compile")

    def test_incremental_session_rejects_compiled_manifest_identity_change(self) -> None:
        self.add("编译版本会话", "响应只能应用到发出请求时的编译版本。", "2026-08-01")
        plan = self.pending_plan()
        manifest_path = self.repo / ".kb" / "manifest.json"
        manifest = load_manifest(self.repo)
        manifest["schema"] = 1
        manifest["kb_version"] = "1.0.0"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")

        with self.assertRaises(StaleSessionError):
            apply_response(self.repo, plan, command="compile")

    def test_incremental_statement_evolution_is_cli_managed(self) -> None:
        raw_id = self.add("重复演进", "同一条洞察不应重复进入演进历史。", "2026-08-05")
        plan = self.pending_plan(states={raw_id: "同一条洞察"})
        duplicate = {
            "date": "2026-08-05",
            "state": "同一条洞察",
            "source_ids": [raw_id],
        }
        plan["node_actions"][0]["evolution"] = [duplicate, dict(duplicate)]

        with self.assertRaisesRegex(ValidationError, "CLI-managed"):
            apply_response(self.repo, plan, command="compile")

    def test_incremental_topic_is_rejected(self) -> None:
        self.add("记录", "内容", "2026-08-01")
        plan = self.pending_plan()
        plan["node_actions"][0]["type"] = "topic"
        with self.assertRaisesRegex(ValidationError, "incremental"):
            apply_response(self.repo, plan, command="compile")

    def test_commit_failure_rolls_back_promoted_files(self) -> None:
        self.add("事务测试", "事务失败不能留下半成品。", "2026-08-01")
        plan = self.pending_plan()
        before_manifest = (self.repo / ".kb" / "manifest.json").read_text(encoding="utf-8")
        before_index = (self.repo / "index.md").read_text(encoding="utf-8")
        with patch.object(GitStorage, "commit_paths", side_effect=RuntimeError("injected commit failure")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                apply_response(self.repo, plan, command="compile")
        self.assertEqual(before_manifest, (self.repo / ".kb" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(before_index, (self.repo / "index.md").read_text(encoding="utf-8"))
        self.assertEqual([], list((self.repo / "wiki" / "statements").glob("*.md")))
        self.assertEqual("clean", transaction_state(self.repo)["state"])
        self.assertEqual(1, len(read_pending(self.repo)))
        self.assertTrue(all(line.startswith("??") or line.startswith(" M") for line in GitStorage(self.repo).status_porcelain()))

    def test_search_and_review_use_compiled_candidates(self) -> None:
        raw_id = self.add("全栈学习路线", "从 Android 扩展到 iOS 和服务端。", "2026-08-03")
        self.apply_pending(states={raw_id: "从 Android 扩展到 iOS 和服务端"})
        level1 = search_level1(self.repo, "Android iOS")
        self.assertEqual(1, len(level1["candidates"]))
        self.assertTrue(all(not str(hit["path"]).startswith("raw/") for hit in level1["hits"]))
        level2 = search_level2_request(self.repo, "Android iOS")
        self.assertEqual(1, len(level2["context"]["source_snippets"]))
        self.assertLessEqual(len(level2["context"]["source_snippets"][0]["snippet"]), 500)
        timeline = collect_timeline_pages(self.repo, range_name=None, on_this_day=None, start_date="2026-08-03", end_date="2026-08-03", max_days=7)
        self.assertEqual([], timeline)

    def test_rg_hits_parses_single_index_file_with_colons_in_text(self) -> None:
        completed = type("Completed", (), {
            "stdout": "index.md:42:| event-id | 事件 | one-on-one | 2026-07-21 17:55 |\n",
        })()
        with patch("second_memory.search.shutil.which", return_value="/usr/bin/rg"), patch(
            "second_memory.search.subprocess.run",
            return_value=completed,
        ) as run:
            hits = rg_hits(self.repo, "one-on-one")

        self.assertIn("--with-filename", run.call_args.args[0])
        self.assertEqual([{
            "path": "index.md",
            "line": 42,
            "text": "| event-id | 事件 | one-on-one | 2026-07-21 17:55 |",
        }], hits)

    def test_rg_hits_deduplicates_same_index_row_across_query_terms(self) -> None:
        completed = type("Completed", (), {
            "stdout": "index.md:42:| statement-id | 洞察 | 睡前拖延 | 工作压力 |\n",
        })()
        with patch("second_memory.search.shutil.which", return_value="/usr/bin/rg"), patch(
            "second_memory.search.subprocess.run",
            return_value=completed,
        ) as run:
            hits = rg_hits(self.repo, "睡前拖延 工作压力")

        self.assertEqual(1, len(hits))
        self.assertEqual(1, run.call_count)

    def test_compound_chinese_query_uses_manifest_compact_index(self) -> None:
        procrastination_raw = self.add(
            "睡前拖延与工作压力",
            "我认为睡前拖延和未完成工作的压力有关。",
            "2026-08-03",
        )
        poverty_raw = self.add(
            "贫穷的本质",
            "《贫穷的本质》改变了我对贫困成因的认知。",
            "2026-08-04",
        )
        self.apply_pending(states={
            procrastination_raw: "睡前拖延与未完成工作的压力有关",
            poverty_raw: "《贫穷的本质》带来了对贫困成因的认知变化",
        })
        manifest_pages = load_manifest(self.repo)["pages"]
        self.assertTrue(all(page.get("title") and page.get("summary") for page in manifest_pages.values()))

        query = "我为什么认为睡前拖延与工作压力有关？《贫穷的本质》带来了什么认知变化？"
        with patch("second_memory.retriever.list_index_pages", side_effect=AssertionError("must use manifest compact index")):
            result = search_level1(self.repo, query)

        titles = {candidate["title"] for candidate in result["candidates"]}
        self.assertEqual({"睡前拖延与工作压力", "贫穷的本质"}, titles)

    def test_compound_chinese_query_prefers_phrase_coverage_over_generic_fragment(self) -> None:
        sleep_raw = self.add(
            "睡前补偿性拖延与主体感饥饿",
            "睡前持续拖延，并担心未完成工作的压力。",
            "2026-08-03",
        )
        ai_raw = self.add(
            "AI 输出优化方案但未完成有效审查",
            "AI 输出很多，但审查没有完成。",
            "2026-08-04",
        )
        self.apply_pending(states={
            sleep_raw: "睡前补偿性拖延与主体感饥饿相关",
            ai_raw: "AI 输出优化方案未完成审查",
        })

        result = search_level1(self.repo, "睡前拖延 未完成工作压力")

        self.assertEqual("睡前补偿性拖延与主体感饥饿", result["candidates"][0]["title"])

    def test_event_status_history_and_single_timeline_projection(self) -> None:
        first_raw = self.add("发布计划", "计划在 8 月 20 日发布 v2。", "2026-08-20")
        request = build_compile_request(self.repo, mode="incremental")
        create = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{"raw_id": first_raw, "summary": "计划发布 v2", "importance": 4, "emotion": "期待", "mentions": [], "occurrences": [{"action": "发布 Second Memory v2", "subject_role": "user", "started_at": "2026-08-20", "factuality": "planned", "event_basis": "milestone", "standalone_reason": "版本发布有明确承诺、日期和交付结果，脱离相关解释后仍值得进入项目时间线。", "confidence": 0.95}], "claims": []}],
            "node_actions": [{
                "action": "create",
                "ref": "release",
                "type": "event",
                "title": "发布 Second Memory v2",
                "summary": "计划发布 v2",
                "source_ids": [first_raw],
                "content": content("计划发布 v2", first_raw, node_type="event"),
                "event_kind": "release",
                "status": "planned",
                "event_date": "2026-08-20",
                "semantics": event_semantics(
                    first_raw,
                    action="发布 Second Memory v2",
                    started_at="2026-08-20",
                    factuality="planned",
                    standalone_reason="版本发布有明确承诺、日期和交付结果，脱离相关解释后仍值得进入项目时间线。",
                ),
            }],
            "out_edges": [{"source_ref": first_raw, "target_ref": "release", "type": "belongs_to"}],
            "candidates": [],
            "consolidation_memo": "",
        }
        first = apply_response(self.repo, create, command="compile")
        event_id = first["updated_pages"][0]
        date_search = search_level1(self.repo, "2026-08-20")
        self.assertNotIn("timeline", {candidate["type"] for candidate in date_search["candidates"]})
        second_raw = self.add("发布结果", "v2 已按计划发布。", "2026-08-20")
        request = build_compile_request(self.repo, mode="incremental")
        occurred = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{"raw_id": second_raw, "summary": "v2 已发布", "importance": 5, "emotion": "轻松", "mentions": [], "occurrences": [{"action": "发布 Second Memory v2", "subject_role": "user", "started_at": "2026-08-20", "factuality": "occurred", "event_basis": "milestone", "standalone_reason": "版本发布有明确交付结果和发生日期，脱离相关解释后仍值得进入项目时间线。", "confidence": 0.95}], "claims": []}],
            "node_actions": [{
                "action": "change",
                "target_id": event_id,
                "summary": "v2 已按计划发布",
                "source_ids": [second_raw],
                "content": content("v2 已按计划发布", second_raw, node_type="event"),
                "status": "occurred",
                "event_date": "2026-08-20",
                "semantics": event_semantics(
                    second_raw,
                    action="发布 Second Memory v2",
                    started_at="2026-08-20",
                    factuality="occurred",
                    standalone_reason="版本发布有明确交付结果和发生日期，脱离相关解释后仍值得进入项目时间线。",
                ),
            }],
            "out_edges": [{"source_ref": second_raw, "target_ref": event_id, "type": "belongs_to"}],
            "candidates": [],
            "consolidation_memo": "",
        }
        apply_response(self.repo, occurred, command="compile")
        event = next(node for node in list_index_pages(self.repo) if node.id == event_id)
        self.assertEqual("occurred", event.status)
        self.assertEqual(["planned", "occurred"], [item["status"] for item in event.attrs["status_history"]])

        third_raw = self.add("发布替代", "该发布记录由后续版本替代。", "2026-08-21")
        request = build_compile_request(self.repo, mode="incremental")
        superseded = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{"raw_id": third_raw, "summary": "发布记录被后续版本替代", "importance": 3, "emotion": "", "mentions": [], "occurrences": [{"action": "发布 Second Memory v2", "subject_role": "user", "started_at": "2026-08-20", "factuality": "occurred", "event_basis": "milestone", "standalone_reason": "原发布记录的有效状态已经发生变化，脱离相关解释后仍需要在项目时间线上追溯。", "confidence": 0.95}], "claims": []}],
            "node_actions": [{
                "action": "supersede",
                "target_id": event_id,
                "summary": "该发布已由后续版本替代",
                "source_ids": [third_raw],
                "content": content("该发布已由后续版本替代", third_raw, node_type="event"),
                "status": "superseded",
                "event_date": "2026-08-20",
                "effective_date": "2026-08-21",
                "semantics": event_semantics(
                    third_raw,
                    action="发布 Second Memory v2",
                    started_at="2026-08-20",
                    factuality="occurred",
                    event_basis="milestone",
                    standalone_reason="原发布记录的有效状态已经发生变化，脱离相关解释后仍需要在项目时间线上追溯。",
                ),
            }],
            "out_edges": [{"source_ref": third_raw, "target_ref": event_id, "type": "belongs_to"}],
            "candidates": [],
            "consolidation_memo": "",
        }
        apply_response(self.repo, superseded, command="compile")
        event = next(node for node in list_index_pages(self.repo) if node.id == event_id)
        self.assertEqual("superseded", event.status)
        self.assertEqual(["planned", "occurred", "superseded"], [item["status"] for item in event.attrs["status_history"]])
        timeline_path = self.repo / "wiki" / "timeline" / "2026-08-20.md"
        _, timeline_body = frontmatter.read_document(timeline_path)
        self.assertEqual(1, timeline_body.count(f"../events/{event_id}.md"))

    def test_event_date_change_preserves_milestone_history(self) -> None:
        first_raw = self.add("里程碑", "计划在 8 月 12 日完成。", "2026-08-05")
        request = build_compile_request(self.repo, mode="incremental")
        create = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{"raw_id": first_raw, "summary": "计划 8 月 12 日完成", "importance": 4, "emotion": "", "mentions": [], "occurrences": [{"action": "完成 CompilePlan v2", "subject_role": "user", "started_at": "2026-08-12", "factuality": "planned", "event_basis": "milestone", "standalone_reason": "该交付有明确完成日期和验收结果，脱离计划说明后仍值得进入项目时间线。", "confidence": 0.95}], "claims": []}],
            "node_actions": [{
                "action": "create",
                "ref": "milestone",
                "type": "event",
                "title": "完成 CompilePlan v2",
                "summary": "计划完成 CompilePlan v2",
                "source_ids": [first_raw],
                "content": content("计划完成 CompilePlan v2", first_raw, node_type="event"),
                "event_kind": "milestone",
                "status": "planned",
                "event_date": "2026-08-12",
                "semantics": event_semantics(
                    first_raw,
                    action="完成 CompilePlan v2",
                    started_at="2026-08-12",
                    factuality="planned",
                    standalone_reason="该交付有明确完成日期和验收结果，脱离计划说明后仍值得进入项目时间线。",
                ),
            }],
            "out_edges": [{"source_ref": first_raw, "target_ref": "milestone", "type": "belongs_to"}],
            "candidates": [],
            "consolidation_memo": "",
        }
        first = apply_response(self.repo, create, command="compile")
        event_id = first["updated_pages"][0]
        second_raw = self.add("里程碑调整", "截止日期调整到 8 月 15 日。", "2026-08-06")
        request = build_compile_request(self.repo, mode="incremental")
        change = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{"raw_id": second_raw, "summary": "截止日期调整到 8 月 15 日", "importance": 4, "emotion": "", "mentions": [], "occurrences": [{"action": "完成 CompilePlan v2", "subject_role": "user", "started_at": "2026-08-15", "factuality": "planned", "event_basis": "milestone", "standalone_reason": "交付日期已明确调整并形成新的承诺，脱离说明后仍需要在项目时间线上回顾。", "confidence": 0.95}], "claims": []}],
            "node_actions": [{
                "action": "change",
                "target_id": event_id,
                "summary": "调整后计划在 8 月 15 日完成",
                "source_ids": [second_raw],
                "content": content("调整后计划在 8 月 15 日完成", second_raw, node_type="event"),
                "status": "planned",
                "event_date": "2026-08-15",
                "effective_date": "2026-08-06",
                "semantics": event_semantics(
                    second_raw,
                    action="完成 CompilePlan v2",
                    started_at="2026-08-15",
                    factuality="planned",
                    standalone_reason="交付日期已明确调整并形成新的承诺，脱离说明后仍需要在项目时间线上回顾。",
                ),
            }],
            "out_edges": [{"source_ref": second_raw, "target_ref": event_id, "type": "belongs_to"}],
            "candidates": [],
            "consolidation_memo": "",
        }
        apply_response(self.repo, change, command="compile")
        event = next(node for node in list_index_pages(self.repo) if node.id == event_id)
        self.assertEqual(["2026-08-12", "2026-08-15"], [item["event_date"] for item in event.attrs["date_history"]])
        self.assertEqual(["2026-08-05", "2026-08-06"], [item["changed_on"] for item in event.attrs["date_history"]])
        self.assertTrue((self.repo / "wiki" / "timeline" / "2026-08-12.md").exists())
        self.assertTrue((self.repo / "wiki" / "timeline" / "2026-08-15.md").exists())

    def test_update_priority_prefers_rebuild_then_incremental_then_consolidate(self) -> None:
        self.assertEqual("noop", determine_update_mode(self.repo)["mode"])
        self.add("待编译", "存在 pending 时走增量。", "2026-08-01")
        self.assertEqual("incremental", determine_update_mode(self.repo)["mode"])
        self.apply_pending()
        manifest = load_manifest(self.repo)
        manifest["consolidation"]["pending_raw"] = [f"raw-{index}" for index in range(10)]
        (self.repo / ".kb" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.assertEqual("consolidate", determine_update_mode(self.repo)["mode"])
        page_path = next((self.repo / "wiki" / "statements").glob("*.md"))
        page_path.write_text(page_path.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
        self.assertEqual("rebuild", determine_update_mode(self.repo)["mode"])

    def test_update_apply_cannot_bypass_incremental_priority(self) -> None:
        self.add("待增量编译", "存在 pending raw 时不能先应用 consolidate 响应。", "2026-08-01")
        response = {
            "schema_version": 2,
            "session_id": create_session_id(self.repo, "consolidate", []),
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": "",
        }

        with self.assertRaisesRegex(StaleSessionError, "expected incremental"):
            apply_response(self.repo, response, command="update")

    def test_same_plan_has_byte_stable_projection(self) -> None:
        self.backend = "plain"
        # Recreate as a plain fixture so applying the same plan does not involve Git metadata.
        self.tearDown()
        self.setUp()
        self.add("确定性记录", "相同计划应生成相同文件树。", "2026-08-01")
        plan = self.pending_plan()
        copy_root = Path(tempfile.mkdtemp(prefix="second-memory-golden-"))
        other = copy_root / "knowledge-base"
        shutil.copytree(self.repo, other)
        try:
            apply_response(self.repo, plan, command="compile")
            apply_response(other, plan, command="compile")
            for relative in ["index.md", ".kb/manifest.json"]:
                self.assertEqual((self.repo / relative).read_bytes(), (other / relative).read_bytes())
            first_tree = {path.relative_to(self.repo / "wiki"): path.read_bytes() for path in sorted((self.repo / "wiki").rglob("*.md"))}
            second_tree = {path.relative_to(other / "wiki"): path.read_bytes() for path in sorted((other / "wiki").rglob("*.md"))}
            self.assertEqual(first_tree, second_tree)
        finally:
            shutil.rmtree(copy_root)

    def test_recovery_rolls_back_promoted_but_uncommitted_transaction(self) -> None:
        original_index = (self.repo / "index.md").read_text(encoding="utf-8")
        original_agents = (self.repo / "AGENTS.md").read_text(encoding="utf-8")
        tx = KnowledgeTransaction(self.repo, "session-crash")
        tx.prepare(["AGENTS.md"])
        (tx.wiki_next / "statements").mkdir(parents=True)
        (tx.wiki_next / "statements" / "statement-crash.md").write_text("partial", encoding="utf-8")
        manifest = load_manifest(self.repo)
        manifest["applied_session_id"] = "session-crash"
        tx.stage_metadata(index="# partial\n", manifest=manifest, pending_rows=[])
        tx.stage_control("AGENTS.md", "# partial v2 rules\n")
        tx.promote()
        self.assertEqual("# partial v2 rules\n", (self.repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertEqual("promoted", transaction_state(self.repo)["state"])
        self.assertEqual("rolled_back", recover_transaction(self.repo))
        self.assertEqual(original_index, (self.repo / "index.md").read_text(encoding="utf-8"))
        self.assertEqual(original_agents, (self.repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertFalse((self.repo / "wiki" / "statements" / "statement-crash.md").exists())
        self.assertEqual("clean", transaction_state(self.repo)["state"])

    def test_recovery_finalizes_when_commit_succeeded_before_journal_update(self) -> None:
        self.add("提交边界", "Git commit 成功后不能回滚已提交投影。", "2026-08-01")
        plan = self.pending_plan()
        with patch.object(KnowledgeTransaction, "mark_committed", side_effect=RuntimeError("journal update failed")):
            with self.assertRaisesRegex(RuntimeError, "journal update failed"):
                apply_response(self.repo, plan, command="compile")
        self.assertEqual("promoted", transaction_state(self.repo)["state"])
        self.assertEqual("finalized", recover_transaction(self.repo))
        self.assertEqual([], read_pending(self.repo))
        self.assertEqual(1, len(list_index_pages(self.repo)))
        self.assertEqual([], GitStorage(self.repo).status_porcelain())

    def test_corrupt_partial_journal_rolls_back_from_transaction_backup(self) -> None:
        original_index = (self.repo / "index.md").read_bytes()
        original_manifest = (self.repo / ".kb" / "manifest.json").read_bytes()
        original_pending = (self.repo / ".kb" / "pending.jsonl").read_bytes()
        tx = KnowledgeTransaction(self.repo, "session-corrupt-journal")
        tx.prepare()
        manifest = load_manifest(self.repo)
        manifest["applied_session_id"] = tx.session_id
        tx.stage_metadata(index="# partially promoted\n", manifest=manifest, pending_rows=[])
        tx.promote()
        tx.journal.write_text("{", encoding="utf-8")

        self.assertTrue(tx.root.exists())
        self.assertTrue(tx.backup.exists())
        self.assertEqual("corrupt", transaction_state(self.repo)["state"])
        self.assertEqual("rolled_back_corrupt_journal", recover_transaction(self.repo))
        self.assertEqual(original_index, (self.repo / "index.md").read_bytes())
        self.assertEqual(original_manifest, (self.repo / ".kb" / "manifest.json").read_bytes())
        self.assertEqual(original_pending, (self.repo / ".kb" / "pending.jsonl").read_bytes())
        self.assertEqual("clean", transaction_state(self.repo)["state"])


class ConsolidationIntegrationTest(RepositoryTestCase):
    def seed_ten(self) -> list[str]:
        raw_ids = []
        for index in range(9):
            raw_ids.append(self.add(f"稳定主题记录 {index}", f"关于 Second Memory 的长期实践 {index}", f"2026-07-{index + 1:02d}"))
        self.apply_pending()
        self.assertIsNone(build_consolidation_request(self.repo))
        raw_ids.append(self.add("稳定主题记录 9", "关于 Second Memory 的长期实践 9", "2026-07-10"))
        self.apply_pending()
        return raw_ids

    def test_ten_entries_enable_topic_consolidation(self) -> None:
        raw_ids = self.seed_ten()
        request = build_consolidation_request(self.repo)
        self.assertIsNotNone(request)
        assert request is not None
        statement_nodes = [node for node in request["context"]["compact_index"] if node["type"] == "statement"]
        statement_ids = [node["id"] for node in statement_nodes]
        member_ids = statement_ids[:-1]
        member_sources = sorted({source for node in statement_nodes[:-1] for source in node["sources"]})
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [{
                "action": "create",
                "ref": "stable-topic",
                "type": "topic",
                "title": "Second Memory 长期实践",
                "summary": "围绕第二记忆系统的持续实践",
                "source_ids": [],
                "membership_mode": "replace",
                "attrs": topic_attrs(member_ids, statement_ids[-1], catalog=statement_nodes),
                "content": content(
                    "围绕第二记忆系统的持续实践",
                    member_sources,
                    node_type="topic",
                    detail="多条独立记录共同呈现了围绕 Second Memory 的连续实践：一部分洞察解释系统为何能够成为长期认知基础设施，另一部分洞察呈现实际行动、反馈和调整如何持续改变使用方式。两个侧面共同回答个人知识系统如何从一次性工具转化为稳定实践。",
                    key_points=["系统价值来自长期认知积累", "行动反馈持续修正使用方式", "稳定实践需要机制与反馈同时存在"],
                ),
            }],
            "out_edges": [{"source_ref": "stable-topic", "target_ref": node_id, "type": "contains", "note": "稳定主题成员", "inferred": False, "attrs": {}} for node_id in member_ids],
            "candidates": [],
            "consolidation_memo": "已形成 Second Memory 长期实践主题。",
        }
        result = apply_response(self.repo, plan, command="consolidate")
        manifest = load_manifest(self.repo)
        topics = [node for node in list_index_pages(self.repo) if node.type == "topic"]
        self.assertEqual(1, len(topics))
        self.assertEqual(set(member_sources), set(topics[0].sources))
        self.assertEqual([], consolidation_state(manifest)["pending_raw"])
        self.assertFalse(result["consolidation_due"])
        self.assertEqual(9, sum(1 for edge in manifest["edges"] if edge["type"] == "contains"))

    def test_ten_zero_node_raws_still_trigger_graph_wide_consolidation(self) -> None:
        for index in range(10):
            self.add(
                f"无需抽取节点的记录 {index}",
                f"第 {index} 条原料没有耐久对象、事件或洞察。",
                f"2026-06-{index + 1:02d}",
            )
        request = build_compile_request(self.repo, mode="incremental")
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": raw["id"],
                "summary": "该原料没有形成耐久节点",
                "importance": 1,
                "emotion": "",
                "mentions": [],
                "occurrences": [],
                "claims": [],
            } for raw in request["context"]["raw_entries"]],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": "",
        }

        result = apply_response(self.repo, plan, command="compile")

        self.assertTrue(result["consolidation_due"])
        self.assertEqual(10, result["consolidation_pending"])
        self.assertIsNotNone(build_consolidation_request(self.repo))

    def test_consolidation_topic_membership_replace_removes_old_edges_and_sources(self) -> None:
        self.seed_ten()
        first_request = build_consolidation_request(self.repo)
        assert first_request is not None
        first_statements = first_request["context"]["statement_catalog"]
        first_members = [node["id"] for node in first_statements[:5]]
        first_sources = sorted({source for node in first_statements[:5] for source in node["sources"]})
        create_plan = {
            "schema_version": 2,
            "session_id": first_request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [{
                "action": "create",
                "ref": "replace-topic",
                "type": "topic",
                "title": "可替换成员主题",
                "summary": "稳定机制与现实反馈共同决定实践能否持续。",
                "source_ids": [],
                "membership_mode": "replace",
                "attrs": topic_attrs(first_members, first_statements[5]["id"], catalog=first_statements),
                "content": content(
                    "稳定机制与现实反馈共同决定实践能否持续。",
                    first_sources,
                    node_type="topic",
                    detail="第一组洞察用于建立待更新主题：长期机制解释实践得以维持的条件，行动反馈解释现实尝试如何修正这些条件。两个侧面共同回答稳定实践如何形成，并通过明确排除项维持成员边界。",
                    key_points=["长期机制提供条件", "行动反馈修正机制", "成员集合需要完整替换"],
                ),
            }],
            "out_edges": self._contains("replace-topic", first_members),
            "candidates": [],
            "consolidation_memo": "已建立待替换主题。",
        }
        apply_response(self.repo, create_plan, command="consolidate")
        topic_id = next(node.id for node in list_index_pages(self.repo) if node.type == "topic")

        new_raw_ids = []
        for index in range(10):
            new_raw_ids.append(self.add(f"新主题记录 {index}", f"第二阶段独立主题原料 {index}", f"2026-08-{index + 1:02d}"))
        self.apply_pending()
        second_request = build_consolidation_request(self.repo)
        assert second_request is not None
        new_statements = [
            node for node in second_request["context"]["statement_catalog"]
            if set(node["sources"]) & set(new_raw_ids)
        ]
        new_members = [node["id"] for node in new_statements[:5]]
        new_sources = sorted({source for node in new_statements[:5] for source in node["sources"]})
        update_plan = {
            "schema_version": 2,
            "session_id": second_request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [{
                "action": "refine",
                "target_id": topic_id,
                "summary": "新的稳定机制与反馈证据替换了旧主题成员。",
                "source_ids": [],
                "membership_mode": "replace",
                "attrs": topic_attrs(new_members, new_statements[5]["id"], catalog=new_statements),
                "content": content(
                    "新的稳定机制与反馈证据替换了旧主题成员。",
                    new_sources,
                    node_type="topic",
                    detail="第二组独立洞察完整替换旧主题成员：长期机制侧面给出新的稳定条件，行动反馈侧面给出新的现实修正方式。旧成员不再回答当前组织问题，因此旧 contains 和旧来源都必须从最终主题投影中消失。",
                    key_points=["新成员完整替换旧成员", "旧 contains 不得复活", "来源必须只来自当前成员"],
                ),
            }],
            "out_edges": self._contains(topic_id, new_members),
            "candidates": [],
            "consolidation_memo": "主题成员已完整替换。",
        }

        apply_response(self.repo, update_plan, command="consolidate")

        manifest = load_manifest(self.repo)
        actual_members = {edge["target"] for edge in manifest["edges"] if edge["source"] == topic_id and edge["type"] == "contains"}
        topic = next(node for node in list_index_pages(self.repo) if node.id == topic_id)
        self.assertEqual(set(new_members), actual_members)
        self.assertTrue(set(first_members).isdisjoint(actual_members))
        self.assertEqual(set(new_sources), set(topic.sources))

    @staticmethod
    def _contains(source: str, targets: list[str]) -> list[dict[str, object]]:
        return [
            {"source_ref": source, "target_ref": target, "type": "contains", "note": "回答同一组织问题", "inferred": False, "attrs": {}}
            for target in targets
        ]

    def test_apply_rejects_forged_eight_entry_consolidation(self) -> None:
        for index in range(8):
            self.add(
                f"不足批次记录 {index}",
                f"只形成八条待整理记录 {index}",
                f"2026-06-{index + 1:02d}",
            )
        self.apply_pending()
        self.assertIsNone(build_consolidation_request(self.repo))
        plan = {
            "schema_version": 2,
            "session_id": create_session_id(self.repo, "consolidate"),
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": "",
        }

        with self.assertRaisesRegex(ValidationError, "exactly 10"):
            apply_response(self.repo, plan, command="consolidate")
        self.assertEqual(8, len(consolidation_state(load_manifest(self.repo))["pending_raw"]))
        self.assertEqual("noop", determine_update_mode(self.repo)["mode"])

    def test_consolidation_session_covers_source_dates_outside_current_batch(self) -> None:
        for index in range(11):
            self.add(f"全库日期记录 {index}", f"用于 source_dates 会话指纹 {index}", f"2026-04-{index + 1:02d}")
        self.apply_pending()
        request = build_consolidation_request(self.repo)
        assert request is not None
        old_session = request["context"]["session_id"]
        outside_batch_id = consolidation_state(load_manifest(self.repo))["pending_raw"][10]
        raw_path = next(
            path
            for path in (self.repo / "raw").rglob("*.md")
            if frontmatter.read_document(path)[0].get("id") == outside_batch_id
        )
        meta, body = frontmatter.read_document(raw_path)
        raw_path.chmod(0o644)
        meta["event_date"] = "2026-06-30"
        frontmatter.write_document(raw_path, meta, body)
        raw_path.chmod(0o444)

        self.assertNotEqual(old_session, create_session_id(self.repo, "consolidate"))

    def test_failed_consolidation_keeps_batch(self) -> None:
        raw_ids = self.seed_ten()
        request = build_consolidation_request(self.repo)
        assert request is not None
        statement_nodes = [node for node in request["context"]["compact_index"] if node["type"] == "statement"]
        statement_ids = [node["id"] for node in statement_nodes]
        member_ids = statement_ids[:3]
        member_sources = sorted({source for node in statement_nodes[:3] for source in node["sources"]})
        invalid = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [{
                "action": "create",
                "ref": "bad-topic",
                "type": "topic",
                "title": "不完整主题",
                "summary": "成员不足",
                "source_ids": [],
                "membership_mode": "replace",
                "attrs": topic_attrs(member_ids, statement_ids[3], catalog=statement_nodes),
                "content": content(
                    "成员不足",
                    member_sources,
                    node_type="topic",
                    detail="该测试主题刻意只包含三条洞察，即使它提供了两个表面侧面、成员理由和来源证据，也不具备跨越足够独立洞察形成高维组织视角的最低基础，因此必须在写入事务开始前被主题契约拒绝。",
                    key_points=["只有三条成员洞察", "成员数量低于主题门槛", "失败后不得消费整理队列"],
                ),
            }],
            "out_edges": [{"source_ref": "bad-topic", "target_ref": node_id, "type": "contains"} for node_id in member_ids],
            "candidates": [],
            "consolidation_memo": "",
        }
        with self.assertRaisesRegex(ValidationError, "at least five direct members"):
            apply_response(self.repo, invalid, command="consolidate")
        self.assertEqual(raw_ids, consolidation_state(load_manifest(self.repo))["pending_raw"])

    def test_uncertain_candidate_is_retained_without_forced_merge(self) -> None:
        self.seed_ten()
        request = build_consolidation_request(self.repo)
        assert request is not None
        statement_nodes = [node for node in request["context"]["compact_index"] if node["type"] == "statement"]
        statement_ids = [node["id"] for node in statement_nodes]
        member_ids = statement_ids[:-1]
        member_sources = sorted({source for node in statement_nodes[:-1] for source in node["sources"]})
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [],
            "out_edges": [],
            "candidates": [{"kind": "merge", "node_ids": statement_ids[:2], "reason": "名称相近但语义证据不足", "confidence": 0.45}],
            "consolidation_memo": "保留歧义，等待更多记录。",
        }
        apply_response(self.repo, plan, command="consolidate")
        self.assertEqual(10, len([node for node in list_index_pages(self.repo) if node.type == "statement"]))
        self.assertEqual(1, len(load_manifest(self.repo)["candidates"]))

    def test_existing_candidate_cannot_disappear_from_later_consolidation(self) -> None:
        self.seed_ten()
        first_request = build_consolidation_request(self.repo)
        assert first_request is not None
        statement_ids = [node["id"] for node in first_request["context"]["statement_catalog"]]
        first_plan = {
            "schema_version": 2,
            "session_id": first_request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [],
            "out_edges": [],
            "candidates": [{
                "kind": "merge",
                "node_ids": statement_ids[:2],
                "reason": "两条洞察表述接近，但现有证据不足以确认它们是同一命题。",
                "confidence": 0.45,
            }],
            "consolidation_memo": "保留待审计候选。",
        }
        apply_response(self.repo, first_plan, command="consolidate")
        candidate_id = load_manifest(self.repo)["candidates"][0]["candidate_id"]

        for index in range(10):
            self.add(f"后续候选审计记录 {index}", f"新增独立记录 {index}", f"2026-08-{index + 1:02d}")
        self.apply_pending()
        second_request = build_consolidation_request(self.repo)
        assert second_request is not None
        second_plan = {
            "schema_version": 2,
            "session_id": second_request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": "保留待审计候选。",
        }
        apply_response(self.repo, second_plan, command="consolidate")

        candidates = load_manifest(self.repo)["candidates"]
        self.assertEqual([candidate_id], [item["candidate_id"] for item in candidates])


class EntityEvidenceApplyIntegrationTest(RepositoryTestCase):
    backend = "plain"

    def test_incremental_plan_cannot_introduce_compiler_policy_detail(self) -> None:
        self.add("政策句原料", "这条原料包含可直接综合的实体事实。", "2026-08-01")
        plan = self.pending_plan()
        plan["node_actions"][0]["content"]["detail"] += (
            " 当前节点只确认已经存在的内容，后续若出现新的实质信息再另行更新。"
        )

        with self.assertRaisesRegex(ValidationError, "introduces compiler-policy detail"):
            apply_response(self.repo, plan, command="compile")

        self.assertEqual([], list_index_pages(self.repo))

    def test_incremental_plan_cannot_introduce_nonspecific_shared_entity_evidence(self) -> None:
        raw_ids = [
            self.add("全球央行原料", "全球央行的独立实体事实。", "2026-08-01"),
            self.add("房地产原料", "房地产的独立实体事实。", "2026-08-02"),
        ]
        request = build_compile_request(self.repo, mode="incremental")
        titles = ["全球央行", "房地产"]
        shared_claim = "房地产在来源中被直接点名，而同一 claim 没有提到另一个实体。"
        actions = []
        for index, (raw_id, title) in enumerate(zip(raw_ids, titles, strict=True)):
            node_content = content(f"{title}是原料直接点名的独立实体", raw_id, node_type="entity")
            node_content["evidence"] = [{"source_id": raw_id, "claim": shared_claim}]
            actions.append({
                "action": "create",
                "ref": f"entity-{index}",
                "type": "entity",
                "title": title,
                "summary": node_content["summary"],
                "source_ids": [raw_id],
                "entity_kind": "concept",
                "content": node_content,
            })
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [
                {
                    "raw_id": raw_id,
                    "summary": title,
                    "importance": 3,
                    "emotion": "",
                    "mentions": [{"text": title, "kind": "concept", "confidence": 0.99}],
                    "occurrences": [],
                    "claims": [],
                }
                for raw_id, title in zip(raw_ids, titles, strict=True)
            ],
            "node_actions": actions,
            "out_edges": [
                {"source_ref": raw_id, "target_ref": f"entity-{index}", "type": "belongs_to"}
                for index, raw_id in enumerate(raw_ids)
            ],
            "candidates": [],
            "consolidation_memo": request["context"]["consolidation_memo"],
        }

        with self.assertRaisesRegex(ValidationError, "introduces nonspecific shared entity evidence"):
            apply_response(self.repo, plan, command="compile")

        self.assertEqual([], list_index_pages(self.repo))


class QualityRepairIntegrationTest(RepositoryTestCase):
    backend = "plain"

    def setUp(self) -> None:
        super().setUp()
        for index in range(2):
            self.add(f"质量修复原料 {index}", f"第 {index} 条原料提供不同的详情事实。", f"2026-08-{index + 1:02d}")
        self.apply_pending()
        statement_nodes = [node for node in list_index_pages(self.repo) if node.type == "statement"]

        entity_raw_ids = [
            self.add("全球央行原料", "全球央行的独立实体事实。", "2026-08-03"),
            self.add("房地产原料", "房地产的独立实体事实。", "2026-08-04"),
        ]
        entity_request = build_compile_request(self.repo, mode="incremental")
        entity_plan = {
            "schema_version": 2,
            "session_id": entity_request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [
                {
                    "raw_id": raw_id,
                    "summary": title,
                    "importance": 3,
                    "emotion": "",
                    "mentions": [{"text": title, "kind": "concept", "confidence": 0.99}],
                    "occurrences": [],
                    "claims": [],
                }
                for raw_id, title in zip(entity_raw_ids, ["全球央行", "房地产"], strict=True)
            ],
            "node_actions": [
                {
                    "action": "create",
                    "ref": f"entity-{index}",
                    "type": "entity",
                    "title": title,
                    "summary": f"{title}是原料直接点名的独立实体",
                    "source_ids": [raw_id],
                    "entity_kind": "concept",
                    "content": content(f"{title}是原料直接点名的独立实体", raw_id, node_type="entity"),
                }
                for index, (raw_id, title) in enumerate(zip(entity_raw_ids, ["全球央行", "房地产"], strict=True))
            ],
            "out_edges": [
                {"source_ref": raw_id, "target_ref": f"entity-{index}", "type": "belongs_to"}
                for index, raw_id in enumerate(entity_raw_ids)
            ],
            "candidates": [],
            "consolidation_memo": entity_request["context"]["consolidation_memo"],
        }
        apply_response(self.repo, entity_plan, command="compile")

        nodes = list_index_pages(self.repo)
        entity_nodes = [node for node in nodes if node.type == "entity"]
        shared_detail = content("跨节点复用的泛化详情模板", statement_nodes[0].sources)["detail"]
        shared_entity_claim = "房地产在来源中被直接点名，而同一 claim 没有提到另一个实体。"
        manifest = load_manifest(self.repo)
        for node in statement_nodes:
            meta, body = frontmatter.read_document(node.path)
            meta["content"]["detail"] = shared_detail
            frontmatter.write_document(node.path, meta, body)
            manifest["pages"][node.id]["content_hash"] = sha256_text(node.path.read_text(encoding="utf-8"))
        for node in entity_nodes:
            meta, body = frontmatter.read_document(node.path)
            meta["content"]["evidence"] = [{"source_id": node.sources[0], "claim": shared_entity_claim}]
            if node.title == "房地产":
                meta["content"]["detail"] += " 当前节点只确认来源中已经写明的关系，后续实质变化需要新的原料支持。"
            frontmatter.write_document(node.path, meta, body)
            manifest["pages"][node.id]["content_hash"] = sha256_text(node.path.read_text(encoding="utf-8"))
        (self.repo / ".kb" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.duplicate_node_ids = sorted(node.id for node in statement_nodes)
        self.policy_node_ids = sorted(node.id for node in entity_nodes if node.title == "房地产")
        self.detail_node_ids = sorted([*self.duplicate_node_ids, *self.policy_node_ids])
        self.weak_evidence_node_ids = sorted(node.id for node in entity_nodes if node.title == "全球央行")
        self.node_ids = sorted([*self.detail_node_ids, *self.weak_evidence_node_ids])

    def repair_plan(
        self,
        request: dict[str, object],
        *,
        keep_duplicate: bool = False,
        keep_policy_detail: bool = False,
        keep_weak_evidence: bool = False,
    ) -> dict[str, object]:
        context = request["context"]
        assert isinstance(context, dict)
        nodes = {node.id: node for node in list_index_pages(self.repo)}
        actions = []
        for node_id in self.node_ids:
            node = nodes[node_id]
            repaired = content(node.summary, node.sources, node_type=node.type)
            if keep_duplicate and node_id in self.duplicate_node_ids:
                repaired["detail"] = nodes[self.duplicate_node_ids[0]].detail
            if keep_policy_detail and node_id in self.policy_node_ids:
                repaired["detail"] = node.detail
            if keep_weak_evidence and node.type == "entity":
                repaired["evidence"] = list(node.evidence)
            actions.append({
                "action": "refine",
                "target_id": node_id,
                "summary": node.summary,
                "source_ids": node.sources,
                "content": repaired,
            })
        return {
            "schema_version": 2,
            "session_id": context["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": actions,
            "out_edges": [],
            "candidates": context["existing_candidates"],
            "consolidation_memo": context["consolidation_memo"],
        }

    def test_update_routes_duplicate_weak_detail_to_zero_batch_quality_repair(self) -> None:
        decision = determine_update_mode(self.repo)
        self.assertEqual("consolidate", decision["mode"])
        self.assertTrue(decision["quality_repair"])
        self.assertEqual(self.detail_node_ids, decision["weak_detail_node_ids"])
        self.assertEqual(self.weak_evidence_node_ids, decision["weak_evidence_node_ids"])
        self.assertEqual(
            self.detail_node_ids,
            build_wiki_model(self.repo)["health"]["semantic_quality"]["weak_detail"],
        )
        self.assertEqual(
            self.weak_evidence_node_ids,
            build_wiki_model(self.repo)["health"]["semantic_quality"]["weak_evidence"],
        )
        self.assertIsNone(build_consolidation_request(self.repo))

        result = CliRunner().invoke(app, ["update", "--emit-request", "--repo", str(self.repo), "--json"])

        self.assertEqual(0, result.exit_code, result.output)
        request = json.loads(result.stdout)["data"]["llm_request"]
        self.assertEqual(0, request["context"]["batch_size"])
        self.assertTrue(request["context"]["quality_repair"])
        self.assertEqual(self.detail_node_ids, request["context"]["weak_detail_node_ids"])
        self.assertEqual(self.weak_evidence_node_ids, request["context"]["weak_evidence_node_ids"])
        with self.assertRaisesRegex(ValidationError, "exactly 10"):
            apply_response(self.repo, self.repair_plan(request), command="consolidate")

    def test_failed_quality_repair_is_atomic_and_preserves_pending_queue(self) -> None:
        emitted = CliRunner().invoke(app, ["update", "--emit-request", "--repo", str(self.repo), "--json"])
        request = json.loads(emitted.stdout)["data"]["llm_request"]
        before_manifest = (self.repo / ".kb" / "manifest.json").read_bytes()
        before_pages = {node.path: node.path.read_bytes() for node in list_index_pages(self.repo)}
        pending_ids = list(consolidation_state(load_manifest(self.repo))["pending_raw"])

        result = CliRunner().invoke(
            app,
            ["update", "--apply-response", "--stdin", "--repo", str(self.repo), "--json"],
            input=json.dumps(self.repair_plan(request, keep_duplicate=True), ensure_ascii=False),
        )

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("must eliminate all cross-node duplicate detail", result.output)
        self.assertEqual(before_manifest, (self.repo / ".kb" / "manifest.json").read_bytes())
        self.assertEqual(before_pages, {path: path.read_bytes() for path in before_pages})
        self.assertEqual(pending_ids, consolidation_state(load_manifest(self.repo))["pending_raw"])

    def test_quality_repair_rejects_remaining_weak_entity_evidence_atomically(self) -> None:
        emitted = CliRunner().invoke(app, ["update", "--emit-request", "--repo", str(self.repo), "--json"])
        request = json.loads(emitted.stdout)["data"]["llm_request"]
        before_manifest = (self.repo / ".kb" / "manifest.json").read_bytes()
        before_pages = {node.path: node.path.read_bytes() for node in list_index_pages(self.repo)}

        result = CliRunner().invoke(
            app,
            ["update", "--apply-response", "--stdin", "--repo", str(self.repo), "--json"],
            input=json.dumps(self.repair_plan(request, keep_weak_evidence=True), ensure_ascii=False),
        )

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("must eliminate all nonspecific shared entity evidence", result.output)
        self.assertEqual(before_manifest, (self.repo / ".kb" / "manifest.json").read_bytes())
        self.assertEqual(before_pages, {path: path.read_bytes() for path in before_pages})

    def test_quality_repair_rejects_remaining_compiler_policy_detail_atomically(self) -> None:
        emitted = CliRunner().invoke(app, ["update", "--emit-request", "--repo", str(self.repo), "--json"])
        request = json.loads(emitted.stdout)["data"]["llm_request"]
        before_manifest = (self.repo / ".kb" / "manifest.json").read_bytes()
        before_pages = {node.path: node.path.read_bytes() for node in list_index_pages(self.repo)}

        result = CliRunner().invoke(
            app,
            ["update", "--apply-response", "--stdin", "--repo", str(self.repo), "--json"],
            input=json.dumps(self.repair_plan(request, keep_policy_detail=True), ensure_ascii=False),
        )

        self.assertNotEqual(0, result.exit_code)
        self.assertIn("must eliminate all compiler-policy detail", result.output)
        self.assertEqual(before_manifest, (self.repo / ".kb" / "manifest.json").read_bytes())
        self.assertEqual(before_pages, {path: path.read_bytes() for path in before_pages})

    def test_successful_quality_repair_clears_weak_detail_without_consuming_pending(self) -> None:
        runner = CliRunner()
        emitted = runner.invoke(app, ["update", "--emit-request", "--repo", str(self.repo), "--json"])
        request = json.loads(emitted.stdout)["data"]["llm_request"]
        pending_ids = list(consolidation_state(load_manifest(self.repo))["pending_raw"])

        result = runner.invoke(
            app,
            ["update", "--apply-response", "--stdin", "--repo", str(self.repo), "--json"],
            input=json.dumps(self.repair_plan(request), ensure_ascii=False),
        )

        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.stdout)["data"]
        self.assertTrue(payload["quality_repair"])
        self.assertEqual(pending_ids, consolidation_state(load_manifest(self.repo))["pending_raw"])
        self.assertEqual([], build_wiki_model(self.repo)["health"]["semantic_quality"]["weak_detail"])
        self.assertEqual([], build_wiki_model(self.repo)["health"]["semantic_quality"]["weak_evidence"])
        self.assertEqual("noop", determine_update_mode(self.repo)["mode"])


class TopicRefreshIntegrationTest(RepositoryTestCase):
    backend = "plain"

    def seed_statements(self) -> None:
        for index in range(6):
            self.add(
                f"主题刷新洞察 {index}",
                f"这是第 {index} 条独立原料，用于验证主题层替换不会改写洞察和原料。",
                f"2026-05-{index + 1:02d}",
            )
        self.apply_pending()

    def topic_plan(self, request: dict[str, object], *, title: str, member_ids: list[str], exclusion_id: str) -> dict[str, object]:
        context = request["context"]
        assert isinstance(context, dict)
        statements = {node["id"]: node for node in context["statement_catalog"]}
        member_sources = sorted({source for node_id in member_ids for source in statements[node_id]["sources"]})
        attrs = topic_attrs(member_ids, exclusion_id, catalog=list(statements.values()))
        reviewed = [exclusion_id, *sorted(
            node_id for node_id in statements if node_id not in member_ids and node_id != exclusion_id
        )]
        attrs["topic_contract"]["exclusions"] = [{
            "member_ref": node_id,
            "reason": f"{node_id} 已完成反向审查，但不能直接回答当前主题的组织问题。",
            "nearby_excerpt": statements[node_id]["current_state"],
        } for node_id in reviewed]
        ref = f"plan-topic-{title}"
        return {
            "schema_version": 2,
            "session_id": context["session_id"],
            "mode": "topics",
            "raw_annotations": [],
            "node_actions": [{
                "action": "create",
                "ref": ref,
                "type": "topic",
                "title": title,
                "summary": "稳定机制与行动反馈共同塑造个人知识实践。",
                "source_ids": [],
                "membership_mode": "replace",
                "attrs": attrs,
                "content": content(
                    "稳定机制与行动反馈共同塑造个人知识实践。",
                    member_sources,
                    node_type="topic",
                    detail="这些洞察不是因为处于同一批次而归并：长期机制侧面解释个人知识实践得以持续的条件，行动反馈侧面解释具体尝试如何反过来修正机制。二者共同回答稳定实践如何形成，并保留不回答该问题的相近洞察作为明确边界。",
                    key_points=["长期机制提供稳定条件", "行动反馈持续修正机制", "主题边界由同一组织问题决定"],
                ),
            }],
            "out_edges": [
                {"source_ref": ref, "target_ref": node_id, "type": "contains", "note": "回答同一组织问题", "inferred": False, "attrs": {}}
                for node_id in member_ids
            ],
            "candidates": [],
            "consolidation_memo": context["consolidation_memo"],
        }

    @staticmethod
    def semantic_nodes(repo: Path) -> dict[str, tuple[object, ...]]:
        return {
            node.id: (
                node.type,
                node.title,
                node.summary,
                tuple(node.sources),
                node.current_state,
                tuple(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in node.evolution),
                json.dumps(node.attrs, ensure_ascii=False, sort_keys=True),
                node.detail,
                tuple(node.key_points),
                tuple(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in node.evidence),
                tuple(node.uncertainties),
                json.dumps(node.semantics, ensure_ascii=False, sort_keys=True),
            )
            for node in list_index_pages(repo)
            if node.type != "topic"
        }

    def test_topic_request_is_full_library_and_does_not_send_raw_bodies(self) -> None:
        self.seed_statements()
        request = build_topic_request(self.repo)
        context = request["context"]

        self.assertEqual("topics", context["mode"])
        self.assertEqual(6, len(context["statement_catalog"]))
        self.assertEqual([], context["existing_topics"])
        self.assertEqual(6, len(context["member_catalog"]))
        self.assertEqual(6, len(context["raw_catalog"]))
        self.assertTrue(all("body" not in raw for raw in context["raw_catalog"]))
        self.assertNotIn("raw_entries", context)
        self.assertNotIn("raw_annotations", context)
        self.assertEqual(6, len(context["source_dates"]))
        self.assertTrue(all("content" in node for node in context["statement_catalog"]))
        self.assertIn("topics refresh", request["agents_rules"])
        contract_schema = request["response_schema"]["node_actions"][0]["attrs"]["topic_contract"]
        self.assertIn("facet_relationship", contract_schema)
        self.assertIn("boundary_rule", contract_schema)
        self.assertIn("member_refs", contract_schema["facets"][0])
        self.assertIn("topic_reading", request["response_schema"]["node_actions"][0]["attrs"])

    def test_topic_session_covers_raw_source_dates(self) -> None:
        self.seed_statements()
        request = build_topic_request(self.repo)
        old_session = request["context"]["session_id"]
        raw_path = next((self.repo / "raw").rglob("*.md"))
        meta, body = frontmatter.read_document(raw_path)
        raw_path.chmod(0o644)
        meta["event_date"] = "2026-06-30"
        frontmatter.write_document(raw_path, meta, body)
        raw_path.chmod(0o444)

        self.assertNotEqual(old_session, create_session_id(self.repo, "topics", []))

    def test_topic_refresh_rejects_plan_ref_that_reuses_statement_id(self) -> None:
        self.seed_statements()
        request = build_topic_request(self.repo)
        statement_ids = [node["id"] for node in request["context"]["statement_catalog"]]
        plan = self.topic_plan(
            request,
            title="冲突引用主题",
            member_ids=statement_ids[:5],
            exclusion_id=statement_ids[5],
        )
        old_ref = plan["node_actions"][0]["ref"]
        conflicting_ref = statement_ids[0]
        plan["node_actions"][0]["ref"] = conflicting_ref
        for edge in plan["out_edges"]:
            if edge["source_ref"] == old_ref:
                edge["source_ref"] = conflicting_ref

        with self.assertRaisesRegex(ValidationError, "plan-local ref cannot reuse"):
            apply_response(self.repo, plan, command="topics")

    def test_topic_refresh_replaces_only_topic_layer(self) -> None:
        self.seed_statements()
        first_request = build_topic_request(self.repo)
        statement_ids = [node["id"] for node in first_request["context"]["statement_catalog"]]
        apply_response(
            self.repo,
            self.topic_plan(first_request, title="旧主题", member_ids=statement_ids[:5], exclusion_id=statement_ids[5]),
            command="topics",
        )

        old_topic_ids = {node.id for node in list_index_pages(self.repo) if node.type == "topic"}
        old_topic_id = next(iter(old_topic_ids))
        statement = next(node for node in list_index_pages(self.repo) if node.id == statement_ids[0])
        statement_meta, statement_body = frontmatter.read_document(statement.path)
        statement_meta["out_edges"] = [
            *statement_meta.get("out_edges", []),
            {"target": old_topic_id, "type": "related_to", "note": "旧主题关系", "inferred": False, "attrs": {}},
        ]
        frontmatter.write_document(statement.path, statement_meta, statement_body)
        raw_path = next((self.repo / "raw").rglob("*.md"))
        raw_meta, raw_body = frontmatter.read_document(raw_path)
        raw_meta["belongs_to"] = [*raw_meta.get("belongs_to", []), old_topic_id]
        raw_path.chmod(0o644)
        frontmatter.write_document(raw_path, raw_meta, raw_body)
        raw_path.chmod(0o444)
        manifest = load_manifest(self.repo)
        raw_id = str(raw_meta["id"])
        manifest["edges"].extend([
            {"source": statement_ids[0], "target": old_topic_id, "type": "related_to", "note": "旧主题关系", "inferred": False, "attrs": {}},
            {"source": raw_id, "target": old_topic_id, "type": "belongs_to", "note": "旧主题原料关系", "inferred": False, "attrs": {}},
        ])
        non_topic_candidate = {"kind": "merge", "node_ids": statement_ids[:2], "reason": "保留非主题候选", "confidence": 0.4}
        manifest["candidates"] = [
            non_topic_candidate,
            {"kind": "split", "node_ids": [old_topic_id], "reason": "旧主题候选", "confidence": 0.8},
        ]
        manifest["redirects"] = {
            "statement-old-alias": statement_ids[0],
            "topic-old-alias": old_topic_id,
        }
        (self.repo / ".kb" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        before_manifest = load_manifest(self.repo)
        before_nodes = self.semantic_nodes(self.repo)
        before_raw = {path.relative_to(self.repo).as_posix(): path.read_bytes() for path in (self.repo / "raw").rglob("*.md")}
        before_pending = (self.repo / ".kb" / "pending.jsonl").read_bytes()
        before_non_topic_edges = [
            edge for edge in before_manifest["edges"]
            if before_manifest["pages"].get(edge["source"], {}).get("type") != "topic"
            and before_manifest["pages"].get(edge["target"], {}).get("type") != "topic"
        ]

        second_request = build_topic_request(self.repo)
        self.assertIn("topic_contract", second_request["context"]["existing_topics"][0]["attrs"])
        result = apply_response(
            self.repo,
            self.topic_plan(second_request, title="旧主题", member_ids=statement_ids[1:6], exclusion_id=statement_ids[0]),
            command="topics",
        )

        after_manifest = load_manifest(self.repo)
        topics = [node for node in list_index_pages(self.repo) if node.type == "topic"]
        self.assertEqual(1, len(topics))
        self.assertEqual("旧主题", topics[0].title)
        self.assertEqual([], result["removed_topics"])
        self.assertEqual(old_topic_ids, set(result["replaced_topics"]))
        self.assertTrue(old_topic_ids <= set(after_manifest["pages"]))
        self.assertEqual(before_nodes, self.semantic_nodes(self.repo))
        self.assertEqual(before_raw, {path.relative_to(self.repo).as_posix(): path.read_bytes() for path in (self.repo / "raw").rglob("*.md")})
        self.assertEqual(before_pending, (self.repo / ".kb" / "pending.jsonl").read_bytes())
        self.assertEqual(before_manifest["consolidation"], after_manifest["consolidation"])
        self.assertEqual(1, len(after_manifest["candidates"]))
        self.assertEqual(non_topic_candidate["kind"], after_manifest["candidates"][0]["kind"])
        self.assertEqual(non_topic_candidate["node_ids"], after_manifest["candidates"][0]["node_ids"])
        self.assertIn("candidate_id", after_manifest["candidates"][0])
        self.assertEqual({"statement-old-alias": statement_ids[0], "topic-old-alias": old_topic_id}, after_manifest["redirects"])
        self.assertEqual(before_non_topic_edges, [
            edge for edge in after_manifest["edges"]
            if after_manifest["pages"].get(edge["source"], {}).get("type") != "topic"
            and after_manifest["pages"].get(edge["target"], {}).get("type") != "topic"
        ])

    def test_empty_topic_refresh_removes_forced_topics(self) -> None:
        self.seed_statements()
        request = build_topic_request(self.repo)
        statement_ids = [node["id"] for node in request["context"]["statement_catalog"]]
        apply_response(
            self.repo,
            self.topic_plan(request, title="待移除主题", member_ids=statement_ids[:5], exclusion_id=statement_ids[5]),
            command="topics",
        )
        old_topic_id = next(node.id for node in list_index_pages(self.repo) if node.type == "topic")
        manifest = load_manifest(self.repo)
        manifest["redirects"] = {"alias-a": "alias-b", "alias-b": old_topic_id}
        (self.repo / ".kb" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        empty_request = build_topic_request(self.repo)
        empty_plan = {
            "schema_version": 2,
            "session_id": empty_request["context"]["session_id"],
            "mode": "topics",
            "raw_annotations": [],
            "node_actions": [],
            "out_edges": [],
            "candidates": [{
                "kind": "topic",
                "node_ids": statement_ids[:5],
                "title": "待移除主题",
                "topic_kind": "life_domain",
                "status": "rejected",
                "reason": "重新审计后发现成员只共享宽泛表述，不能共同回答一个稳定组织问题。",
                "confidence": 0.9,
            }],
            "consolidation_memo": empty_request["context"]["consolidation_memo"],
        }

        result = apply_response(self.repo, empty_plan, command="topics")

        self.assertEqual([], [node for node in list_index_pages(self.repo) if node.type == "topic"])
        manifest = load_manifest(self.repo)
        self.assertFalse(any(edge["type"] == "contains" for edge in manifest["edges"]))
        self.assertEqual({}, manifest["redirects"])
        self.assertEqual("rejected", manifest["candidates"][0]["status"])
        self.assertEqual([old_topic_id], result["removed_topics"])
        self.assertEqual([], result["replaced_topics"])


class RawOnlySequentialRebuildTest(RepositoryTestCase):
    backend = "plain"

    def setUp(self) -> None:
        super().setUp()
        timestamps = [
            datetime(2025, 1, 3, 8, 0, tzinfo=timezone.utc),
            datetime(2025, 1, 2, 8, 0, tzinfo=timezone.utc),
            datetime(2025, 1, 2, 8, 0, tzinfo=timezone.utc),
        ]
        records = [
            ("第三条原料", "第三条的正文。", "2025-01-03"),
            ("第一条原料", "第一条的正文。", "2025-01-02"),
            ("第二条原料", "第二条的正文。", "2025-01-02"),
        ]
        raw_rows: list[dict[str, str]] = []
        for timestamp, (title, body, event_date) in zip(timestamps, records):
            with patch("second_memory.compiler.now_local", return_value=timestamp):
                raw_id = self.add(title, body, event_date)
            path = self._raw_path(raw_id)
            meta, raw_body = frontmatter.read_document(path)
            path.chmod(0o644)
            meta.update({
                "compiled": True,
                "summary": "这是 v1 产物中的摘要，rebuild 请求不得读取。",
                "importance": 5,
                "emotion": "v1 情绪注解",
                "belongs_to": ["topic-v1-legacy"],
            })
            frontmatter.write_document(path, meta, raw_body)
            path.chmod(0o444)
            raw_rows.append({"id": raw_id, "created": str(meta["created"]), "title": title})

        self.ordered_raw = sorted(raw_rows, key=lambda item: (item["created"], item["id"]))
        self.body_hashes = self._raw_body_hashes()
        first_raw_id = self.ordered_raw[0]["id"]
        legacy_topic = self.repo / "wiki" / "topics" / "topic-v1-legacy.md"
        legacy_entity = self.repo / "wiki" / "entities" / "entity-v1-legacy.md"
        frontmatter.write_document(
            legacy_topic,
            {"id": "topic-v1-legacy", "type": "topic", "title": "v1 旧主题", "summary": "v1 主题摘要", "sources": [first_raw_id]},
            "## 概述\n这是 v1 编译生成的主题。\n",
        )
        frontmatter.write_document(
            legacy_entity,
            {"id": "entity-v1-legacy", "type": "entity", "title": "v1 旧实体", "summary": "v1 实体摘要", "sources": [first_raw_id]},
            "## 概述\n这是 v1 编译生成的实体。\n",
        )
        (self.repo / "index.md").write_text("# v1 索引\n\n- topic-v1-legacy\n- entity-v1-legacy\n", encoding="utf-8")
        legacy_manifest = {
            "schema": 1,
            "compiled_raw": [item["id"] for item in self.ordered_raw],
            "pages": {
                "topic-v1-legacy": {"path": "wiki/topics/topic-v1-legacy.md", "type": "topic", "sources": [first_raw_id]},
                "entity-v1-legacy": {"path": "wiki/entities/entity-v1-legacy.md", "type": "entity", "sources": [first_raw_id]},
            },
            "kb_version": "1.0.0",
            "redirects": {"topic-v1-removed": "topic-v1-legacy"},
            "candidates": [{"kind": "merge", "node_ids": ["topic-v1-legacy", "entity-v1-legacy"], "reason": "v1 候选", "confidence": 0.9}],
            "consolidation": {"pending_raw": [first_raw_id], "memo": "v1 consolidation memo", "last_session_id": "session-v1"},
        }
        (self.repo / ".kb" / "manifest.json").write_text(json.dumps(legacy_manifest, ensure_ascii=False), encoding="utf-8")
        (self.repo / ".kb" / "pending.jsonl").write_text("", encoding="utf-8")
        (self.repo / ".kb" / "config.yaml").write_text(
            'schema: 1\nscope: "agent"\nagent: "test"\npath: "/v1/leaked/path"\nbackend: "plain"\ncompile_version: 1\n',
            encoding="utf-8",
        )
        (self.repo / "AGENTS.md").write_text("# v1 编译规则\n", encoding="utf-8")

    def _raw_path(self, raw_id: str) -> Path:
        for path in (self.repo / "raw").rglob("*.md"):
            meta, _ = frontmatter.read_document(path)
            if meta.get("id") == raw_id:
                return path
        self.fail(f"raw not found: {raw_id}")

    def _raw_body_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in (self.repo / "raw").rglob("*.md"):
            meta, body = frontmatter.read_document(path)
            hashes[str(meta["id"])] = sha256_text(body)
        return hashes

    @staticmethod
    def _replay_plan(request: dict[str, object]) -> dict[str, object]:
        context = request["context"]
        assert isinstance(context, dict)
        raw_entries = context["raw_entries"]
        assert isinstance(raw_entries, list) and len(raw_entries) == 1
        raw = raw_entries[0]
        assert isinstance(raw, dict)
        raw_id = str(raw["id"])
        ref = "replayed-statement"
        return {
            "schema_version": 2,
            "session_id": context["session_id"],
            "mode": "rebuild",
            "raw_annotations": [{"raw_id": raw_id, "summary": f"{raw['title']} 的 v2 摘要", "importance": 3, "emotion": "", "mentions": [], "occurrences": [], "claims": [{"kind": "insight", "text": str(raw["body"]).strip()}]}],
            "node_actions": [{
                "action": "create",
                "ref": ref,
                "type": "statement",
                "title": str(raw["title"]),
                "summary": f"{raw['title']} 的 v2 摘要",
                "source_ids": [raw_id],
                "content": content(f"{raw['title']} 的 v2 摘要", raw_id),
                "current_state": str(raw["body"]).strip(),
                "effective_date": str(raw["event_date"]),
            }],
            "out_edges": [{"source_ref": raw_id, "target_ref": ref, "type": "belongs_to", "note": "按原料顺序重放", "inferred": False, "attrs": {}}],
            "candidates": [],
            "consolidation_memo": str(context["consolidation_memo"]),
        }

    @staticmethod
    def _consolidation_plan(request: dict[str, object], memo: str = "已完成 rebuild 批次主题审查。") -> dict[str, object]:
        context = request["context"]
        assert isinstance(context, dict)
        return {
            "schema_version": 2,
            "session_id": context["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": memo,
        }

    def test_first_request_uses_only_earliest_raw_and_no_v1_artifacts(self) -> None:
        request = build_rebuild_request(self.repo)
        context = request["context"]

        self.assertEqual("rebuild", context["mode"])
        self.assertEqual([self.ordered_raw[0]["id"]], [item["id"] for item in context["raw_entries"]])
        self.assertEqual({"phase": "replay", "step": 1, "total": 3, "completed": 0}, context["rebuild"])
        self.assertEqual([], context["existing_nodes"])
        self.assertEqual({}, context["redirects"])
        self.assertEqual([], context["existing_candidates"])
        self.assertEqual("", context["consolidation_memo"])
        self.assertFalse(context["raw_entries"][0].get("annotations"))
        self.assertFalse(rebuild_state(self.repo)["active"])
        self.assertFalse(rebuild_workspace(self.repo).exists())

    def test_first_apply_is_isolated_and_next_request_sees_only_new_nodes(self) -> None:
        first_request = build_rebuild_request(self.repo)
        first_raw_id = first_request["context"]["raw_entries"][0]["id"]
        result = apply_rebuild_response(self.repo, self._replay_plan(first_request))
        workspace = rebuild_workspace(self.repo)

        self.assertFalse(result["rebuild_complete"])
        self.assertTrue(workspace.exists())
        self.assertTrue((self.repo / "wiki" / "topics" / "topic-v1-legacy.md").exists())
        self.assertEqual(1, load_manifest(self.repo)["schema"])
        state = rebuild_state(self.repo)
        self.assertEqual(
            {"active": True, "phase": "replay", "processed": 1, "total": 3, "remaining": 2, "workspace": str(workspace)},
            state,
        )
        workspace_manifest = load_manifest(workspace)
        self.assertEqual([item["id"] for item in self.ordered_raw], workspace_manifest["rebuild"]["ordered_raw_ids"])
        self.assertEqual(1, workspace_manifest["rebuild"]["cursor"])
        self.assertEqual(3, workspace_manifest["rebuild"]["total"])
        self.assertTrue(workspace_manifest["rebuild"]["generation"])

        next_request = build_rebuild_request(self.repo)
        next_context = next_request["context"]
        self.assertEqual([self.ordered_raw[1]["id"]], [item["id"] for item in next_context["raw_entries"]])
        self.assertEqual({"phase": "replay", "step": 2, "total": 3, "completed": 1}, next_context["rebuild"])
        self.assertEqual(1, len(next_context["existing_nodes"]))
        self.assertEqual([first_raw_id], next_context["existing_nodes"][0]["sources"])
        self.assertNotIn("topic-v1-legacy", {node["id"] for node in next_context["existing_nodes"]})

    def test_rebuild_rejects_second_node_that_reuses_first_node_long_detail(self) -> None:
        first_request = build_rebuild_request(self.repo)
        apply_rebuild_response(self.repo, self._replay_plan(first_request))
        second_request = build_rebuild_request(self.repo)
        plan = self._replay_plan(second_request)
        first_detail = second_request["context"]["existing_nodes"][0]["content"]["detail"]
        plan["node_actions"][0]["content"]["detail"] = first_detail

        with self.assertRaisesRegex(ValidationError, "cross-node duplicate detail"):
            apply_rebuild_response(self.repo, plan)

        self.assertEqual(1, rebuild_state(self.repo)["processed"])
        self.assertEqual(1, len(list_index_pages(rebuild_workspace(self.repo))))

    def test_zero_node_replay_advances_and_counts_for_consolidation(self) -> None:
        request = build_rebuild_request(self.repo)
        raw = request["context"]["raw_entries"][0]
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "rebuild",
            "raw_annotations": [{
                "raw_id": raw["id"],
                "summary": "原料没有形成耐久节点",
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
        }

        result = apply_rebuild_response(self.repo, plan)
        workspace = rebuild_workspace(self.repo)
        next_request = build_rebuild_request(self.repo)

        self.assertFalse(result["rebuild_complete"])
        self.assertEqual(1, rebuild_state(self.repo)["processed"])
        self.assertEqual([raw["id"]], consolidation_state(load_manifest(workspace))["pending_raw"])
        self.assertEqual([], next_request["context"]["existing_nodes"])

    def test_replay_rejects_topics_and_merge_actions(self) -> None:
        request = build_rebuild_request(self.repo)
        raw_id = request["context"]["raw_entries"][0]["id"]
        for forbidden_action in [
            {
                "action": "create",
                "ref": "forbidden-topic",
                "type": "topic",
                "title": "重放阶段不应创建主题",
                "summary": "等 consolidation 再整理",
                "source_ids": [],
                "content": content("等 consolidation 再整理", raw_id, node_type="topic"),
            },
            {
                "action": "merge",
                "target_id": "statement-not-in-workspace",
                "absorbed_ids": ["statement-also-missing"],
                "source_ids": [raw_id],
            },
        ]:
            with self.subTest(action=forbidden_action["action"]):
                plan = self._replay_plan(request)
                plan["node_actions"] = [forbidden_action]
                plan["out_edges"] = [{"source_ref": raw_id, "target_ref": "forbidden-topic", "type": "belongs_to"}]
                with self.assertRaisesRegex(ValidationError, "replay"):
                    apply_rebuild_response(self.repo, plan)
        state = rebuild_state(self.repo)
        self.assertEqual(0, state["processed"])
        self.assertEqual("replay", state["phase"])
        self.assertEqual([], list_index_pages(rebuild_workspace(self.repo)))

    def test_completed_rebuild_contains_only_replayed_v2_outputs(self) -> None:
        results = []
        for expected in self.ordered_raw:
            request = build_rebuild_request(self.repo)
            self.assertEqual(expected["id"], request["context"]["raw_entries"][0]["id"])
            results.append(apply_rebuild_response(self.repo, self._replay_plan(request)))

        self.assertFalse(any(result["rebuild_complete"] for result in results))
        tail_request = build_rebuild_request(self.repo)
        self.assertIsNotNone(tail_request)
        assert tail_request is not None
        self.assertEqual("consolidate", tail_request["context"]["mode"])
        self.assertEqual(3, tail_request["context"]["batch_size"])
        self.assertTrue(tail_request["context"]["final_tail"])
        with self.assertRaisesRegex(ValidationError, "queue must be empty"):
            finalize_rebuild(self.repo)
        result = apply_rebuild_response(self.repo, self._consolidation_plan(tail_request))

        self.assertTrue(result["rebuild_complete"])
        self.assertFalse(rebuild_state(self.repo)["active"])
        self.assertEqual(self.body_hashes, self._raw_body_hashes())

        manifest = load_manifest(self.repo)
        nodes = list_index_pages(self.repo)
        self.assertEqual(2, manifest["schema"])
        self.assertEqual({}, manifest["redirects"])
        self.assertEqual([], manifest["candidates"])
        self.assertEqual(3, len(nodes))
        self.assertEqual({"statement"}, {node.type for node in nodes})
        self.assertNotIn("topic-v1-legacy", manifest["pages"])
        self.assertNotIn("entity-v1-legacy", manifest["pages"])
        self.assertFalse((self.repo / "wiki" / "topics" / "topic-v1-legacy.md").exists())
        self.assertFalse((self.repo / "wiki" / "entities" / "entity-v1-legacy.md").exists())
        self.assertEqual([], consolidation_state(manifest)["pending_raw"])
        config = frontmatter.parse_mapping((self.repo / ".kb" / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(2, config["schema"])
        self.assertEqual(str(self.repo), config["path"])
        self.assertEqual("2.4.0", config["kb_version"])
        self.assertNotIn("compile_version", config)
        self.assertIn("v2.4 编译与检索规则", (self.repo / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertEqual(DEFAULT_GITIGNORE, (self.repo / ".gitignore").read_text(encoding="utf-8"))

    def test_final_tail_rejects_stale_or_invalid_response_without_promotion(self) -> None:
        for _ in self.ordered_raw:
            request = build_rebuild_request(self.repo)
            apply_rebuild_response(self.repo, self._replay_plan(request))

        tail_request = build_rebuild_request(self.repo)
        assert tail_request is not None
        workspace = rebuild_workspace(self.repo)
        workspace_manifest = (workspace / ".kb" / "manifest.json").read_bytes()
        source_manifest = (self.repo / ".kb" / "manifest.json").read_bytes()

        stale = self._consolidation_plan(tail_request)
        stale["session_id"] = "session-stale-final-tail"
        with self.assertRaises(StaleSessionError):
            apply_rebuild_response(self.repo, stale)
        self.assertEqual(workspace_manifest, (workspace / ".kb" / "manifest.json").read_bytes())
        self.assertEqual(source_manifest, (self.repo / ".kb" / "manifest.json").read_bytes())

        invalid = self._consolidation_plan(tail_request)
        invalid["raw_annotations"] = [{"raw_id": self.ordered_raw[0]["id"]}]
        with self.assertRaisesRegex(ValidationError, "raw_annotations must be empty"):
            apply_rebuild_response(self.repo, invalid)
        self.assertEqual(workspace_manifest, (workspace / ".kb" / "manifest.json").read_bytes())
        self.assertEqual(source_manifest, (self.repo / ".kb" / "manifest.json").read_bytes())
        self.assertEqual(
            [item["id"] for item in self.ordered_raw],
            consolidation_state(load_manifest(workspace))["pending_raw"],
        )

    def test_old_promoted_rebuild_tail_is_recovered_once(self) -> None:
        for _ in self.ordered_raw:
            request = build_rebuild_request(self.repo)
            apply_rebuild_response(self.repo, self._replay_plan(request))
        tail_request = build_rebuild_request(self.repo)
        assert tail_request is not None
        apply_rebuild_response(self.repo, self._consolidation_plan(tail_request))

        manifest = load_manifest(self.repo)
        ordered_ids = list(manifest["rebuild"]["ordered_raw_ids"])
        manifest["consolidation"]["pending_raw"] = [ordered_ids[-1], ordered_ids[-2]]
        (self.repo / ".kb" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.assertEqual("noop", determine_update_mode(self.repo)["mode"])
        self.assertIsNone(build_consolidation_request(self.repo))

        manifest["consolidation"]["pending_raw"] = ordered_ids[-2:]
        manifest["consolidation"]["last_session_id"] = "session-old-rebuild"
        (self.repo / ".kb" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        self.assertEqual("consolidate", determine_update_mode(self.repo)["mode"])
        runner = CliRunner()
        emitted = runner.invoke(app, ["update", "--emit-request", "--repo", str(self.repo), "--json"])
        self.assertEqual(0, emitted.exit_code, emitted.output)
        emitted_payload = json.loads(emitted.stdout)
        self.assertEqual("consolidate", emitted_payload["data"]["mode"])
        recovery_request = emitted_payload["data"]["llm_request"]
        self.assertIsNotNone(recovery_request)
        assert recovery_request is not None
        self.assertEqual(2, recovery_request["context"]["batch_size"])
        self.assertTrue(recovery_request["context"]["final_tail"])
        self.assertTrue(recovery_request["context"]["tail_recovery"])

        applied = runner.invoke(
            app,
            ["update", "--apply-response", "--stdin", "--repo", str(self.repo), "--json"],
            input=json.dumps(self._consolidation_plan(recovery_request), ensure_ascii=False),
        )

        self.assertEqual(0, applied.exit_code, applied.output)
        result = json.loads(applied.stdout)
        self.assertEqual(0, result["data"]["consolidation_pending"])
        self.assertEqual([], consolidation_state(load_manifest(self.repo))["pending_raw"])
        self.assertEqual("noop", determine_update_mode(self.repo)["mode"])
        self.assertIsNone(build_consolidation_request(self.repo))

    def test_raw_compiled_after_rebuild_does_not_enable_short_tail_recovery(self) -> None:
        for _ in self.ordered_raw:
            request = build_rebuild_request(self.repo)
            apply_rebuild_response(self.repo, self._replay_plan(request))
        tail_request = build_rebuild_request(self.repo)
        assert tail_request is not None
        apply_rebuild_response(self.repo, self._consolidation_plan(tail_request))

        for index in range(8):
            self.add(
                f"重建后新增原料 {index}",
                f"该记录在 rebuild 完成后才进入增量编译 {index}。",
                f"2026-02-{index + 1:02d}",
            )
        self.apply_pending()

        manifest = load_manifest(self.repo)
        self.assertEqual("complete", manifest["rebuild"]["phase"])
        self.assertEqual(8, len(consolidation_state(manifest)["pending_raw"]))
        self.assertEqual("noop", determine_update_mode(self.repo)["mode"])
        self.assertIsNone(build_consolidation_request(self.repo))

    def test_rebuild_interleaves_due_consolidation_before_next_raw(self) -> None:
        for index in range(8):
            timestamp = datetime(2025, 1, 4 + index, 8, 0, tzinfo=timezone.utc)
            with patch("second_memory.compiler.now_local", return_value=timestamp):
                self.add(f"交错原料 {index}", f"交错正文 {index}。", f"2025-01-{4 + index:02d}")

        for _ in range(10):
            request = build_rebuild_request(self.repo)
            self.assertEqual("rebuild", request["context"]["mode"])
            result = apply_rebuild_response(self.repo, self._replay_plan(request))
            self.assertFalse(result["rebuild_complete"])

        request = build_rebuild_request(self.repo)
        self.assertEqual("consolidate", request["context"]["mode"])
        self.assertEqual(10, rebuild_state(self.repo)["processed"])
        self.assertEqual(1, rebuild_state(self.repo)["remaining"])
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": "首批十条已完成主题审查，当前没有达到主题门槛的组合。",
        }
        result = apply_rebuild_response(self.repo, plan)
        self.assertFalse(result["rebuild_complete"])

        next_request = build_rebuild_request(self.repo)
        self.assertEqual("rebuild", next_request["context"]["mode"])
        ordered_ids = load_manifest(rebuild_workspace(self.repo))["rebuild"]["ordered_raw_ids"]
        self.assertEqual(ordered_ids[10], next_request["context"]["raw_entries"][0]["id"])

    def test_update_applies_interleaved_rebuild_consolidation_response(self) -> None:
        for index in range(8):
            timestamp = datetime(2025, 1, 4 + index, 8, 0, tzinfo=timezone.utc)
            with patch("second_memory.compiler.now_local", return_value=timestamp):
                self.add(f"update 交错原料 {index}", f"update 交错正文 {index}。", f"2025-01-{4 + index:02d}")

        for _ in range(10):
            request = build_rebuild_request(self.repo)
            apply_rebuild_response(self.repo, self._replay_plan(request))

        request = build_rebuild_request(self.repo)
        self.assertEqual("consolidate", request["context"]["mode"])
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": "首批十条已完成主题审查，未发现达到主题门槛的组合。",
        }

        result = CliRunner().invoke(
            app,
            ["update", "--apply-response", "--stdin", "--repo", str(self.repo), "--json"],
            input=json.dumps(plan, ensure_ascii=False),
        )

        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("consolidate", payload["data"]["mode"])
        self.assertEqual(0, payload["data"]["consolidation_pending"])
        next_request = build_rebuild_request(self.repo)
        self.assertEqual("rebuild", next_request["context"]["mode"])

    def test_update_exposes_and_executes_rebuild_finalize_recovery(self) -> None:
        for _ in range(3):
            request = build_rebuild_request(self.repo)
            apply_rebuild_response(self.repo, self._replay_plan(request))

        tail_request = build_rebuild_request(self.repo)
        assert tail_request is not None
        self.assertTrue(tail_request["context"]["final_tail"])
        with patch("second_memory.compiler.finalize_rebuild", side_effect=RuntimeError("promotion interrupted")):
            with self.assertRaisesRegex(RuntimeError, "promotion interrupted"):
                apply_rebuild_response(self.repo, self._consolidation_plan(tail_request))

        runner = CliRunner()
        emitted = runner.invoke(app, ["update", "--emit-request", "--repo", str(self.repo), "--json"])
        self.assertEqual(0, emitted.exit_code, emitted.output)
        payload = json.loads(emitted.stdout)
        self.assertEqual("rebuild", payload["data"]["update_mode"])
        self.assertEqual("finalize", payload["data"]["mode"])
        self.assertTrue(payload["data"]["ready_to_finalize"])
        self.assertIsNone(payload["data"]["llm_request"])

        finalized = runner.invoke(app, ["update", "--finalize", "--repo", str(self.repo), "--json"])
        self.assertEqual(0, finalized.exit_code, finalized.output)
        finalized_payload = json.loads(finalized.stdout)
        self.assertTrue(finalized_payload["data"]["rebuild_complete"])
        self.assertFalse(rebuild_state(self.repo)["active"])

    def test_rebuild_runs_due_consolidation_before_final_promotion(self) -> None:
        for index in range(7):
            timestamp = datetime(2025, 1, 4 + index, 8, 0, tzinfo=timezone.utc)
            with patch("second_memory.compiler.now_local", return_value=timestamp):
                self.add(f"补充原料 {index}", f"补充正文 {index}。", f"2025-01-{4 + index:02d}")

        replay_results = []
        for _ in range(10):
            request = build_rebuild_request(self.repo)
            self.assertEqual("rebuild", request["context"]["mode"])
            replay_results.append(apply_rebuild_response(self.repo, self._replay_plan(request)))
        self.assertFalse(replay_results[-1]["rebuild_complete"])

        request = build_rebuild_request(self.repo)
        self.assertEqual("consolidate", request["context"]["mode"])
        self.assertEqual(10, request["context"]["batch_size"])
        self.assertFalse(request["context"]["final_tail"])
        statement_nodes = [node for node in request["context"]["compact_index"] if node["type"] == "statement"]
        statement_ids = [node["id"] for node in statement_nodes]
        member_ids = statement_ids[:-1]
        member_sources = sorted({source for node in statement_nodes[:-1] for source in node["sources"]})
        self.assertEqual(10, len(statement_ids))
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [{
                "action": "create",
                "ref": "fresh-topic",
                "type": "topic",
                "title": "全新重建主题",
                "summary": "仅基于顺序重放后的新节点形成。",
                "source_ids": [],
                "membership_mode": "replace",
                "attrs": topic_attrs(member_ids, statement_ids[-1], catalog=statement_nodes),
                "content": content(
                    "仅基于顺序重放后的新节点形成。",
                    member_sources,
                    node_type="topic",
                    detail="该主题只综合本轮 raw-only 顺序重放产生的新洞察：一个侧面说明多条原料如何形成稳定认知，另一个侧面说明后续行动如何产生反馈。所有成员和证据都来自本轮重放，不继承任何旧编译产物，也不会因为同批进入队列就自动归入主题。",
                    key_points=["来源仅包含重放原料", "成员按两个稳定侧面组织", "主题在 consolidation 阶段形成"],
                ),
            }],
            "out_edges": [
                {"source_ref": "fresh-topic", "target_ref": node_id, "type": "contains", "note": "重建后整理", "inferred": False, "attrs": {}}
                for node_id in member_ids
            ],
            "candidates": [],
            "consolidation_memo": "完成首批全新节点整理。",
        }
        result = apply_rebuild_response(self.repo, plan)

        self.assertTrue(result["rebuild_complete"])
        self.assertFalse(rebuild_workspace(self.repo).exists())
        self.assertEqual([], consolidation_state(load_manifest(self.repo))["pending_raw"])
        self.assertEqual(1, len([node for node in list_index_pages(self.repo) if node.type == "topic"]))

    def test_final_eight_tail_refines_existing_topic_before_promotion(self) -> None:
        for index in range(15):
            timestamp = datetime(2025, 1, 4 + index, 8, 0, tzinfo=timezone.utc)
            with patch("second_memory.compiler.now_local", return_value=timestamp):
                self.add(f"尾批主题原料 {index}", f"尾批主题正文 {index} 提供稳定机制与行动反馈。", f"2025-01-{4 + index:02d}")

        for _ in range(10):
            request = build_rebuild_request(self.repo)
            apply_rebuild_response(self.repo, self._replay_plan(request))

        first_batch = build_rebuild_request(self.repo)
        assert first_batch is not None
        self.assertEqual(10, first_batch["context"]["batch_size"])
        first_statements = first_batch["context"]["statement_catalog"]
        first_members = [node["id"] for node in first_statements[:5]]
        first_sources = sorted({source for node in first_statements[:5] for source in node["sources"]})
        create_topic = {
            "schema_version": 2,
            "session_id": first_batch["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [{
                "action": "create",
                "ref": "tail-topic",
                "type": "topic",
                "title": "尾批必须补入的既有主题",
                "summary": "稳定机制与行动反馈共同推动这一主题持续演进。",
                "source_ids": [],
                "membership_mode": "replace",
                "attrs": topic_attrs(first_members, first_statements[5]["id"], catalog=first_statements),
                "content": content(
                    "稳定机制与行动反馈共同推动这一主题持续演进。",
                    first_sources,
                    node_type="topic",
                    detail="前十条重放记录先形成稳定主题：长期机制解释认知结构如何维持，行动反馈解释现实尝试如何持续修正结构。两个侧面共同回答主题如何随连续实践演进，并为最终尾批提供可更新的既有阅读入口。",
                    key_points=["长期机制维持主题结构", "行动反馈修正既有判断", "后续尾批必须进入主题审查"],
                ),
            }],
            "out_edges": [
                {"source_ref": "tail-topic", "target_ref": node_id, "type": "contains", "note": "首批主题成员", "inferred": False, "attrs": {}}
                for node_id in first_members
            ],
            "candidates": [],
            "consolidation_memo": "前十条已形成稳定主题。",
        }
        first_result = apply_rebuild_response(self.repo, create_topic)
        self.assertFalse(first_result["rebuild_complete"])
        topic_id = next(node.id for node in list_index_pages(rebuild_workspace(self.repo)) if node.type == "topic")

        for _ in range(8):
            request = build_rebuild_request(self.repo)
            self.assertEqual("rebuild", request["context"]["mode"])
            result = apply_rebuild_response(self.repo, self._replay_plan(request))
            self.assertFalse(result["rebuild_complete"])

        tail_request = build_rebuild_request(self.repo)
        assert tail_request is not None
        self.assertEqual("consolidate", tail_request["context"]["mode"])
        self.assertEqual(8, tail_request["context"]["batch_size"])
        self.assertTrue(tail_request["context"]["final_tail"])
        tail_raw_ids = {item["id"] for item in tail_request["context"]["raw_annotations"]}
        tail_statements = [
            node for node in tail_request["context"]["statement_catalog"]
            if tail_raw_ids & set(node["sources"])
        ]
        tail_members = [node["id"] for node in tail_statements[:5]]
        tail_sources = sorted({source for node in tail_statements[:5] for source in node["sources"]})
        refine_topic = {
            "schema_version": 2,
            "session_id": tail_request["context"]["session_id"],
            "mode": "consolidate",
            "raw_annotations": [],
            "node_actions": [{
                "action": "refine",
                "target_id": topic_id,
                "summary": "最终八条尾批带来的新机制与反馈已经进入主题阅读。",
                "source_ids": [],
                "membership_mode": "replace",
                "attrs": topic_attrs(tail_members, tail_statements[5]["id"], catalog=tail_statements),
                "content": content(
                    "最终八条尾批带来的新机制与反馈已经进入主题阅读。",
                    tail_sources,
                    node_type="topic",
                    detail="最终尾批完整替换既有主题成员：新的长期机制记录说明稳定结构如何延续，新的行动反馈记录说明实践如何改变当前理解。主题阅读因此覆盖全部重放原料，而不是停留在前六个常规批次的审查结果。",
                    key_points=["最终尾批进入主题成员", "主题阅读覆盖新增来源", "队列清空后才能正式提升"],
                ),
            }],
            "out_edges": [
                {"source_ref": topic_id, "target_ref": node_id, "type": "contains", "note": "最终尾批主题成员", "inferred": False, "attrs": {}}
                for node_id in tail_members
            ],
            "candidates": [],
            "consolidation_memo": "最终八条已完成主题审查。",
        }

        result = apply_rebuild_response(self.repo, refine_topic)

        self.assertTrue(result["rebuild_complete"])
        self.assertFalse(rebuild_workspace(self.repo).exists())
        manifest = load_manifest(self.repo)
        self.assertEqual([], consolidation_state(manifest)["pending_raw"])
        topic = next(node for node in list_index_pages(self.repo) if node.id == topic_id)
        actual_members = {
            edge["target"] for edge in manifest["edges"]
            if edge["source"] == topic_id and edge["type"] == "contains"
        }
        self.assertEqual(set(tail_members), actual_members)
        self.assertTrue(set(tail_sources) <= set(topic.sources))
        reading_sources = {
            source_id
            for item in topic.attrs["topic_reading"]["evolution"]
            for source_id in item["source_ids"]
        }
        self.assertTrue(reading_sources & tail_raw_ids)

    def test_rebuild_rejects_source_raw_metadata_change(self) -> None:
        request = build_rebuild_request(self.repo)
        apply_rebuild_response(self.repo, self._replay_plan(request))
        changed_path = self._raw_path(self.ordered_raw[1]["id"])
        meta, body = frontmatter.read_document(changed_path)
        changed_path.chmod(0o644)
        meta["title"] = "重建期间被修改的标题"
        frontmatter.write_document(changed_path, meta, body)
        changed_path.chmod(0o444)

        with self.assertRaises(StaleSessionError):
            build_rebuild_request(self.repo)

    def test_rebuild_rejects_added_custom_user_metadata_without_overwriting_it(self) -> None:
        first_request = build_rebuild_request(self.repo)
        apply_rebuild_response(self.repo, self._replay_plan(first_request))
        second_request = build_rebuild_request(self.repo)
        changed_path = self._raw_path(self.ordered_raw[1]["id"])
        meta, body = frontmatter.read_document(changed_path)
        custom_user_meta = {"owner": "郑焕", "purpose": "必须保留的自定义字段"}
        changed_path.chmod(0o644)
        meta["custom_user_meta"] = custom_user_meta
        frontmatter.write_document(changed_path, meta, body)
        changed_path.chmod(0o444)

        with self.assertRaises(StaleSessionError):
            apply_rebuild_response(self.repo, self._replay_plan(second_request))

        current_meta, current_body = frontmatter.read_document(changed_path)
        self.assertEqual(custom_user_meta, current_meta["custom_user_meta"])
        self.assertEqual(body, current_body)
        self.assertEqual(1, rebuild_state(self.repo)["processed"])


class EmptyRawRebuildTest(RepositoryTestCase):
    backend = "plain"

    def test_empty_raw_version_drift_can_complete_rebuild(self) -> None:
        manifest = load_manifest(self.repo)
        manifest["kb_version"] = "1.0.0"
        (self.repo / ".kb" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        request = build_rebuild_request(self.repo)
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual([], request["context"]["raw_entries"])
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "rebuild",
            "raw_annotations": [],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": "",
        }

        result = apply_rebuild_response(self.repo, plan)

        self.assertTrue(result["rebuild_complete"])
        self.assertEqual("complete", rebuild_state(self.repo)["phase"])
        self.assertFalse(rebuild_workspace(self.repo).exists())


if __name__ == "__main__":
    unittest.main()
