from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def rg_hits(repo: Path, query: str, limit: int = 20) -> list[dict[str, object]]:
    terms = [term for term in query.split() if term]
    if not terms:
        return []
    if shutil.which("rg"):
        hits: list[dict[str, object]] = []
        for term in terms:
            result = subprocess.run(
                ["rg", "--line-number", "--ignore-case", "--fixed-strings", term, "wiki", "raw", "index.md"],
                cwd=repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            for line in result.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) == 3:
                    hits.append({"path": parts[0], "line": int(parts[1]), "text": parts[2].strip()})
                if len(hits) >= limit:
                    return hits
        return hits
    return python_hits(repo, terms, limit)


def python_hits(repo: Path, terms: list[str], limit: int) -> list[dict[str, object]]:
    roots = [repo / "wiki", repo / "raw"]
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
