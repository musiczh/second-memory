from __future__ import annotations

import subprocess
from pathlib import Path

from ..errors import DirtyWorktreeError, SecondMemoryError


class GitStorage:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def ensure_initialized(self) -> None:
        if not (self.repo / ".git").exists():
            self._git("init", "-b", "master")
        self._ensure_identity()

    def _ensure_identity(self) -> None:
        name = self._git("config", "user.name", check=False)
        if name.returncode != 0 or not name.stdout.strip():
            self._git("config", "user.name", "Second Memory")
        email = self._git("config", "user.email", check=False)
        if email.returncode != 0 or not email.stdout.strip():
            self._git("config", "user.email", "second-memory@local")

    def status_porcelain(self) -> list[str]:
        result = self._git("status", "--porcelain")
        return [line for line in result.stdout.splitlines() if line.strip()]

    def assert_only_known_changes(self) -> None:
        allowed = ("raw/", "wiki/", ".kb/", "index.md", "AGENTS.md")
        unknown: list[str] = []
        for line in self.status_porcelain():
            path = line[3:] if len(line) > 3 else line
            if not path.startswith(allowed):
                unknown.append(line)
        if unknown:
            raise DirtyWorktreeError("unknown worktree changes: " + "; ".join(unknown))

    def commit_paths(self, message: str, paths: list[str]) -> str | None:
        """Commit only transaction-owned paths, leaving unrelated staged work alone."""
        unique = sorted({path for path in paths if self._path_has_content_or_history(path)})
        if not unique:
            return self.current_commit()
        self._git("add", "-A", "--", *unique)
        diff = self._git("diff", "--cached", "--quiet", "--", *unique, check=False)
        if diff.returncode == 0:
            return self.current_commit()
        result = self._git("commit", "--only", "-m", message, "--", *unique, check=False)
        if result.returncode != 0:
            raise SecondMemoryError(result.stderr.strip() or "git commit failed", "git_commit_failed")
        return self.current_commit()

    def _path_has_content_or_history(self, relative: str) -> bool:
        path = self.repo / relative
        if path.is_file() or (path.is_dir() and any(value.is_file() for value in path.rglob("*"))):
            return True
        return bool(self._git("ls-files", "--", relative, check=False).stdout.strip())

    def unstage_paths(self, paths: list[str]) -> None:
        unique = sorted(set(paths))
        if not unique:
            return
        self._git("restore", "--staged", "--", *unique, check=False)

    def current_commit(self) -> str | None:
        result = self._git("rev-parse", "--short", "HEAD", check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def full_commit(self) -> str | None:
        result = self._git("rev-parse", "HEAD", check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
