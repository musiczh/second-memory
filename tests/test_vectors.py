from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from second_memory import frontmatter
from second_memory.chunking import annotation_hash
from second_memory.compiler import (
    DEFAULT_GITIGNORE,
    add_raw,
    apply_response,
    build_compile_request,
    initialize,
    load_manifest,
    manifest_drift,
)
from second_memory.config import load_config, write_config
from second_memory.embedding import EmbeddingError, EmbeddingSpec
from second_memory.vectors import (
    VectorCacheError,
    build_vector_units,
    reindex_vectors,
    resolve_vector_unit_text,
    search_vectors,
    vector_status,
)
from tests.helpers import raw_annotation_fields


def embedding(score: float = 1.0) -> list[float]:
    return [score, math.sqrt(max(0.0, 1.0 - score * score)), *([0.0] * 510)]


class FakeProvider:
    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self._spec = EmbeddingSpec(
            provider="fastembed",
            model="BAAI/bge-small-zh-v1.5",
            dimension=512,
            dtype="float32",
            normalization="l2",
            runtime="onnxruntime-cpu",
            model_hash=hashlib.sha256(b"fake-onnx-model").hexdigest(),
        )
        self.scores = scores or {}
        self.passage_inputs: list[list[str]] = []
        self.query_inputs: list[str] = []

    @property
    def spec(self) -> EmbeddingSpec:
        return self._spec

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.passage_inputs.append(list(texts))
        return [embedding(next((score for marker, score in self.scores.items() if marker in text), 1.0)) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_inputs.append(text)
        return embedding(1.0)


class VectorRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="second-memory-vectors-")
        self.repo = Path(self.temporary.name) / "knowledge-base"
        initialize(self.repo, "agent", "test", "plain")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def compile_raws(
        self,
        raws: list[tuple[str, str]],
        *,
        body_groups: dict[str, list[list[str]]] | None = None,
    ) -> list[str]:
        raw_ids = [str(add_raw(self.repo, title, body, "2026-08-20", ["test"])["raw_id"]) for title, body in raws]
        request = build_compile_request(self.repo, mode="incremental")
        annotations = []
        for entry in request["context"]["raw_entries"]:
            label = str(entry["title"])
            fields = raw_annotation_fields(label)
            annotations.append({
                "raw_id": entry["id"],
                "summary": fields["summary"],
                "summary_segments": fields["summary_segments"],
                "body_groups": (body_groups or {}).get(str(entry["id"]), []),
                "importance": 3,
                "emotion": "",
                "mentions": [],
                "occurrences": [],
                "claims": [],
            })
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": annotations,
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": request["context"]["consolidation_memo"],
        }
        apply_response(self.repo, plan, command="compile")
        return raw_ids

    def raw_entry(self, raw_id: str):
        from second_memory.compiler import raw_lookup

        return raw_lookup(self.repo)[raw_id]

    def compile_sectioned_raw(self, tail_length: int) -> str:
        body = "甲" * 600 + "乙" * tail_length
        raw_id = str(add_raw(self.repo, "分段长原料", body, "2026-08-20", ["test"])["raw_id"])
        request = build_compile_request(self.repo, mode="incremental")
        entry = request["context"]["raw_entries"][0]
        atom_ids = [atom["id"] for atom in entry["body_atoms"]]
        fields = raw_annotation_fields(str(entry["title"]))
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": raw_id,
                **fields,
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
        apply_response(self.repo, plan, command="compile")
        return raw_id

    def rewrite_cached_rows(self, raw_id: str, mutate) -> None:
        manifest_path = self.repo / ".kb/vectors/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        info = manifest["raws"][raw_id]
        jsonl_path = self.repo / ".kb/vectors" / info["file"]
        rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
        mutate(rows)
        content = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        )
        jsonl_path.write_text(content, encoding="utf-8")
        info["file_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    def test_units_are_stable_locators_and_body_chunks_never_cross_sections(self) -> None:
        body = "甲" * 600 + "乙" * 50
        raw_id = str(add_raw(self.repo, "分段长原料", body, "2026-08-20", ["test"])["raw_id"])
        request = build_compile_request(self.repo, mode="incremental")
        atom_ids = [atom["id"] for atom in request["context"]["raw_entries"][0]["body_atoms"]]
        label = "分段长原料"
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": raw_id,
                "summary": (f"这条原料围绕「{label}」记录用户的事实与判断，并保留向量召回、后续编译、周期回顾和来源追溯所需的清晰语义边界。" + "补充说明。")[:100],
                "summary_segments": [f"原料以「{label}」为核心，说明用户当时经历的具体事实和形成的判断，并保留与向量召回、知识编译、周期回顾及来源追溯有关的完整语义信息。"],
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
        apply_response(self.repo, plan, command="compile")
        entry = self.raw_entry(raw_id)

        first = build_vector_units(entry, load_config(self.repo))
        second = build_vector_units(entry, load_config(self.repo))

        self.assertEqual([unit.chunk_id for unit in first], [unit.chunk_id for unit in second])
        self.assertEqual(["headline", "summary"], [unit.kind for unit in first[:2]])
        self.assertEqual(0, first[1].segment_index)
        body_units = [unit for unit in first if unit.kind == "body"]
        self.assertEqual([(0, 300), (255, 555), (510, 600), (600, len(entry.body))], [(unit.start, unit.end) for unit in body_units])
        self.assertTrue(all(50 <= len(unit.text) <= 300 for unit in body_units))
        self.assertTrue(all(not (unit.start < 600 < unit.end) for unit in body_units))
        self.assertEqual([unit.text for unit in first], [resolve_vector_unit_text(entry, unit) for unit in first])

    def test_short_raw_body_is_the_only_body_chunk_below_the_minimum(self) -> None:
        raw_id = self.compile_raws([("短原料", "正文很短。")])[0]

        units = build_vector_units(self.raw_entry(raw_id), load_config(self.repo))

        body_units = [unit for unit in units if unit.kind == "body"]
        self.assertEqual([self.raw_entry(raw_id).body], [unit.text for unit in body_units])

    def test_non_short_raw_rejects_a_persisted_section_below_the_minimum(self) -> None:
        raw_id = self.compile_sectioned_raw(20)

        with self.assertRaisesRegex(VectorCacheError, "section.*minimum"):
            build_vector_units(self.raw_entry(raw_id), load_config(self.repo))
        with self.assertRaisesRegex(VectorCacheError, "section.*minimum"):
            reindex_vectors(self.repo, FakeProvider())
        self.assertFalse((self.repo / ".kb/vectors").exists())

    def test_reindex_writes_text_free_jsonl_and_a_complete_fingerprinted_manifest(self) -> None:
        raw_id = self.compile_raws([("缓存结构", "甲" * 80 + "。" + "乙" * 80 + "。")])[0]
        provider = FakeProvider()

        state = reindex_vectors(self.repo, provider)

        self.assertEqual("ready", state.status)
        manifest = json.loads((self.repo / ".kb/vectors/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(1, manifest["schema"])
        self.assertEqual("fastembed", manifest["provider"])
        self.assertEqual("BAAI/bge-small-zh-v1.5", manifest["model"])
        self.assertEqual(provider.spec.model_hash, manifest["model_hash"])
        self.assertEqual(64, len(manifest["spec_fingerprint"]))
        self.assertEqual(64, len(manifest["config_fingerprint"]))
        self.assertEqual(64, len(manifest["input_fingerprint"]))
        self.assertEqual(1, manifest["raw_count"])
        self.assertEqual(sum(item["unit_count"] for item in manifest["raws"].values()), manifest["unit_count"])
        raw_info = manifest["raws"][raw_id]
        self.assertEqual(71, len(raw_info["body_hash"]))
        self.assertEqual(64, len(raw_info["annotation_hash"]))
        self.assertEqual(64, len(raw_info["raw_fingerprint"]))
        rows = [json.loads(line) for line in (self.repo / ".kb/vectors" / raw_info["file"]).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(rows)
        self.assertTrue(all("text" not in row for row in rows))
        self.assertTrue(all(len(row["vector"]) == 512 for row in rows))
        summary = next(row for row in rows if row["kind"] == "summary")
        body = next(row for row in rows if row["kind"] == "body")
        self.assertEqual(0, summary["segment_index"])
        self.assertIn("start", body)
        self.assertIn("end", body)

    def test_status_rejects_missing_corrupt_dimension_and_stale_cache_as_a_whole(self) -> None:
        raw_ids = self.compile_raws([("完整性甲", "甲" * 90), ("完整性乙", "乙" * 90)])
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)
        manifest_path = self.repo / ".kb/vectors/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        first_path = self.repo / ".kb/vectors" / manifest["raws"][raw_ids[0]]["file"]

        first_content = first_path.read_text(encoding="utf-8")
        first_path.unlink()
        self.assertEqual("missing", vector_status(self.repo).status)
        self.assertEqual([], search_vectors(self.repo, "查询", provider).units)

        first_path.write_text("not-json\n", encoding="utf-8")
        self.assertEqual("corrupt", vector_status(self.repo).status)
        self.assertEqual([], search_vectors(self.repo, "查询", provider).raws)

        first_path.write_text(first_content, encoding="utf-8")
        rows = [json.loads(line) for line in first_content.splitlines()]
        rows[0]["vector"] = rows[0]["vector"][:-1]
        changed = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
        first_path.write_text(changed, encoding="utf-8")
        manifest["raws"][raw_ids[0]]["file_hash"] = hashlib.sha256(changed.encode("utf-8")).hexdigest()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        self.assertEqual("corrupt", vector_status(self.repo).status)

        reindex_vectors(self.repo, provider)
        raw_path = self.raw_entry(raw_ids[0]).path
        os.chmod(raw_path, 0o644)
        meta, body = frontmatter.read_document(raw_path)
        meta["summary"] = str(meta["summary"])[:-1] + "新"
        raw_path.write_text(frontmatter.dump_document(meta, body), encoding="utf-8")
        self.assertEqual("stale", vector_status(self.repo).status)
        result = search_vectors(self.repo, "查询", provider)
        self.assertEqual("stale", result.status)
        self.assertEqual([], result.units)
        self.assertEqual([], result.raws)

    def test_status_rejects_body_locator_that_crosses_a_persisted_section(self) -> None:
        raw_id = self.compile_sectioned_raw(50)
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)

        def cross_section(rows: list[dict[str, object]]) -> None:
            unit = next(row for row in rows if row.get("kind") == "body" and row.get("end") == 600)
            unit["end"] = 610

        self.rewrite_cached_rows(raw_id, cross_section)

        self.assertEqual("corrupt", vector_status(self.repo).status)
        result = search_vectors(self.repo, "查询", provider)
        self.assertEqual("corrupt", result.status)
        self.assertEqual([], result.units)
        self.assertEqual([], result.raws)

    def test_status_rejects_tampered_chunk_id_even_when_file_hash_matches(self) -> None:
        raw_id = self.compile_raws([("篡改块标识", "篡改块标识正文" * 10)])[0]
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)

        self.rewrite_cached_rows(raw_id, lambda rows: rows[0].__setitem__("chunk_id", "chunk-tampered"))

        self.assertEqual("corrupt", vector_status(self.repo).status)
        self.assertEqual([], search_vectors(self.repo, "查询", provider).units)

    def test_config_drift_and_pending_or_disabled_state_never_initialize_a_provider(self) -> None:
        raw_id = self.compile_raws([("状态边界", "状态边界正文" * 10)])[0]
        reindex_vectors(self.repo, FakeProvider())
        config = load_config(self.repo)
        config["vector_chunk_target"] = 250
        write_config(self.repo, config)
        self.assertEqual("stale", vector_status(self.repo).status)

        with patch("second_memory.vectors.FastEmbedProvider", side_effect=AssertionError("provider must stay lazy")):
            self.assertEqual([], search_vectors(self.repo, "查询").units)

        config["vector_enabled"] = False
        write_config(self.repo, config)
        self.assertEqual("disabled", vector_status(self.repo).status)
        config["vector_enabled"] = True
        config["vector_chunk_target"] = 300
        write_config(self.repo, config)
        add_raw(self.repo, "待编译", "新增待编译正文", "2026-08-20", ["test"])
        self.assertEqual("pending", vector_status(self.repo).status)
        self.assertEqual([], search_vectors(self.repo, "查询", FakeProvider()).raws)
        self.assertIn(raw_id, load_manifest(self.repo)["compiled_raw"])

    def test_ready_cache_with_missing_local_model_degrades_without_calling_query(self) -> None:
        self.compile_raws([("本地模型", "本地模型正文" * 10)])
        reindex_vectors(self.repo, FakeProvider())

        with patch("second_memory.vectors.FastEmbedProvider", side_effect=RuntimeError("local model missing")):
            result = search_vectors(self.repo, "查询")

        self.assertEqual("pending", result.status)
        self.assertIn("local model", result.reason)
        self.assertEqual([], result.units)
        self.assertEqual([], result.raws)

    def test_provider_constructor_enforces_reindex_and_search_local_file_boundaries(self) -> None:
        self.compile_raws([("构造边界", "构造边界正文" * 10)])
        offline_destination = self.repo / ".kb/offline-vectors"
        online_destination = self.repo / ".kb/online-vectors"

        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider()) as constructor:
            reindex_vectors(self.repo, offline=True, destination=offline_destination)
        constructor.assert_called_once_with(local_files_only=True)

        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider()) as constructor:
            reindex_vectors(self.repo, destination=online_destination)
        constructor.assert_called_once_with(local_files_only=False)

        reindex_vectors(self.repo, FakeProvider())
        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider()) as constructor:
            result = search_vectors(self.repo, "查询")
        constructor.assert_called_once_with(local_files_only=True)
        self.assertEqual("ready", result.status)

    def test_offline_reindex_fails_without_a_local_model_and_keeps_destination_absent(self) -> None:
        self.compile_raws([("离线缺模型", "离线缺模型正文" * 10)])
        destination = self.repo / ".kb/offline-missing"

        with patch(
            "second_memory.vectors.FastEmbedProvider",
            side_effect=EmbeddingError("local model missing"),
        ) as constructor:
            with self.assertRaisesRegex(EmbeddingError, "local model missing"):
                reindex_vectors(self.repo, offline=True, destination=destination)

        constructor.assert_called_once_with(local_files_only=True)
        self.assertFalse(destination.exists())

    def test_search_uses_stable_score_order_then_limits_units_and_deduplicates_raws(self) -> None:
        raw_ids = self.compile_raws([
            (("低分" if i == 5 else "高分") + f"排序原料{i:02d}", ("低分" if i == 5 else "高分") + "正文内容" * 15)
            for i in range(6)
        ])
        provider = FakeProvider({"低分": 0.34, "高分": 0.8})
        reindex_vectors(self.repo, provider)

        result = search_vectors(self.repo, "排序查询", provider)

        self.assertEqual("ready", result.status)
        self.assertEqual(10, len(result.units))
        self.assertLessEqual(len(result.raws), 5)
        self.assertEqual(sorted((unit.chunk_id for unit in result.units)), [unit.chunk_id for unit in result.units])
        self.assertTrue(all(unit.score >= 0.35 for unit in result.units))
        self.assertNotIn(raw_ids[5], {unit.raw_id for unit in result.units})
        self.assertEqual(len(result.raws), len({item["raw_id"] for item in result.raws}))
        self.assertEqual([item["raw_id"] for item in result.raws], list(dict.fromkeys(unit.raw_id for unit in result.units))[:5])

    def test_search_orders_distinct_scores_before_the_thirty_unit_scan_limit(self) -> None:
        markers = [f"分数{i:02d}" for i in range(12)]
        self.compile_raws([(marker, marker + "正文内容" * 15) for marker in markers])
        config = load_config(self.repo)
        config["vector_unit_limit"] = 40
        config["vector_raw_limit"] = 40
        write_config(self.repo, config)
        scores = {marker: 0.99 - index * 0.04 for index, marker in enumerate(markers)}
        provider = FakeProvider(scores)
        reindex_vectors(self.repo, provider)

        result = search_vectors(self.repo, "排序查询", provider)

        actual_scores = [float(unit.score or 0.0) for unit in result.units]
        self.assertEqual("ready", result.status)
        self.assertEqual(30, len(result.units))
        self.assertEqual(sorted(actual_scores, reverse=True), actual_scores)
        self.assertGreater(actual_scores[0], actual_scores[-1])
        for score in set(actual_scores):
            tied_ids = [unit.chunk_id for unit in result.units if unit.score == score]
            self.assertEqual(sorted(tied_ids), tied_ids)

    def test_destination_build_is_complete_and_does_not_replace_the_live_cache(self) -> None:
        self.compile_raws([("目标目录", "目标目录正文" * 12)])
        provider = FakeProvider()
        destination = self.repo / ".kb/transaction/vectors.next"

        state = reindex_vectors(self.repo, provider, offline=True, destination=destination)

        self.assertEqual("ready", state.status)
        self.assertTrue((destination / "manifest.json").is_file())
        self.assertFalse((self.repo / ".kb/vectors").exists())
        self.assertEqual("ready", vector_status(self.repo, destination=destination).status)

    def test_destination_delta_reembeds_selected_raw_and_reuses_other_valid_jsonl(self) -> None:
        raw_ids = self.compile_raws([("增量甲", "增量甲正文" * 12), ("增量乙", "增量乙正文" * 12)])
        reindex_vectors(self.repo, FakeProvider())
        live_manifest = json.loads((self.repo / ".kb/vectors/manifest.json").read_text(encoding="utf-8"))
        unchanged_file = Path(str(live_manifest["raws"][raw_ids[1]]["file"]))
        unchanged_bytes = (self.repo / ".kb/vectors" / unchanged_file).read_bytes()
        provider = FakeProvider()
        destination = self.repo / ".kb/transaction/vectors.next"

        state = reindex_vectors(self.repo, provider, raw_ids=[raw_ids[0]], destination=destination)

        self.assertEqual("ready", state.status)
        self.assertEqual(1, len(provider.passage_inputs))
        self.assertTrue(provider.passage_inputs[0])
        self.assertTrue(all("增量乙" not in text for text in provider.passage_inputs[0]))
        self.assertEqual(unchanged_bytes, (destination / unchanged_file).read_bytes())
        self.assertEqual("ready", vector_status(self.repo, destination=destination).status)

    def test_main_manifest_records_annotation_hash_and_reports_drift_kind(self) -> None:
        raw_id = self.compile_raws([("主清单", "主清单正文" * 10)])[0]
        entry = self.raw_entry(raw_id)
        info = load_manifest(self.repo)["raw_hashes"][raw_id]

        expected = annotation_hash(
            entry.title,
            str(entry.annotations["summary"]),
            entry.annotations["summary_segments"],
            entry.annotations["body_sections"],
        )
        self.assertEqual(expected, info["annotation_hash"])
        raw_path = entry.path
        os.chmod(raw_path, 0o644)
        meta, body = frontmatter.read_document(raw_path)
        meta["summary"] = str(meta["summary"])[:-1] + "改"
        raw_path.write_text(frontmatter.dump_document(meta, body), encoding="utf-8")
        self.assertEqual([f"raw:{raw_id}:annotation"], manifest_drift(self.repo))

        raw_path.write_text(frontmatter.dump_document({**meta, "summary": entry.annotations["summary"]}, body + "新增"), encoding="utf-8")
        self.assertEqual([f"raw:{raw_id}:body"], manifest_drift(self.repo))

    def test_default_gitignore_excludes_vector_and_evaluation_caches(self) -> None:
        self.assertIn(".kb/vectors/\n", DEFAULT_GITIGNORE)
        self.assertIn(".kb/eval/\n", DEFAULT_GITIGNORE)
        self.assertEqual(DEFAULT_GITIGNORE, (self.repo / ".gitignore").read_text(encoding="utf-8"))

    def test_initialize_preserves_existing_gitignore_and_adds_required_cache_entries(self) -> None:
        repo = Path(self.temporary.name) / "existing-gitignore"
        repo.mkdir()
        (repo / ".gitignore").write_text("user-cache/\n", encoding="utf-8")

        initialize(repo, "agent", "test", "plain")

        content = (repo / ".gitignore").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("user-cache/\n"))
        self.assertIn(".kb/vectors/\n", content)
        self.assertIn(".kb/eval/\n", content)


if __name__ == "__main__":
    unittest.main()
