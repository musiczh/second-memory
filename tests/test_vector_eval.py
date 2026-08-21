from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from second_memory.cli import app
from second_memory.vector_eval import (
    GoldQuery,
    GoldValidationError,
    VectorEvaluationError,
    evaluate_gold,
    evaluate_repository,
    load_gold,
    metrics_for_ranking,
    rank_vector_units,
    union_rankings,
)
from second_memory.vectors import VectorCacheState, VectorUnit


class GoldJsonlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="second-memory-vector-eval-")
        self.gold_path = Path(self.temporary.name) / "vector-gold.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_rows(self, rows: list[object]) -> None:
        self.gold_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_loads_required_fields_and_optional_expected_chunk_ids(self) -> None:
        self.write_rows([
            {
                "query": "如何改善睡眠拖延？",
                "relevant_raw_ids": ["raw-20260820-1200-0123abcd", "raw-20260820-1201-89abcdef"],
                "expected_units": ["chunk-headline", "chunk-body"],
            }
        ])

        rows = load_gold(self.gold_path)

        self.assertEqual("如何改善睡眠拖延？", rows[0].query)
        self.assertEqual(
            ("raw-20260820-1200-0123abcd", "raw-20260820-1201-89abcdef"),
            rows[0].relevant_raw_ids,
        )
        self.assertEqual(("chunk-headline", "chunk-body"), rows[0].expected_units)

    def test_rejects_empty_file_empty_or_duplicate_query_and_invalid_raw_id(self) -> None:
        cases = [
            ([], "gold file is empty"),
            ([{"query": " ", "relevant_raw_ids": ["raw-20260820-1200-0123abcd"]}], "query must be non-empty"),
            (
                [
                    {"query": "重复", "relevant_raw_ids": ["raw-20260820-1200-0123abcd"]},
                    {"query": "重复", "relevant_raw_ids": ["raw-20260820-1201-89abcdef"]},
                ],
                "duplicate query",
            ),
            ([{"query": "非法 ID", "relevant_raw_ids": ["not-a-raw-id"]}], "invalid raw ID"),
        ]
        for rows, message in cases:
            with self.subTest(message=message):
                self.write_rows(rows)
                with self.assertRaisesRegex(GoldValidationError, message):
                    load_gold(self.gold_path)

    def test_rejects_wrong_field_types_and_empty_relevant_raw_ids(self) -> None:
        cases = [
            ({"query": 1, "relevant_raw_ids": ["raw-20260820-1200-0123abcd"]}, "query must be a string"),
            ({"query": "类型", "relevant_raw_ids": "raw-20260820-1200-0123abcd"}, "relevant_raw_ids must be an array"),
            ({"query": "空相关项", "relevant_raw_ids": []}, "relevant_raw_ids must not be empty"),
            (
                {
                    "query": "错误相关项",
                    "relevant_raw_ids": ["raw-20260820-1200-0123abcd", 2],
                },
                "relevant_raw_ids must contain strings",
            ),
            (
                {
                    "query": "错误单元",
                    "relevant_raw_ids": ["raw-20260820-1200-0123abcd"],
                    "expected_units": "chunk-headline",
                },
                "expected_units must be an array",
            ),
            (
                {
                    "query": "错误单元项",
                    "relevant_raw_ids": ["raw-20260820-1200-0123abcd"],
                    "expected_units": [1],
                },
                "expected_units must contain non-empty strings",
            ),
        ]
        for row, message in cases:
            with self.subTest(message=message):
                self.write_rows([row])
                with self.assertRaisesRegex(GoldValidationError, message):
                    load_gold(self.gold_path)


class RankingMetricsTest(unittest.TestCase):
    def test_multiple_relevant_items_use_binary_macro_metrics_at_five(self) -> None:
        metrics = metrics_for_ranking(
            ("raw-20260820-1200-0123abcd", "raw-20260820-1201-89abcdef"),
            (
                "raw-20260820-1202-11111111",
                "raw-20260820-1201-89abcdef",
                "raw-20260820-1200-0123abcd",
            ),
        )

        self.assertEqual(
            {
                "recall_at_5": 1.0,
                "mrr": 0.5,
                "ndcg_at_5": 0.693426,
                "noise_rate": 0.333333,
                "zero_result_rate": 0.0,
            },
            metrics,
        )

    def test_empty_ranking_has_zero_metrics_and_one_zero_result_rate(self) -> None:
        self.assertEqual(
            {
                "recall_at_5": 0.0,
                "mrr": 0.0,
                "ndcg_at_5": 0.0,
                "noise_rate": 0.0,
                "zero_result_rate": 1.0,
            },
            metrics_for_ranking(("raw-20260820-1200-0123abcd",), ()),
        )

    def test_mrr_uses_the_full_ranking_while_at_five_metrics_stay_bounded(self) -> None:
        relevant = "raw-20260820-1200-0123abcd"
        ranking = [f"raw-20260820-120{index}-{index:08x}" for index in range(1, 6)] + [relevant]

        self.assertEqual(
            {
                "recall_at_5": 0.0,
                "mrr": 0.166667,
                "ndcg_at_5": 0.0,
                "noise_rate": 1.0,
                "zero_result_rate": 0.0,
            },
            metrics_for_ranking((relevant,), ranking),
        )

    def test_union_preserves_all_keyword_order_then_appends_unseen_vector_raws(self) -> None:
        self.assertEqual(
            ["raw-k2", "raw-k1", "raw-v1", "raw-v2"],
            union_rankings(["raw-k2", "raw-k1"], ["raw-k1", "raw-v1", "raw-v2"]),
        )

    def test_vector_ablation_filters_units_without_mutation_and_ties_are_deterministic(self) -> None:
        units = [
            VectorUnit("chunk-z", "raw-body", "body", score=0.8),
            VectorUnit("chunk-b", "raw-summary", "summary", score=0.9),
            VectorUnit("chunk-a", "raw-headline", "headline", score=0.9),
            VectorUnit("chunk-c", "raw-headline", "body", score=0.7),
        ]

        ranking = rank_vector_units(units, disabled_unit_types={"summary"})

        self.assertEqual(["raw-headline", "raw-body"], ranking)
        self.assertEqual(["body", "summary", "headline", "body"], [unit.kind for unit in units])

    def test_vector_ablation_rejects_unknown_unit_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown vector unit type"):
            rank_vector_units([], disabled_unit_types={"title"})


class EvaluationReportTest(unittest.TestCase):
    def test_outputs_per_query_rankings_expected_unit_evidence_and_macro_summary(self) -> None:
        gold = [
            load_gold_row(
                "睡眠",
                ["raw-20260820-1200-0123abcd", "raw-20260820-1201-89abcdef"],
                ["chunk-b", "chunk-missing"],
            ),
            load_gold_row("专注", ["raw-20260820-1203-33333333"]),
        ]
        results = {
            "睡眠": level1_result(
                keyword_sources=[
                    ["raw-20260820-1200-0123abcd", "raw-20260820-1202-11111111"],
                    ["raw-20260820-1201-89abcdef"],
                ],
                units=[
                    unit("chunk-a", "raw-20260820-1200-0123abcd", "body", 0.7),
                    unit("chunk-c", "raw-20260820-1202-11111111", "summary", 0.8),
                    unit("chunk-b", "raw-20260820-1201-89abcdef", "headline", 0.9),
                ],
            ),
            "专注": level1_result(
                keyword_sources=[],
                units=[unit("chunk-d", "raw-20260820-1203-33333333", "body", 0.9)],
            ),
        }

        report = evaluate_gold(
            gold,
            lambda query: results[query],
            disabled_unit_types={"body"},
        )

        first = report["queries"][0]
        self.assertEqual(
            {
                "keyword": [
                    "raw-20260820-1200-0123abcd",
                    "raw-20260820-1202-11111111",
                    "raw-20260820-1201-89abcdef",
                ],
                "vector": ["raw-20260820-1201-89abcdef", "raw-20260820-1202-11111111"],
                "union": [
                    "raw-20260820-1200-0123abcd",
                    "raw-20260820-1202-11111111",
                    "raw-20260820-1201-89abcdef",
                ],
            },
            first["rankings"],
        )
        self.assertEqual(
            {
                "expected": ["chunk-b", "chunk-missing"],
                "matched": ["chunk-b"],
                "missing": ["chunk-missing"],
            },
            first["expected_unit_evidence"],
        )
        self.assertEqual(["chunk-b", "chunk-c"], [row["chunk_id"] for row in first["vector_units"]])
        self.assertEqual(
            {
                "recall_at_5": 0.5,
                "mrr": 0.5,
                "ndcg_at_5": 0.45986,
                "noise_rate": 0.166667,
                "zero_result_rate": 0.5,
            },
            report["summary"]["keyword"],
        )
        self.assertEqual(
            {
                "recall_at_5": 0.25,
                "mrr": 0.5,
                "ndcg_at_5": 0.306574,
                "noise_rate": 0.25,
                "zero_result_rate": 0.5,
            },
            report["summary"]["vector"],
        )
        self.assertEqual(report["summary"]["keyword"], report["summary"]["union"])
        self.assertEqual(["body"], report["ablation"]["disabled_unit_types"])

    def test_repository_evaluation_fails_before_search_when_cache_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory(prefix="second-memory-vector-eval-state-") as temporary:
            path = Path(temporary) / "gold.jsonl"
            path.write_text(
                json.dumps({"query": "睡眠", "relevant_raw_ids": ["raw-20260820-1200-0123abcd"]}) + "\n",
                encoding="utf-8",
            )
            with patch(
                "second_memory.vector_eval.vector_status",
                return_value=VectorCacheState("stale", "Raw input changed"),
            ), patch("second_memory.vector_eval.search_level1", side_effect=AssertionError("search must not run")):
                with self.assertRaisesRegex(VectorEvaluationError, "vector cache is not ready: stale: Raw input changed"):
                    evaluate_repository(Path(temporary), path)


class EvaluationCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="second-memory-vector-eval-cli-")
        self.repo = Path(self.temporary.name) / "knowledge-base"
        from second_memory.compiler import initialize

        initialize(self.repo, "agent", "test", "plain")
        self.gold = self.repo / ".kb/eval/vector-gold.jsonl"
        self.gold.parent.mkdir(parents=True)
        self.gold.write_text(
            json.dumps({"query": "睡眠", "relevant_raw_ids": ["raw-20260820-1200-0123abcd"]}) + "\n",
            encoding="utf-8",
        )
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cli_evaluates_read_only_and_never_reindexes_or_constructs_a_provider(self) -> None:
        state = VectorCacheState(
            "ready",
            "vector cache is ready",
            {"raws": {"raw-20260820-1200-0123abcd": {}}, "raw_count": 1, "unit_count": 1},
        )
        before = tree_bytes(self.repo)
        with patch("second_memory.vector_eval.vector_status", return_value=state), patch(
            "second_memory.vector_eval.search_level1",
            return_value=level1_result(
                keyword_sources=[["raw-20260820-1200-0123abcd"]],
                units=[unit("chunk-headline", "raw-20260820-1200-0123abcd", "headline", 0.9)],
            ),
        ), patch("second_memory.cli.reindex_vectors", side_effect=AssertionError("evaluate must not reindex")), patch(
            "second_memory.vectors.FastEmbedProvider",
            side_effect=AssertionError("the mocked online search boundary must not construct a provider"),
        ):
            response = self.runner.invoke(
                app,
                [
                    "vectors",
                    "evaluate",
                    "--gold",
                    ".kb/eval/vector-gold.jsonl",
                    "--disable-body",
                    "--repo",
                    str(self.repo),
                    "--json",
                ],
            )

        self.assertEqual(0, response.exit_code, response.output)
        payload = json.loads(response.stdout)
        self.assertEqual("vectors evaluate", payload["command"])
        self.assertEqual(1.0, payload["data"]["summary"]["union"]["recall_at_5"])
        self.assertEqual(before, tree_bytes(self.repo))

    def test_cli_reports_non_ready_cache_as_an_error_without_searching(self) -> None:
        with patch(
            "second_memory.vector_eval.vector_status",
            return_value=VectorCacheState("missing", "vector manifest is missing"),
        ), patch("second_memory.vector_eval.search_level1", side_effect=AssertionError("search must not run")):
            response = self.runner.invoke(
                app,
                ["vectors", "evaluate", "--gold", str(self.gold), "--repo", str(self.repo), "--json"],
            )

        self.assertEqual(1, response.exit_code)
        payload = json.loads(response.stderr)
        self.assertIn("vector cache is not ready: missing", payload["error"]["message"])

    def test_cli_reports_malformed_gold_as_validation_error(self) -> None:
        self.gold.write_text("{}\n", encoding="utf-8")

        response = self.runner.invoke(
            app,
            ["vectors", "evaluate", "--gold", str(self.gold), "--repo", str(self.repo), "--json"],
        )

        self.assertEqual(1, response.exit_code)
        self.assertEqual("validation_error", json.loads(response.stderr)["error"]["code"])


def load_gold_row(
    query: str,
    relevant_raw_ids: list[str],
    expected_units: list[str] | None = None,
) -> GoldQuery:
    return GoldQuery(query, tuple(relevant_raw_ids), tuple(expected_units or []))


def unit(chunk_id: str, raw_id: str, kind: str, score: float) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "raw_id": raw_id,
        "kind": kind,
        "segment_index": None,
        "section_index": None,
        "start": None,
        "end": None,
        "score": score,
        "snippet": chunk_id,
    }


def level1_result(*, keyword_sources: list[list[str]], units: list[dict[str, object]]) -> dict[str, object]:
    return {
        "query": "test",
        "candidates": [{"sources": sources} for sources in keyword_sources],
        "hits": [],
        "supplemental_raw": {
            "status": "ready",
            "reason": "vector cache is ready",
            "units": units,
            "raws": [],
        },
    }


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }

if __name__ == "__main__":
    unittest.main()
