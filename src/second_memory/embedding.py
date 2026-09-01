from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence


class EmbeddingError(RuntimeError):
    """Raised when an embedding backend cannot satisfy the fixed vector contract."""


@dataclass(frozen=True)
class EmbeddingSpec:
    provider: str
    model: str
    dimension: int
    dtype: str
    normalization: str
    runtime: str
    model_hash: str

    @property
    def model_sha256(self) -> str:
        """SHA-256 of the resolved ONNX model file."""
        return self.model_hash


class EmbeddingProvider(Protocol):
    @property
    def spec(self) -> EmbeddingSpec: ...

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedProvider:
    """Pinned FastEmbed v0.8.0 BGE provider with a CPU-only vector contract."""

    PROVIDER = "fastembed"
    MODEL = "BAAI/bge-small-zh-v1.5"
    DIMENSION = 512
    DTYPE = "float32"
    NORMALIZATION = "l2"
    RUNTIME = "onnxruntime-cpu"
    _ONNX_FILE = "model_optimized.onnx"

    def __init__(
        self,
        *,
        local_files_only: bool = True,
        cache_dir: Path | None = None,
        backend: Any | None = None,
    ) -> None:
        self._backend = backend or self._create_backend(local_files_only, cache_dir)
        resolved_model_path = _fastembed_v080_model_file(self._backend)
        self._spec = EmbeddingSpec(
            provider=self.PROVIDER,
            model=self.MODEL,
            dimension=self.DIMENSION,
            dtype=self.DTYPE,
            normalization=self.NORMALIZATION,
            runtime=self.RUNTIME,
            model_hash=_sha256_file(resolved_model_path),
        )

    @property
    def spec(self) -> EmbeddingSpec:
        return self._spec

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        source_texts = list(texts)
        vectors = [self._normalize(vector) for vector in self._backend.passage_embed(source_texts)]
        if len(vectors) != len(source_texts):
            raise EmbeddingError("passage embedding count does not match input texts")
        return vectors

    def embed_query(self, text: str) -> list[float]:
        vectors = [self._normalize(vector) for vector in self._backend.query_embed([text])]
        if len(vectors) != 1:
            raise EmbeddingError("query embedding count does not match one input query")
        return vectors[0]

    @classmethod
    def _create_backend(cls, local_files_only: bool, cache_dir: Path | None) -> Any:
        try:
            from fastembed import TextEmbedding
        except ImportError as error:
            raise EmbeddingError("fastembed==0.8.0 is required to initialize the embedding provider") from error
        try:
            return TextEmbedding(
                cls.MODEL,
                cache_dir=str(cache_dir) if cache_dir is not None else None,
                providers=["CPUExecutionProvider"],
                local_files_only=local_files_only,
            )
        except Exception as error:
            mode = "local-only" if local_files_only else "download-enabled"
            raise EmbeddingError(f"failed to initialize FastEmbed ({mode}): {error}") from error

    @classmethod
    def _normalize(cls, source: Iterable[object]) -> list[float]:
        values = list(source)
        if len(values) != cls.DIMENSION:
            raise EmbeddingError(f"embedding dimension must be {cls.DIMENSION}, got {len(values)}")
        vector = [_as_float32(value) for value in values]
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if not math.isfinite(norm):
            raise EmbeddingError("embedding norm must be finite")
        if norm == 0:
            raise EmbeddingError("embedding norm must be non-zero")
        return [_as_float32(value / norm) for value in vector]


def _fastembed_v080_model_file(backend: Any) -> Path:
    """Locate the v0.8.0 OnnxTextEmbedding model file through its pinned internals."""
    try:
        model_dir = Path(str(backend.model._model_dir))
    except AttributeError as error:
        raise EmbeddingError(
            "FastEmbed v0.8.0 model directory is unavailable; cannot identify the resolved ONNX model file"
        ) from error
    model_path = model_dir / FastEmbedProvider._ONNX_FILE
    if not model_path.is_file():
        raise EmbeddingError(f"resolved FastEmbed ONNX model file is missing: {model_path}")
    return model_path


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        raise EmbeddingError(f"resolved FastEmbed ONNX model file is missing: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _as_float32(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise EmbeddingError("embedding values must be float32-compatible") from error
    if not math.isfinite(number):
        raise EmbeddingError("embedding values must be finite")
    try:
        value32 = struct.unpack("!f", struct.pack("!f", number))[0]
    except OverflowError as error:
        raise EmbeddingError("embedding values must be float32-compatible") from error
    if not math.isfinite(value32):
        raise EmbeddingError("embedding values must be finite")
    return value32
