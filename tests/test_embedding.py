from __future__ import annotations

import hashlib
import math
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from second_memory import frontmatter
from second_memory.config import KB_VERSION, default_config, load_config
from second_memory.embedding import EmbeddingError, FastEmbedProvider


def vector(*values: float) -> list[float]:
    return [*values, *([0.0] * (512 - len(values)))]


class FakeBackend:
    def __init__(self, passage: list[float], query: list[float], *, model_dir: Path) -> None:
        self.passage = passage
        self.query = query
        self.model = types.SimpleNamespace(_model_dir=str(model_dir))
        self.passage_inputs: list[list[str]] = []
        self.query_inputs: list[list[str]] = []

    def passage_embed(self, texts: list[str]):
        self.passage_inputs.append(list(texts))
        return iter([self.passage for _ in texts])

    def query_embed(self, queries: list[str]):
        self.query_inputs.append(list(queries))
        return iter([self.query for _ in queries])


class EmbeddingProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="second-memory-embedding-")
        self.model_path = Path(self.temporary.name) / "model_optimized.onnx"
        self.model_path.write_bytes(b"onnx-model")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def provider(self, passage: list[float], query: list[float]) -> tuple[FastEmbedProvider, FakeBackend]:
        backend = FakeBackend(passage, query, model_dir=self.model_path.parent)
        return FastEmbedProvider(backend=backend), backend

    def test_spec_reports_the_pinned_cpu_bge_contract_and_resolved_model_hash(self) -> None:
        provider, _ = self.provider(vector(3.0, 4.0), vector(3.0, 4.0))

        self.assertEqual("fastembed", provider.spec.provider)
        self.assertEqual("BAAI/bge-small-zh-v1.5", provider.spec.model)
        self.assertEqual(512, provider.spec.dimension)
        self.assertEqual("float32", provider.spec.dtype)
        self.assertEqual("l2", provider.spec.normalization)
        self.assertEqual("onnxruntime-cpu", provider.spec.runtime)
        self.assertEqual(hashlib.sha256(b"onnx-model").hexdigest(), provider.spec.model_sha256)

    def test_passage_and_query_embeddings_are_normalized_finite_float32_vectors(self) -> None:
        provider, backend = self.provider(vector(3.0, 4.0), vector(5.0, 12.0))

        passages = provider.embed_passages(["原料甲", "原料乙"])
        query = provider.embed_query("查询")

        self.assertEqual([["原料甲", "原料乙"]], backend.passage_inputs)
        self.assertEqual([["查询"]], backend.query_inputs)
        self.assertEqual(2, len(passages))
        self.assertEqual(512, len(query))
        self.assertEqual(0.6000000238418579, passages[0][0])
        self.assertEqual(0.800000011920929, passages[0][1])
        self.assertEqual(0.38461539149284363, query[0])
        self.assertEqual(0.9230769276618958, query[1])
        for item in [*passages, query]:
            self.assertTrue(all(math.isfinite(value) for value in item))
            self.assertAlmostEqual(1.0, math.sqrt(sum(value * value for value in item)))
            self.assertEqual(item[0], struct.unpack("!f", struct.pack("!f", item[0]))[0])

    def test_rejects_wrong_dimension_non_finite_and_zero_norm_vectors(self) -> None:
        cases = [
            (vector(1.0)[:-1], "dimension"),
            (vector(math.nan), "finite"),
            (vector(math.inf), "finite"),
            (vector(), "non-zero"),
        ]
        for invalid, reason in cases:
            with self.subTest(reason=reason):
                provider, _ = self.provider(invalid, vector(3.0, 4.0))
                with self.assertRaisesRegex(EmbeddingError, reason):
                    provider.embed_passages(["原料"])

    def test_rejects_a_model_path_override_and_missing_resolved_model_file(self) -> None:
        backend = FakeBackend(vector(3.0, 4.0), vector(3.0, 4.0), model_dir=self.model_path.parent)
        with self.assertRaises(TypeError):
            FastEmbedProvider(backend=backend, model_path=self.model_path)

        missing_backend = FakeBackend(vector(3.0, 4.0), vector(3.0, 4.0), model_dir=self.model_path.parent / "missing")
        with self.assertRaisesRegex(EmbeddingError, "missing"):
            FastEmbedProvider(backend=missing_backend)

    def test_fastembed_forces_cpu_local_only_and_uses_passage_and_query_apis(self) -> None:
        created: list[FakeTextEmbedding] = []
        model_dir = self.model_path.parent

        class FakeTextEmbedding(FakeBackend):
            def __init__(self, model_name: str, **kwargs: object) -> None:
                super().__init__(vector(3.0, 4.0), vector(3.0, 4.0), model_dir=model_dir)
                self.model_name = model_name
                self.kwargs = kwargs
                created.append(self)

        fake_module = types.SimpleNamespace(TextEmbedding=FakeTextEmbedding)
        with patch.dict(sys.modules, {"fastembed": fake_module}):
            provider = FastEmbedProvider(local_files_only=True)

        self.assertEqual(1, len(created))
        backend = created[0]
        self.assertEqual("BAAI/bge-small-zh-v1.5", backend.model_name)
        self.assertEqual(["CPUExecutionProvider"], backend.kwargs["providers"])
        self.assertTrue(backend.kwargs["local_files_only"])
        provider.embed_passages(["原料"])
        provider.embed_query("查询")
        self.assertEqual([["原料"]], backend.passage_inputs)
        self.assertEqual([["查询"]], backend.query_inputs)


class VectorConfigurationTest(unittest.TestCase):
    vector_defaults = {
        "vector_enabled": True,
        "vector_provider": "fastembed",
        "vector_model": "BAAI/bge-small-zh-v1.5",
        "vector_dimension": 512,
        "vector_min_score": 0.35,
        "vector_scan_k": 30,
        "vector_unit_limit": 10,
        "vector_raw_limit": 5,
        "vector_chunk_target": 300,
        "vector_chunk_min": 50,
        "vector_chunk_max": 300,
        "vector_chunk_overlap": 0.15,
    }

    def test_default_config_contains_the_v25_vector_defaults(self) -> None:
        config = default_config(Path("/tmp/knowledge-base"), "shared", None, "plain")

        self.assertEqual("2.5.0", KB_VERSION)
        self.assertEqual(self.vector_defaults, {key: config[key] for key in self.vector_defaults})

    def test_loading_an_old_config_adds_missing_vector_defaults_without_overriding_user_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="second-memory-config-") as temporary:
            repo = Path(temporary)
            path = repo / ".kb" / "config.yaml"
            path.parent.mkdir()
            path.write_text(frontmatter.dump_mapping({"schema": 2, "vector_enabled": False, "vector_scan_k": 99}), encoding="utf-8")

            config = load_config(repo)

        self.assertFalse(config["vector_enabled"])
        self.assertEqual(99, config["vector_scan_k"])
        self.assertEqual("BAAI/bge-small-zh-v1.5", config["vector_model"])
