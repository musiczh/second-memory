from __future__ import annotations

import shutil
import subprocess
import re
from pathlib import Path


QUERY_STOPWORDS = {
    "为什么",
    "什么",
    "怎么",
    "如何",
    "认为",
    "有关",
    "带来",
    "我的",
    "是否",
}


def query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for chunk in query_chunks(query):
        if "\u4e00" <= chunk[0] <= "\u9fff" and len(chunk) >= 4:
            terms.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
        else:
            terms.append(chunk)
    return list(dict.fromkeys(term for term in terms if term not in QUERY_STOPWORDS))


def query_chunks(query: str) -> list[str]:
    chunks: list[str] = []
    for chunk in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*|[\u4e00-\u9fff]+", query.lower()):
        if chunk in QUERY_STOPWORDS:
            continue
        chunks.append(chunk)
    return list(dict.fromkeys(chunks))


def phrase_coverage_bonus(query: str, title: str, haystack: str) -> int:
    title = title.lower()
    haystack = haystack.lower()
    bonus = 0
    for chunk in query_chunks(query):
        if not ("\u4e00" <= chunk[0] <= "\u9fff") or len(chunk) < 4:
            continue
        pairs = [chunk[index : index + 2] for index in range(len(chunk) - 1)]
        title_matches = sum(pair in title for pair in pairs)
        haystack_matches = sum(pair in haystack for pair in pairs)
        if title_matches >= 2 and title_matches * 2 >= len(pairs):
            bonus += 4
        elif haystack_matches >= 2 and haystack_matches * 2 >= len(pairs):
            bonus += 2
    return bonus


def rg_hits(repo: Path, query: str, limit: int = 20, *, include_raw: bool = False) -> list[dict[str, object]]:
    terms = query_terms(query)
    if not terms:
        return []
    if shutil.which("rg"):
        hits: list[dict[str, object]] = []
        seen: set[tuple[str, int]] = set()
        patterns = [value for term in terms for value in ("-e", term)]
        result = subprocess.run(
            [
                "rg",
                "--with-filename",
                "--line-number",
                "--ignore-case",
                "--fixed-strings",
                *patterns,
                *(["raw"] if include_raw else []),
                "index.md",
            ],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        for line in result.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                key = (parts[0], int(parts[1]))
                if key in seen:
                    continue
                seen.add(key)
                hits.append({"path": key[0], "line": key[1], "text": parts[2].strip()})
            if len(hits) >= limit:
                return hits
        return hits
    return python_hits(repo, terms, limit, include_raw=include_raw)


def python_hits(repo: Path, terms: list[str], limit: int, *, include_raw: bool = False) -> list[dict[str, object]]:
    roots = []
    if include_raw:
        roots.append(repo / "raw")
    files = [repo / "index.md"]
    for root in roots:
        if root.exists():
            files.extend(root.rglob("*.md"))
    hits: list[dict[str, object]] = []
    lowered = [term.lower() for term in terms]
    for path in files:
        if not path.exists():
            continue
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            text = line.lower()
            if any(term in text for term in lowered):
                hits.append({"path": path.relative_to(repo).as_posix(), "line": idx, "text": line.strip()})
                if len(hits) >= limit:
                    return hits
    return hits
