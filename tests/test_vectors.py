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
    VECTOR_CACHE_SCHEMA,
    VectorCacheError,
    build_vector_units,
    plan_vector_update,
    reindex_vectors,
    resolve_vector_unit_text,
    search_vectors,
    vector_status,
)
from tests.helpers import raw_annotation_fields


def embedding(score: float = 1.0) -> list[float]:
    return [score, math.sqrt(max(0.0, 1.0 - score * score)), *([0.0] * 510)]


class FakeProvider:
    def __init__(self, scores: dict[str, float] | None = None, *, model_seed: bytes = b"fake-onnx-model") -> None:
        self._spec = EmbeddingSpec(
            provider="fastembed",
            model="BAAI/bge-small-zh-v1.5",
            dimension=512,
            dtype="float32",
            normalization="l2",
            runtime="onnxruntime-cpu",
            model_hash=hashlib.sha256(model_seed).hexdigest(),
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
        duplicate_summary: bool = False,
    ) -> list[str]:
        raw_ids = [str(add_raw(self.repo, title, body, "2026-08-20", ["test"])["raw_id"]) for title, body in raws]
        request = build_compile_request(self.repo, mode="incremental")
        annotations = []
        for entry in request["context"]["raw_entries"]:
            label = str(entry["title"])
            fields = raw_annotation_fields(label)
            if duplicate_summary:
                fields["summary"] = label
            annotations.append({
                "raw_id": entry["id"],
                "summary": fields["summary"],
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

    def rewrite_cache_manifest(self, mutate) -> None:
        manifest_path = self.repo / ".kb/vectors/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mutate(manifest)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    def test_units_are_stable_locators_derived_without_vector_compile_annotations(self) -> None:
        body = "甲" * 600 + "乙" * 50
        raw_id = self.compile_raws([("分段长原料", body)])[0]
        entry = self.raw_entry(raw_id)

        first = build_vector_units(entry, load_config(self.repo))
        second = build_vector_units(entry, load_config(self.repo))

        self.assertEqual([unit.chunk_id for unit in first], [unit.chunk_id for unit in second])
        self.assertEqual(["headline", "summary"], [unit.kind for unit in first[:2]])
        self.assertEqual(0, first[1].segment_index)
        self.assertNotIn("summary_segments", entry.annotations)
        self.assertNotIn("body_sections", entry.annotations)
        body_units = [unit for unit in first if unit.kind == "body"]
        self.assertEqual([(0, 300), (255, 555), (510, len(entry.body))], [(unit.start, unit.end) for unit in body_units])
        self.assertTrue(all(50 <= len(unit.text) <= 300 for unit in body_units))
        self.assertEqual([unit.text for unit in first], [resolve_vector_unit_text(entry, unit) for unit in first])

    def test_short_raw_body_is_the_only_body_chunk_below_the_minimum(self) -> None:
        raw_id = self.compile_raws([("短原料", "正文很短。")])[0]

        units = build_vector_units(self.raw_entry(raw_id), load_config(self.repo))

        body_units = [unit for unit in units if unit.kind == "body"]
        self.assertEqual([self.raw_entry(raw_id).body], [unit.text for unit in body_units])

    def test_reindex_writes_text_free_jsonl_and_a_complete_fingerprinted_manifest(self) -> None:
        raw_id = self.compile_raws([("缓存结构", "甲" * 80 + "。" + "乙" * 80 + "。")])[0]
        provider = FakeProvider()

        state = reindex_vectors(self.repo, provider)

        self.assertEqual("ready", state.status)
        manifest = json.loads((self.repo / ".kb/vectors/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(VECTOR_CACHE_SCHEMA, manifest["schema"])
        self.assertEqual("fastembed", manifest["provider"])
        self.assertEqual("BAAI/bge-small-zh-v1.5", manifest["model"])
        self.assertEqual(provider.spec.model_hash, manifest["model_hash"])
        self.assertEqual(64, len(manifest["spec_fingerprint"]))
        self.assertEqual(64, len(manifest["config_fingerprint"]))
        self.assertEqual(64, len(manifest["global_fingerprint"]))
        self.assertEqual(64, len(manifest["input_fingerprint"]))
        self.assertEqual(1, manifest["raw_count"])
        self.assertEqual(sum(item["unit_count"] for item in manifest["raws"].values()), manifest["unit_count"])
        raw_info = manifest["raws"][raw_id]
        self.assertEqual({
            "schema", "provider", "model", "model_hash", "dimension", "spec",
            "spec_fingerprint", "config_fingerprint", "global_fingerprint", "input_fingerprint",
            "raw_count", "unit_count", "raws",
        }, set(manifest))
        self.assertEqual({
            "provider", "model", "dimension", "dtype", "normalization", "runtime", "model_hash",
        }, set(manifest["spec"]))
        self.assertEqual({
            "path", "body_hash", "annotation_hash", "raw_fingerprint", "file", "file_hash", "unit_count",
        }, set(raw_info))
        self.assertEqual(71, len(raw_info["body_hash"]))
        self.assertEqual(64, len(raw_info["annotation_hash"]))
        self.assertEqual(64, len(raw_info["raw_fingerprint"]))
        rows = [json.loads(line) for line in (self.repo / ".kb/vectors" / raw_info["file"]).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(rows)
        self.assertTrue(all("text" not in row for row in rows))
        self.assertTrue(all(len(row["vector"]) == 512 for row in rows))
        summary = next(row for row in rows if row["kind"] == "summary")
        body = next(row for row in rows if row["kind"] == "body")
        headline = next(row for row in rows if row["kind"] == "headline")
        common = {"chunk_id", "raw_id", "kind", "vector", "body_hash", "annotation_hash"}
        self.assertEqual(common, set(headline))
        self.assertEqual(common | {"segment_index"}, set(summary))
        self.assertEqual(common | {"section_index", "start", "end"}, set(body))
        self.assertEqual(0, summary["segment_index"])
        self.assertIn("start", body)
        self.assertIn("end", body)

    def test_status_rejects_self_consistent_non_fixed_embedding_specs(self) -> None:
        self.compile_raws([("固定模型契约", "固定模型契约正文" * 10)])
        provider = FakeProvider()
        manifest_path = self.repo / ".kb/vectors/manifest.json"
        cases = [
            ("dtype", "float64"),
            ("normalization", "none"),
            ("runtime", "cuda"),
            ("model_hash", "A" * 64),
            ("model_hash", "abc"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                reindex_vectors(self.repo, provider)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["spec"][field] = value
                if field == "model_hash":
                    manifest["model_hash"] = value
                canonical_spec = json.dumps(
                    manifest["spec"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                manifest["spec_fingerprint"] = hashlib.sha256(canonical_spec.encode("utf-8")).hexdigest()
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")

                self.assertEqual("corrupt", vector_status(self.repo).status)

    def test_status_rejects_extra_manifest_spec_and_raw_info_fields(self) -> None:
        raw_id = self.compile_raws([("精确缓存结构", "精确缓存结构正文" * 10)])[0]
        provider = FakeProvider()
        manifest_path = self.repo / ".kb/vectors/manifest.json"
        mutations = [
            lambda manifest: manifest.__setitem__("unexpected", True),
            lambda manifest: manifest["spec"].__setitem__("unexpected", True),
            lambda manifest: manifest["raws"][raw_id].__setitem__("unexpected", True),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                reindex_vectors(self.repo, provider)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutate(manifest)
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")

                self.assertEqual("corrupt", vector_status(self.repo).status)

    def test_status_rejects_body_snippet_and_unknown_jsonl_fields(self) -> None:
        raw_id = self.compile_raws([("精确行结构", "精确行结构正文" * 10)])[0]
        provider = FakeProvider()
        for field in ("body", "snippet", "unexpected"):
            with self.subTest(field=field):
                reindex_vectors(self.repo, provider)
                self.rewrite_cached_rows(raw_id, lambda rows: rows[0].__setitem__(field, "leak"))

                self.assertEqual("corrupt", vector_status(self.repo).status)

    def test_status_rejects_bool_locator_and_vector_values_equal_to_zero_or_one(self) -> None:
        raw_id = self.compile_raws([(
            "bool 单元校验",
            "bool 单元校验正文需要足够长，以生成可回切的 body 向量单元。" * 8,
        )])[0]
        provider = FakeProvider()
        cases = {
            "summary segment_index false": lambda rows: next(
                row for row in rows if row["kind"] == "summary"
            ).update({"segment_index": False}),
            "body zero locators false": lambda rows: next(
                row for row in rows if row["kind"] == "body"
            ).update({"section_index": False, "start": False}),
            "vector true": lambda rows: rows[0]["vector"].__setitem__(0, True),
        }

        for label, mutate in cases.items():
            with self.subTest(label=label):
                reindex_vectors(self.repo, provider)
                self.assertEqual("ready", vector_status(self.repo).status)
                self.rewrite_cached_rows(raw_id, mutate)
                self.assertEqual("corrupt", vector_status(self.repo).status)

    def test_vector_scalars_reject_strings_and_bytes_but_accept_numpy_floats(self) -> None:
        import numpy as np

        class ByteProvider(FakeProvider):
            def embed_passages(self, texts: list[str]):
                return [[b"1.0", *embedding(1.0)[1:]] for _ in texts]

        class NumpyProvider(FakeProvider):
            def embed_passages(self, texts: list[str]):
                return [[np.float32(value) for value in embedding(1.0)] for _ in texts]

        raw_id = self.compile_raws([("数值标量校验", "数值标量校验正文" * 12)])[0]
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)
        self.rewrite_cached_rows(raw_id, lambda rows: rows[0]["vector"].__setitem__(0, "1.0"))
        self.assertEqual("corrupt", vector_status(self.repo).status)

        with self.assertRaisesRegex(VectorCacheError, "numeric"):
            reindex_vectors(self.repo, ByteProvider())

        self.assertEqual("ready", reindex_vectors(self.repo, NumpyProvider()).status)

    def test_status_rejects_bool_manifest_and_raw_count_fields(self) -> None:
        raw_id = self.compile_raws([("bool 计数校验", "bool 计数校验正文" * 12)])[0]
        provider = FakeProvider()
        cases = {
            "schema": lambda manifest: manifest.update({"schema": True}),
            "raw_count": lambda manifest: manifest.update({"raw_count": True}),
            "unit_count": lambda manifest: manifest.update({"unit_count": True}),
            "raw unit_count": lambda manifest: manifest["raws"][raw_id].update({"unit_count": True}),
        }

        for label, mutate in cases.items():
            with self.subTest(label=label):
                reindex_vectors(self.repo, provider)
                self.assertEqual("ready", vector_status(self.repo).status)
                self.rewrite_cache_manifest(mutate)
                self.assertEqual("corrupt", vector_status(self.repo).status)

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

    def test_status_rejects_body_locator_that_differs_from_deterministic_chunking(self) -> None:
        raw_id = self.compile_raws([("定位校验", "甲" * 600 + "乙" * 50)])[0]
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)

        def alter_locator(rows: list[dict[str, object]]) -> None:
            unit = next(row for row in rows if row.get("kind") == "body")
            unit["end"] = int(unit["end"]) - 1

        self.rewrite_cached_rows(raw_id, alter_locator)

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

    def test_ready_cache_degrades_when_model_download_fails(self) -> None:
        self.compile_raws([("本地模型", "本地模型正文" * 10)])
        reindex_vectors(self.repo, FakeProvider())

        with patch("second_memory.vectors.FastEmbedProvider", side_effect=RuntimeError("model download failed")):
            result = search_vectors(self.repo, "查询")

        self.assertEqual("pending", result.status)
        self.assertIn("model download failed", result.reason)
        self.assertEqual([], result.units)
        self.assertEqual([], result.raws)

    def test_provider_constructor_enforces_reindex_and_search_local_file_boundaries(self) -> None:
        self.compile_raws([("构造边界", "构造边界正文" * 10)])
        offline_destination = self.repo / ".kb/offline-vectors"
        online_destination = self.repo / ".kb/online-vectors"

        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider()) as constructor:
            reindex_vectors(self.repo, offline=True, destination=offline_destination)
        constructor.assert_called_once_with(
            local_files_only=True,
            cache_dir=self.repo / ".kb/models",
        )

        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider()) as constructor:
            reindex_vectors(self.repo, destination=online_destination)
        constructor.assert_called_once_with(
            local_files_only=False,
            cache_dir=self.repo / ".kb/models",
        )

        reindex_vectors(self.repo, FakeProvider())
        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider()) as constructor:
            result = search_vectors(self.repo, "查询")
        constructor.assert_called_once_with(
            local_files_only=False,
            cache_dir=self.repo / ".kb/models",
        )
        self.assertEqual("ready", result.status)

        with patch("second_memory.vectors.FastEmbedProvider", return_value=FakeProvider()) as constructor:
            result = search_vectors(self.repo, "查询", offline=True)
        constructor.assert_called_once_with(
            local_files_only=True,
            cache_dir=self.repo / ".kb/models",
        )
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

        constructor.assert_called_once_with(
            local_files_only=True,
            cache_dir=self.repo / ".kb/models",
        )
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
        self.assertEqual(15, len(result.units))
        self.assertLessEqual(len(result.raws), 5)
        self.assertEqual(sorted((unit.chunk_id for unit in result.units)), [unit.chunk_id for unit in result.units])
        self.assertTrue(all(unit.score >= 0.58 for unit in result.units))
        self.assertNotIn(raw_ids[5], {unit.raw_id for unit in result.units})
        self.assertEqual(len(result.raws), len({item["raw_id"] for item in result.raws}))
        self.assertEqual([item["raw_id"] for item in result.raws], list(dict.fromkeys(unit.raw_id for unit in result.units))[:5])

    def test_search_orders_distinct_scores_before_the_forty_five_unit_scan_limit(self) -> None:
        markers = [f"分数{i:02d}" for i in range(20)]
        self.compile_raws([(marker, marker + "正文内容" * 15) for marker in markers])
        config = load_config(self.repo)
        config["vector_min_score"] = 0.0
        config["vector_unit_limit"] = 60
        config["vector_units_per_raw_limit"] = 60
        config["vector_raw_limit"] = 60
        write_config(self.repo, config)
        scores = {marker: 0.99 - index * 0.04 for index, marker in enumerate(markers)}
        provider = FakeProvider(scores)
        reindex_vectors(self.repo, provider)

        result = search_vectors(self.repo, "排序查询", provider)

        actual_scores = [float(unit.score or 0.0) for unit in result.units]
        self.assertEqual("ready", result.status)
        self.assertEqual(45, len(result.units))
        self.assertEqual(sorted(actual_scores, reverse=True), actual_scores)
        self.assertGreater(actual_scores[0], actual_scores[-1])
        for score in set(actual_scores):
            tied_ids = [unit.chunk_id for unit in result.units if unit.score == score]
            self.assertEqual(sorted(tied_ids), tied_ids)

    def test_ablation_filters_body_before_scan_threshold_and_unit_limits(self) -> None:
        raw_ids = self.compile_raws([
            (f"消融原料{index:02d}", f"消融原料{index:02d}" + "正文" * 180)
            for index in range(27)
        ])
        relevant_raw_id = raw_ids[-1]
        config = load_config(self.repo)
        high_body_count = sum(
            unit.kind == "body"
            for raw_id in raw_ids[:-1]
            for unit in build_vector_units(self.raw_entry(raw_id), config)
        )
        self.assertGreater(high_body_count, int(config["vector_scan_k"]))
        self.assertGreater(high_body_count, int(config["vector_unit_limit"]))
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)

        for raw_id in raw_ids:
            def rescore(rows, *, current_raw_id=raw_id):
                for row in rows:
                    score = 0.9 if current_raw_id != relevant_raw_id and row["kind"] == "body" else 0.1
                    if current_raw_id == relevant_raw_id and row["kind"] == "headline":
                        score = 0.8
                    row["vector"] = embedding(score)

            self.rewrite_cached_rows(raw_id, rescore)

        baseline = search_vectors(self.repo, "消融查询", provider)
        ablated = search_vectors(self.repo, "消融查询", provider, disabled_unit_types={"body"})

        self.assertEqual(15, len(baseline.units))
        self.assertTrue(all(unit.kind == "body" for unit in baseline.units))
        self.assertNotIn(relevant_raw_id, {unit.raw_id for unit in baseline.units})
        self.assertEqual(relevant_raw_id, ablated.units[0].raw_id)
        self.assertEqual("headline", ablated.units[0].kind)
        self.assertIn(relevant_raw_id, {raw["raw_id"] for raw in ablated.raws})

    def test_search_rejects_unknown_ablation_unit_type_before_provider_initialization(self) -> None:
        with patch(
            "second_memory.vectors.FastEmbedProvider",
            side_effect=AssertionError("invalid ablation must fail before provider initialization"),
        ):
            with self.assertRaisesRegex(ValueError, "unknown vector unit type: title"):
                search_vectors(self.repo, "查询", disabled_unit_types={"title"})

    def test_destination_build_is_complete_and_does_not_replace_the_live_cache(self) -> None:
        config = load_config(self.repo)
        config["vector_enabled"] = False
        write_config(self.repo, config)
        self.compile_raws([("目标目录", "目标目录正文" * 12)])
        config = load_config(self.repo)
        config["vector_enabled"] = True
        write_config(self.repo, config)
        provider = FakeProvider()
        destination = self.repo / ".kb/transaction/vectors.next"

        state = reindex_vectors(self.repo, provider, offline=True, destination=destination)

        self.assertEqual("ready", state.status)
        self.assertTrue((destination / "manifest.json").is_file())
        self.assertFalse((self.repo / ".kb/vectors").exists())
        self.assertEqual("ready", vector_status(self.repo, destination=destination).status)

    def test_search_deduplicates_normalized_text_only_within_the_same_raw(self) -> None:
        raw_ids = self.compile_raws([
            ("重复甲", "重复甲正文内容" * 20),
            ("重复乙", "重复乙正文内容" * 20),
        ], duplicate_summary=True)
        config = load_config(self.repo)
        config.update({
            "vector_min_score": 0.0,
            "vector_scan_k": 45,
            "vector_unit_limit": 15,
            "vector_units_per_raw_limit": 15,
            "vector_raw_limit": 15,
        })
        write_config(self.repo, config)
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)

        result = search_vectors(self.repo, "重复", provider)

        for raw_id in raw_ids:
            duplicate_kinds = [
                unit.kind
                for unit in result.units
                if unit.raw_id == raw_id and unit.text == self.raw_entry(raw_id).annotations["summary"]
            ]
            self.assertEqual(["summary"], duplicate_kinds)

    def test_search_applies_score_boundary_and_does_not_backfill_below_threshold(self) -> None:
        raw_ids = self.compile_raws([
            ("边界保留", "边界保留正文内容" * 180),
            ("边界过滤", "边界过滤正文内容" * 180),
        ])
        config = load_config(self.repo)
        config.update({
            "vector_min_score": 0.58,
            "vector_scan_k": 45,
            "vector_unit_limit": 15,
            "vector_units_per_raw_limit": 3,
            "vector_raw_limit": 15,
        })
        write_config(self.repo, config)
        provider = FakeProvider({"边界保留": 0.58, "边界过滤": 0.579})
        reindex_vectors(self.repo, provider)

        result = search_vectors(self.repo, "边界", provider)

        self.assertEqual({raw_ids[0]}, {unit.raw_id for unit in result.units})
        self.assertEqual(3, len(result.units))
        self.assertTrue(all(float(unit.score or 0.0) >= 0.58 for unit in result.units))

    def test_search_returns_top_fifteen_with_at_most_three_units_per_raw(self) -> None:
        self.compile_raws([
            (f"日常输入{index:02d}", f"日常输入{index:02d}" + "正文内容" * 180)
            for index in range(6)
        ])
        config = load_config(self.repo)
        config.update({
            "vector_min_score": 0.58,
            "vector_scan_k": 45,
            "vector_unit_limit": 15,
            "vector_units_per_raw_limit": 3,
            "vector_raw_limit": 15,
        })
        write_config(self.repo, config)
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)

        result = search_vectors(self.repo, "日常问题", provider)

        counts: dict[str, int] = {}
        for unit in result.units:
            counts[unit.raw_id] = counts.get(unit.raw_id, 0) + 1
        self.assertEqual(15, len(result.units))
        self.assertTrue(all(count <= 3 for count in counts.values()))
        self.assertGreaterEqual(len(counts), 5)

    def test_query_time_limit_changes_do_not_make_the_vector_cache_stale(self) -> None:
        self.compile_raws([("查询配置", "查询配置正文内容" * 20)])
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)
        config = load_config(self.repo)
        config.update({
            "vector_min_score": 0.61,
            "vector_scan_k": 60,
            "vector_unit_limit": 20,
            "vector_units_per_raw_limit": 4,
            "vector_raw_limit": 7,
        })
        write_config(self.repo, config)

        self.assertEqual("ready", vector_status(self.repo).status)

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

    def test_destination_delta_never_reuses_cache_with_an_invalid_global_fingerprint(self) -> None:
        raw_ids = self.compile_raws([("版本甲", "版本甲正文" * 12), ("版本乙", "版本乙正文" * 12)])
        reindex_vectors(self.repo, FakeProvider())
        manifest_path = self.repo / ".kb/vectors/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["global_fingerprint"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(VectorCacheError, "cannot build a complete destination cache"):
            reindex_vectors(
                self.repo,
                FakeProvider(),
                raw_ids=[raw_ids[0]],
                destination=self.repo / ".kb/transaction/vectors.next",
            )

    def test_default_reindex_only_embeds_raws_missing_from_compatible_cache(self) -> None:
        first_raw = self.compile_raws([("增量补齐甲", "增量补齐甲正文" * 12)])[0]
        reindex_vectors(self.repo, FakeProvider())
        with patch("second_memory.compiler.reindex_vectors", side_effect=VectorCacheError("defer delta")):
            second_raw = self.compile_raws([("增量补齐乙", "增量补齐乙正文" * 12)])[0]
        provider = FakeProvider()

        plan = plan_vector_update(self.repo, provider=provider)
        state = reindex_vectors(self.repo, provider)

        self.assertEqual("incremental", plan.mode)
        self.assertEqual([second_raw], plan.raw_ids)
        self.assertEqual("ready", state.status)
        self.assertEqual(1, len(provider.passage_inputs))
        embedded = provider.passage_inputs[0]
        self.assertTrue(embedded)
        self.assertTrue(all("增量补齐甲" not in text for text in embedded))
        self.assertTrue(any("增量补齐乙" in text for text in embedded))
        self.assertEqual({first_raw, second_raw}, set(state.manifest["raws"]))

    def test_embedding_fingerprint_change_requires_full_reindex(self) -> None:
        raw_ids = self.compile_raws([
            ("模型变化甲", "模型变化甲正文" * 12),
            ("模型变化乙", "模型变化乙正文" * 12),
        ])
        reindex_vectors(self.repo, FakeProvider())
        provider = FakeProvider(model_seed=b"replacement-model")

        plan = plan_vector_update(self.repo, provider=provider)
        state = reindex_vectors(self.repo, provider)

        self.assertEqual("full", plan.mode)
        self.assertEqual(sorted(raw_ids), plan.raw_ids)
        self.assertEqual("ready", state.status)
        self.assertEqual(1, len(provider.passage_inputs))
        self.assertTrue(any("模型变化甲" in text for text in provider.passage_inputs[0]))
        self.assertTrue(any("模型变化乙" in text for text in provider.passage_inputs[0]))

    def test_force_reindex_reembeds_all_raws_without_global_fingerprint_change(self) -> None:
        self.compile_raws([
            ("强制重建甲", "强制重建甲正文" * 12),
            ("强制重建乙", "强制重建乙正文" * 12),
        ])
        reindex_vectors(self.repo, FakeProvider())
        provider = FakeProvider()

        state = reindex_vectors(self.repo, provider, force=True)

        self.assertEqual("ready", state.status)
        self.assertEqual(1, len(provider.passage_inputs))
        self.assertTrue(any("强制重建甲" in text for text in provider.passage_inputs[0]))
        self.assertTrue(any("强制重建乙" in text for text in provider.passage_inputs[0]))

    def test_summary_change_only_invalidates_its_vector_entry_not_the_compile_layer(self) -> None:
        raw_id = self.compile_raws([("主清单", "主清单正文" * 10)])[0]
        entry = self.raw_entry(raw_id)
        info = load_manifest(self.repo)["raw_hashes"][raw_id]
        self.assertEqual({"path", "body_hash"}, set(info))
        reindex_vectors(self.repo, FakeProvider())
        raw_path = entry.path
        os.chmod(raw_path, 0o644)
        meta, body = frontmatter.read_document(raw_path)
        meta["summary"] = str(meta["summary"])[:-1] + "改"
        raw_path.write_text(frontmatter.dump_document(meta, body), encoding="utf-8")
        self.assertEqual([], manifest_drift(self.repo))
        vector_plan = plan_vector_update(self.repo, provider=FakeProvider())
        self.assertEqual("incremental", vector_plan.mode)
        self.assertEqual([raw_id], vector_plan.raw_ids)

        raw_path.write_text(frontmatter.dump_document({**meta, "summary": entry.annotations["summary"]}, body + "新增"), encoding="utf-8")
        self.assertEqual([f"raw:{raw_id}:body"], manifest_drift(self.repo))

    def test_status_requires_main_manifest_raw_path_to_match_the_actual_source(self) -> None:
        raw_ids = self.compile_raws([
            ("路径校验甲", "路径校验甲正文" * 10),
            ("路径校验乙", "路径校验乙正文" * 10),
        ])
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)
        manifest_path = self.repo / ".kb/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        original_path = manifest["raw_hashes"][raw_ids[0]]["path"]
        wrong_paths = [
            "raw/2099/01/missing.md",
            manifest["raw_hashes"][raw_ids[1]]["path"],
        ]
        for wrong_path in wrong_paths:
            with self.subTest(wrong_path=wrong_path):
                manifest["raw_hashes"][raw_ids[0]]["path"] = wrong_path
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
                self.assertEqual("stale", vector_status(self.repo).status)
        manifest["raw_hashes"][raw_ids[0]]["path"] = original_path
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual("ready", vector_status(self.repo).status)

    def test_zero_raw_limit_returns_no_raw_aggregates(self) -> None:
        config = load_config(self.repo)
        config["vector_raw_limit"] = 0
        write_config(self.repo, config)
        self.compile_raws([("零原料上限", "零原料上限正文" * 10)])
        provider = FakeProvider()
        reindex_vectors(self.repo, provider)

        result = search_vectors(self.repo, "零原料上限", provider)

        self.assertEqual("ready", result.status)
        self.assertTrue(result.units)
        self.assertEqual([], result.raws)

    def test_default_gitignore_excludes_vector_and_evaluation_caches(self) -> None:
        self.assertIn(".kb/vectors/\n", DEFAULT_GITIGNORE)
        self.assertIn(".kb/models/\n", DEFAULT_GITIGNORE)
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
        self.assertIn(".kb/models/\n", content)
        self.assertIn(".kb/eval/\n", content)


if __name__ == "__main__":
    unittest.main()
