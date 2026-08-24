from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import frontmatter
from .errors import NotInitializedError, ValidationError

DEFAULT_HOME = Path("~/.second-memory").expanduser()
VALID_SCOPES = {"shared", "agent"}

# Knowledge-base compile-rule version. Bump this (semver) whenever the wiki
# organization or compile rules change in a way that requires rebuilding the
# compiled layer from the raw archive. It is intentionally separate from the
# package version (__init__.__version__): plain code updates that do not change
# compile rules must NOT bump this, so `update` will not force a rebuild.
KB_VERSION = "2.4.0"

VECTOR_DEFAULTS: dict[str, Any] = {
    "vector_enabled": True,
    "vector_provider": "fastembed",
    "vector_model": "BAAI/bge-small-zh-v1.5",
    "vector_dimension": 512,
    "vector_min_score": 0.58,
    "vector_scan_k": 45,
    "vector_unit_limit": 15,
    "vector_units_per_raw_limit": 3,
    "vector_raw_limit": 5,
    "vector_chunk_target": 300,
    "vector_chunk_min": 50,
    "vector_chunk_max": 300,
    "vector_chunk_overlap": 0.15,
}
VECTOR_CONFIG_KEYS = tuple(VECTOR_DEFAULTS)
VECTOR_CACHE_CONFIG_KEYS = (
    "vector_provider",
    "vector_model",
    "vector_dimension",
    "vector_chunk_target",
    "vector_chunk_min",
    "vector_chunk_max",
    "vector_chunk_overlap",
)


def default_repo_for_scope(scope: str = "shared", agent: str | None = None) -> Path:
    home = Path(os.environ.get("SECOND_MEMORY_HOME", str(DEFAULT_HOME))).expanduser()
    if scope == "shared":
        return home / "knowledge-base"
    if scope == "agent":
        if not agent:
            raise ValidationError("--agent is required when --scope agent")
        return home / "agents" / agent / "knowledge-base"
    raise ValidationError("scope must be shared or agent")


def resolve_repo(repo: str | None = None, *, for_init: bool = False, scope: str = "shared", agent: str | None = None) -> Path:
    if repo:
        return Path(repo).expanduser().resolve()
    env_repo = os.environ.get("SECOND_MEMORY_REPO")
    if env_repo:
        return Path(env_repo).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if not for_init and (cwd / ".kb" / "config.yaml").exists():
        return cwd
    if for_init:
        return default_repo_for_scope(scope, agent).resolve()
    return cwd


def config_path(repo: Path) -> Path:
    return repo / ".kb" / "config.yaml"


def skill_repo_root() -> Path:
    """Filesystem root of the installed Skill/CLI code repo.

    The package lives at ``<root>/src/second_memory/config.py``; allow an env
    override for non-standard installs. This is the *code* repo (Skill source),
    distinct from the runtime knowledge-base repo resolved by ``resolve_repo``.
    """
    override = os.environ.get("SECOND_MEMORY_SKILL_REPO")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def load_config(repo: Path) -> dict[str, Any]:
    path = config_path(repo)
    if not path.exists():
        raise NotInitializedError(f"{repo} is not initialized; run second-memory init first")
    return {**VECTOR_DEFAULTS, **frontmatter.parse_mapping(path.read_text(encoding="utf-8"))}


def write_config(repo: Path, config: dict[str, Any]) -> None:
    path = config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dump_mapping(config), encoding="utf-8")


def default_config(repo: Path, scope: str, agent: str | None, backend: str) -> dict[str, Any]:
    if scope not in VALID_SCOPES:
        raise ValidationError("scope must be shared or agent")
    return {
        "schema": 2,
        "scope": scope,
        "agent": agent or "",
        "path": str(repo),
        "language": "zh-CN",
        "review_max_days": 7,
        "backend": backend,
        "kb_version": KB_VERSION,
        **VECTOR_DEFAULTS,
    }
