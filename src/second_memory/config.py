from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import frontmatter
from .errors import NotInitializedError, ValidationError

DEFAULT_HOME = Path("~/.second-memory").expanduser()
VALID_SCOPES = {"shared", "agent"}


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


def load_config(repo: Path) -> dict[str, Any]:
    path = config_path(repo)
    if not path.exists():
        raise NotInitializedError(f"{repo} is not initialized; run second-memory init first")
    return frontmatter.parse_mapping(path.read_text(encoding="utf-8"))


def write_config(repo: Path, config: dict[str, Any]) -> None:
    path = config_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dump_mapping(config), encoding="utf-8")


def default_config(repo: Path, scope: str, agent: str | None, backend: str) -> dict[str, Any]:
    if scope not in VALID_SCOPES:
        raise ValidationError("scope must be shared or agent")
    return {
        "schema": 1,
        "scope": scope,
        "agent": agent or "",
        "path": str(repo),
        "language": "zh-CN",
        "review_max_days": 7,
        "backend": backend,
        "compile_version": 1,
    }
