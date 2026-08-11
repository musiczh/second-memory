from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .utils import json_dumps


class KnowledgeTransaction:
    def __init__(self, repo: Path, session_id: str) -> None:
        self.repo = repo
        self.session_id = session_id
        self.root = repo / ".kb" / "transaction"
        self.journal = repo / ".kb" / "transaction.json"
        self.wiki_next = self.root / "wiki.next"
        self.wiki_previous = self.root / "wiki.previous"
        self.backup = self.root / "backup"
        self.staged = self.root / "staged"
        self._originals: dict[str, bool] = {}

    def prepare(self, control_paths: list[str] | None = None) -> None:
        if self.journal.exists():
            raise RuntimeError("unfinished knowledge-base transaction requires recovery")
        if self.root.exists():
            shutil.rmtree(self.root)
        self.wiki_next.mkdir(parents=True)
        self.backup.mkdir(parents=True)
        self.staged.mkdir(parents=True)
        paths = ["index.md", ".kb/manifest.json", ".kb/pending.jsonl", *(control_paths or [])]
        for relative in dict.fromkeys(paths):
            source = self.repo / relative
            self._originals[relative] = source.exists()
            if source.exists():
                target = self.backup / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        self._write_journal("prepared")

    def stage_metadata(self, *, index: str, manifest: dict[str, Any], pending_rows: list[dict[str, Any]]) -> None:
        (self.staged / "index.md").write_text(index, encoding="utf-8")
        (self.staged / "manifest.json").write_text(json_dumps(manifest) + "\n", encoding="utf-8")
        (self.staged / "pending.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in pending_rows),
            encoding="utf-8",
        )

    def stage_raw(self, relative: str, content: str) -> None:
        source = self.repo / relative
        backup = self.backup / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
        target = self.staged / "raw" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def stage_control(self, relative: str, content: str) -> None:
        target = self.staged / "control" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def promote(self) -> None:
        self._write_journal("promoting")
        wiki = self.repo / "wiki"
        if wiki.exists():
            os.replace(wiki, self.wiki_previous)
        os.replace(self.wiki_next, wiki)
        os.replace(self.staged / "index.md", self.repo / "index.md")
        os.replace(self.staged / "manifest.json", self.repo / ".kb" / "manifest.json")
        os.replace(self.staged / "pending.jsonl", self.repo / ".kb" / "pending.jsonl")
        staged_control = self.staged / "control"
        if staged_control.exists():
            for path in sorted(value for value in staged_control.rglob("*") if value.is_file()):
                relative = path.relative_to(staged_control)
                target = self.repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
        staged_raw = self.staged / "raw"
        if staged_raw.exists():
            for path in sorted(staged_raw.rglob("*.md")):
                relative = path.relative_to(staged_raw)
                target = self.repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
                try:
                    os.chmod(target, 0o444)
                except OSError:
                    pass
        self._write_journal("promoted")

    def mark_committed(self, commit: str | None) -> None:
        self._write_journal("committed", commit=commit)

    def rollback(self) -> None:
        wiki = self.repo / "wiki"
        if self.wiki_previous.exists():
            if wiki.exists():
                shutil.rmtree(wiki)
            os.replace(self.wiki_previous, wiki)
        for relative, existed in self._originals.items():
            target = self.repo / relative
            backup = self.backup / relative
            if existed and backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
            elif not existed and target.exists():
                target.unlink()
        raw_backup = self.backup / "raw"
        if raw_backup.exists():
            for path in sorted(raw_backup.rglob("*.md")):
                relative = path.relative_to(self.backup)
                target = self.repo / relative
                if target.exists():
                    try:
                        os.chmod(target, 0o644)
                    except OSError:
                        pass
                shutil.copy2(path, target)
        self.finalize()

    def finalize(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        if self.journal.exists():
            self.journal.unlink()
        temporary = self.journal.with_suffix(".json.tmp")
        if temporary.exists():
            temporary.unlink()

    def _write_journal(self, phase: str, *, commit: str | None = None) -> None:
        payload = {
            "schema": 1,
            "session_id": self.session_id,
            "phase": phase,
            "commit": commit,
            "originals": self._originals,
        }
        temporary = self.journal.with_suffix(".json.tmp")
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json_dumps(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.journal)
        directory = os.open(self.journal.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def transaction_state(repo: Path) -> dict[str, Any]:
    journal = repo / ".kb" / "transaction.json"
    if not journal.exists():
        return {"state": "clean", "session_id": None, "recovery_required": False}
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "corrupt", "session_id": None, "recovery_required": True}
    return {
        "state": str(payload.get("phase", "unknown")),
        "session_id": payload.get("session_id"),
        "recovery_required": payload.get("phase") != "committed",
    }


def recover_transaction(repo: Path) -> str:
    state = transaction_state(repo)
    if state["state"] == "clean":
        return "clean"
    journal = repo / ".kb" / "transaction.json"
    if state["state"] == "corrupt":
        return recover_corrupt_transaction(repo)
    payload = json.loads(journal.read_text(encoding="utf-8"))
    tx = KnowledgeTransaction(repo, str(payload.get("session_id", "unknown")))
    tx._originals = dict(payload.get("originals", {}))
    phase = payload.get("phase")
    committed_session = git_head_manifest_session(repo)
    current_session = current_manifest_session(repo)
    promoted_is_committed = committed_session == tx.session_id if (repo / ".git").exists() else current_session == tx.session_id
    if phase == "committed" or (phase == "promoted" and promoted_is_committed):
        tx.finalize()
        return "finalized"
    tx.rollback()
    return "rolled_back"


def recover_corrupt_transaction(repo: Path) -> str:
    tx = KnowledgeTransaction(repo, "corrupt-journal")
    if not tx.root.exists():
        raise RuntimeError("corrupt transaction journal has no recovery workspace")

    current_session = current_manifest_session(repo)
    committed_session = git_head_manifest_session(repo)
    backup_session = None
    backup_manifest = tx.backup / ".kb" / "manifest.json"
    if backup_manifest.exists():
        try:
            backup_session = json.loads(backup_manifest.read_text(encoding="utf-8")).get("applied_session_id")
        except json.JSONDecodeError:
            backup_session = None
    if (
        (repo / ".git").exists()
        and current_session
        and current_session != backup_session
        and current_session == committed_session
    ):
        tx.finalize()
        return "finalized_corrupt_journal"

    originals: dict[str, bool] = {}
    if tx.backup.exists():
        for path in sorted(value for value in tx.backup.rglob("*") if value.is_file()):
            relative = path.relative_to(tx.backup).as_posix()
            if not relative.startswith("raw/"):
                originals[relative] = True
    tx._originals = originals
    tx.rollback()
    return "rolled_back_corrupt_journal"


def git_head_manifest_session(repo: Path) -> str | None:
    if not (repo / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "show", "HEAD:.kb/manifest.json"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout).get("applied_session_id")
    except json.JSONDecodeError:
        return None


def current_manifest_session(repo: Path) -> str | None:
    path = repo / ".kb" / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("applied_session_id")
    except json.JSONDecodeError:
        return None
