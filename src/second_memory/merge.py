from __future__ import annotations

from pathlib import Path
from typing import Any

from .compiler import list_compiled_pages, read_text
from .config import load_config
from .models import Page
from .promptio import llm_request, merge_response_schema


def build_merge_request(repo: Path) -> dict[str, Any] | None:
    """Build an LLM request that consolidates existing entity/topic pages.

    The knowledge base grows one page per distinct name, so over time the same
    person/project/concept accrues several pages under different ids, and topics
    fragment. This request hands the model every entity and topic page (with body)
    and asks it to merge co-referent entities, merge mergeable topics, and refine
    topic definitions to fit the accumulated content.

    Returns None when there are fewer than two pages, i.e. nothing to consolidate.
    """
    load_config(repo)
    pages = [page for page in list_compiled_pages(repo) if page.type in {"entity", "topic"}]
    if len(pages) < 2:
        return None

    context = {"pages": [page_payload(page) for page in pages]}
    instructions = (
        "请审视 pages 中的全部实体页与主题页,做一次实体与主题的合并与精炼。"
        "找出指代同一对象但 id 或名称不同的实体予以合并;归并可以合并的主题;"
        "并根据积累内容精炼主题定义(标题、摘要、正文、别名)。"
        "每组合并输出一个 canonical 页(op=upsert,全字段)与被吸收页 id 列表 absorbed:"
        "纯合并时 canonical 为保留页、absorbed 为被吸收页;仅精炼时 absorbed 为空;"
        "改名时 canonical 用新 id、absorbed 列出旧 id。"
        "absorbed 只能引用 pages 中给定的页面 id,且与 canonical 同类型;"
        "canonical.sources 必须并入所有被吸收页的 sources,不得丢失来源;"
        "只基于给定页面判断,不得臆造;没有可合并或可精炼的内容时输出空的 merges。"
    )
    return llm_request(
        "merge",
        read_text(repo / "AGENTS.md"),
        context,
        instructions,
        merge_response_schema(),
    )


def page_payload(page: Page) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": page.id,
        "type": page.type,
        "title": page.title,
        "aliases": page.aliases,
        "summary": page.summary,
        "sources": page.sources,
        "body": page.body,
    }
    if page.entity_kind:
        payload["entity_kind"] = page.entity_kind
    return payload
