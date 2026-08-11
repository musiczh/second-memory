from __future__ import annotations

import unittest
from pathlib import Path

from second_memory.models import Node
from second_memory.resolver import Resolver, deterministic_node_id, normalize_name


class ResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.node = Node(
            id="entity-second-memory",
            type="entity",
            title="Second Memory",
            summary="个人第二记忆项目",
            path=Path("wiki/entities/entity-second-memory.md"),
            aliases=["第二记忆"],
            entity_kind="project",
        )
        self.resolver = Resolver([self.node])

    def test_resolution_order(self) -> None:
        self.assertEqual("exact_id", self.resolver.resolve("entity-second-memory")[0]["match"])
        self.assertEqual("title_or_alias", self.resolver.resolve("Ｓｅｃｏｎｄ　Ｍｅｍｏｒｙ")[0]["match"])
        self.assertEqual("title_or_alias", self.resolver.resolve("第二记忆")[0]["match"])
        self.assertEqual("lexical", self.resolver.resolve("个人项目")[0]["match"])

    def test_collision_suffix_is_deterministic(self) -> None:
        occupied = {"statement-技术选型"}
        first = deterministic_node_id("statement", "技术选型", "raw-1", occupied)
        second = deterministic_node_id("statement", "技术选型", "raw-1", occupied)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("statement-技术选型-"))
        third = deterministic_node_id("statement", "技术选型", "raw-1", {*occupied, first})
        self.assertEqual(f"{first}-2", third)

    def test_nfkc_normalization(self) -> None:
        self.assertEqual(normalize_name("Ｓｅｃｏｎｄ　Ｍｅｍｏｒｙ"), "second memory")


if __name__ == "__main__":
    unittest.main()
