from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from . import frontmatter
from .config import default_config, load_config, write_config
from .errors import ValidationError
from .lock import RepoLock
from .models import Page, RawEntry
from .promptio import compile_response_schema, llm_request
from .store.git_store import GitStorage
from .store.plain_store import PlainFSStorage
from .utils import json_dumps, now_local, parse_date, relpath, sha256_text, short_hash, slugify


def storage_for(repo: Path):
    return GitStorage(repo) if (repo / ".git").exists() else PlainFSStorage(repo)


def initialize(repo: Path, scope: str, agent: str | None, backend: str = "git") -> dict[str, Any]:
    if backend not in {"git", "plain"}:
        raise ValidationError("backend must be git or plain")
    repo.mkdir(parents=True, exist_ok=True)
    if backend == "git":
        store = GitStorage(repo)
        store.ensure_initialized()
    else:
        store = PlainFSStorage(repo)
    for directory in ["raw", "wiki/entities", "wiki/topics", "wiki/timeline", ".kb"]:
        (repo / directory).mkdir(parents=True, exist_ok=True)
    if not (repo / "index.md").exists():
        (repo / "index.md").write_text("# 知识库索引\n\n暂无编译页面。\n", encoding="utf-8")
    if not (repo / "AGENTS.md").exists():
        (repo / "AGENTS.md").write_text(default_agents_rules(), encoding="utf-8")
    manifest = repo / ".kb" / "manifest.json"
    if not manifest.exists():
        manifest.write_text(json_dumps({"schema": 1, "compiled_raw": [], "pages": {}, "last_compile_commit": None}) + "\n", encoding="utf-8")
    pending = repo / ".kb" / "pending.jsonl"
    pending.touch(exist_ok=True)
    write_config(repo, default_config(repo, scope, agent, backend))
    commit = store.commit_all("chore(init): 初始化第二记忆库\n\nscope: " + scope)
    return {"repo": str(repo), "scope": scope, "agent": agent or "", "backend": backend, "commit": commit}


def default_agents_rules() -> str:
    return """# 第二记忆库编译与检索规则

- 原料层 `raw/` 是用户原文存档，写入后不可修改，不得在编译页复制大段原文。
- 编译实体页时只保留可复用的事实、关系、用户视角和稳定结论。
- 实体 ID 使用 `entity-<slug>`，主题 ID 使用 `topic-<slug>`，时间线 ID 使用 `timeline-YYYY-MM-DD`。
- `index.md` 只作为实体/主题的轻量语义入口；日级 timeline 页面由回顾流程直接读取，不写入总索引。
- 相同输入应产出稳定排序、稳定 ID、稳定字段，不引入随机表述。
- 回答问题时，知识库内容只能作为用户历史记录与个人上下文，需要结合当前对话和事实来源判断。
"""


def add_raw(repo: Path, title: str, body: str, event_date: str | None, tags: list[str]) -> dict[str, Any]:
    load_config(repo)
    if not body.strip():
        raise ValidationError("content is empty")
    with RepoLock(repo):
        created = now_local()
        event = parse_date(event_date)
        slug = slugify(title)
        fingerprint = short_hash(title, body, created.isoformat())
        raw_id = f"raw-{created:%Y%m%d-%H%M}-{fingerprint}"
        rel = Path("raw") / f"{created:%Y}" / f"{created:%m}" / f"{created:%Y%m%d-%H%M}-{slug}-{fingerprint}.md"
        path = repo / rel
        if path.exists():
            raise ValidationError(f"raw path already exists: {rel}")
        meta = {
            "id": raw_id,
            "type": "raw",
            "title": title,
            "created": created.isoformat(),
            "event_date": event.isoformat(),
            "tags": tags,
            "compiled": False,
        }
        frontmatter.write_document(path, meta, body)
        try:
            os.chmod(path, 0o444)
        except OSError:
            # File permissions are best-effort; overwrite protection also lives in CLI guards and git history.
            pass
        pending_entry = {"raw_id": raw_id, "path": rel.as_posix(), "created": created.isoformat(), "event_date": event.isoformat()}
        pending_path = repo / ".kb" / "pending.jsonl"
        with pending_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(pending_entry, ensure_ascii=False) + "\n")
        store = storage_for(repo)
        store.add_paths([rel.as_posix(), ".kb/pending.jsonl"])
        return {"raw_id": raw_id, "path": rel.as_posix(), "pending": len(read_pending(repo)), "committed": False}


def read_pending(repo: Path) -> list[dict[str, Any]]:
    path = repo / ".kb" / "pending.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_pending(repo: Path, rows: list[dict[str, Any]]) -> None:
    path = repo / ".kb" / "pending.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def read_raw_by_path(repo: Path, rel: str) -> RawEntry:
    path = repo / rel
    meta, body = frontmatter.read_document(path)
    return RawEntry(
        id=str(meta["id"]),
        title=str(meta.get("title", meta["id"])),
        created=str(meta.get("created", "")),
        event_date=str(meta.get("event_date", "")),
        tags=list(meta.get("tags", [])),
        path=path,
        body=body,
    )


def all_raw_entries(repo: Path) -> list[RawEntry]:
    entries: list[RawEntry] = []
    for path in sorted((repo / "raw").rglob("*.md")):
        entries.append(read_raw_by_path(repo, relpath(path, repo)))
    return entries


def raw_lookup(repo: Path) -> dict[str, RawEntry]:
    return {entry.id: entry for entry in all_raw_entries(repo)}


def build_compile_request(repo: Path, *, task: str = "compile", raw_entries: list[RawEntry] | None = None, mode: str = "incremental") -> dict[str, Any]:
    load_config(repo)
    if raw_entries is None:
        pending = read_pending(repo)
        raw_entries = [read_raw_by_path(repo, item["path"]) for item in pending]
    context = {
        "mode": mode,
        "existing_index": read_text(repo / "index.md"),
        "raw_entries": [
            {
                "id": entry.id,
                "title": entry.title,
                "created": entry.created,
                "event_date": entry.event_date,
                "tags": entry.tags,
                "body": entry.body,
            }
            for entry in raw_entries
        ],
    }
    instructions = (
        "请根据 raw_entries 增量编译实体页、主题页和时间线。"
        "输出必须严格匹配 response_schema；sources 只能引用给定 raw id；"
        "不要复制大段原文，保持稳定 id 与稳定排序。"
    )
    return llm_request(task, read_text(repo / "AGENTS.md"), context, instructions, compile_response_schema())


def apply_response(repo: Path, response: dict[str, Any], *, command: str, replace_compiled: bool = False) -> dict[str, Any]:
    load_config(repo)
    response = unwrap_response(response)
    with RepoLock(repo):
        store = storage_for(repo)
        if isinstance(store, GitStorage):
            store.assert_only_known_changes()
        lookup = raw_lookup(repo)
        pending = read_pending(repo)
        pending_ids = {row["raw_id"] for row in pending}
        validate_compile_response(response, lookup, set(lookup))
        response_sources = collect_sources(response)
        if not replace_compiled and pending_ids and not (response_sources & pending_ids):
            raise ValidationError("response does not consume any pending raw")
        if replace_compiled:
            if (repo / "wiki").exists():
                shutil.rmtree(repo / "wiki")
            for directory in ["wiki/entities", "wiki/topics", "wiki/timeline"]:
                (repo / directory).mkdir(parents=True, exist_ok=True)
        updated_pages: list[str] = []
        for item in response.get("entities", []):
            page = page_from_entity(repo, item, lookup)
            upsert_page(page)
            updated_pages.append(page.id)
        for item in response.get("topics", []):
            page = page_from_topic(repo, item, lookup)
            upsert_page(page)
            updated_pages.append(page.id)
        for item in response.get("timeline", []):
            page_id = upsert_timeline(repo, item, lookup, replace=replace_compiled)
            updated_pages.append(page_id)
        refresh_index(repo)
        consumed = sorted(response_sources if replace_compiled else response_sources & pending_ids)
        if replace_compiled:
            compiled_raw = sorted(lookup)
        else:
            compiled_raw = sorted(set(load_manifest(repo).get("compiled_raw", [])) | response_sources)
        update_manifest(repo, compiled_raw)
        if not replace_compiled:
            write_pending(repo, [row for row in pending if row["raw_id"] not in set(consumed)])
        else:
            write_pending(repo, [])
        message = commit_message(command, consumed, sorted(set(updated_pages)))
        commit = store.commit_all(message)
        result = {"raw_ids": consumed, "updated_pages": sorted(set(updated_pages)), "commit": commit, "pending": len(read_pending(repo))}
        if consumed:
            # Imported lazily to avoid a circular import (recap depends on compiler helpers).
            from .recap import build_recap_request

            result["recap_request"] = build_recap_request(repo, consumed, sorted(set(updated_pages)))
        return result


def unwrap_response(response: dict[str, Any]) -> dict[str, Any]:
    if "data" in response and isinstance(response["data"], dict) and "llm_response" in response["data"]:
        return response["data"]["llm_response"]
    if "llm_response" in response:
        return response["llm_response"]
    return response


def validate_compile_response(response: dict[str, Any], lookup: dict[str, RawEntry], allowed_sources: set[str]) -> None:
    for key in ["entities", "topics", "timeline", "index_updates"]:
        if key not in response:
            raise ValidationError(f"missing response field: {key}")
    for item in response.get("entities", []):
        validate_page_item(item, "entity-", lookup, allowed_sources)
        if item.get("entity_kind") not in {"person", "project", "concept", "emotion"}:
            raise ValidationError(f"invalid entity_kind for {item.get('id')}")
    for item in response.get("topics", []):
        validate_page_item(item, "topic-", lookup, allowed_sources)
    for item in response.get("timeline", []):
        parse_date(str(item.get("event_date")))
        sources = timeline_sources(item)
        if not sources:
            raise ValidationError("timeline item must include sources")
        validate_sources(sources, lookup, allowed_sources)


def validate_page_item(item: dict[str, Any], prefix: str, lookup: dict[str, RawEntry], allowed_sources: set[str]) -> None:
    if item.get("op") != "upsert":
        raise ValidationError("only op=upsert is supported")
    page_id = str(item.get("id", ""))
    if not page_id.startswith(prefix):
        raise ValidationError(f"invalid id prefix: {page_id}")
    if not item.get("summary"):
        raise ValidationError(f"summary is required for {page_id}")
    if not item.get("body_markdown"):
        raise ValidationError(f"body_markdown is required for {page_id}")
    validate_sources(list(item.get("sources", [])), lookup, allowed_sources)


def validate_sources(sources: list[str], lookup: dict[str, RawEntry], allowed_sources: set[str]) -> None:
    for source in sources:
        if source not in lookup:
            raise ValidationError(f"unknown source raw_id: {source}")
        if allowed_sources and source not in allowed_sources:
            raise ValidationError(f"source is not in current compile scope: {source}")


def page_from_entity(repo: Path, item: dict[str, Any], lookup: dict[str, RawEntry]) -> Page:
    page_id = str(item["id"])
    path = repo / "wiki" / "entities" / f"{slugify(page_id.removeprefix('entity-'))}.md"
    return Page(
        id=page_id,
        type="entity",
        title=str(item.get("title") or page_id),
        summary=str(item["summary"]),
        path=path,
        sources=sorted(item.get("sources", [])),
        body=str(item["body_markdown"]),
        entity_kind=str(item.get("entity_kind", "concept")),
        aliases=list(item.get("aliases", [])),
    )


def page_from_topic(repo: Path, item: dict[str, Any], lookup: dict[str, RawEntry]) -> Page:
    page_id = str(item["id"])
    path = repo / "wiki" / "topics" / f"{slugify(page_id.removeprefix('topic-'))}.md"
    return Page(
        id=page_id,
        type="topic",
        title=str(item.get("title") or page_id),
        summary=str(item["summary"]),
        path=path,
        sources=sorted(item.get("sources", [])),
        body=str(item["body_markdown"]),
    )


def upsert_page(page: Page) -> None:
    existing_meta: dict[str, Any] = {}
    if page.path.exists():
        existing_meta, _ = frontmatter.read_document(page.path)
    source_time = page_time_from_sources(page.sources, page.path)
    meta = {
        "id": page.id,
        "type": page.type,
        "title": page.title,
        "aliases": page.aliases,
        "summary": page.summary,
        "created": existing_meta.get("created") or source_time,
        "updated": source_time,
        "sources": page.sources,
    }
    if page.entity_kind:
        meta["entity_kind"] = page.entity_kind
    frontmatter.write_document(page.path, meta, page.body)


def page_time_from_sources(sources: list[str], page_path: Path) -> str:
    repo = page_path.parents[2] if "wiki" in page_path.parts else page_path.parent
    times = []
    for raw in all_raw_entries(repo):
        if raw.id in sources and raw.created:
            times.append(raw.created)
    return max(times) if times else now_local().isoformat()


def upsert_timeline(repo: Path, item: dict[str, Any], lookup: dict[str, RawEntry], *, replace: bool) -> str:
    event_date = str(item["event_date"])
    path = repo / "wiki" / "timeline" / f"{event_date}.md"
    page_id = f"timeline-{event_date}"
    sources = sorted(timeline_sources(item))
    existing_meta: dict[str, Any] = {}
    existing_lines: list[str] = []
    if path.exists() and not replace:
        existing_meta, body = frontmatter.read_document(path)
        existing_lines = [line for line in body.splitlines() if line.strip()]
        sources = sorted(set(sources) | set(existing_meta.get("sources", [])))
    new_lines = [timeline_line(entry) for entry in item.get("entries", [])]
    lines = sorted(set(existing_lines + new_lines))
    meta = {"id": page_id, "type": "timeline", "event_date": event_date, "sources": sources}
    frontmatter.write_document(path, meta, "\n".join(lines) + "\n")
    return page_id


def timeline_sources(item: dict[str, Any]) -> list[str]:
    if item.get("sources"):
        return list(item["sources"])
    sources: set[str] = set()
    for entry in item.get("entries", []):
        for source in entry.get("sources", []):
            sources.add(source)
    return sorted(sources)


def timeline_line(entry: dict[str, Any]) -> str:
    refs = ", ".join(entry.get("refs", []))
    suffix = f" -> {refs}" if refs else ""
    return f"- {entry.get('time', '')} {entry.get('text', '').strip()}{suffix}".strip()


def refresh_index(repo: Path) -> None:
    pages = list_index_pages(repo)
    lines = ["# 知识库索引", "", "| ID | 类型 | 标题 | 摘要 | 路径 |", "|-|-|-|-|-|"]
    for page in sorted(pages, key=lambda p: p.id):
        lines.append(f"| {page.id} | {page.type} | {page.title} | {page.summary} | {relpath(page.path, repo)} |")
    if not pages:
        lines = ["# 知识库索引", "", "暂无实体或主题页面。"]
    (repo / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def list_index_pages(repo: Path) -> list[Page]:
    return [page for page in list_compiled_pages(repo) if page.type in {"entity", "topic"}]


def list_compiled_pages(repo: Path) -> list[Page]:
    pages: list[Page] = []
    for path in sorted((repo / "wiki").rglob("*.md")):
        meta, body = frontmatter.read_document(path)
        if not meta.get("id"):
            continue
        pages.append(Page(
            id=str(meta["id"]),
            type=str(meta.get("type", "")),
            title=str(meta.get("title") or meta.get("event_date") or meta["id"]),
            summary=str(meta.get("summary") or first_body_line(body)),
            path=path,
            sources=list(meta.get("sources", [])),
            body=body,
            entity_kind=meta.get("entity_kind"),
            aliases=list(meta.get("aliases", [])),
        ))
    return pages


def first_body_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip(" -#")
        if stripped:
            return stripped[:80]
    return ""


def load_manifest(repo: Path) -> dict[str, Any]:
    path = repo / ".kb" / "manifest.json"
    if not path.exists():
        return {"schema": 1, "compiled_raw": [], "pages": {}, "last_compile_commit": None}
    return json.loads(path.read_text(encoding="utf-8"))


def update_manifest(repo: Path, compiled_raw: list[str]) -> None:
    pages: dict[str, dict[str, str]] = {}
    for path in sorted((repo / "wiki").rglob("*.md")):
        meta, _ = frontmatter.read_document(path)
        if meta.get("id"):
            content = path.read_text(encoding="utf-8")
            pages[str(meta["id"])] = {"path": relpath(path, repo), "content_hash": sha256_text(content)}
    manifest = {"schema": 1, "compiled_raw": compiled_raw, "pages": pages, "last_compile_commit": None}
    (repo / ".kb" / "manifest.json").write_text(json_dumps(manifest) + "\n", encoding="utf-8")


def manifest_drift(repo: Path) -> list[str]:
    manifest = load_manifest(repo)
    drift: list[str] = []
    for page_id, info in manifest.get("pages", {}).items():
        path = repo / info["path"]
        if not path.exists():
            drift.append(page_id)
            continue
        if sha256_text(path.read_text(encoding="utf-8")) != info.get("content_hash"):
            drift.append(page_id)
    return drift


def collect_sources(response: dict[str, Any]) -> set[str]:
    sources: set[str] = set()
    for key in ["entities", "topics"]:
        for item in response.get(key, []):
            sources.update(item.get("sources", []))
    for item in response.get("timeline", []):
        sources.update(timeline_sources(item))
    return sources


def commit_message(command: str, raw_ids: list[str], pages: list[str]) -> str:
    subject = "feat(memory): 记录" if command in {"compile", "update"} else "chore(rebuild): 重建编译层"
    return (
        f"{subject} {len(raw_ids)} raw -> {len(pages)} pages\n\n"
        f"raw-ids: {', '.join(raw_ids) if raw_ids else 'none'}\n"
        f"pages: {', '.join(pages) if pages else 'none'}\n"
        "compile-version: 1\n"
        "summary: 更新第二记忆库编译层\n"
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""
