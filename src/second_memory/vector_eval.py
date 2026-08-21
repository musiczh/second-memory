from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import SecondMemoryError, ValidationError
from .retriever import search_level1
from .vectors import VectorUnit, vector_status


_RAW_ID = re.compile(r"raw-\d{8}-\d{4}-[0-9a-f]{8}")


class GoldValidationError(ValidationError):
    """Raised when a vector evaluation gold file is malformed."""


class VectorEvaluationError(SecondMemoryError):
    """Raised when read-only evaluation cannot use the current vector cache."""

    code = "vector_evaluation_error"


@dataclass(frozen=True)
class GoldQuery:
    query: str
    relevant_raw_ids: tuple[str, ...]
    expected_units: tuple[str, ...] = ()


def load_gold(path: Path) -> list[GoldQuery]:
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise GoldValidationError(f"cannot read gold file: {path}: {error}") from error
    if not any(line.strip() for line in lines):
        raise GoldValidationError("gold file is empty")

    rows: list[GoldQuery] = []
    queries: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise GoldValidationError(f"gold line {line_number} is invalid JSON") from error
        if not isinstance(value, dict):
            raise GoldValidationError(f"gold line {line_number} must be an object")
        row = _gold_query(value, line_number)
        if row.query in queries:
            raise GoldValidationError(f"gold line {line_number} has a duplicate query: {row.query}")
        queries.add(row.query)
        rows.append(row)
    if not rows:
        raise GoldValidationError("gold file is empty")
    return rows


def _gold_query(value: dict[str, Any], line_number: int) -> GoldQuery:
    query = value.get("query")
    if not isinstance(query, str):
        raise GoldValidationError(f"gold line {line_number} query must be a string")
    query = query.strip()
    if not query:
        raise GoldValidationError(f"gold line {line_number} query must be non-empty")

    relevant = value.get("relevant_raw_ids")
    if not isinstance(relevant, list):
        raise GoldValidationError(f"gold line {line_number} relevant_raw_ids must be an array")
    if not relevant:
        raise GoldValidationError(f"gold line {line_number} relevant_raw_ids must not be empty")
    if any(not isinstance(raw_id, str) for raw_id in relevant):
        raise GoldValidationError(f"gold line {line_number} relevant_raw_ids must contain strings")
    if any(_RAW_ID.fullmatch(raw_id) is None for raw_id in relevant):
        raise GoldValidationError(f"gold line {line_number} contains an invalid raw ID")
    if len(set(relevant)) != len(relevant):
        raise GoldValidationError(f"gold line {line_number} relevant_raw_ids must not contain duplicates")

    expected = value.get("expected_units", [])
    if not isinstance(expected, list):
        raise GoldValidationError(f"gold line {line_number} expected_units must be an array")
    if any(not isinstance(unit, str) or not unit.strip() for unit in expected):
        raise GoldValidationError(f"gold line {line_number} expected_units must contain non-empty strings")
    if len(set(expected)) != len(expected):
        raise GoldValidationError(f"gold line {line_number} expected_units must not contain duplicates")
    return GoldQuery(query, tuple(relevant), tuple(expected))


def metrics_for_ranking(
    relevant_raw_ids: tuple[str, ...] | list[str],
    ranked_raw_ids: tuple[str, ...] | list[str],
) -> dict[str, float]:
    raw_metrics = _raw_metrics(relevant_raw_ids, ranked_raw_ids)
    return {name: _stable(value) for name, value in raw_metrics.items()}


def _raw_metrics(
    relevant_raw_ids: tuple[str, ...] | list[str],
    ranked_raw_ids: tuple[str, ...] | list[str],
) -> dict[str, float]:
    relevant = set(relevant_raw_ids)
    if not relevant:
        raise ValueError("relevant_raw_ids must not be empty")
    full_ranking = list(dict.fromkeys(ranked_raw_ids))
    ranking = full_ranking[:5]
    relevant_ranks = [rank for rank, raw_id in enumerate(ranking, start=1) if raw_id in relevant]
    first_relevant_rank = next(
        (rank for rank, raw_id in enumerate(full_ranking, start=1) if raw_id in relevant),
        None,
    )
    recall = len(relevant_ranks) / len(relevant)
    mrr = 1.0 / first_relevant_rank if first_relevant_rank is not None else 0.0
    dcg = math.fsum(1.0 / math.log2(rank + 1) for rank in relevant_ranks)
    ideal_count = min(len(relevant), 5)
    ideal_dcg = math.fsum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    noise = sum(raw_id not in relevant for raw_id in ranking) / len(ranking) if ranking else 0.0
    return {
        "recall_at_5": recall,
        "mrr": mrr,
        "ndcg_at_5": dcg / ideal_dcg,
        "noise_rate": noise,
        "zero_result_rate": 0.0 if full_ranking else 1.0,
    }


def union_rankings(keyword_raw_ids: list[str], vector_raw_ids: list[str]) -> list[str]:
    combined = list(keyword_raw_ids)
    seen = set(keyword_raw_ids)
    for raw_id in vector_raw_ids:
        if raw_id not in seen:
            seen.add(raw_id)
            combined.append(raw_id)
    return combined


def rank_vector_units(
    units: list[VectorUnit] | list[dict[str, Any]],
    *,
    disabled_unit_types: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    unknown = set(disabled_unit_types) - {"headline", "summary", "body"}
    if unknown:
        raise ValueError("unknown vector unit type: " + ", ".join(sorted(unknown)))
    eligible = [unit for unit in units if _unit_value(unit, "kind") not in disabled_unit_types]
    eligible.sort(key=lambda unit: (-float(_unit_value(unit, "score") or 0.0), str(_unit_value(unit, "chunk_id"))))
    ranking: list[str] = []
    seen: set[str] = set()
    for unit in eligible:
        raw_id = str(_unit_value(unit, "raw_id"))
        if raw_id in seen:
            continue
        seen.add(raw_id)
        ranking.append(raw_id)
    return ranking


def evaluate_gold(
    gold: list[GoldQuery],
    search: Callable[[str], dict[str, Any]],
    *,
    disabled_unit_types: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not gold:
        raise GoldValidationError("gold file is empty")
    unknown = set(disabled_unit_types) - {"headline", "summary", "body"}
    if unknown:
        raise ValueError("unknown vector unit type: " + ", ".join(sorted(unknown)))

    query_reports: list[dict[str, Any]] = []
    for gold_query in gold:
        result = search(gold_query.query)
        supplemental = result.get("supplemental_raw", {})
        if supplemental.get("status") != "ready":
            status = str(supplemental.get("status", "unknown"))
            reason = str(supplemental.get("reason", "vector search is unavailable"))
            raise VectorEvaluationError(f"vector search is not ready: {status}: {reason}")
        keyword_ranking = _keyword_ranking(result.get("candidates", []))
        vector_units = _ranked_vector_units(supplemental.get("units", []), disabled_unit_types)
        vector_ranking = rank_vector_units(vector_units)
        rankings = {
            "keyword": keyword_ranking,
            "vector": vector_ranking,
            "union": union_rankings(keyword_ranking, vector_ranking),
        }
        returned_chunks = {str(_unit_value(unit, "chunk_id")) for unit in vector_units}
        expected = list(gold_query.expected_units)
        query_reports.append({
            "query": gold_query.query,
            "relevant_raw_ids": list(gold_query.relevant_raw_ids),
            "rankings": rankings,
            "metrics": {
                name: metrics_for_ranking(gold_query.relevant_raw_ids, ranking)
                for name, ranking in rankings.items()
            },
            "vector_units": [_unit_evidence(unit) for unit in vector_units],
            "expected_unit_evidence": {
                "expected": expected,
                "matched": [chunk_id for chunk_id in expected if chunk_id in returned_chunks],
                "missing": [chunk_id for chunk_id in expected if chunk_id not in returned_chunks],
            },
        })

    return {
        "query_count": len(query_reports),
        "ablation": {"disabled_unit_types": sorted(disabled_unit_types)},
        "queries": query_reports,
        "summary": {
            method: {
                metric: _stable(math.fsum(
                    _raw_metrics(row["relevant_raw_ids"], row["rankings"][method])[metric]
                    for row in query_reports
                ) / len(query_reports))
                for metric in ("recall_at_5", "mrr", "ndcg_at_5", "noise_rate", "zero_result_rate")
            }
            for method in ("keyword", "vector", "union")
        },
    }


def evaluate_repository(
    repo: Path,
    gold_path: Path,
    *,
    disabled_unit_types: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    repo = Path(repo)
    gold = load_gold(gold_path)
    state = vector_status(repo)
    if not state.ready or state.manifest is None:
        raise VectorEvaluationError(f"vector cache is not ready: {state.status}: {state.reason}")
    cached_raws = state.manifest.get("raws")
    if not isinstance(cached_raws, dict):
        raise VectorEvaluationError("vector cache manifest has no Raw catalog")
    unknown_raw_ids = sorted({raw_id for row in gold for raw_id in row.relevant_raw_ids} - set(cached_raws))
    if unknown_raw_ids:
        raise GoldValidationError("gold references Raw IDs outside the ready vector cache: " + ", ".join(unknown_raw_ids))
    report = evaluate_gold(
        gold,
        lambda query: search_level1(repo, query, disabled_unit_types=disabled_unit_types),
        disabled_unit_types=disabled_unit_types,
    )
    return {
        "gold": str(Path(gold_path)),
        "cache": {
            "status": state.status,
            "reason": state.reason,
            "raw_count": state.manifest.get("raw_count", len(cached_raws)),
            "unit_count": state.manifest.get("unit_count", 0),
        },
        **report,
    }


def _keyword_ranking(candidates: Any) -> list[str]:
    if not isinstance(candidates, list):
        raise VectorEvaluationError("keyword search candidates are invalid")
    ranking: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        sources = candidate.get("sources", []) if isinstance(candidate, dict) else []
        if not isinstance(sources, list):
            raise VectorEvaluationError("keyword candidate sources are invalid")
        for raw_id in sources:
            if isinstance(raw_id, str) and raw_id not in seen:
                seen.add(raw_id)
                ranking.append(raw_id)
    return ranking


def _ranked_vector_units(
    units: Any,
    disabled_unit_types: set[str] | frozenset[str],
) -> list[VectorUnit | dict[str, Any]]:
    if not isinstance(units, list):
        raise VectorEvaluationError("vector search units are invalid")
    eligible = [unit for unit in units if str(_unit_value(unit, "kind")) not in disabled_unit_types]
    eligible.sort(key=lambda unit: (-float(_unit_value(unit, "score") or 0.0), str(_unit_value(unit, "chunk_id"))))
    return eligible


def _unit_evidence(unit: VectorUnit | dict[str, Any]) -> dict[str, Any]:
    fields = (
        "chunk_id",
        "raw_id",
        "kind",
        "segment_index",
        "section_index",
        "start",
        "end",
        "score",
        "snippet",
    )
    return {field: _unit_value(unit, field) for field in fields}


def _unit_value(unit: VectorUnit | dict[str, Any], field: str) -> Any:
    return getattr(unit, field) if isinstance(unit, VectorUnit) else unit.get(field)


def _stable(value: float) -> float:
    return round(value, 6)


__all__ = [
    "GoldQuery",
    "GoldValidationError",
    "VectorEvaluationError",
    "evaluate_gold",
    "evaluate_repository",
    "load_gold",
    "metrics_for_ranking",
    "rank_vector_units",
    "union_rankings",
]
