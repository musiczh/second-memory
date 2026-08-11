from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from second_memory import cli as cli_module
from second_memory.cli import app
from second_memory.compiler import load_manifest
from second_memory.errors import SecondMemoryError
from second_memory.store.git_store import GitStorage
from second_memory.utils import json_dumps
from tests.helpers import RepositoryTestCase


CURRENT_CODE_UPDATE = {
    "attempted": True,
    "ok": True,
    "updated": False,
    "before": "current-code",
    "after": "current-code",
    "target": "current-code",
    "remote_branch": "origin/master",
    "message": "Already up to date.",
}
UPDATED_CODE_UPDATE = {
    **CURRENT_CODE_UPDATE,
    "updated": True,
    "before": "previous-code",
}


class GitCodeUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="second-memory-code-update-")
        root = Path(self._temporary.name)
        self.remote = root / "origin.git"
        self.seed = root / "seed"
        self.installed = root / "installed"
        self._run("git", "init", "--bare", "--initial-branch=master", str(self.remote))
        self._run("git", "init", "--initial-branch=master", str(self.seed))
        self._run("git", "-C", str(self.seed), "config", "user.name", "Test")
        self._run("git", "-C", str(self.seed), "config", "user.email", "test@example.com")
        (self.seed / "version.txt").write_text("one\n", encoding="utf-8")
        self._run("git", "-C", str(self.seed), "add", "version.txt")
        self._run("git", "-C", str(self.seed), "commit", "-m", "initial")
        self._run("git", "-C", str(self.seed), "remote", "add", "origin", str(self.remote))
        self._run("git", "-C", str(self.seed), "push", "-u", "origin", "master")
        self._run("git", "clone", str(self.remote), str(self.installed))
        self._run("git", "-C", str(self.installed), "switch", "-c", "installed-feature")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    @staticmethod
    def _run(*args: str) -> str:
        result = subprocess.run(args, text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def _advance_master(self) -> str:
        (self.seed / "version.txt").write_text("two\n", encoding="utf-8")
        self._run("git", "-C", str(self.seed), "add", "version.txt")
        self._run("git", "-C", str(self.seed), "commit", "-m", "advance master")
        self._run("git", "-C", str(self.seed), "push", "origin", "master")
        return self._run("git", "-C", str(self.seed), "rev-parse", "HEAD")

    def test_pull_ff_targets_origin_master_even_from_another_local_branch(self) -> None:
        target = self._advance_master()

        result = GitStorage(self.installed).pull_ff("origin", "master")

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["updated"])
        self.assertEqual(target, result["after"])
        self.assertEqual(target, result["target"])
        self.assertEqual("origin/master", result["remote_branch"])
        self.assertEqual("installed-feature", self._run("git", "-C", str(self.installed), "branch", "--show-current"))

    def test_pull_ff_rejects_local_head_that_is_not_exact_remote_master(self) -> None:
        self._run("git", "-C", str(self.installed), "config", "user.name", "Test")
        self._run("git", "-C", str(self.installed), "config", "user.email", "test@example.com")
        (self.installed / "local.txt").write_text("local\n", encoding="utf-8")
        self._run("git", "-C", str(self.installed), "add", "local.txt")
        self._run("git", "-C", str(self.installed), "commit", "-m", "local commit")

        result = GitStorage(self.installed).pull_ff("origin", "master")

        self.assertFalse(result["ok"])
        self.assertFalse(result["updated"])
        self.assertNotEqual(result["after"], result["target"])
        self.assertIn("does not match origin/master", result["message"])


class SkillCodeSyncTest(unittest.TestCase):
    def test_updated_code_reexecs_once_with_carried_result(self) -> None:
        updated = {**CURRENT_CODE_UPDATE, "updated": True, "before": "old", "after": "new", "target": "new"}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(cli_module._REEXEC_PULL_ENV, None)
            with patch("second_memory.cli.pull_skill_code", return_value=updated), patch(
                "second_memory.cli.os.execve", side_effect=RuntimeError("reexec")
            ) as execve:
                with self.assertRaisesRegex(RuntimeError, "reexec"):
                    cli_module.sync_skill_code()

        executable, argv, env = execve.call_args.args
        self.assertEqual(cli_module.sys.executable, executable)
        self.assertEqual([cli_module.sys.executable, "-m", "second_memory.cli"], argv[:3])
        self.assertEqual(updated, json.loads(env[cli_module._REEXEC_PULL_ENV]))

    def test_carried_result_skips_second_pull(self) -> None:
        with patch.dict(os.environ, {cli_module._REEXEC_PULL_ENV: json_dumps(CURRENT_CODE_UPDATE)}):
            with patch("second_memory.cli.pull_skill_code") as pull:
                result = cli_module.sync_skill_code()

        self.assertEqual(CURRENT_CODE_UPDATE, result)
        pull.assert_not_called()

    def test_failed_master_sync_blocks_update_decision(self) -> None:
        failed = {**CURRENT_CODE_UPDATE, "ok": False, "message": "diverged from origin/master"}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(cli_module._REEXEC_PULL_ENV, None)
            with patch("second_memory.cli.pull_skill_code", return_value=failed):
                with self.assertRaises(SecondMemoryError) as raised:
                    cli_module.sync_skill_code()

        self.assertEqual("code_update_failed", raised.exception.code)
        self.assertIn("diverged", str(raised.exception))


class UpdateVersionRoutingTest(RepositoryTestCase):
    @staticmethod
    def _env() -> dict[str, str]:
        return {cli_module._REEXEC_PULL_ENV: json_dumps(UPDATED_CODE_UPDATE)}

    def test_matching_master_and_database_version_does_not_rebuild(self) -> None:
        result = CliRunner().invoke(
            app,
            ["update", "--emit-request", "--repo", str(self.repo), "--json"],
            env=self._env(),
        )

        self.assertEqual(0, result.exit_code, result.output)
        data = json.loads(result.stdout)["data"]
        self.assertEqual("noop", data["mode"])
        self.assertFalse(data["version_changed"])
        self.assertTrue(data["code_update"]["updated"])
        self.assertEqual(UPDATED_CODE_UPDATE, data["code_update"])

    def test_database_version_mismatch_routes_to_rebuild(self) -> None:
        manifest_path = self.repo / ".kb" / "manifest.json"
        manifest = load_manifest(self.repo)
        manifest["kb_version"] = "0.0.0"
        manifest_path.write_text(json_dumps(manifest) + "\n", encoding="utf-8")

        result = CliRunner().invoke(
            app,
            ["update", "--emit-request", "--repo", str(self.repo), "--json"],
            env=self._env(),
        )

        self.assertEqual(0, result.exit_code, result.output)
        data = json.loads(result.stdout)["data"]
        self.assertEqual("rebuild", data["update_mode"])
        self.assertTrue(data["version_changed"])

    def test_code_sync_failure_stops_before_database_mode_check(self) -> None:
        runner = CliRunner()
        with patch(
            "second_memory.cli.sync_skill_code",
            side_effect=SecondMemoryError("pull failed", "code_update_failed"),
        ), patch("second_memory.cli.determine_update_mode") as determine:
            result = runner.invoke(
                app,
                ["update", "--emit-request", "--repo", str(self.repo), "--json"],
            )

        self.assertEqual(1, result.exit_code)
        payload = json.loads(result.stderr)
        self.assertEqual("code_update_failed", payload["error"]["code"])
        determine.assert_not_called()
