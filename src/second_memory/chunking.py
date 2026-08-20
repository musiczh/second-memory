from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable, Sequence


HARD_MAXIMUM = 300
_STRUCTURAL_LINE = re.compile(r"^\s*(?:#{1,6}[ \t]+|[-*+] |\d+[.)] )")
_SENTENCE_END = set("。！？!?；;")


@dataclass(frozen=True)
class BodyAtom:
    index: int
    id: str
    start: int
    end: int
    text: str


def atomize_body(body: str) -> list[BodyAtom]:
    """Split a Raw body into stable, lossless atoms using code-point offsets."""
    spans: list[tuple[int, int, bool]] = []
    cursor = 0
    paragraph_start: int | None = None
    for line in body.splitlines(keepends=True):
        end = cursor + len(line)
        is_blank = not line.strip()
        is_structural = bool(_STRUCTURAL_LINE.match(line))
        if is_blank or is_structural:
            if paragraph_start is not None:
                spans.append((paragraph_start, cursor, False))
                paragraph_start = None
            spans.append((cursor, end, True))
        elif paragraph_start is None:
            paragraph_start = cursor
        cursor = end
    if paragraph_start is not None:
        spans.append((paragraph_start, cursor, False))
    if cursor < len(body):
        spans.append((cursor, len(body), False))

    chunks: list[tuple[int, int]] = []
    for start, end, structural in spans:
        chunks.extend(_hard_chunks(start, end) if structural else _sentence_chunks(body, start, end))
    return [_body_atom(index, body, start, end) for index, (start, end) in enumerate(chunks)]


def validate_body_groups(atoms: Sequence[BodyAtom], groups: object) -> list[list[str]]:
    """Validate ordered, complete atom coverage and return a normalized copy."""
    if not isinstance(groups, list) or not groups:
        raise ValueError("body_groups must be a non-empty array")
    expected = [atom.id for atom in atoms]
    normalized: list[list[str]] = []
    flattened: list[str] = []
    for group in groups:
        if not isinstance(group, list) or not group or any(not isinstance(atom_id, str) for atom_id in group):
            raise ValueError("body_groups must contain non-empty atom ID arrays")
        copied = list(group)
        normalized.append(copied)
        flattened.extend(copied)
    if flattened != expected:
        raise ValueError("body_groups must be continuous, ordered, and cover every atom exactly once")
    return normalized


def default_body_groups(
    atoms: Sequence[BodyAtom],
    target: int = 300,
    minimum: int = 50,
    maximum: int = 300,
) -> list[list[str]]:
    """Deterministically collect adjacent atoms without crossing the maximum."""
    if minimum < 1 or target < minimum or maximum < target:
        raise ValueError("invalid body group bounds")
    groups: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for atom in atoms:
        size = len(atom.text)
        if size > maximum:
            raise ValueError("atom exceeds body group maximum")
        if current and current_size + size > target:
            groups.append(current)
            current = []
            current_size = 0
        current.append(atom.id)
        current_size += size
    if current:
        groups.append(current)
    if len(groups) > 1:
        sizes = {atom.id: len(atom.text) for atom in atoms}
        last_size = sum(sizes[atom_id] for atom_id in groups[-1])
        previous_size = sum(sizes[atom_id] for atom_id in groups[-2])
        if last_size < minimum and previous_size + last_size <= maximum:
            groups[-2].extend(groups.pop())
    return groups


def sections_from_groups(
    body: str,
    atoms: Sequence[BodyAtom],
    groups: Sequence[Sequence[str]],
    *,
    source: str = "agent",
) -> list[dict[str, int | str]]:
    """Compile contiguous atom groups into body offset sections."""
    validated = validate_body_groups(atoms, list(map(list, groups)))
    by_id = {atom.id: atom for atom in atoms}
    sections: list[dict[str, int | str]] = []
    for group in validated:
        first = by_id[group[0]]
        last = by_id[group[-1]]
        if body[first.start:last.end] != "".join(by_id[atom_id].text for atom_id in group):
            raise ValueError("body group does not map to a contiguous body section")
        sections.append({"start": first.start, "end": last.end, "source": source})
    return sections


def annotation_hash(
    title: str,
    summary: str,
    summary_segments: Sequence[str],
    body_sections: Sequence[dict[str, int | str]],
) -> str:
    payload = {
        "title": title,
        "summary": summary,
        "summary_segments": list(summary_segments),
        "body_sections": list(body_sections),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sentence_chunks(body: str, start: int, end: int) -> Iterable[tuple[int, int]]:
    cursor = start
    for index in range(start, end):
        if body[index] in _SENTENCE_END:
            yield from _hard_chunks(cursor, index + 1)
            cursor = index + 1
    if cursor < end:
        yield from _hard_chunks(cursor, end)


def _hard_chunks(start: int, end: int) -> Iterable[tuple[int, int]]:
    for cursor in range(start, end, HARD_MAXIMUM):
        yield cursor, min(cursor + HARD_MAXIMUM, end)


def _body_atom(index: int, body: str, start: int, end: int) -> BodyAtom:
    text = body[start:end]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return BodyAtom(index=index, id=f"atom-{index}-{start}-{digest}", start=start, end=end, text=text)
