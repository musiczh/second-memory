from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from .chunking import annotation_hash
from .config import VECTOR_CONFIG_KEYS, load_config
from .embedding import EmbeddingProvider, EmbeddingSpec, FastEmbedProvider
from .models import RawEntry
from .utils import sha256_text


VECTOR_CACHE_SCHEMA = 1
_CACHE_DIR = Path(".kb/vectors")
_MANIFEST_KEYS = {
    "schema", "provider", "model", "model_hash", "dimension", "spec",
    "spec_fingerprint", "config_fingerprint", "input_fingerprint",
    "raw_count", "unit_count", "raws",
}
_SPEC_KEYS = {"provider", "model", "dimension", "dtype", "normalization", "runtime", "model_hash"}
_RAW_INFO_KEYS = {
    "path", "body_hash", "annotation_hash", "raw_fingerprint", "file", "file_hash", "unit_count",
}
_COMMON_ROW_KEYS = {"chunk_id", "raw_id", "kind", "vector", "body_hash", "annotation_hash"}
_ROW_KEYS = {
    "headline": _COMMON_ROW_KEYS,
    "summary": _COMMON_ROW_KEYS | {"segment_index"},
    "body": _COMMON_ROW_KEYS | {"section_index", "start", "end"},
}


class VectorCacheError(RuntimeError):
    """Raised when a complete deterministic vector cache cannot be built."""


class VectorCacheStaleError(VectorCacheError):
    """Raised when cache inputs differ from the authoritative main manifest."""


@dataclass(frozen=True)
class VectorCacheState:
    status: str
    reason: str
    manifest: dict[str, Any] | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass(frozen=True)
class VectorUnit:
    chunk_id: str
    raw_id: str
    kind: str
    text: str = ""
    vector: tuple[float, ...] = ()
    segment_index: int | None = None
    section_index: int | None = None
    start: int | None = None
    end: int | None = None
    score: float | None = None

    @property
    def unit_type(self) -> str:
        return self.kind


@dataclass(frozen=True)
class VectorSearchResult:
    status: str
    reason: str
    units: list[VectorUnit]
    raws: list[dict[str, Any]]


def build_vector_units(entry: RawEntry, config: dict[str, Any]) -> list[VectorUnit]:
    """Build stable text-bearing locators without calling an embedding provider."""
    minimum, target, maximum, overlap = _chunk_config(config)
    annotations = entry.annotations
    headline = str(annotations.get("summary", ""))
    segments = annotations.get("summary_segments")
    sections = annotations.get("body_sections")
    if not headline or not isinstance(segments, list) or not segments:
        raise VectorCacheError(f"raw annotation is incomplete: {entry.id}")
    if not isinstance(sections, list) or not sections:
        raise VectorCacheError(f"raw body sections are incomplete: {entry.id}")

    units = [_unit(entry.id, "headline", headline)]
    for index, value in enumerate(segments):
        if not isinstance(value, str) or not value:
            raise VectorCacheError(f"raw summary segment is invalid: {entry.id}")
        units.append(_unit(entry.id, "summary", value, segment_index=index))

    previous_end = 0
    for section_index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise VectorCacheError(f"raw body section is invalid: {entry.id}")
        start = _integer(section.get("start"), "body section start")
        end = _integer(section.get("end"), "body section end")
        if start != previous_end or end <= start or end > len(entry.body):
            raise VectorCacheError(f"raw body sections must be ordered contiguous offsets: {entry.id}")
        previous_end = end
        length = end - start
        if length < minimum and len(entry.body) >= minimum:
            raise VectorCacheError(
                f"raw body section is shorter than the vector chunk minimum: {entry.id}: section {section_index}"
            )
        for chunk_start, chunk_end in _chunk_offsets(start, end, target, minimum, maximum, overlap):
            text = entry.body[chunk_start:chunk_end]
            units.append(
                _unit(
                    entry.id,
                    "body",
                    text,
                    section_index=section_index,
                    start=chunk_start,
                    end=chunk_end,
                )
            )
    if previous_end != len(entry.body):
        raise VectorCacheError(f"raw body sections must cover the complete body: {entry.id}")
    return units


def resolve_vector_unit_text(entry: RawEntry, unit: VectorUnit | dict[str, Any]) -> str:
    """Resolve a text-free cache locator against the immutable Raw document."""
    kind = str(unit.kind if isinstance(unit, VectorUnit) else unit.get("kind", ""))
    if kind == "headline":
        return str(entry.annotations.get("summary", ""))
    if kind == "summary":
        index = unit.segment_index if isinstance(unit, VectorUnit) else unit.get("segment_index")
        if not isinstance(index, int):
            raise VectorCacheError(f"summary unit has no segment index: {entry.id}")
        segments = entry.annotations.get("summary_segments", [])
        if not isinstance(segments, list) or not 0 <= index < len(segments):
            raise VectorCacheError(f"summary segment index is out of range: {entry.id}")
        return str(segments[index])
    if kind == "body":
        start = unit.start if isinstance(unit, VectorUnit) else unit.get("start")
        end = unit.end if isinstance(unit, VectorUnit) else unit.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(entry.body):
            raise VectorCacheError(f"body unit offsets are invalid: {entry.id}")
        return entry.body[start:end]
    raise VectorCacheError(f"unknown vector unit kind: {kind}")


def reindex_vectors(
    repo: Path,
    provider: EmbeddingProvider | None = None,
    offline: bool = False,
    raw_ids: Sequence[str] | None = None,
    destination: Path | None = None,
) -> VectorCacheState:
    """Build a complete cache and atomically install it at destination."""
    repo = Path(repo)
    config = load_config(repo)
    if not bool(config.get("vector_enabled", True)):
        return VectorCacheState("disabled", "vector retrieval is disabled")
    embedding_provider = provider or FastEmbedProvider(local_files_only=offline)
    _validate_spec_against_config(embedding_provider.spec, config)

    entries = _raw_lookup(repo)
    compiled_ids = sorted(str(raw_id) for raw_id in _load_manifest(repo).get("compiled_raw", []))
    if any(raw_id not in entries for raw_id in compiled_ids):
        raise VectorCacheError("compiled Raw set contains a missing source file")
    selected = set(compiled_ids if raw_ids is None else map(str, raw_ids))
    unknown = selected - set(compiled_ids)
    if unknown:
        raise VectorCacheError("reindex raw_ids are outside the compiled Raw set: " + ", ".join(sorted(unknown)))

    inputs = _current_inputs(repo, entries, compiled_ids, require_main_manifest=True)
    target = Path(destination) if destination is not None else repo / _CACHE_DIR
    reused = (
        _reusable_files(repo, embedding_provider.spec, config, inputs, entries, selected)
        if selected != set(compiled_ids)
        else {}
    )

    generated: dict[str, tuple[bytes, int]] = {}
    selected_units: list[tuple[str, list[VectorUnit]]] = []
    passage_texts: list[str] = []
    for raw_id in compiled_ids:
        if raw_id not in selected:
            continue
        units = build_vector_units(entries[raw_id], config)
        selected_units.append((raw_id, units))
        passage_texts.extend(unit.text for unit in units)
    vectors = embedding_provider.embed_passages(passage_texts) if passage_texts else []
    if len(vectors) != len(passage_texts):
        raise VectorCacheError("passage embedding count does not match vector units")
    vector_cursor = 0
    for raw_id, units in selected_units:
        lines: list[str] = []
        raw_input = inputs[raw_id]
        for unit in units:
            vector = _validated_vector(vectors[vector_cursor], embedding_provider.spec.dimension)
            vector_cursor += 1
            row = _unit_row(unit, vector, raw_input)
            lines.append(_canonical_json(row) + "\n")
        generated[raw_id] = ("".join(lines).encode("utf-8"), len(units))

    files: dict[str, tuple[bytes, int]] = {}
    for raw_id in compiled_ids:
        if raw_id in generated:
            files[raw_id] = generated[raw_id]
        elif raw_id in reused:
            files[raw_id] = reused[raw_id]
        else:
            raise VectorCacheError(f"cannot build a complete destination cache for unchanged Raw: {raw_id}")

    manifest = _build_cache_manifest(embedding_provider.spec, config, inputs, files)
    _install_cache(target, manifest, files)
    state = _inspect_cache(repo, target, ignore_pending=True)
    if not state.ready:
        raise VectorCacheError(f"installed vector cache did not validate: {state.status}: {state.reason}")
    return state


def vector_status(repo: Path, *, destination: Path | None = None) -> VectorCacheState:
    """Inspect only local metadata and cache bytes; never initialize a provider."""
    return _inspect_cache(Path(repo), Path(destination) if destination is not None else Path(repo) / _CACHE_DIR)


def search_vectors(
    repo: Path,
    query: str,
    provider: EmbeddingProvider | None = None,
    *,
    disabled_unit_types: set[str] | frozenset[str] = frozenset(),
) -> VectorSearchResult:
    unknown = set(disabled_unit_types) - {"headline", "summary", "body"}
    if unknown:
        raise ValueError("unknown vector unit type: " + ", ".join(sorted(unknown)))
    repo = Path(repo)
    state = vector_status(repo)
    if not state.ready or state.manifest is None:
        return VectorSearchResult(state.status, state.reason, [], [])
    try:
        embedding_provider = provider or FastEmbedProvider(local_files_only=True)
    except Exception as error:
        return VectorSearchResult("pending", f"local model is unavailable: {error}", [], [])
    if _spec_fingerprint(embedding_provider.spec) != state.manifest["spec_fingerprint"]:
        return VectorSearchResult("stale", "embedding provider spec differs from the cache", [], [])
    try:
        query_vector = _validated_vector(embedding_provider.embed_query(query), int(state.manifest["dimension"]))
    except Exception as error:
        return VectorSearchResult("pending", f"local model query failed: {error}", [], [])
    try:
        entries = _raw_lookup(repo)
        candidates: list[VectorUnit] = []
        cache_root = repo / _CACHE_DIR
        for raw_id in sorted(state.manifest["raws"]):
            info = state.manifest["raws"][raw_id]
            rows = _read_rows(cache_root / str(info["file"]), raw_id, info, int(state.manifest["dimension"]))
            entry = entries[raw_id]
            for row in rows:
                unit = _row_unit(row)
                if unit.kind in disabled_unit_types:
                    continue
                score = math.fsum(left * right for left, right in zip(query_vector, unit.vector, strict=True))
                candidates.append(replace(unit, text=resolve_vector_unit_text(entry, unit), score=score))
    except Exception as error:
        return VectorSearchResult("corrupt", f"vector search is unavailable: {error}", [], [])

    config = load_config(repo)
    candidates.sort(key=lambda unit: (-float(unit.score or 0.0), unit.chunk_id))
    scanned = candidates[: int(config["vector_scan_k"])]
    qualified = [unit for unit in scanned if float(unit.score or 0.0) >= float(config["vector_min_score"])]
    units = qualified[: int(config["vector_unit_limit"])]
    raws: list[dict[str, Any]] = []
    raw_limit = int(config["vector_raw_limit"])
    if raw_limit <= 0:
        return VectorSearchResult("ready", "vector cache is ready", units, raws)
    seen: set[str] = set()
    entries = _raw_lookup(repo)
    for unit in units:
        if unit.raw_id in seen:
            continue
        seen.add(unit.raw_id)
        entry = entries[unit.raw_id]
        raws.append({
            "raw_id": unit.raw_id,
            "title": entry.title,
            "path": str(entry.path.relative_to(repo)),
            "score": unit.score,
            "chunk_id": unit.chunk_id,
        })
        if len(raws) == raw_limit:
            break
    return VectorSearchResult("ready", "vector cache is ready", units, raws)


def _chunk_offsets(
    start: int,
    end: int,
    target: int,
    minimum: int,
    maximum: int,
    overlap: float,
) -> Iterable[tuple[int, int]]:
    overlap_size = int(target * overlap)
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + target, end)
        if end - chunk_end < minimum and end - cursor <= maximum:
            chunk_end = end
        if chunk_end - cursor < minimum and cursor > start:
            cursor = max(start, end - minimum)
            chunk_end = end
        yield cursor, chunk_end
        if chunk_end == end:
            break
        next_cursor = chunk_end - overlap_size
        if end - next_cursor < minimum:
            next_cursor = end - minimum
        if next_cursor <= cursor:
            raise VectorCacheError("vector chunk configuration does not advance")
        cursor = next_cursor


def _unit(
    raw_id: str,
    kind: str,
    text: str,
    *,
    segment_index: int | None = None,
    section_index: int | None = None,
    start: int | None = None,
    end: int | None = None,
) -> VectorUnit:
    locator = {
        "raw_id": raw_id,
        "kind": kind,
        "segment_index": segment_index,
        "section_index": section_index,
        "start": start,
        "end": end,
        "text_hash": sha256_text(text),
    }
    digest = hashlib.sha256(_canonical_json(locator).encode("utf-8")).hexdigest()[:24]
    return VectorUnit(
        chunk_id=f"chunk-{digest}",
        raw_id=raw_id,
        kind=kind,
        text=text,
        segment_index=segment_index,
        section_index=section_index,
        start=start,
        end=end,
    )


def _unit_row(unit: VectorUnit, vector: tuple[float, ...], raw_input: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "chunk_id": unit.chunk_id,
        "raw_id": unit.raw_id,
        "kind": unit.kind,
        "vector": list(vector),
        "body_hash": raw_input["body_hash"],
        "annotation_hash": raw_input["annotation_hash"],
    }
    for key in ("segment_index", "section_index", "start", "end"):
        value = getattr(unit, key)
        if value is not None:
            row[key] = value
    return row


def _row_unit(row: dict[str, Any]) -> VectorUnit:
    return VectorUnit(
        chunk_id=str(row["chunk_id"]),
        raw_id=str(row["raw_id"]),
        kind=str(row["kind"]),
        vector=tuple(float(value) for value in row["vector"]),
        segment_index=row.get("segment_index"),
        section_index=row.get("section_index"),
        start=row.get("start"),
        end=row.get("end"),
    )


def _build_cache_manifest(
    spec: EmbeddingSpec,
    config: dict[str, Any],
    inputs: dict[str, dict[str, Any]],
    files: dict[str, tuple[bytes, int]],
) -> dict[str, Any]:
    raws = {
        raw_id: {
            **inputs[raw_id],
            "file": f"raw/{raw_id}.jsonl",
            "file_hash": hashlib.sha256(content).hexdigest(),
            "unit_count": unit_count,
        }
        for raw_id, (content, unit_count) in sorted(files.items())
    }
    return {
        "schema": VECTOR_CACHE_SCHEMA,
        "provider": spec.provider,
        "model": spec.model,
        "model_hash": spec.model_hash,
        "dimension": spec.dimension,
        "spec": asdict(spec),
        "spec_fingerprint": _spec_fingerprint(spec),
        "config_fingerprint": _config_fingerprint(config),
        "input_fingerprint": _fingerprint(inputs),
        "raw_count": len(raws),
        "unit_count": sum(int(info["unit_count"]) for info in raws.values()),
        "raws": raws,
    }


def _install_cache(target: Path, manifest: dict[str, Any], files: dict[str, tuple[bytes, int]]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
    backup = target.parent / f".{target.name}.previous-{uuid.uuid4().hex}"
    try:
        (temporary / "raw").mkdir(parents=True)
        for raw_id, (content, _) in sorted(files.items()):
            (temporary / "raw" / f"{raw_id}.jsonl").write_bytes(content)
        (temporary / "manifest.json").write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        if target.exists():
            os.replace(target, backup)
            try:
                os.replace(temporary, target)
            except Exception:
                os.replace(backup, target)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if backup.exists() and target.exists():
            shutil.rmtree(backup)


def _inspect_cache(repo: Path, cache_root: Path, *, ignore_pending: bool = False) -> VectorCacheState:
    try:
        config = load_config(repo)
    except Exception as error:
        return VectorCacheState("corrupt", f"vector config cannot be read: {error}")
    if not bool(config.get("vector_enabled", True)):
        return VectorCacheState("disabled", "vector retrieval is disabled")
    if not ignore_pending:
        try:
            if _read_pending(repo):
                return VectorCacheState("pending", "Raw annotations are pending compilation")
        except Exception as error:
            return VectorCacheState("corrupt", f"pending Raw state cannot be read: {error}")
    if not cache_root.exists():
        return VectorCacheState("pending", "vector cache has not been built")
    manifest_path = cache_root / "manifest.json"
    if not manifest_path.is_file():
        return VectorCacheState("missing", "vector cache manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or int(manifest.get("schema", 0)) != VECTOR_CACHE_SCHEMA:
            raise VectorCacheError("vector cache schema is invalid")
        if set(manifest) != _MANIFEST_KEYS:
            raise VectorCacheError("vector cache manifest fields are invalid")
        spec = _spec_from_manifest(manifest)
        if _spec_fingerprint(spec) != manifest.get("spec_fingerprint"):
            raise VectorCacheError("embedding spec fingerprint is invalid")
        if manifest.get("provider") != spec.provider or manifest.get("model") != spec.model:
            raise VectorCacheError("embedding spec fields are inconsistent")
        if manifest.get("model_hash") != spec.model_hash or int(manifest.get("dimension", 0)) != spec.dimension:
            raise VectorCacheError("embedding model fields are inconsistent")
        if manifest.get("config_fingerprint") != _config_fingerprint(config):
            return VectorCacheState("stale", "vector configuration differs from the cache", manifest)
        _validate_spec_against_config(spec, config)
        entries = _raw_lookup(repo)
        compiled_ids = sorted(str(raw_id) for raw_id in _load_manifest(repo).get("compiled_raw", []))
        inputs = _current_inputs(repo, entries, compiled_ids, require_main_manifest=True)
        if manifest.get("input_fingerprint") != _fingerprint(inputs):
            return VectorCacheState("stale", "Raw annotation or body input differs from the cache", manifest)
        raw_manifests = manifest.get("raws")
        if not isinstance(raw_manifests, dict) or set(raw_manifests) != set(compiled_ids):
            return VectorCacheState("stale", "compiled Raw set differs from the cache", manifest)
        if int(manifest.get("raw_count", -1)) != len(raw_manifests):
            raise VectorCacheError("vector Raw count is invalid")
        unit_count = 0
        for raw_id in compiled_ids:
            info = raw_manifests[raw_id]
            if not isinstance(info, dict) or set(info) != _RAW_INFO_KEYS:
                raise VectorCacheError(f"vector Raw manifest fields are invalid: {raw_id}")
            if any(info.get(key) != inputs[raw_id][key] for key in inputs[raw_id]):
                return VectorCacheState("stale", f"Raw fingerprint differs from the cache: {raw_id}", manifest)
            relative = Path(str(info.get("file", "")))
            if relative.parts != ("raw", f"{raw_id}.jsonl"):
                raise VectorCacheError(f"vector JSONL path is invalid: {raw_id}")
            path = cache_root / relative
            if not path.is_file():
                return VectorCacheState("missing", f"vector JSONL is missing: {raw_id}", manifest)
            rows = _read_rows(path, raw_id, info, spec.dimension)
            if len(rows) != int(info.get("unit_count", -1)):
                raise VectorCacheError(f"vector unit count is invalid: {raw_id}")
            expected_units = build_vector_units(entries[raw_id], config)
            _validate_rows_against_units(rows, expected_units, raw_id)
            unit_count += len(rows)
        if unit_count != int(manifest.get("unit_count", -1)):
            raise VectorCacheError("vector cache total unit count is invalid")
        return VectorCacheState("ready", "vector cache is ready", manifest)
    except VectorCacheStaleError as error:
        return VectorCacheState("stale", str(error))
    except VectorCacheError as error:
        return VectorCacheState("corrupt", str(error))
    except Exception as error:
        return VectorCacheState("corrupt", f"vector cache cannot be parsed: {error}")


def _current_inputs(
    repo: Path,
    entries: dict[str, RawEntry],
    compiled_ids: Sequence[str],
    *,
    require_main_manifest: bool,
) -> dict[str, dict[str, Any]]:
    main_raws = _load_manifest(repo).get("raw_hashes", {})
    inputs: dict[str, dict[str, Any]] = {}
    for raw_id in compiled_ids:
        entry = entries.get(raw_id)
        if entry is None:
            raise VectorCacheError(f"compiled Raw source is missing: {raw_id}")
        body_hash = sha256_text(entry.body)
        current_annotation_hash = annotation_hash(
            entry.title,
            str(entry.annotations.get("summary", "")),
            list(entry.annotations.get("summary_segments", [])),
            list(entry.annotations.get("body_sections", [])),
        )
        main = main_raws.get(raw_id, {})
        path = str(entry.path.relative_to(repo))
        if require_main_manifest and (
            main.get("path") != path
            or main.get("body_hash") != body_hash
            or main.get("annotation_hash") != current_annotation_hash
        ):
            raise VectorCacheStaleError(f"main manifest Raw fingerprint is stale: {raw_id}")
        raw_fingerprint = _fingerprint({
            "path": path,
            "body_hash": body_hash,
            "annotation_hash": current_annotation_hash,
        })
        inputs[raw_id] = {
            "path": path,
            "body_hash": body_hash,
            "annotation_hash": current_annotation_hash,
            "raw_fingerprint": raw_fingerprint,
        }
    return inputs


def _reusable_files(
    repo: Path,
    spec: EmbeddingSpec,
    config: dict[str, Any],
    inputs: dict[str, dict[str, Any]],
    entries: dict[str, RawEntry],
    selected: set[str],
) -> dict[str, tuple[bytes, int]]:
    root = repo / _CACHE_DIR
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("spec_fingerprint") != _spec_fingerprint(spec):
            return {}
        if manifest.get("config_fingerprint") != _config_fingerprint(config):
            return {}
        raws = manifest.get("raws", {})
        reused: dict[str, tuple[bytes, int]] = {}
        for raw_id, current in inputs.items():
            if raw_id in selected:
                continue
            info = raws.get(raw_id)
            if not isinstance(info, dict) or any(info.get(key) != value for key, value in current.items()):
                continue
            path = root / str(info.get("file", ""))
            rows = _read_rows(path, raw_id, info, spec.dimension)
            _validate_rows_against_units(rows, build_vector_units(entries[raw_id], config), raw_id)
            reused[raw_id] = (path.read_bytes(), len(rows))
        return reused
    except Exception:
        return {}


def _read_rows(path: Path, raw_id: str, info: dict[str, Any], dimension: int) -> list[dict[str, Any]]:
    if not path.is_file():
        raise VectorCacheError(f"vector JSONL is missing: {raw_id}")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != info.get("file_hash"):
        raise VectorCacheError(f"vector JSONL fingerprint is invalid: {raw_id}")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise VectorCacheError(f"vector JSONL is not UTF-8: {raw_id}") from error
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise VectorCacheError(f"vector JSONL contains invalid JSON: {raw_id}") from error
        if not isinstance(row, dict):
            raise VectorCacheError(f"vector JSONL row is invalid: {raw_id}")
        kind = row.get("kind")
        if kind not in _ROW_KEYS or set(row) != _ROW_KEYS[kind]:
            raise VectorCacheError(f"vector JSONL row fields are invalid: {raw_id}")
        if row.get("raw_id") != raw_id:
            raise VectorCacheError(f"vector JSONL locator is invalid: {raw_id}")
        chunk_id = row.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id or chunk_id in seen:
            raise VectorCacheError(f"vector chunk ID is invalid or duplicated: {raw_id}")
        seen.add(chunk_id)
        if row.get("body_hash") != info.get("body_hash") or row.get("annotation_hash") != info.get("annotation_hash"):
            raise VectorCacheError(f"vector JSONL input hash is invalid: {raw_id}")
        row["vector"] = list(_validated_vector(row.get("vector", []), dimension))
        _validate_locator(row)
        rows.append(row)
    if not rows and int(info.get("unit_count", 0)) != 0:
        raise VectorCacheError(f"vector JSONL is empty: {raw_id}")
    return rows


def _validate_locator(row: dict[str, Any]) -> None:
    kind = row["kind"]
    if kind == "headline":
        if any(key in row for key in ("segment_index", "start", "end")):
            raise VectorCacheError("headline vector locator is invalid")
    elif kind == "summary":
        if not isinstance(row.get("segment_index"), int):
            raise VectorCacheError("summary vector locator is invalid")
    elif not isinstance(row.get("start"), int) or not isinstance(row.get("end"), int):
        raise VectorCacheError("body vector locator is invalid")


def _validate_rows_against_units(
    rows: Sequence[dict[str, Any]],
    expected_units: Sequence[VectorUnit],
    raw_id: str,
) -> None:
    if len(rows) != len(expected_units):
        raise VectorCacheError(f"deterministic vector unit count is invalid: {raw_id}")
    for index, (row, expected) in enumerate(zip(rows, expected_units, strict=True)):
        expected_locator = _unit_locator(expected)
        actual_locator = {
            key: row[key]
            for key in ("chunk_id", "raw_id", "kind", "segment_index", "section_index", "start", "end")
            if key in row
        }
        if actual_locator != expected_locator:
            raise VectorCacheError(f"deterministic vector locator is invalid: {raw_id}: row {index}")


def _unit_locator(unit: VectorUnit) -> dict[str, Any]:
    locator: dict[str, Any] = {
        "chunk_id": unit.chunk_id,
        "raw_id": unit.raw_id,
        "kind": unit.kind,
    }
    for key in ("segment_index", "section_index", "start", "end"):
        value = getattr(unit, key)
        if value is not None:
            locator[key] = value
    return locator


def _validated_vector(values: Iterable[object], dimension: int) -> tuple[float, ...]:
    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError) as error:
        raise VectorCacheError("embedding vector is not numeric") from error
    if len(vector) != dimension:
        raise VectorCacheError(f"embedding dimension must be {dimension}, got {len(vector)}")
    if not all(math.isfinite(value) for value in vector):
        raise VectorCacheError("embedding vector must contain finite values")
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if not math.isclose(norm, 1.0, rel_tol=1e-5, abs_tol=1e-5):
        raise VectorCacheError("embedding vector must be L2 normalized")
    return vector


def _chunk_config(config: dict[str, Any]) -> tuple[int, int, int, float]:
    minimum = int(config["vector_chunk_min"])
    target = int(config["vector_chunk_target"])
    maximum = int(config["vector_chunk_max"])
    overlap = float(config["vector_chunk_overlap"])
    if minimum < 1 or target < minimum or maximum < target or not 0 <= overlap < 1:
        raise VectorCacheError("vector chunk configuration is invalid")
    if int(target * overlap) >= target:
        raise VectorCacheError("vector chunk overlap must leave a positive stride")
    return minimum, target, maximum, overlap


def _validate_spec_against_config(spec: EmbeddingSpec, config: dict[str, Any]) -> None:
    if spec.provider != str(config["vector_provider"]):
        raise VectorCacheError("embedding provider differs from vector configuration")
    if spec.model != str(config["vector_model"]):
        raise VectorCacheError("embedding model differs from vector configuration")
    if spec.dimension != int(config["vector_dimension"]):
        raise VectorCacheError("embedding dimension differs from vector configuration")
    if spec.dtype != "float32":
        raise VectorCacheError("embedding dtype must be float32")
    if spec.normalization != "l2":
        raise VectorCacheError("embedding normalization must be l2")
    if spec.runtime != "onnxruntime-cpu":
        raise VectorCacheError("embedding runtime must be onnxruntime-cpu")
    if len(spec.model_hash) != 64 or any(character not in "0123456789abcdef" for character in spec.model_hash):
        raise VectorCacheError("embedding model hash must be a lowercase SHA-256 digest")


def _spec_from_manifest(manifest: dict[str, Any]) -> EmbeddingSpec:
    value = manifest.get("spec")
    if not isinstance(value, dict) or set(value) != _SPEC_KEYS:
        raise VectorCacheError("embedding spec is missing")
    try:
        return EmbeddingSpec(
            provider=str(value["provider"]),
            model=str(value["model"]),
            dimension=int(value["dimension"]),
            dtype=str(value["dtype"]),
            normalization=str(value["normalization"]),
            runtime=str(value["runtime"]),
            model_hash=str(value["model_hash"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise VectorCacheError("embedding spec is invalid") from error


def _spec_fingerprint(spec: EmbeddingSpec) -> str:
    return _fingerprint(asdict(spec))


def _config_fingerprint(config: dict[str, Any]) -> str:
    return _fingerprint({key: config.get(key) for key in VECTOR_CONFIG_KEYS})


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int):
        raise VectorCacheError(f"{label} must be an integer")
    return value


def _load_manifest(repo: Path) -> dict[str, Any]:
    from .compiler import load_manifest

    return load_manifest(repo)


def _raw_lookup(repo: Path) -> dict[str, RawEntry]:
    from .compiler import raw_lookup

    return raw_lookup(repo)


def _read_pending(repo: Path) -> list[dict[str, Any]]:
    from .compiler import read_pending

    return read_pending(repo)
