from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from second_memory.cli import app
from second_memory.compiler import initialize
from second_memory.vectors import VectorCacheError, VectorCacheState, VectorSearchResult, VectorUnit


class VectorCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="second-memory-vector-cli-")
        self.repo = Path(self.temporary.name) / "knowledge-base"
        initialize(self.repo, "agent", "test", "plain")
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reindex_commands_emit_envelopes_and_forward_offline_mode(self) -> None:
        state = VectorCacheState("ready", "vector cache is ready", {
            "provider": "fastembed", "model": "test-model", "dimension": 512,
            "raw_count": 1, "unit_count": 2,
        })
        with patch("second_memory.cli.reindex_vectors", create=True, return_value=state) as reindex:
            online = self.runner.invoke(app, ["vectors", "reindex", "--repo", str(self.repo), "--json"])
            offline = self.runner.invoke(app, ["vectors", "reindex", "--offline", "--repo", str(self.repo), "--json"])
            forced = self.runner.invoke(app, ["vectors", "reindex", "--force", "--repo", str(self.repo), "--json"])

        self.assertEqual(0, online.exit_code, online.output)
        self.assertEqual(0, offline.exit_code, offline.output)
        self.assertEqual(0, forced.exit_code, forced.output)
        self.assertEqual("vectors reindex", json.loads(online.stdout)["command"])
        self.assertTrue(json.loads(online.stdout)["ok"])
        self.assertEqual([False, True, False], [call.kwargs["offline"] for call in reindex.call_args_list])
        self.assertEqual([False, False, True], [call.kwargs["force"] for call in reindex.call_args_list])

    def test_vector_search_is_local_only_and_returns_uniform_error_envelope(self) -> None:
        result = VectorSearchResult("pending", "local model is unavailable", [], [])
        with patch("second_memory.cli.search_vectors", create=True, return_value=result) as search:
            response = self.runner.invoke(app, ["vectors", "search", "--query", "查询", "--repo", str(self.repo), "--json"])

        self.assertEqual(0, response.exit_code, response.output)
        payload = json.loads(response.stdout)
        self.assertEqual({"ok", "command", "data", "error"}, set(payload))
        self.assertEqual("vectors search", payload["command"])
        self.assertEqual("pending", payload["data"]["status"])
        search.assert_called_once_with(self.repo.resolve(), "查询")

    def test_vector_commands_return_uniform_error_envelopes(self) -> None:
        with patch("second_memory.cli.reindex_vectors", create=True, side_effect=VectorCacheError("cache is invalid")):
            response = self.runner.invoke(app, ["vectors", "reindex", "--repo", str(self.repo), "--json"])

        self.assertEqual(1, response.exit_code)
        payload = json.loads(response.stderr)
        self.assertEqual(False, payload["ok"])
        self.assertEqual("vectors reindex", payload["command"])
        self.assertEqual("error", payload["error"]["code"])

    def test_status_reports_cache_contract_without_constructing_provider(self) -> None:
        state = VectorCacheState("stale", "Raw input changed", {
            "provider": "fastembed", "model": "BAAI/bge-small-zh-v1.5", "dimension": 512,
            "raw_count": 3, "unit_count": 12,
        })
        with patch("second_memory.cli.vector_status", create=True, return_value=state), patch(
            "second_memory.vectors.FastEmbedProvider", side_effect=AssertionError("status must not load provider")
        ):
            response = self.runner.invoke(app, ["status", "--repo", str(self.repo), "--json"])

        self.assertEqual(0, response.exit_code, response.output)
        vectors = json.loads(response.stdout)["data"]["vectors"]
        self.assertEqual({
            "cache_state": "stale", "provider": "fastembed", "model": "BAAI/bge-small-zh-v1.5",
            "dimension": 512, "raw_count": 3, "unit_count": 12, "reason": "Raw input changed",
        }, vectors)
