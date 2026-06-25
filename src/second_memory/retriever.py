from __future__ import annotations

from pathlib import Path
from typing import Any

from .compiler import list_index_pages, read_text
from .config import load_config
from .promptio import llm_request, search_response_schema
from .search import rg_hits


def search_level1(repo: Path, query: str) -> dict[str, Any]:
    load_config(repo)
    terms = [term.lower() for term in query.split() if term]
    candidates = []
    for page in list_index_pages(repo):
        haystack = " ".join([page.id, page.title, page.summary, " ".join(page.aliases), page.body]).lower()
        score = sum(3 if term in page.title.lower() else 1 for term in terms if term in haystack)
        if score:
            candidates.append({
                "id": page.id,
                "type": page.type,
                "title": page.title,
                "summary": page.summary,
                "path": page.path.relative_to(repo).as_posix(),
                "score": score,
                "sources": page.sources,
            })
    candidates.sort(key=lambda item: (-int(item["score"]), str(item["id"])))
    return {"query": query, "candidates": candidates[:10], "hits": rg_hits(repo, query)}


def search_level2_request(repo: Path, query: str) -> dict[str, Any]:
    l1 = search_level1(repo, query)
    pages = []
    for candidate in l1["candidates"][:5]:
        path = repo / str(candidate["path"])
        pages.append({**candidate, "body": read_text(path)})
    instructions = "请只基于 candidate_pages 与必要命中片段，归纳这些历史记录能为当前问题提供的个人上下文。"
    return llm_request(
        "search_l2",
        read_text(repo / "AGENTS.md"),
        {"query": query, "level1": l1, "candidate_pages": pages},
        instructions,
        search_response_schema(),
    )
