from __future__ import annotations

import json

from typer.testing import CliRunner

from second_memory.cli import app
from second_memory.compiler import load_manifest
from second_memory.store.git_store import GitStorage
from second_memory.tips import TIPS, next_tip

from tests.helpers import RepositoryTestCase


class TipsTest(RepositoryTestCase):
    def test_next_tip_is_deterministic_and_does_not_repeat(self) -> None:
        first, seen = next_tip([])
        second, seen = next_tip(seen)
        exhausted, final_seen = next_tip(seen)

        self.assertEqual(TIPS[0], first)
        self.assertEqual(TIPS[1], second)
        self.assertIsNone(exhausted)
        self.assertEqual(sorted(tip["id"] for tip in TIPS), final_seen)

    def test_successful_apply_hoists_tip_and_persists_seen_state_transactionally(self) -> None:
        runner = CliRunner()
        emitted_tips = []
        for index in range(3):
            self.add(
                f"提示状态记录 {index}",
                f"第 {index} 条独立记录用于验证一次性提示只随成功事务推进。",
                f"2026-08-{index + 1:02d}",
            )
            result = runner.invoke(
                app,
                ["compile", "--apply-response", "--stdin", "--repo", str(self.repo), "--json"],
                input=json.dumps(self.pending_plan(), ensure_ascii=False),
            )

            self.assertEqual(0, result.exit_code, result.output)
            payload = json.loads(result.stdout)
            self.assertNotIn("tip", payload["data"])
            emitted_tips.append(payload.get("tip"))

        self.assertEqual([TIPS[0], TIPS[1], None], emitted_tips)
        self.assertEqual(
            sorted(tip["id"] for tip in TIPS),
            load_manifest(self.repo)["tips_seen"],
        )
        self.assertEqual([], GitStorage(self.repo).status_porcelain())
