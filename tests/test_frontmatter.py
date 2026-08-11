from __future__ import annotations

import unittest

from second_memory.frontmatter import dump_document, dump_mapping, parse_document, parse_mapping


class FrontmatterTest(unittest.TestCase):
    def test_json_values_round_trip(self) -> None:
        meta = {
            "schema": 2,
            "enabled": True,
            "ratio": 0.75,
            "aliases": ["第二记忆", "Second Memory"],
            "attrs": {"owner": "郑焕", "priority": 5},
            "empty": None,
        }
        self.assertEqual(meta, parse_mapping(dump_mapping(meta)))

    def test_document_preserves_body(self) -> None:
        text = dump_document({"id": "raw-1", "importance": 4}, "第一行\n第二行\n")
        meta, body = parse_document(text)
        self.assertEqual(4, meta["importance"])
        self.assertEqual("第一行\n第二行\n", body)


if __name__ == "__main__":
    unittest.main()
