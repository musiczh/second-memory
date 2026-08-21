from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from second_memory import transaction as transaction_module
from second_memory.compiler import (
    add_raw,
    apply_rebuild_response,
    apply_response,
    build_compile_request,
    build_rebuild_request,
    initialize,
    load_manifest,
    read_pending,
    rebuild_state,
    rebuild_workspace,
)
from second_memory.config import load_config, write_config
from second_memory.embedding import EmbeddingSpec
from second_memory.transaction import KnowledgeTransaction, recover_transaction, transaction_state
from second_memory.vectors import VectorCacheError, VectorCacheState, reindex_vectors, vector_status
from tests.helpers import raw_annotation_fields


def embedding(axis: int = 0) -> list[float]:
    vector = [0.0] * 512
    vector[axis] = 1.0
    return vector


class FakeProvider:
    def __init__(self, *, axis: int = 0, failure: Exception | None = None) -> None:
        self._spec = EmbeddingSpec(
            provider="fastembed",
            model="BAAI/bge-small-zh-v1.5",
            dimension=512,
            dtype="float32",
            normalization="l2",
            runtime="onnxruntime-cpu",
            model_hash=hashlib.sha256(b"fake-onnx-model").hexdigest(),
        )
        self.axis = axis
        self.failure = failure
        self.passage_inputs: list[list[str]] = []

    @property
    def spec(self) -> EmbeddingSpec:
        return self._spec

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.passage_inputs.append(list(texts))
        if self.failure is not None:
            raise self.failure
        return [embedding(self.axis) for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return embedding(self.axis)


class VectorTestRepository(unittest.TestCase):
    backend = "plain"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="second-memory-vector-transaction-")
        self.repo = Path(self.temporary.name) / "knowledge-base"
        initialize(self.repo, "agent", "test", self.backend)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def set_vectors_enabled(self, enabled: bool) -> None:
        config = load_config(self.repo)
        config["vector_enabled"] = enabled
        write_config(self.repo, config)

    def add_plan(self, title: str, body: str) -> tuple[str, dict[str, object]]:
        raw_id = str(add_raw(self.repo, title, body, "2026-08-20", ["test"])["raw_id"])
        request = build_compile_request(self.repo, mode="incremental")
        plan: dict[str, object] = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": raw_id,
                **raw_annotation_fields(title),
                "importance": 3,
                "emotion": "",
                "mentions": [],
                "occurrences": [],
                "claims": [],
            }],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": request["context"]["consolidation_memo"],
        }
        return raw_id, plan

    def compile_without_vectors(self, title: str = "事务原料") -> str:
        self.set_vectors_enabled(False)
        raw_id, plan = self.add_plan(title, (title + "正文内容") * 20)
        apply_response(self.repo, plan, command="compile")
        self.set_vectors_enabled(True)
        return raw_id

    def stage_current_core(self, tx: KnowledgeTransaction) -> None:
        for path in sorted((self.repo / "wiki").rglob("*.md")):
            target = tx.wiki_next / path.relative_to(self.repo / "wiki")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        tx.stage_metadata(
            index=(self.repo / "index.md").read_text(encoding="utf-8"),
            manifest=load_manifest(self.repo),
            pending_rows=read_pending(self.repo),
        )

    @staticmethod
    def cache_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }


class KnowledgeVectorTransactionTest(VectorTestRepository):
    def test_promote_switches_ready_cache_and_rollback_restores_previous_cache(self) -> None:
        self.compile_without_vectors()
        live = self.repo / ".kb/vectors"
        reindex_vectors(self.repo, FakeProvider(axis=0))
        original = self.cache_bytes(live)
        tx = KnowledgeTransaction(self.repo, "session-vector-switch")
        tx.prepare(include_vectors=True)
        self.stage_current_core(tx)
        reindex_vectors(self.repo, FakeProvider(axis=1), destination=tx.vectors_next)

        tx.promote()

        self.assertNotEqual(original, self.cache_bytes(live))
        self.assertTrue(tx.vectors_previous.exists())
        tx.rollback()
        self.assertEqual(original, self.cache_bytes(live))

    def test_rollback_removes_new_cache_when_no_previous_cache_existed(self) -> None:
        self.compile_without_vectors()
        tx = KnowledgeTransaction(self.repo, "session-vector-first")
        tx.prepare(include_vectors=True)
        self.stage_current_core(tx)
        reindex_vectors(self.repo, FakeProvider(), destination=tx.vectors_next)

        tx.promote()
        self.assertTrue((self.repo / ".kb/vectors").exists())
        tx.rollback()

        self.assertFalse((self.repo / ".kb/vectors").exists())
        self.assertEqual("clean", transaction_state(self.repo)["state"])

    def test_prepared_recovery_discards_next_without_touching_live_cache(self) -> None:
        self.compile_without_vectors()
        live = self.repo / ".kb/vectors"
        reindex_vectors(self.repo, FakeProvider(axis=0))
        original = self.cache_bytes(live)
        tx = KnowledgeTransaction(self.repo, "session-vector-prepared")
        tx.prepare(include_vectors=True)
        reindex_vectors(self.repo, FakeProvider(axis=1), destination=tx.vectors_next)

        self.assertEqual("rolled_back", recover_transaction(self.repo))

        self.assertEqual(original, self.cache_bytes(live))
        self.assertEqual("clean", transaction_state(self.repo)["state"])

    def test_promoting_recovery_uses_directory_state_to_restore_live_cache(self) -> None:
        self.compile_without_vectors()
        live = self.repo / ".kb/vectors"
        reindex_vectors(self.repo, FakeProvider(axis=0))
        original = self.cache_bytes(live)
        tx = KnowledgeTransaction(self.repo, "session-vector-promoting")
        tx.prepare(include_vectors=True)
        reindex_vectors(self.repo, FakeProvider(axis=1), destination=tx.vectors_next)
        tx._write_journal("promoting")
        os.replace(live, tx.vectors_previous)
        os.replace(tx.vectors_next, live)

        self.assertEqual("rolled_back", recover_transaction(self.repo))

        self.assertEqual(original, self.cache_bytes(live))

    def test_corrupt_journal_and_missing_marker_restore_crash_between_vector_replaces(self) -> None:
        self.compile_without_vectors()
        live = self.repo / ".kb/vectors"
        reindex_vectors(self.repo, FakeProvider(axis=0))
        original = self.cache_bytes(live)
        tx = KnowledgeTransaction(self.repo, "session-vector-between-replaces")
        tx.prepare(include_vectors=True)
        reindex_vectors(self.repo, FakeProvider(axis=1), destination=tx.vectors_next)
        tx._write_journal("promoting")
        os.replace(live, tx.vectors_previous)
        tx.journal.write_text("{", encoding="utf-8")
        tx.vectors_marker.unlink()

        self.assertEqual("rolled_back_corrupt_journal", recover_transaction(self.repo))

        self.assertEqual(original, self.cache_bytes(live))

    def test_corrupt_journal_and_marker_remove_promoted_cache_when_none_existed(self) -> None:
        self.compile_without_vectors()
        tx = KnowledgeTransaction(self.repo, "session-vector-first-corrupt")
        tx.prepare(include_vectors=True)
        reindex_vectors(self.repo, FakeProvider(), destination=tx.vectors_next)
        tx._write_journal("promoting")
        os.replace(tx.vectors_next, self.repo / ".kb/vectors")
        tx.journal.write_text("{", encoding="utf-8")
        tx.vectors_marker.write_text("{", encoding="utf-8")

        self.assertEqual("rolled_back_corrupt_journal", recover_transaction(self.repo))

        self.assertFalse((self.repo / ".kb/vectors").exists())

    def test_corrupt_journal_and_missing_marker_remove_live_promoted_without_old_cache(self) -> None:
        self.compile_without_vectors()
        original_index = (self.repo / "index.md").read_text(encoding="utf-8")
        tx = KnowledgeTransaction(self.repo, "session-vector-first-missing-marker")
        tx.prepare(include_vectors=True)
        self.stage_current_core(tx)
        tx.stage_metadata(
            index="promoted core\n",
            manifest=load_manifest(self.repo),
            pending_rows=read_pending(self.repo),
        )
        reindex_vectors(self.repo, FakeProvider(), destination=tx.vectors_next)
        tx.promote()
        self.assertTrue(tx.vectors_previous.is_dir())
        self.assertEqual([], list(tx.vectors_previous.iterdir()))
        tx.journal.write_text("{", encoding="utf-8")
        tx.vectors_marker.unlink()

        self.assertEqual("rolled_back_corrupt_journal", recover_transaction(self.repo))

        self.assertEqual(original_index, (self.repo / "index.md").read_text(encoding="utf-8"))
        self.assertFalse((self.repo / ".kb/vectors").exists())

    def test_corrupt_journal_after_vector_discard_preserves_original_live_only_cache(self) -> None:
        self.compile_without_vectors()
        live = self.repo / ".kb/vectors"
        reindex_vectors(self.repo, FakeProvider(axis=0))
        original = self.cache_bytes(live)
        original_index = (self.repo / "index.md").read_text(encoding="utf-8")
        tx = KnowledgeTransaction(self.repo, "session-vector-discard-missing-marker")
        tx.prepare(include_vectors=True)
        self.stage_current_core(tx)
        tx.stage_metadata(
            index="promoted core only\n",
            manifest=load_manifest(self.repo),
            pending_rows=read_pending(self.repo),
        )
        tx.discard_vectors()
        tx.promote()
        self.assertTrue(live.is_dir())
        self.assertFalse(tx.vectors_next.exists())
        self.assertFalse(tx.vectors_previous.exists())
        self.assertFalse(tx.vectors_marker.exists())
        tx.journal.write_text("{", encoding="utf-8")

        self.assertEqual("rolled_back_corrupt_journal", recover_transaction(self.repo))

        self.assertEqual(original_index, (self.repo / "index.md").read_text(encoding="utf-8"))
        self.assertEqual(original, self.cache_bytes(live))

    def test_absence_sentinel_is_durable_before_next_to_live_failure_and_is_cleaned(self) -> None:
        self.compile_without_vectors()
        live = self.repo / ".kb/vectors"
        tx = KnowledgeTransaction(self.repo, "session-vector-absence-order")
        tx.prepare(include_vectors=True)
        self.stage_current_core(tx)
        reindex_vectors(self.repo, FakeProvider(), destination=tx.vectors_next)
        real_replace = os.replace
        reached_next_to_live = False

        def fail_next_to_live(source, destination) -> None:
            nonlocal reached_next_to_live
            if Path(source) == tx.vectors_next and Path(destination) == live:
                reached_next_to_live = True
                self.assertTrue(tx.vectors_previous.is_dir())
                self.assertEqual([], list(tx.vectors_previous.iterdir()))
                directory_fsync.assert_any_call(tx.root)
                raise OSError("next to live failed after absence sentinel")
            real_replace(source, destination)

        with patch(
            "second_memory.transaction._fsync_directory",
            wraps=transaction_module._fsync_directory,
        ) as directory_fsync, patch(
            "second_memory.transaction.os.replace",
            side_effect=fail_next_to_live,
        ):
            with self.assertRaisesRegex(VectorCacheError, "next to live failed after absence sentinel"):
                tx.promote()

        self.assertTrue(reached_next_to_live)
        self.assertFalse(tx.vectors_previous.exists())
        self.assertFalse(live.exists())
        self.assertTrue(tx.vectors_next.exists())
        tx.rollback()

    def test_corrupt_recovery_handles_crash_after_absence_sentinel_before_live_replace(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        self.compile_without_vectors()
        live = self.repo / ".kb/vectors"
        tx = KnowledgeTransaction(self.repo, "session-vector-absence-crash")
        tx.prepare(include_vectors=True)
        self.stage_current_core(tx)
        reindex_vectors(self.repo, FakeProvider(), destination=tx.vectors_next)
        real_replace = os.replace

        def crash_next_to_live(source, destination) -> None:
            if Path(source) == tx.vectors_next and Path(destination) == live:
                raise SimulatedCrash
            real_replace(source, destination)

        with patch("second_memory.transaction.os.replace", side_effect=crash_next_to_live):
            with self.assertRaises(SimulatedCrash):
                tx.promote()

        self.assertTrue(tx.vectors_previous.is_dir())
        self.assertEqual([], list(tx.vectors_previous.iterdir()))
        self.assertTrue(tx.vectors_next.exists())
        self.assertFalse(live.exists())
        tx.journal.write_text("{", encoding="utf-8")
        tx.vectors_marker.unlink()

        self.assertEqual("rolled_back_corrupt_journal", recover_transaction(self.repo))

        self.assertFalse(live.exists())
        self.assertFalse(tx.root.exists())

    def test_vector_replaces_fsync_both_parent_directories_before_core_switch(self) -> None:
        self.compile_without_vectors()
        live = self.repo / ".kb/vectors"
        reindex_vectors(self.repo, FakeProvider(axis=0))
        tx = KnowledgeTransaction(self.repo, "session-vector-durable-swap")
        tx.prepare(include_vectors=True)
        self.stage_current_core(tx)
        reindex_vectors(self.repo, FakeProvider(axis=1), destination=tx.vectors_next)
        events: list[tuple[str, Path, Path | None]] = []
        real_replace = os.replace

        def recording_replace(source, destination) -> None:
            events.append(("replace", Path(source), Path(destination)))
            real_replace(source, destination)

        def recording_fsync(path: Path) -> None:
            events.append(("fsync", Path(path), None))

        with patch("second_memory.transaction.os.replace", side_effect=recording_replace), patch(
            "second_memory.transaction._fsync_directory",
            side_effect=recording_fsync,
        ):
            tx.promote()

        old_to_previous = events.index(("replace", live, tx.vectors_previous))
        next_to_live = events.index(("replace", tx.vectors_next, live))
        core_switch = events.index(("replace", self.repo / "wiki", tx.wiki_previous))
        self.assertIn(("fsync", live.parent, None), events[old_to_previous + 1 : next_to_live])
        self.assertIn(("fsync", tx.root, None), events[old_to_previous + 1 : next_to_live])
        self.assertIn(("fsync", live.parent, None), events[next_to_live + 1 : core_switch])
        self.assertIn(("fsync", tx.root, None), events[next_to_live + 1 : core_switch])

    def test_vectors_marker_fsyncs_file_then_atomically_replaces_then_fsyncs_parent(self) -> None:
        tx = KnowledgeTransaction(self.repo, "session-vector-marker-durable")
        tx.root.mkdir(parents=True)
        events: list[tuple[str, Path | None]] = []
        real_replace = os.replace
        real_fsync = os.fsync

        def recording_replace(source, destination) -> None:
            events.append(("replace", Path(destination)))
            real_replace(source, destination)

        def recording_fsync(descriptor: int) -> None:
            events.append(("fsync", None))
            real_fsync(descriptor)

        def recording_directory_fsync(path: Path) -> None:
            events.append(("directory_fsync", Path(path)))

        with patch("second_memory.transaction.os.replace", side_effect=recording_replace), patch(
            "second_memory.transaction.os.fsync",
            side_effect=recording_fsync,
        ), patch(
            "second_memory.transaction._fsync_directory",
            side_effect=recording_directory_fsync,
        ):
            tx._write_vectors_marker()

        file_fsync = events.index(("fsync", None))
        replace = events.index(("replace", tx.vectors_marker))
        directory_fsync = events.index(("directory_fsync", tx.root))
        self.assertLess(file_fsync, replace)
        self.assertLess(replace, directory_fsync)
        self.assertFalse(tx.vectors_marker.with_suffix(".json.tmp").exists())


class GitKnowledgeVectorRecoveryTest(VectorTestRepository):
    backend = "git"

    def prepare_promoted_vector_transaction(self, session_id: str) -> tuple[KnowledgeTransaction, dict[str, bytes]]:
        self.compile_without_vectors()
        live = self.repo / ".kb/vectors"
        reindex_vectors(self.repo, FakeProvider(axis=0))
        original = self.cache_bytes(live)
        tx = KnowledgeTransaction(self.repo, session_id)
        tx.prepare(include_vectors=True)
        self.stage_current_core(tx)
        reindex_vectors(self.repo, FakeProvider(axis=1), destination=tx.vectors_next)
        tx.promote()
        return tx, original

    def test_promoted_uncommitted_recovery_restores_previous_cache(self) -> None:
        _, original = self.prepare_promoted_vector_transaction("session-vector-promoted")

        self.assertEqual("rolled_back", recover_transaction(self.repo))

        self.assertEqual(original, self.cache_bytes(self.repo / ".kb/vectors"))

    def test_corrupt_journal_recovery_restores_previous_cache(self) -> None:
        tx, original = self.prepare_promoted_vector_transaction("session-vector-corrupt")
        tx.journal.write_text("{", encoding="utf-8")

        self.assertEqual("rolled_back_corrupt_journal", recover_transaction(self.repo))

        self.assertEqual(original, self.cache_bytes(self.repo / ".kb/vectors"))

    def test_corrupt_journal_and_marker_restore_previous_cache(self) -> None:
        tx, original = self.prepare_promoted_vector_transaction("session-vector-corrupt-marker")
        tx.journal.write_text("{", encoding="utf-8")
        tx.vectors_marker.write_text("{", encoding="utf-8")

        self.assertEqual("rolled_back_corrupt_journal", recover_transaction(self.repo))

        self.assertEqual(original, self.cache_bytes(self.repo / ".kb/vectors"))

    def test_committed_recovery_replaces_corrupt_live_with_previous_cache(self) -> None:
        tx, original = self.prepare_promoted_vector_transaction("session-vector-committed-corrupt")
        (self.repo / ".kb/vectors/manifest.json").write_text("{", encoding="utf-8")
        tx.mark_committed("commit-vector-corrupt")

        self.assertEqual("finalized", recover_transaction(self.repo))

        self.assertEqual(original, self.cache_bytes(self.repo / ".kb/vectors"))


class IncrementalVectorApplyTest(VectorTestRepository):
    backend = "git"

    def test_incremental_apply_builds_only_changed_raw_and_commits_no_cache_path(self) -> None:
        first_body = "第一条原料的独特正文" * 20
        _, first_plan = self.add_plan("第一条原料", first_body)
        first_provider = FakeProvider(axis=0)
        with patch("second_memory.vectors.FastEmbedProvider", return_value=first_provider) as constructor:
            first_result = apply_response(self.repo, first_plan, command="compile")
        constructor.assert_called_once_with(local_files_only=True)
        self.assertEqual("ready", first_result["vector_status"])

        second_body = "第二条原料的独特正文" * 20
        second_id, second_plan = self.add_plan("第二条原料", second_body)
        second_provider = FakeProvider(axis=1)
        with patch("second_memory.vectors.FastEmbedProvider", return_value=second_provider) as constructor:
            result = apply_response(self.repo, second_plan, command="compile")

        constructor.assert_called_once_with(local_files_only=True)
        embedded = "".join(second_provider.passage_inputs[0])
        self.assertIn("第二条原料", embedded)
        self.assertNotIn("第一条原料的独特正文", embedded)
        self.assertEqual([second_id], result["raw_ids"])
        self.assertEqual("ready", result["vector_status"])
        self.assertEqual("ready", vector_status(self.repo).status)
        committed_paths = subprocess.run(
            ["git", "show", "--pretty=format:", "--name-only", "HEAD"],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
        self.assertFalse(any(path.startswith(".kb/vectors") for path in committed_paths))

    def test_missing_model_does_not_rollback_core_apply(self) -> None:
        raw_id, plan = self.add_plan("缺少本地模型", "缺少本地模型时核心编译仍应完成" * 20)
        with patch("second_memory.vectors.FastEmbedProvider", side_effect=RuntimeError("local model missing")) as constructor:
            result = apply_response(self.repo, plan, command="compile")

        constructor.assert_called_once_with(local_files_only=True)
        self.assertEqual([raw_id], load_manifest(self.repo)["compiled_raw"])
        self.assertEqual([], read_pending(self.repo))
        self.assertEqual("pending", result["vector_status"])
        self.assertIn("local model missing", result["vector_reason"])
        self.assertFalse((self.repo / ".kb/vectors").exists())

    def test_inference_failure_keeps_old_cache_and_reports_stale(self) -> None:
        _, first_plan = self.add_plan("缓存基线", "缓存基线原料正文" * 20)
        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider()):
            apply_response(self.repo, first_plan, command="compile")
        live = self.repo / ".kb/vectors"
        original = self.cache_bytes(live)
        raw_id, second_plan = self.add_plan("推理失败", "推理失败仍然提交核心原料" * 20)
        failing = FakeProvider(failure=RuntimeError("inference exploded"))

        with patch("second_memory.vectors.FastEmbedProvider", return_value=failing):
            result = apply_response(self.repo, second_plan, command="compile")

        self.assertIn(raw_id, load_manifest(self.repo)["compiled_raw"])
        self.assertEqual(original, self.cache_bytes(live))
        self.assertEqual("stale", result["vector_status"])
        self.assertIn("inference exploded", result["vector_reason"])
        self.assertFalse((self.repo / ".kb/transaction").exists())

    def test_vector_cache_error_from_short_section_degrades_apply(self) -> None:
        body = "甲" * 600 + "乙" * 20
        raw_id = str(add_raw(self.repo, "短语义分段", body, "2026-08-20", ["test"])["raw_id"])
        request = build_compile_request(self.repo, mode="incremental")
        raw = request["context"]["raw_entries"][0]
        atom_ids = [atom["id"] for atom in raw["body_atoms"]]
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": raw_id,
                **raw_annotation_fields("短语义分段"),
                "body_groups": [atom_ids[:2], atom_ids[2:]],
                "importance": 3,
                "emotion": "",
                "mentions": [],
                "occurrences": [],
                "claims": [],
            }],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": request["context"]["consolidation_memo"],
        }

        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider()):
            result = apply_response(self.repo, plan, command="compile")

        self.assertIn(raw_id, load_manifest(self.repo)["compiled_raw"])
        self.assertEqual("pending", result["vector_status"])
        self.assertIn("shorter than the vector chunk minimum", result["vector_reason"])

    def test_destination_staging_failure_does_not_rollback_core_apply(self) -> None:
        raw_id, plan = self.add_plan("缓存落盘失败", "缓存落盘失败时仍保留核心编译结果" * 20)

        with patch("second_memory.compiler.reindex_vectors", side_effect=OSError("staging disk failure")):
            result = apply_response(self.repo, plan, command="compile")

        self.assertIn(raw_id, load_manifest(self.repo)["compiled_raw"])
        self.assertEqual("pending", result["vector_status"])
        self.assertIn("staging disk failure", result["vector_reason"])
        self.assertFalse((self.repo / ".kb/transaction").exists())

    def test_non_validating_ready_stage_is_discarded_before_core_only_retry(self) -> None:
        raw_id, plan = self.add_plan("伪就绪缓存", "伪就绪缓存不能参与事务晋升但也不能阻断核心编译" * 20)

        with patch(
            "second_memory.compiler.reindex_vectors",
            return_value=VectorCacheState("ready", "claimed ready"),
        ):
            result = apply_response(self.repo, plan, command="compile")

        self.assertIn(raw_id, load_manifest(self.repo)["compiled_raw"])
        self.assertEqual("pending", result["vector_status"])
        self.assertIn("destination is not ready", result["vector_reason"])
        self.assertFalse((self.repo / ".kb/vectors").exists())

    def test_next_to_live_replace_failure_keeps_old_cache_and_completes_core_apply(self) -> None:
        _, first_plan = self.add_plan("交换失败基线", "交换失败基线原料正文" * 20)
        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider(axis=0)):
            apply_response(self.repo, first_plan, command="compile")
        live = self.repo / ".kb/vectors"
        original = self.cache_bytes(live)
        raw_id, second_plan = self.add_plan("交换失败增量", "乙" * 599)
        real_replace = os.replace
        failure_count = 0

        def fail_next_to_live_once(source, destination) -> None:
            nonlocal failure_count
            if Path(source) == self.repo / ".kb/transaction/vectors.next" and Path(destination) == live:
                failure_count += 1
                raise OSError("next to live failed")
            real_replace(source, destination)

        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider(axis=1)), patch(
            "second_memory.transaction.os.replace",
            side_effect=fail_next_to_live_once,
        ):
            result = apply_response(self.repo, second_plan, command="compile")

        self.assertEqual(1, failure_count)
        self.assertIn(raw_id, load_manifest(self.repo)["compiled_raw"])
        self.assertEqual([], read_pending(self.repo))
        self.assertEqual(original, self.cache_bytes(live))
        self.assertEqual("stale", result["vector_status"])
        self.assertIn("next to live failed", result["vector_reason"])


class RawOnlyRebuildVectorTest(VectorTestRepository):
    def seed_compiled_raw(self) -> None:
        self.set_vectors_enabled(False)
        _, plan = self.add_plan("重建原料", "重建原料正文必须足够长以支持完整向量分块" * 20)
        apply_response(self.repo, plan, command="compile")
        self.set_vectors_enabled(True)

    @staticmethod
    def replay_plan(request: dict[str, object]) -> dict[str, object]:
        context = request["context"]
        assert isinstance(context, dict)
        raw = context["raw_entries"][0]
        return {
            "schema_version": 2,
            "session_id": context["session_id"],
            "mode": "rebuild",
            "raw_annotations": [{
                "raw_id": raw["id"],
                **raw_annotation_fields("重建后的原料摘要"),
                "importance": 3,
                "emotion": "",
                "mentions": [],
                "occurrences": [],
                "claims": [],
            }],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": context["consolidation_memo"],
        }

    @staticmethod
    def consolidation_plan(request: dict[str, object]) -> dict[str, object]:
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
            "consolidation_memo": "重建尾批已审查。",
        }

    def test_workspace_steps_skip_vectors_and_final_promotion_reindexes_once_local_only(self) -> None:
        self.seed_compiled_raw()
        first_request = build_rebuild_request(self.repo)
        provider = FakeProvider()
        with patch("second_memory.vectors.FastEmbedProvider", return_value=provider) as constructor:
            replay_result = apply_rebuild_response(self.repo, self.replay_plan(first_request))
            workspace = rebuild_workspace(self.repo)
            self.assertFalse((workspace / ".kb/vectors").exists())
            self.assertFalse(replay_result["rebuild_complete"])
            tail_request = build_rebuild_request(self.repo)
            result = apply_rebuild_response(self.repo, self.consolidation_plan(tail_request))

        constructor.assert_called_once_with(local_files_only=True)
        self.assertTrue(result["rebuild_complete"])
        self.assertEqual("ready", result["vector_status"])
        self.assertEqual("ready", vector_status(self.repo).status)

    def test_final_reindex_failure_does_not_undo_completed_core_rebuild(self) -> None:
        self.seed_compiled_raw()
        first_request = build_rebuild_request(self.repo)
        with patch("second_memory.compiler.reindex_vectors", side_effect=VectorCacheError("rebuild vectors failed")) as reindex:
            apply_rebuild_response(self.repo, self.replay_plan(first_request))
            self.assertFalse((rebuild_workspace(self.repo) / ".kb/vectors").exists())
            tail_request = build_rebuild_request(self.repo)
            result = apply_rebuild_response(self.repo, self.consolidation_plan(tail_request))

        reindex.assert_called_once_with(self.repo, offline=True)
        self.assertTrue(result["rebuild_complete"])
        self.assertEqual("complete", rebuild_state(self.repo)["phase"])
        self.assertEqual("pending", result["vector_status"])
        self.assertIn("rebuild vectors failed", result["vector_reason"])

    def test_workspace_cleanup_failure_still_reindexes_once_and_returns_cleanup_error(self) -> None:
        self.seed_compiled_raw()
        first_request = build_rebuild_request(self.repo)
        workspace = rebuild_workspace(self.repo)
        real_rmtree = shutil.rmtree

        def fail_workspace_cleanup(path, *args, **kwargs) -> None:
            if Path(path) == workspace:
                raise OSError("workspace cleanup failed")
            real_rmtree(path, *args, **kwargs)

        with patch("second_memory.compiler.shutil.rmtree", side_effect=fail_workspace_cleanup), patch(
            "second_memory.compiler.reindex_vectors",
            return_value=VectorCacheState("ready", "rebuilt once"),
        ) as reindex:
            apply_rebuild_response(self.repo, self.replay_plan(first_request))
            tail_request = build_rebuild_request(self.repo)
            result = apply_rebuild_response(self.repo, self.consolidation_plan(tail_request))

        reindex.assert_called_once_with(self.repo, offline=True)
        self.assertTrue(result["rebuild_complete"])
        self.assertEqual("complete", load_manifest(self.repo)["rebuild"]["phase"])
        self.assertEqual("ready", result["vector_status"])
        self.assertIn("workspace cleanup failed", result["cleanup_error"])


if __name__ == "__main__":
    unittest.main()
