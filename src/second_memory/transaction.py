from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .utils import json_dumps


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_directory(source: Path, target: Path) -> None:
    os.replace(source, target)
    _fsync_directory(source.parent)
    if target.parent != source.parent:
        _fsync_directory(target.parent)


class KnowledgeTransaction:
    def __init__(self, repo: Path, session_id: str) -> None:
        self.repo = repo
        self.session_id = session_id
        self.root = repo / ".kb" / "transaction"
        self.journal = repo / ".kb" / "transaction.json"
        self.wiki_next = self.root / "wiki.next"
        self.wiki_previous = self.root / "wiki.previous"
        self.vectors_next = self.root / "vectors.next"
        self.vectors_previous = self.root / "vectors.previous"
        self.vectors_failed = self.root / "vectors.failed"
        self.vectors_marker = self.root / "vectors.state.json"
        self.backup = self.root / "backup"
        self.staged = self.root / "staged"
        self._originals: dict[str, bool] = {}
        self._vectors_included = False
        self._vectors_original_existed = False
        self._vectors_switched = False

    def prepare(
        self,
        control_paths: list[str] | None = None,
        *,
        include_vectors: bool = False,
    ) -> None:
        if self.journal.exists():
            raise RuntimeError("unfinished knowledge-base transaction requires recovery")
        if self.root.exists():
            shutil.rmtree(self.root)
        self.wiki_next.mkdir(parents=True)
        self.backup.mkdir(parents=True)
        self.staged.mkdir(parents=True)
        self._vectors_included = include_vectors
        self._vectors_original_existed = (self.repo / ".kb" / "vectors").exists()
        self._vectors_switched = False
        if include_vectors:
            self.vectors_next.mkdir()
            self._write_vectors_marker()
        paths = ["index.md", ".kb/manifest.json", ".kb/pending.jsonl", *(control_paths or [])]
        for relative in dict.fromkeys(paths):
            source = self.repo / relative
            self._originals[relative] = source.exists()
            if source.exists():
                target = self.backup / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        self._write_journal("prepared")

    def discard_vectors(self) -> None:
        """Drop an unpromoted vector stage while keeping the core transaction active."""
        if self.vectors_previous.exists():
            raise RuntimeError("cannot discard vectors after cache promotion has started")
        if self.vectors_next.exists():
            shutil.rmtree(self.vectors_next)
            _fsync_directory(self.root)
        self._vectors_included = False
        self._vectors_switched = False
        if self.vectors_marker.exists():
            self.vectors_marker.unlink()
            _fsync_directory(self.root)
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

    def promote(self, *, vector_repo: Path | None = None) -> None:
        if self._vectors_included:
            from .vectors import VectorCacheError, vector_status

            state = vector_status(vector_repo or self.repo, destination=self.vectors_next)
            if not state.ready:
                raise VectorCacheError(
                    f"vector transaction destination is not ready: {state.status}: {state.reason}"
                )
        self._write_journal("promoting")
        if self._vectors_included:
            self._promote_vectors()
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
        self._rollback_vectors()
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
        if self.vectors_previous.exists() and not self._vectors_previous_marks_absence():
            from .vectors import vector_status

            if not vector_status(self.repo).ready:
                self._restore_previous_vectors()
        if self.root.exists():
            shutil.rmtree(self.root)
        if self.journal.exists():
            self.journal.unlink()
        temporary = self.journal.with_suffix(".json.tmp")
        if temporary.exists():
            temporary.unlink()

    def _write_journal(self, phase: str, *, commit: str | None = None) -> None:
        payload = {
            "schema": 2,
            "session_id": self.session_id,
            "phase": phase,
            "commit": commit,
            "originals": self._originals,
            "vectors": {
                "included": self._vectors_included,
                "original_existed": self._vectors_original_existed,
                "switched": self._vectors_switched,
            },
        }
        temporary = self.journal.with_suffix(".json.tmp")
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json_dumps(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.journal)
        _fsync_directory(self.journal.parent)

    def _write_vectors_marker(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.vectors_marker.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json_dumps({
                "included": self._vectors_included,
                "original_existed": self._vectors_original_existed,
                "switched": self._vectors_switched,
            }) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.vectors_marker)
        _fsync_directory(self.root)

    def _load_vectors_state(self, value: object) -> bool:
        if not isinstance(value, dict):
            return False
        keys = ("included", "original_existed", "switched")
        if any(not isinstance(value.get(key), bool) for key in keys):
            return False
        self._vectors_included = value["included"]
        self._vectors_original_existed = value["original_existed"]
        self._vectors_switched = value["switched"]
        return True

    def _load_vectors_marker(self) -> bool:
        marker_present = self.vectors_marker.exists()
        if not marker_present:
            self._infer_vectors_state(marker_present=False)
            return False
        try:
            if self._load_vectors_state(json.loads(self.vectors_marker.read_text(encoding="utf-8"))):
                return True
        except (OSError, json.JSONDecodeError):
            pass
        self._infer_vectors_state(marker_present=True)
        return False

    def _infer_vectors_state(self, *, marker_present: bool) -> None:
        vectors = self.repo / ".kb" / "vectors"
        has_next = self.vectors_next.exists()
        has_previous = self.vectors_previous.exists()
        previous_marks_absence = has_previous and self._vectors_previous_marks_absence()
        has_failed = self.vectors_failed.exists()
        has_live = vectors.exists()
        self._vectors_included = marker_present or has_next or has_previous or has_failed or has_live
        if not self._vectors_included:
            self._vectors_original_existed = False
            self._vectors_switched = False
            return
        if (has_previous and not previous_marks_absence) or has_failed:
            self._vectors_original_existed = True
        elif previous_marks_absence:
            self._vectors_original_existed = False
        elif has_next:
            self._vectors_original_existed = has_live
        elif not marker_present:
            self._vectors_original_existed = has_live
        else:
            self._vectors_original_existed = False
        self._vectors_switched = has_live and not has_next and not has_failed

    def _promote_vectors(self) -> None:
        from .vectors import VectorCacheError

        vectors = self.repo / ".kb" / "vectors"
        try:
            if vectors.exists():
                _replace_directory(vectors, self.vectors_previous)
            _replace_directory(self.vectors_next, vectors)
            if not self._vectors_original_existed:
                # Empty previous records that this transaction created the only live cache.
                self.vectors_previous.mkdir()
                _fsync_directory(self.root)
        except OSError as error:
            try:
                self._restore_failed_vector_swap()
            except OSError as restore_error:
                raise RuntimeError(
                    f"failed to promote vector cache and restore the previous cache: {restore_error}"
                ) from error
            self._vectors_switched = False
            raise VectorCacheError(f"failed to promote vector cache: {error}") from error
        self._vectors_switched = True
        try:
            self._write_vectors_marker()
            self._write_journal("promoting")
        except OSError as error:
            try:
                self._restore_failed_vector_swap()
            except OSError as restore_error:
                raise RuntimeError(
                    f"failed to record vector promotion and restore the previous cache: {restore_error}"
                ) from error
            self._vectors_switched = False
            raise VectorCacheError(f"failed to record vector promotion: {error}") from error

    def _restore_failed_vector_swap(self) -> None:
        vectors = self.repo / ".kb" / "vectors"
        if self._vectors_original_existed and self.vectors_previous.exists():
            if vectors.exists():
                if self.vectors_failed.exists():
                    shutil.rmtree(self.vectors_failed)
                    _fsync_directory(self.root)
                _replace_directory(vectors, self.vectors_failed)
            if not vectors.exists():
                _replace_directory(self.vectors_previous, vectors)
        elif not self._vectors_original_existed:
            if self.vectors_previous.exists():
                self.vectors_previous.rmdir()
                _fsync_directory(self.root)
            if vectors.exists():
                shutil.rmtree(vectors)
                _fsync_directory(vectors.parent)

    def _vectors_previous_marks_absence(self) -> bool:
        return self.vectors_previous.is_dir() and next(self.vectors_previous.iterdir(), None) is None

    def _restore_previous_vectors(self) -> None:
        vectors = self.repo / ".kb" / "vectors"
        if vectors.exists():
            if self.vectors_failed.exists():
                shutil.rmtree(self.vectors_failed)
                _fsync_directory(self.root)
            _replace_directory(vectors, self.vectors_failed)
        _replace_directory(self.vectors_previous, vectors)

    def _rollback_vectors(self) -> None:
        if not self._vectors_included:
            return
        vectors = self.repo / ".kb" / "vectors"
        if self._vectors_original_existed:
            if self.vectors_previous.exists():
                self._restore_previous_vectors()
        elif vectors.exists() and (self._vectors_switched or not self.vectors_next.exists()):
            shutil.rmtree(vectors)
            _fsync_directory(vectors.parent)


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
    if not tx._load_vectors_state(payload.get("vectors", {})):
        tx._load_vectors_marker()
    elif not tx._vectors_included and (
        tx.vectors_marker.exists()
        or tx.vectors_next.exists()
        or tx.vectors_previous.exists()
        or tx.vectors_failed.exists()
    ):
        tx._load_vectors_marker()
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
    tx._load_vectors_marker()

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
