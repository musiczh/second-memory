from __future__ import annotations

import unittest

from second_memory.chunking import (
    annotation_hash,
    atomize_body,
    default_body_groups,
    sections_from_groups,
    validate_body_groups,
)
from second_memory import frontmatter
from second_memory.compiler import apply_response, build_compile_request
from second_memory.utils import sha256_text

from tests.helpers import RepositoryTestCase, raw_annotation_fields


class BodyAtomizerTest(unittest.TestCase):
    def test_preserves_body_and_uses_markdown_then_sentence_boundaries(self) -> None:
        body = "# 标题。仍是标题\n\n第一段第一句。第二句！\n\n- 列表甲。仍是列表项\n- 列表乙\n"

        atoms = atomize_body(body)

        self.assertEqual(body, "".join(atom.text for atom in atoms))
        self.assertTrue(any(atom.text == "# 标题。仍是标题\n" for atom in atoms))
        self.assertTrue(any(atom.text == "第一段第一句。" for atom in atoms))
        self.assertTrue(any(atom.text == "第二句！" for atom in atoms))
        self.assertTrue(any(atom.text == "- 列表甲。仍是列表项\n" for atom in atoms))
        for index, atom in enumerate(atoms):
            self.assertEqual(body[atom.start:atom.end], atom.text)
            self.assertEqual(index, atom.index)
            self.assertIn(str(atom.start), atom.id)

    def test_hard_splits_an_atom_over_300_code_points(self) -> None:
        body = "甲" * 301

        atoms = atomize_body(body)

        self.assertEqual([300, 1], [len(atom.text) for atom in atoms])
        self.assertEqual(body, "".join(atom.text for atom in atoms))


class BodyGroupTest(unittest.TestCase):
    def test_validates_explicit_groups_and_builds_sections(self) -> None:
        body = "甲" * 60 + "。" + "乙" * 60 + "。"
        atoms = atomize_body(body)
        groups = [[atom.id for atom in atoms]]

        validated = validate_body_groups(atoms, groups)
        sections = sections_from_groups(body, atoms, validated, source="agent")

        self.assertEqual(groups, validated)
        self.assertEqual([{"start": 0, "end": len(body), "source": "agent"}], sections)
        self.assertEqual(body, body[sections[0]["start"]:sections[0]["end"]])

    def test_rejects_non_contiguous_groups_and_default_groups_cover_atoms(self) -> None:
        atoms = atomize_body("甲" * 80 + "。" + "乙" * 80 + "。" + "丙" * 80 + "。")

        with self.assertRaisesRegex(ValueError, "continuous|ordered|cover"):
            validate_body_groups(atoms, [[atoms[1].id], [atoms[0].id, atoms[2].id]])

        groups = default_body_groups(atoms)
        self.assertEqual([atom.id for atom in atoms], [atom_id for group in groups for atom_id in group])

    def test_default_groups_absorb_a_short_tail_when_the_maximum_allows_it(self) -> None:
        atoms = atomize_body("甲" * 240 + "。" + "乙" * 20 + "。")

        groups = default_body_groups(atoms, target=250, minimum=50, maximum=300)

        self.assertEqual([[atom.id for atom in atoms]], groups)

    def test_annotation_hash_is_stable_for_annotation_content(self) -> None:
        value = annotation_hash("标题", "甲" * 60, ["乙" * 50], [{"start": 0, "end": 50, "source": "agent"}])

        self.assertEqual(value, annotation_hash("标题", "甲" * 60, ["乙" * 50], [{"start": 0, "end": 50, "source": "agent"}]))
        self.assertNotEqual(value, annotation_hash("标题", "甲" * 61, ["乙" * 50], [{"start": 0, "end": 50, "source": "agent"}]))


class CompileSectionProtocolTest(RepositoryTestCase):
    backend = "plain"

    def test_compile_request_exposes_lossless_body_atoms(self) -> None:
        raw_id = self.add("切片协议", "# 标题\n\n第一段内容。\n\n- 列表内容\n", "2026-08-20")

        request = build_compile_request(self.repo, mode="incremental")
        entry = next(item for item in request["context"]["raw_entries"] if item["id"] == raw_id)

        self.assertEqual("2.5-raw-semantic-sections", request["context"]["contract_version"])
        self.assertEqual(entry["body"], "".join(atom["text"] for atom in entry["body_atoms"]))
        for atom in entry["body_atoms"]:
            self.assertEqual(entry["body"][atom["start"]:atom["end"]], atom["text"])

    def test_apply_persists_semantic_sections_without_changing_body_hash(self) -> None:
        body = "第一段说明了原料的具体经历，并且保留了后续回顾所需的事实。第二段补充了当前判断与来源边界。"
        raw_id = self.add("持久化切片", body, "2026-08-20")
        request = build_compile_request(self.repo, mode="incremental")
        entry = request["context"]["raw_entries"][0]
        plan = {
            "schema_version": 2,
            "session_id": request["context"]["session_id"],
            "mode": "incremental",
            "raw_annotations": [{
                "raw_id": raw_id,
                **raw_annotation_fields("持久化切片原料不生成耐久节点"),
                "importance": 1,
                "emotion": "",
                "mentions": [],
                "occurrences": [],
                "claims": [],
            }],
            "node_actions": [],
            "out_edges": [],
            "candidates": [],
            "consolidation_memo": request["context"]["consolidation_memo"],
        }
        raw_path = next((self.repo / "raw").rglob("*.md"))
        _, before_body = frontmatter.read_document(raw_path)

        apply_response(self.repo, plan, command="compile")

        meta, after_body = frontmatter.read_document(raw_path)
        self.assertEqual(sha256_text(before_body), sha256_text(after_body))
        self.assertEqual(plan["raw_annotations"][0]["summary_segments"], meta["summary_segments"])
        self.assertEqual(entry["body"], "".join(entry["body"][section["start"]:section["end"]] for section in meta["body_sections"]))
        self.assertTrue(all(set(section) == {"start", "end", "source"} for section in meta["body_sections"]))
