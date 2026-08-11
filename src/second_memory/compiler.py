from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import frontmatter
from .config import KB_VERSION, default_config, load_config, skill_repo_root, write_config
from .errors import StaleSessionError, ValidationError
from .graph import (
    apply_node_actions,
    compiler_policy_detail_node_ids,
    cross_node_duplicate_detail_groups,
    cross_node_duplicate_detail_node_ids,
    event_contract_issues,
    first_body_line,
    load_nodes,
    nonspecific_shared_entity_evidence_node_ids,
    node_content_quality_issues,
    render_graph,
    resolve_edges,
)
from .lock import RepoLock
from .models import (
    ENTITY_KINDS,
    EVENT_BASES,
    EVENT_CONFIDENCE_THRESHOLD,
    EVENT_FACTUALITIES,
    EVENT_STATUSES,
    EVENT_SUBJECT_ROLES,
    EVENT_TIME_PRECISIONS,
    NODE_ACTIONS,
    NODE_TYPES,
    PLAN_MODES,
    CompilePlan,
    Node,
    RawEntry,
)
from .promptio import compile_response_schema, llm_request
from .resolver import Resolver, normalize_name
from .store.git_store import GitStorage
from .store.plain_store import PlainFSStorage
from .tips import next_tip
from .transaction import KnowledgeTransaction, recover_transaction, transaction_state
from .topics import validate_materialized_topic_contracts, validate_topic_plan
from .utils import json_dumps, now_local, parse_date, parse_temporal_anchor, relpath, sha256_text, short_hash, slugify

CONSOLIDATION_BATCH_SIZE = 10
REBUILD_WORKSPACE_NAME = ".second-memory-rebuild-workspace-v2"
RAW_COMPILED_FIELDS = {"compiled", "summary", "importance", "emotion", "mentions", "occurrences", "claims", "belongs_to"}
DEFAULT_GITIGNORE = ".kb/lock\n.kb/transaction/\n.kb/transaction.json\n.kb/transaction.json.tmp\n"
REBUILD_CONTROL_PATHS = [".gitignore", "AGENTS.md", ".kb/config.yaml"]


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
    for directory in ["raw", "wiki/entities", "wiki/events", "wiki/statements", "wiki/topics", "wiki/timeline", ".kb"]:
        (repo / directory).mkdir(parents=True, exist_ok=True)
    if not (repo / "index.md").exists():
        (repo / "index.md").write_text("# 知识库索引\n\n暂无编译节点。\n", encoding="utf-8")
    if not (repo / "AGENTS.md").exists():
        (repo / "AGENTS.md").write_text(default_agents_rules(), encoding="utf-8")
    if not (repo / ".gitignore").exists():
        (repo / ".gitignore").write_text(DEFAULT_GITIGNORE, encoding="utf-8")
    manifest = repo / ".kb" / "manifest.json"
    if not manifest.exists():
        manifest.write_text(json_dumps(empty_manifest()) + "\n", encoding="utf-8")
    pending = repo / ".kb" / "pending.jsonl"
    pending.touch(exist_ok=True)
    write_config(repo, default_config(repo, scope, agent, backend))
    commit = store.commit_paths(
        "chore(init): 初始化第二记忆库\n\nscope: " + scope,
        [".gitignore", "AGENTS.md", "index.md", ".kb/config.yaml", ".kb/manifest.json", ".kb/pending.jsonl"],
    )
    return {"repo": str(repo), "scope": scope, "agent": agent or "", "backend": backend, "commit": commit}


def empty_manifest() -> dict[str, Any]:
    return {
        "schema": 2,
        "kb_version": KB_VERSION,
        "compiled_raw": [],
        "raw_hashes": {},
        "pages": {},
        "edges": [],
        "redirects": {},
        "candidates": [],
        "consolidation": {"pending_raw": [], "memo": "", "last_session_id": None},
        "tips_seen": [],
        "rebuild": {"phase": "idle", "ordered_raw_ids": [], "cursor": 0, "total": 0, "generation": None, "last_session_id": None},
        "applied_session_id": None,
    }


def default_agents_rules() -> str:
    return """# 第二记忆库 v2.4 编译与检索规则

- `raw/` 保存用户原文。正文哈希不可变；CLI 只可写入摘要、重要度、情绪和由边推导的 `belongs_to` 元数据。
- 图谱节点只有 entity、event、statement、topic。产品层把 statement 称为「洞察」。
- entity 是纯指代锚点，覆盖人、组织、地点、作品、产品、工具、项目、任务、对象、概念和情绪；认知、方法与关系模式不得伪装成实体。
- entity、event、statement 独立抽取；同一 raw 可以同时贡献多类节点。
- event 只记录值得进入时间线的发生事实，而不是带日期的洞察。删除反思、解释、情绪标签与结论后，必须仍有用户做过、参加、完成、遭遇、交易或被实质影响的一件有边界的事。
- event 必须声明 event_basis=appointment|scheduled_commitment|incident|milestone|transaction|material_change 和来源约束的 standalone_reason。planned 只允许明确排期的会面、承诺或里程碑；basis 必须由可观察动作支撑，不能把普通活动自报为 incident。
- event 标题和 semantics.action 必须是同一条只描述发生事实的短语；觉察、识别、反思、重构、复盘、理解、整合、思考、捕捉、感悟、发现自己的模式、收到启发、发生认知改变等结果拆成洞察。“发生／收到／遇到”不是 incident 的正向事实锚点。普通聊天、普通阅读、短暂感受、自我观察、一般决定和行为模式不得因带日期成为 event；“项目计划会”中的计划是名词，不得误杀真实参会事件。
- statement 记录可演进的决策、偏好、目标、信念、计划、感受、方法与洞察；「AI 协作」属于洞察而不是实体。
- 每个 create 或实质更新 action 必须携带 `content`，包含 summary、detail、key_points、evidence、uncertainties；detail 至少四个实质句，并按节点类型使用固定的两段标签：entity“对象与关系／历史与现状”、event“发生与背景／结果与关联”、statement“洞察与依据／演进与影响”、topic“组织视角／脉络与边界”。detail 必须明确覆盖 summary 的中心概念；至少三个不重复且不少于 8 个字的关键点，其中至少两条复用中心概念。按节点类型综合全部有效来源，每句都要由该节点自己的 source/evidence/语义历史支撑，规范化达到 24 字的非 evidence 句不得跨节点精确复用，也不得写入“当前节点只确认／节点仅保留／节点不把／后续若出现新的实质信息／后续实质变化需要”等编译政策填充；必要短术语和 evidence 原文可重复。每条 evidence 必须引用有效 raw；同一 claim 用于多个 entity 时必须直接点名每个实体的 title 或 alias，不得把只描述其中一个实体的 claim 复制给其他实体。
- 每条 raw annotation 必须分别给出 mentions、occurrences、claims 数组，空数组有效但不得省略，以便审计三条抽取通道。
- 引用某条 raw 的 entity、event、statement action 必须分别由该 raw 非空的 mentions、occurrences、claims 支撑；belongs_to 的目标必须是同一条带该 raw source_id 的节点动作。source-only reinforce 仅用于 incremental／rebuild replay；使用前必须比较新来源与节点完整内容，只有纯重复提及、不会新增历史、推翻旧不确定性、改变综合或让详情过期时才可只返回 target_id、type、source_ids（不接受 sources 别名），否则必须完整 refine 并综合全部新旧来源。
- 每个耐久实体 mention 都必须解析或创建，并通过 belongs_to 留下直接来源；event／statement 经明确 involves／about／instance_of 指向实体时，它们的来源形成实体关联来源。不得用关键词相似推导来源。
- 三个抽取通道均为空时允许零节点、零 belongs_to 完成编译；不得为了挂靠 raw 制造微小事件或空泛洞察。所有成功编译 Raw 都进入 Consolidation 计数。
- event 时间必须是合法 ISO 日期或时间；minute 需要 datetime，日级及更粗精度使用 date-only，range 必须提供不早于开始时间的 ended_at。
- 增量编译和 rebuild 重放不得创建 topic，也不得执行 merge/split，只能产生候选。topic 仅由 consolidate 或 topics refresh 创建，且是全库洞察之上的组织视角，不是局部批次摘要或更大的洞察。
- topic 至少 contains 五个直属成员、两个 statement、两个 facet 和三个独立 raw capture；成员可为 raw、entity、event、statement 或 child topic，最大深度三。每个成员都要有 facet、包含准确 facet 名称的具体归属理由和从成员自身内容复制的 supporting_excerpt；成员自己的 content/evidence 必须直接回答 organizing_question 并支持该 facet，不得靠 rationale 发明桥接，禁止用非洞察成员凑数。
- topic 还必须返回 attrs.topic_reading，结构化表达 core_understanding、带来源的 evolution、真实 contradictions、证据约束的 open_questions 与 confidence。矛盾和开放问题没有证据时使用空数组，不得编造。
- 高维不等于跨域。AI 协作等长期领域可形成 life_domain，睡眠等长期变化可形成 longitudinal_arc；只有同一机制确实跨领域复现才使用 cross_domain_pattern。每十条成功编译 Raw 触发一次全库主题审计，重复讨论簇必须物化或写入稳定 topic 候选状态。
- 允许洞察暂时不属于任何 topic；共同日期、情绪、批次、泛化词或事后补写的桥接句不能证明主题成立。主题实质更新使用 membership_mode=replace，完整替换旧成员。
- 逐成员检查成员自身可用注解、current_state、content、semantics 与 evolution 是否独立贡献 organizing_question；不得让 rationale 引入成员没有的领域语义，也不得用通用内容替换被拒绝的边缘成员。移除不合格成员后不达门槛时删除或暂缓主题。
- topics 全量刷新必须反向检查全库成员和已有候选。局部 exclusion 说明边界，稳定 topic candidate 记录尚未成熟或被拒绝的重复讨论簇；候选不得因一次响应遗漏而消失。
- 新节点使用 plan-local ref，最终稳定 ID 和路径由 CLI 生成；已有节点必须使用 `target_id`。
- raw 通过 `belongs_to` 指向编译节点；topic 通过 `contains` 组织主题；对称关系由 CLI 规范化排序。
- statement 的 evolution 只追加，event 的状态变化保留历史；sources 永远做并集，不得丢失来源。
- incremental／rebuild 的 statement action 不得返回 evolution，只提供 current_state 与 effective_date；CLI 负责确定性追加历史。
- `index.md` 和 timeline 是图谱投影，不由 Agent 直接编写。timeline 只包含 event；洞察 evolution 在节点详情中独立展示。
- 检索先读取 index；只有需要深层上下文时才加载候选节点，不得发送整个 raw 归档。
- 输出必须严格匹配 CompilePlan v2.4（顶层 schema_version 仍为 2），并原样返回请求中的 schema_version、session_id 与 mode。
- 知识库内容只能作为用户历史记录和个人上下文，不能替代外部事实来源。
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
        relative = Path("raw") / f"{created:%Y}" / f"{created:%m}" / f"{created:%Y%m%d-%H%M}-{slug}-{fingerprint}.md"
        path = repo / relative
        if path.exists():
            raise ValidationError(f"raw path already exists: {relative}")
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
        set_readonly(path)
        pending_entry = {"raw_id": raw_id, "path": relative.as_posix(), "created": created.isoformat(), "event_date": event.isoformat()}
        pending_path = repo / ".kb" / "pending.jsonl"
        with pending_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(pending_entry, ensure_ascii=False) + "\n")
        return {"raw_id": raw_id, "path": relative.as_posix(), "pending": len(read_pending(repo)), "committed": False}


def read_pending(repo: Path) -> list[dict[str, Any]]:
    path = repo / ".kb" / "pending.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def read_raw_by_path(repo: Path, relative: str) -> RawEntry:
    path = repo / relative
    meta, body = frontmatter.read_document(path)
    return RawEntry(
        id=str(meta["id"]),
        title=str(meta.get("title", meta["id"])),
        created=str(meta.get("created", "")),
        event_date=str(meta.get("event_date", "")),
        tags=list(meta.get("tags", [])),
        path=path,
        body=body,
        annotations={
            key: meta[key]
            for key in ["summary", "importance", "emotion", "mentions", "occurrences", "claims", "belongs_to"]
            if key in meta
        },
    )


def all_raw_entries(repo: Path) -> list[RawEntry]:
    root = repo / "raw"
    if not root.exists():
        return []
    return [read_raw_by_path(repo, relpath(path, repo)) for path in sorted(root.rglob("*.md"))]


def ordered_raw_entries(repo: Path) -> list[RawEntry]:
    """Return raw entries in their original intake order.

    ``created`` is the durable ingestion timestamp. ``event_date`` describes
    when the remembered event happened and must not reorder the intake stream.
    """
    return sorted(all_raw_entries(repo), key=lambda entry: (entry.created, entry.id))


def rebuild_workspace(repo: Path) -> Path:
    return repo.parent / f".{repo.name}{REBUILD_WORKSPACE_NAME}"


def rebuild_manifest_state(manifest: dict[str, Any]) -> dict[str, Any]:
    value = manifest.get("rebuild", {})
    ordered = [str(raw_id) for raw_id in value.get("ordered_raw_ids", [])]
    cursor = int(value.get("cursor", 0))
    return {
        "phase": str(value.get("phase", "idle")),
        "ordered_raw_ids": ordered,
        "cursor": cursor,
        "total": int(value.get("total", len(ordered))),
        "generation": value.get("generation"),
        "last_session_id": value.get("last_session_id"),
        "source_manifest_schema": value.get("source_manifest_schema"),
        "source_kb_version": value.get("source_kb_version"),
    }


def rebuild_generation(entries: list[RawEntry]) -> str:
    payload = [
        [raw_source_metadata(entry), sha256_text(entry.body)]
        for entry in entries
    ]
    return short_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), length=16)


def raw_source_metadata(entry: RawEntry) -> dict[str, Any]:
    meta, _ = frontmatter.read_document(entry.path)
    return {key: value for key, value in meta.items() if key not in RAW_COMPILED_FIELDS}


def rebuild_state(repo: Path) -> dict[str, Any]:
    workspace = rebuild_workspace(repo)
    if not (workspace / ".kb" / "manifest.json").exists():
        source_state = rebuild_manifest_state(load_manifest(repo))
        if source_state["phase"] == "complete":
            return {
                "active": False,
                "phase": "complete",
                "processed": source_state["cursor"],
                "total": source_state["total"],
                "remaining": max(0, source_state["total"] - source_state["cursor"]),
                "workspace": str(workspace),
            }
        total = len(ordered_raw_entries(repo))
        return {
            "active": False,
            "phase": "idle",
            "processed": 0,
            "total": total,
            "remaining": total,
            "workspace": str(workspace),
        }
    state = rebuild_manifest_state(load_manifest(workspace))
    return {
        "active": state["phase"] in {"replay", "consolidate"},
        "phase": state["phase"],
        "processed": state["cursor"],
        "total": state["total"],
        "remaining": max(0, state["total"] - state["cursor"]),
        "workspace": str(workspace),
    }


def validate_rebuild_source(repo: Path, workspace: Path) -> None:
    state = rebuild_manifest_state(load_manifest(workspace))
    entries = ordered_raw_entries(repo)
    if [entry.id for entry in entries] != state["ordered_raw_ids"] or rebuild_generation(entries) != state["generation"]:
        raise StaleSessionError("raw archive changed during rebuild")


def raw_lookup(repo: Path) -> dict[str, RawEntry]:
    return {entry.id: entry for entry in all_raw_entries(repo)}


def scope_entries(repo: Path, mode: str) -> list[RawEntry]:
    lookup = raw_lookup(repo)
    if mode == "incremental":
        return [read_raw_by_path(repo, item["path"]) for item in read_pending(repo)]
    if mode == "rebuild":
        ordered = ordered_raw_entries(repo)
        state = rebuild_manifest_state(load_manifest(repo))
        cursor = state["cursor"] if state["phase"] == "replay" else 0
        return ordered[cursor : cursor + 1]
    if mode == "consolidate":
        queue = consolidation_state(load_manifest(repo))["pending_raw"][:CONSOLIDATION_BATCH_SIZE]
        return [lookup[raw_id] for raw_id in queue if raw_id in lookup]
    if mode == "topics":
        return []
    raise ValidationError(f"invalid compile mode: {mode}")


def is_final_rebuild_tail(repo: Path) -> bool:
    manifest = load_manifest(repo)
    rebuild = rebuild_manifest_state(manifest)
    pending_count = len(consolidation_state(manifest)["pending_raw"])
    return (
        rebuild["phase"] == "consolidate"
        and rebuild["cursor"] == rebuild["total"]
        and 0 < pending_count < CONSOLIDATION_BATCH_SIZE
    )


def is_promoted_rebuild_tail_recovery(repo: Path) -> bool:
    manifest = load_manifest(repo)
    rebuild = rebuild_manifest_state(manifest)
    ordered_ids = list(rebuild["ordered_raw_ids"])
    compiled_ids = [str(raw_id) for raw_id in manifest.get("compiled_raw", [])]
    pending_ids = list(consolidation_state(manifest)["pending_raw"])
    current_ordered_ids = [entry.id for entry in ordered_raw_entries(repo)]
    return (
        rebuild["phase"] == "complete"
        and rebuild["cursor"] == rebuild["total"] == len(ordered_ids)
        and len(compiled_ids) == len(ordered_ids)
        and set(compiled_ids) == set(ordered_ids)
        and current_ordered_ids == ordered_ids
        and 0 < len(pending_ids) < CONSOLIDATION_BATCH_SIZE
        and pending_ids == ordered_ids[-len(pending_ids) :]
        and not read_pending(repo)
    )


def quality_repair_node_ids(repo: Path) -> list[str]:
    nodes, _ = load_nodes(repo)
    return sorted(
        compiler_policy_detail_node_ids(nodes)
        | cross_node_duplicate_detail_node_ids(nodes)
        | nonspecific_shared_entity_evidence_node_ids(nodes)
    )


def quality_repair_issues(repo: Path) -> dict[str, list[str]]:
    nodes, _ = load_nodes(repo)
    return {
        "weak_detail_node_ids": sorted(
            compiler_policy_detail_node_ids(nodes)
            | cross_node_duplicate_detail_node_ids(nodes)
        ),
        "weak_evidence_node_ids": sorted(nonspecific_shared_entity_evidence_node_ids(nodes)),
    }


def is_quality_repair_due(repo: Path) -> bool:
    manifest = load_manifest(repo)
    rebuild = rebuild_manifest_state(manifest)
    return (
        rebuild["phase"] not in {"replay", "consolidate"}
        and not read_pending(repo)
        and len(consolidation_state(manifest)["pending_raw"]) < CONSOLIDATION_BATCH_SIZE
        and not is_promoted_rebuild_tail_recovery(repo)
        and bool(quality_repair_node_ids(repo))
    )


def create_session_id(repo: Path, mode: str, entries: list[RawEntry] | None = None) -> str:
    entries = entries if entries is not None else scope_entries(repo, mode)
    session_entries = ordered_raw_entries(repo) if mode in {"consolidate", "topics"} else entries
    manifest = load_manifest(repo)
    rebuild = rebuild_manifest_state(manifest)
    fresh_rebuild = mode == "rebuild" and rebuild["phase"] != "replay"
    page_state = []
    if not fresh_rebuild:
        for path in sorted((repo / "wiki").rglob("*.md")) if (repo / "wiki").exists() else []:
            page_state.append([relpath(path, repo), sha256_text(path.read_text(encoding="utf-8"))])
    ordered = ordered_raw_entries(repo) if mode == "rebuild" else []
    generation = rebuild_generation(ordered) if ordered else None
    cursor = rebuild["cursor"] if rebuild["phase"] == "replay" else 0
    source_manifest_schema = rebuild.get("source_manifest_schema") if mode == "rebuild" else None
    source_kb_version = rebuild.get("source_kb_version") if mode == "rebuild" else None
    payload = {
        "schema": 2,
        "kb_version": KB_VERSION,
        "manifest_schema": source_manifest_schema if source_manifest_schema is not None else manifest.get("schema"),
        "compiled_kb_version": source_kb_version if source_kb_version is not None else manifest.get("kb_version"),
        "mode": mode,
        "raw": [
            [
                raw_source_metadata(entry),
                entry.annotations if mode != "rebuild" else {},
                sha256_text(entry.body),
            ]
            for entry in sorted(session_entries, key=lambda item: item.id)
        ],
        "pending": [] if fresh_rebuild else read_pending(repo),
        "pages": page_state,
        "candidates": [] if fresh_rebuild else manifest.get("candidates", []),
        "redirects": {} if fresh_rebuild else manifest.get("redirects", {}),
        "edges": [] if fresh_rebuild else manifest.get("edges", []),
        "consolidation": {"pending_raw": [], "memo": "", "last_session_id": None} if fresh_rebuild else consolidation_state(manifest),
        "rebuild": {"generation": generation, "cursor": cursor, "ordered_raw_ids": [entry.id for entry in ordered]},
        "agents_rules": default_agents_rules() if mode in {"rebuild", "topics"} else read_text(repo / "AGENTS.md"),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "session-" + short_hash(canonical, length=16)


def build_compile_request(
    repo: Path,
    *,
    task: str = "compile",
    raw_entries: list[RawEntry] | None = None,
    mode: str = "incremental",
) -> dict[str, Any]:
    load_config(repo)
    entries = raw_entries if raw_entries is not None else scope_entries(repo, mode)
    session_id = create_session_id(repo, mode, entries)
    manifest = load_manifest(repo)
    rebuild = rebuild_manifest_state(manifest)
    fresh_rebuild = mode == "rebuild" and rebuild["phase"] != "replay"
    nodes, _ = ({}, {}) if fresh_rebuild else load_nodes(repo)
    resolver = Resolver(nodes.values())
    resolution_candidates = {
        entry.id: resolver.resolve(f"{entry.title} {entry.body[:1200]}")
        for entry in entries
    }
    seed_ids = {
        str(candidate["id"])
        for candidates in resolution_candidates.values()
        for candidate in candidates
        if str(candidate.get("id", "")) in nodes
    }
    related_ids = set(seed_ids)
    for node_id in list(seed_ids):
        node = nodes[node_id]
        related_ids.update(str(edge.get("target")) for edge in node.out_edges if str(edge.get("target")) in nodes)
        related_ids.update(str(edge.get("source")) for edge in node.backrefs if str(edge.get("source")) in nodes)
    context = {
        "schema_version": 2,
        "contract_version": "2.4-entity-topic-understanding",
        "session_id": session_id,
        "mode": mode,
        "raw_entries": [raw_payload(entry, include_body=True, include_annotations=mode != "rebuild") for entry in entries],
        "compact_index": [node_payload(node, include_content=False) for node in sorted(nodes.values(), key=lambda item: item.id)],
        "existing_nodes": [node_payload(nodes[node_id], include_edges=True) for node_id in sorted(related_ids)],
        "resolution_candidates": resolution_candidates,
        "existing_candidates": [] if fresh_rebuild else candidates_payload(manifest.get("candidates", [])),
        "redirects": {} if fresh_rebuild else dict(manifest.get("redirects", {})),
        "consolidation_memo": "" if fresh_rebuild else consolidation_state(manifest)["memo"],
    }
    instructions = (
        "请输出 CompilePlan v2.4（顶层 schema_version 仍为 2）。复用节点时使用 target_id，新节点使用 ref；raw 通过 belongs_to 关联每个被抽取或补强的耐久节点。"
        "对每条 raw 分别执行实体提及抽取、事件事实判定和洞察线程识别；一条 raw 可以同时贡献多类节点。"
        "raw_annotations 必须显式返回 mentions、occurrences、claims 三个数组，没有候选时返回空数组。"
        "三个通道都为空时允许返回零 node_actions 和零 belongs_to；不得为满足挂靠要求制造微小事件或空泛洞察。"
        "每个 entity/event/statement action 的 source_ids 必须在对应 raw 的 mentions/occurrences/claims 中有非空依据，belongs_to 必须指向该 source-grounded action。选择 source-only reinforce 前先比较新来源与节点完整 summary/detail/evidence/uncertainties/history；只有纯重复、不会新增历史、推翻旧不确定性、改变综合或让详情过期时，才仅返回 action、target_id、type、source_ids。否则必须完整 refine 并综合全部新旧来源。mention 可用 target_id 明确解析结果。"
        "event 必须在 semantics 中证明用户相关、发生事实、时间锚点、事实性、event_basis 和 standalone_reason；删掉认知结果后仍应有可独立回顾的一件事，basis 还须由可观察动作支撑。raw.event_date 本身不能证明存在事件。"
        "event title 和 semantics.action 必须是同一条发生短语；觉察、识别、反思、重构、复盘、理解、整合、思考、捕捉、感悟、发现自己的模式、收到启发、发生认知改变拆入 statement。发生／收到／遇到本身不是 incident 事实锚点。咨询写成参加某次咨询；普通聊天、普通阅读、短暂感受、一般决定和行为模式不得建 event；项目计划会中的计划是名词。"
        "纯情绪、抽象关系、反思、习惯、愿望和方法论必须建为 statement 洞察；书籍、地点、任务等稳定对象应独立建 entity。"
        "除 source-only reinforce 外，每个 create/reinforce/refine/change/supersede action 必须返回完整 content；detail 至少四个实质句，并按类型使用固定两段标签：entity 对象与关系／历史与现状，event 发生与背景／结果与关联，statement 洞察与依据／演进与影响，topic 组织视角／脉络与边界。detail 必须明确写出 summary 的中心概念，覆盖节点全部有效来源；每句都须由该节点自己的 source/evidence/语义历史支撑，规范化达到 24 字的非 evidence 句不得跨节点精确复用，必要短术语和 evidence 引文可重复；给出至少三个不重复且不少于 8 个字的 key_points，其中至少两条复用中心概念；拒绝“当前节点只确认／节点仅保留／节点不把／后续若出现新的实质信息／后续实质变化需要”等编译政策填充，并用 evidence 将结论绑定到 raw source。同一 evidence claim 用于多个 entity 时必须直接点名每个实体的 title 或 alias，禁止复制只支撑另一个实体的 claim。"
        "所有耐久 entity mention 都必须解析或创建。若 event 或 statement 明确涉及实体，补充 involves／about／instance_of 边；不得靠共享关键词推断。"
        "statement action 只返回 current_state 与 effective_date，evolution 必须为空数组，由 CLI 负责追加。"
        "增量模式不得创建 topic 或执行 merge/split；sources 只能引用请求范围内 raw。CLI 将确定性生成 ID、页面、索引和事件时间线。"
    )
    if mode == "rebuild":
        instructions += "本次是 raw-only 顺序重放的一步，只能按增量规则创建或更新 entity、event、statement；不得创建 topic 或执行 merge/split。existing_nodes 只来自本次 rebuild 的前序原料。"
    agents_rules = default_agents_rules() if mode == "rebuild" else read_text(repo / "AGENTS.md")
    return llm_request(task, agents_rules, context, instructions, compile_response_schema(session_id, mode))


def build_rebuild_request(repo: Path) -> dict[str, Any] | None:
    workspace = rebuild_workspace(repo)
    workspace_exists = (workspace / ".kb" / "manifest.json").exists()
    if workspace_exists:
        validate_rebuild_source(repo, workspace)
    target = workspace if workspace_exists else repo
    state = rebuild_manifest_state(load_manifest(target))
    if workspace_exists and len(consolidation_state(load_manifest(target))["pending_raw"]) >= CONSOLIDATION_BATCH_SIZE:
        return build_consolidation_request(target, task="rebuild")
    if state["phase"] == "consolidate":
        return build_consolidation_request(target, task="rebuild", allow_partial=True)
    request = build_compile_request(target, task="rebuild", mode="rebuild")
    if not request["context"]["raw_entries"]:
        if state["phase"] == "consolidate":
            return None
        if (state["total"] if state["phase"] == "replay" else len(ordered_raw_entries(target))) != 0:
            return None
    total = state["total"] if state["phase"] == "replay" else len(ordered_raw_entries(target))
    completed = state["cursor"] if state["phase"] == "replay" else 0
    request["context"]["rebuild"] = {
        "phase": "replay",
        "step": completed + 1 if total else 0,
        "total": total,
        "completed": completed,
    }
    return request


def build_consolidation_request(
    repo: Path,
    *,
    task: str = "consolidate",
    allow_partial: bool = False,
    quality_repair: bool = False,
) -> dict[str, Any] | None:
    load_config(repo)
    repair_issues = quality_repair_issues(repo) if quality_repair else {
        "weak_detail_node_ids": [],
        "weak_evidence_node_ids": [],
    }
    weak_detail_node_ids = repair_issues["weak_detail_node_ids"]
    weak_evidence_node_ids = repair_issues["weak_evidence_node_ids"]
    if quality_repair and not is_quality_repair_due(repo):
        return None
    entries = [] if quality_repair else scope_entries(repo, "consolidate")
    active_rebuild_tail = is_final_rebuild_tail(repo)
    promoted_tail_recovery = is_promoted_rebuild_tail_recovery(repo)
    final_tail = len(entries) < CONSOLIDATION_BATCH_SIZE and (active_rebuild_tail or promoted_tail_recovery)
    partial_allowed = (allow_partial and active_rebuild_tail) or promoted_tail_recovery
    if len(entries) < CONSOLIDATION_BATCH_SIZE and not (partial_allowed or quality_repair):
        return None
    session_id = create_session_id(repo, "consolidate", entries)
    nodes, _ = load_nodes(repo)
    batch_ids = {entry.id for entry in entries}
    seed_ids = {node.id for node in nodes.values() if batch_ids & set(node.sources)}
    related_ids = set(seed_ids)
    for node in nodes.values():
        for edge in node.out_edges:
            if node.id in seed_ids or edge.get("target") in seed_ids:
                related_ids.add(node.id)
                if str(edge.get("target")) in nodes:
                    related_ids.add(str(edge["target"]))
    manifest = load_manifest(repo)
    context = {
        "schema_version": 2,
        "contract_version": "2.4-entity-topic-understanding",
        "session_id": session_id,
        "mode": "consolidate",
        "batch_size": len(entries),
        "final_tail": final_tail,
        "tail_recovery": promoted_tail_recovery,
        "quality_repair": quality_repair,
        "weak_detail_node_ids": weak_detail_node_ids,
        "weak_evidence_node_ids": weak_evidence_node_ids,
        "raw_annotations": [raw_payload(entry, include_body=False) for entry in entries],
        "compact_index": [node_payload(node, include_content=False) for node in sorted(nodes.values(), key=lambda item: item.id)],
        "statement_catalog": [
            node_payload(node, include_edges=True)
            for node in sorted(nodes.values(), key=lambda item: item.id)
            if node.type == "statement"
        ],
        "topic_catalog": [
            node_payload(node, include_edges=True)
            for node in sorted(nodes.values(), key=lambda item: item.id)
            if node.type == "topic"
        ],
        "member_catalog": [
            node_payload(node, include_edges=True)
            for node in sorted(nodes.values(), key=lambda item: item.id)
        ],
        "raw_catalog": [
            raw_payload(entry, include_body=False)
            for entry in sorted(raw_lookup(repo).values(), key=lambda item: item.id)
        ],
        "source_dates": {
            raw_id: entry.event_date
            for raw_id, entry in sorted(raw_lookup(repo).items())
        },
        "related_one_hop_nodes": [node_payload(nodes[node_id], include_edges=True) for node_id in sorted(related_ids)],
        "existing_candidates": candidates_payload(manifest.get("candidates", [])),
        "consolidation_memo": consolidation_state(manifest)["memo"],
    }
    instructions = (
        "请仅基于批次注解、compact_index、候选项、一跳节点、全库 member_catalog／raw_catalog 与 source_dates 输出 CompilePlan v2.4（顶层 schema_version 仍为 2），不得假设未提供的 raw 正文。"
        "本批 Raw 只决定全库审计时机，不决定主题边界或数量。审查全部成员和已有候选：真正反复出现的讨论簇必须创建／更新主题，或返回带稳定 candidate_id、topic_kind、pending|watching|rejected|materialized 状态和理由的 topic 候选。不得静默遗漏旧候选。"
        "topic 是人类优先阅读的全库组织视角，不是更大的洞察。life_domain 可组织 AI 协作等稳定领域，longitudinal_arc 可组织睡眠等长期变化；cross_domain_pattern 仅在同一机制确实跨域复现时使用。直属成员可为 raw、entity、event、statement 或 child topic，至少五个成员、两个 statement、两个 facet、三个独立 raw capture；longitudinal_arc 另需十四天。"
        "每个 topic 使用 membership_mode=replace、source_ids=[]，完整返回 attrs.topic_contract：topic_kind、organizing_question、facet_relationship、boundary_rule、facets[].member_refs、覆盖全部成员的 member_rationales 和可为空的 exclusions。每个 rationale.reason 必须复用所分配 facet 的准确名称，并从成员自身可用内容复制 supporting_excerpt；成员自己的 content/evidence 必须直接回答 organizing_question 并支持该 facet，不得由 rationale 发明桥接。topic sources 由 contains 成员自动推导。"
        "同时返回 attrs.topic_reading：core_understanding、按时间组织且带 source_ids 的 evolution、真实存在时才填写的 contradictions、证据不足仍需追踪的 open_questions、confidence。contradictions 的 source_ids 必须实际覆盖每个 cited member；member rationale 可以引用 Raw annotation 三通道或 event standalone_reason。主题正文必须理解和综合，不能只枚举记录。"
        "隐藏 topic 标题、facet 和 rationale 后，每个成员自身仍须直接贡献 organizing_question；移除边缘成员后若不达门槛，应暂缓并形成候选，禁止用通用机制或非洞察节点凑数。"
        "完成成员正向检查后，反向遍历全库 member_catalog、raw_catalog 与已有候选，纳入遗漏的直接贡献成员，保留真正相近但不属于主题的 exclusion。"
        "共同时间、情绪、来源批次、宽泛词或事后桥接句不能作为归属依据；允许成员暂不归类。"
        "raw_annotations 必须返回空数组，consolidate 不得改写已有 raw 注解。"
        "任何 create 或实质更新都必须返回完整 content；topic.summary 必须直接陈述知识，不能以组织、汇集、整理或聚合开头；detail 至少两个实质段落和三个关键点，解释组织问题、facet 关系、纵向综合与边界，不是摘要拼接。同一 evidence claim 用于多个 entity 时必须直接点名每个实体的 title 或 alias。"
    )
    if final_tail:
        tail_instruction = (
            f"本次是旧版 raw-only rebuild 已提升后的尾批恢复，共 {len(entries)} 条；必须补做与常规 Consolidation 相同的全库主题审查，成功后一次性消费遗留队列。"
            if promoted_tail_recovery
            else f"本次是 raw-only rebuild 全部重放完成后的最终尾批，共 {len(entries)} 条；必须完成与常规 Consolidation 相同的全库主题审查，成功消费尾批后才允许提升 workspace。"
        )
        instructions = tail_instruction + instructions
    if quality_repair:
        instructions = (
            "本次是零批次内容质量修复，不得创建节点、改变结构或消费 Consolidation 队列。"
            f"只可对以下 weak detail 节点做完整 source-grounded 更新：{', '.join(weak_detail_node_ids)}。"
            f"只可对以下 weak evidence 节点修正实体专属 evidence：{', '.join(weak_evidence_node_ids)}。"
            "逐节点改写为由其自身来源和 evidence 支撑的内容；共享 entity claim 必须直接点名该实体的 title 或 alias。"
            "投影图中不得保留任何达到 24 字的跨节点重复非 evidence 句、单节点编译政策措辞或未点名实体的共享 evidence；candidates 与 consolidation_memo 必须原样保留。"
            + instructions
        )
    response_schema = compile_response_schema(session_id, "consolidate")
    response_schema["raw_annotations"] = []
    return llm_request(task, read_text(repo / "AGENTS.md"), context, instructions, response_schema)


def build_topic_request(repo: Path) -> dict[str, Any]:
    load_config(repo)
    nodes, _ = load_nodes(repo)
    lookup = raw_lookup(repo)
    session_id = create_session_id(repo, "topics", [])
    context = {
        "schema_version": 2,
        "contract_version": "2.4-entity-topic-understanding",
        "session_id": session_id,
        "mode": "topics",
        "statement_catalog": [
            node_payload(node, include_edges=True)
            for node in sorted(nodes.values(), key=lambda item: item.id)
            if node.type == "statement"
        ],
        "supporting_catalog": [
            node_payload(node, include_edges=True)
            for node in sorted(nodes.values(), key=lambda item: item.id)
            if node.type in {"entity", "event"}
        ],
        "member_catalog": [
            node_payload(node, include_edges=True)
            for node in sorted(nodes.values(), key=lambda item: item.id)
        ],
        "raw_catalog": [
            raw_payload(entry, include_body=False)
            for entry in sorted(lookup.values(), key=lambda item: item.id)
        ],
        "existing_topics": [
            node_payload(node, include_edges=True)
            for node in sorted(nodes.values(), key=lambda item: item.id)
            if node.type == "topic"
        ],
        "source_dates": {raw_id: entry.event_date for raw_id, entry in sorted(lookup.items())},
        "existing_candidates": candidates_payload(load_manifest(repo).get("candidates", [])),
        "consolidation_memo": consolidation_state(load_manifest(repo))["memo"],
    }
    instructions = (
        "请审计 existing_topics、existing_candidates 和完整 member_catalog／raw_catalog，输出整个 topic 层的替换方案。不要复制旧 topic ID；只提供 ref 与标题，CLI 可在标题稳定时复用确定性 ID。不要修改 entity、event、statement 或 raw。"
        "只允许 create topic action 与 topic contains 边，目标可为 raw、entity、event、statement 或 plan-local child topic。主题可以多父，最大深度三。若证据不足以创建主题，必须返回稳定 topic candidate 的明确状态和理由，不能静默返回空。"
        "topic 是比洞察更高维的稳定阅读视角，不是批次摘要或更大的洞察。高维不等于跨域：AI 协作可为 life_domain，睡眠可为 longitudinal_arc，只有同一机制确实跨域复现才使用 cross_domain_pattern。每个主题至少五个直属成员、两个 statement、两个 facet、三个独立 raw capture；longitudinal_arc 另需十四天跨度。"
        "每个 action 使用 membership_mode=replace、source_ids=[]，提供完整 attrs.topic_contract：topic_kind、organizing_question、facet_relationship、boundary_rule、facets[].member_refs、member_rationales、可为空的 exclusions。每个 rationale.reason 必须复用所分配 facet 的准确名称，并从成员自身内容复制 supporting_excerpt；成员自己的 content/evidence 必须直接回答 organizing_question 并支持该 facet，不得由 rationale 发明桥接。topic sources 由 contains 成员自动推导。"
        "还必须提供 attrs.topic_reading：core_understanding、带来源的 evolution、真实证据冲突才填写的 contradictions、基于证据缺口的 open_questions、confidence。contradictions 的 source_ids 必须实际覆盖每个 cited member；member rationale 可以引用 Raw annotation 三通道或 event standalone_reason。正文要解释用户在该主题上的机制、变化、矛盾和未决问题，不能只是成员列表。"
        "逐成员执行独立贡献测试，并反向审计全库成员与候选。移除不合格成员后重新计算门槛；不达标则暂缓为候选，禁止用通用洞察、实体、事件或 Raw 凑数。共同日期、情绪、批次、泛化词和桥接句不构成归属。"
        "summary 直接陈述该主题揭示的知识，不能以组织、汇集、整理或聚合开头；detail 至少两个实质段落和三个关键点，解释 organizing question、facet 间稳定关系、纵向综合与边界，并由至少三条成员来源 evidence 支撑。"
        "raw_annotations 必须为空；candidates 必须保留或更新全部已有稳定候选，并记录本轮发现的未成熟主题；consolidation_memo 必须原样返回。"
    )
    response_schema = compile_response_schema(session_id, "topics")
    response_schema["raw_annotations"] = []
    return llm_request("topics", default_agents_rules(), context, instructions, response_schema)


def clean_raw_document(entry: RawEntry) -> str:
    meta, body = frontmatter.read_document(entry.path)
    for key in RAW_COMPILED_FIELDS:
        meta.pop(key, None)
    content = frontmatter.dump_document(meta, body)
    _, cleaned_body = frontmatter.parse_document(content)
    if sha256_text(cleaned_body) != sha256_text(body):
        raise ValidationError(f"raw body changed while preparing rebuild: {entry.id}")
    return content


def initialize_rebuild_workspace(repo: Path) -> Path:
    workspace = rebuild_workspace(repo)
    if (workspace / ".kb" / "manifest.json").exists():
        return workspace
    if workspace.exists():
        raise ValidationError(f"incomplete rebuild workspace exists: {workspace}")
    config = load_config(repo)
    source_manifest = load_manifest(repo)
    initialize(workspace, str(config.get("scope", "shared")), str(config.get("agent") or "") or None, "plain")
    entries = ordered_raw_entries(repo)
    for entry in entries:
        relative = Path(relpath(entry.path, repo))
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(clean_raw_document(entry), encoding="utf-8")
        set_readonly(target)
    state = {
        "phase": "replay",
        "ordered_raw_ids": [entry.id for entry in entries],
        "cursor": 0,
        "total": len(entries),
        "generation": rebuild_generation(entries),
        "last_session_id": None,
        "source_manifest_schema": source_manifest.get("schema"),
        "source_kb_version": source_manifest.get("kb_version"),
    }
    manifest = empty_manifest()
    manifest["tips_seen"] = sorted(set(str(value) for value in source_manifest.get("tips_seen", [])))
    manifest["rebuild"] = state
    (workspace / ".kb" / "manifest.json").write_text(json_dumps(manifest) + "\n", encoding="utf-8")
    (workspace / ".kb" / "pending.jsonl").write_text("", encoding="utf-8")
    return workspace


def apply_response(repo: Path, response: dict[str, Any], *, command: str) -> dict[str, Any]:
    load_config(repo)
    value = unwrap_response(response)
    required = {"schema_version", "session_id", "mode", "raw_annotations", "node_actions", "out_edges", "candidates", "consolidation_memo"}
    missing = required - set(value)
    if missing:
        raise ValidationError("missing CompilePlan fields: " + ", ".join(sorted(missing)))
    plan = CompilePlan.from_dict(value)
    if plan.mode not in PLAN_MODES:
        raise ValidationError(f"invalid plan mode: {plan.mode}")
    expected_command_modes = {
        "compile": {"incremental"},
        "rebuild": {"rebuild"},
        "consolidate": {"consolidate"},
        "topics": {"topics"},
    }
    if command in expected_command_modes and plan.mode not in expected_command_modes[command]:
        raise ValidationError(f"{command} cannot apply mode={plan.mode}")
    with RepoLock(repo):
        recovery = recover_transaction(repo)
        if command == "update":
            expected_mode = determine_update_mode(repo)["mode"]
            if plan.mode != expected_mode:
                raise StaleSessionError(f"update mode changed: expected {expected_mode}, got {plan.mode}")
        store = storage_for(repo)
        if isinstance(store, GitStorage):
            store.assert_only_known_changes()
        quality_repair = command == "update" and plan.mode == "consolidate" and is_quality_repair_due(repo)
        entries = [] if quality_repair else scope_entries(repo, plan.mode)
        partial_rebuild_tail = plan.mode == "consolidate" and (
            is_final_rebuild_tail(repo) or is_promoted_rebuild_tail_recovery(repo)
        )
        if (
            plan.mode == "consolidate"
            and len(entries) != CONSOLIDATION_BATCH_SIZE
            and not partial_rebuild_tail
            and not quality_repair
        ):
            raise ValidationError(f"consolidate requires exactly {CONSOLIDATION_BATCH_SIZE} pending raw entries")
        expected_session = create_session_id(repo, plan.mode, entries)
        if plan.session_id != expected_session:
            raise StaleSessionError(f"stale session: expected {expected_session}, got {plan.session_id or 'empty'}")
        old_manifest = load_manifest(repo)
        if plan.mode == "rebuild" and rebuild_manifest_state(old_manifest)["phase"] != "replay":
            raise ValidationError("rebuild responses must be applied through the isolated rebuild workspace")
        if plan.mode in {"incremental", "rebuild", "topics"} and plan.consolidation_memo != consolidation_state(old_manifest)["memo"]:
            raise ValidationError(f"{plan.mode} plans must preserve consolidation_memo")
        nodes, _ = load_nodes(repo)
        old_duplicate_groups = cross_node_duplicate_detail_groups(nodes)
        old_policy_detail_ids = compiler_policy_detail_node_ids(nodes)
        old_weak_evidence_ids = nonspecific_shared_entity_evidence_node_ids(nodes)
        lookup = raw_lookup(repo)
        validate_compile_plan(plan, entries, lookup, nodes)
        if quality_repair:
            weak_targets = set(quality_repair_node_ids(repo))
            if not plan.node_actions:
                raise ValidationError("quality repair requires at least one weak node update")
            invalid_actions = [
                action
                for action in plan.node_actions
                if action.get("action") not in {"reinforce", "refine", "change"}
                or str(action.get("target_id", "")) not in weak_targets
            ]
            if invalid_actions:
                raise ValidationError("quality repair may update listed weak nodes only")
            if plan.consolidation_memo != consolidation_state(old_manifest)["memo"]:
                raise ValidationError("quality repair must preserve consolidation_memo")
            expected_candidates = {
                json.dumps(item, ensure_ascii=False, sort_keys=True)
                for item in candidates_payload(old_manifest.get("candidates", []))
            }
            returned_candidates = {
                json.dumps(item, ensure_ascii=False, sort_keys=True)
                for item in plan.candidates
            }
            if returned_candidates != expected_candidates:
                raise ValidationError("quality repair must preserve candidates")
        old_topic_ids = {node.id for node in nodes.values() if node.type == "topic"}
        if plan.mode == "topics":
            nodes = {node_id: node for node_id, node in nodes.items() if node.type != "topic"}
        refs, action_redirects, affected = apply_node_actions(nodes, plan.node_actions, mode=plan.mode, raw_entries=lookup)
        projected_duplicate_groups = cross_node_duplicate_detail_groups(nodes)
        projected_policy_detail_ids = compiler_policy_detail_node_ids(nodes)
        projected_weak_evidence_ids = nonspecific_shared_entity_evidence_node_ids(nodes)
        if quality_repair:
            if projected_duplicate_groups:
                duplicate_ids = sorted({
                    node_id
                    for node_ids in projected_duplicate_groups.values()
                    for node_id in node_ids
                })
                raise ValidationError(
                    "quality repair must eliminate all cross-node duplicate detail: " + ", ".join(duplicate_ids)
                )
            if projected_policy_detail_ids:
                raise ValidationError(
                    "quality repair must eliminate all compiler-policy detail: "
                    + ", ".join(sorted(projected_policy_detail_ids))
                )
            if projected_weak_evidence_ids:
                raise ValidationError(
                    "quality repair must eliminate all nonspecific shared entity evidence: "
                    + ", ".join(sorted(projected_weak_evidence_ids))
                )
        else:
            expanded_duplicates = {
                fragment: node_ids
                for fragment, node_ids in projected_duplicate_groups.items()
                if node_ids - old_duplicate_groups.get(fragment, set())
            }
            if expanded_duplicates:
                duplicate_ids = sorted({
                    node_id
                    for node_ids in expanded_duplicates.values()
                    for node_id in node_ids
                })
                raise ValidationError(
                    "plan introduces cross-node duplicate detail: " + ", ".join(duplicate_ids)
                )
            new_policy_detail_ids = projected_policy_detail_ids - old_policy_detail_ids
            if new_policy_detail_ids:
                raise ValidationError(
                    "plan introduces compiler-policy detail: "
                    + ", ".join(sorted(new_policy_detail_ids))
                )
            new_weak_evidence_ids = projected_weak_evidence_ids - old_weak_evidence_ids
            if new_weak_evidence_ids:
                raise ValidationError(
                    "plan introduces nonspecific shared entity evidence: "
                    + ", ".join(sorted(new_weak_evidence_ids))
                )
        new_topic_ids = {node.id for node in nodes.values() if node.type == "topic"}
        removed_topic_ids = old_topic_ids - new_topic_ids if plan.mode == "topics" else set()
        replaced_topic_ids_by_refresh = old_topic_ids & new_topic_ids if plan.mode == "topics" else set()
        if plan.mode == "topics":
            redirects = redirects_without_removed_topics(
                old_manifest.get("redirects", {}),
                removed_topic_ids,
                new_topic_ids,
            )
        else:
            redirects = {**dict(old_manifest.get("redirects", {})), **action_redirects}
        replaced_topic_ids = {
            str(action.get("target_id"))
            for action in plan.node_actions
            if action.get("membership_mode") == "replace" and action.get("target_id")
        }
        for node in nodes.values():
            node.out_edges = [
                edge
                for edge in node.out_edges
                if not (plan.mode == "topics" and str(edge.get("target")) in old_topic_ids)
                and not (node.id in replaced_topic_ids and edge.get("type") == "contains")
            ]
        existing_edges = [
            {
                "source_ref": edge.get("source"),
                "target_ref": edge.get("target"),
                "type": edge.get("type"),
                "note": edge.get("note", ""),
                "inferred": edge.get("inferred", False),
                "attrs": edge.get("attrs", {}),
            }
            for edge in old_manifest.get("edges", [])
            if not (
                plan.mode == "topics"
                and (str(edge.get("source")) in old_topic_ids or str(edge.get("target")) in old_topic_ids)
            )
            and not (
                edge.get("type") == "contains"
                and str(edge.get("source")) in replaced_topic_ids
            )
        ]
        edges = resolve_edges(nodes, set(lookup), refs, [*existing_edges, *plan.out_edges], redirects)
        if quality_repair:
            old_edge_keys = {json.dumps(edge, ensure_ascii=False, sort_keys=True) for edge in old_manifest.get("edges", [])}
            new_edge_keys = {json.dumps(edge, ensure_ascii=False, sort_keys=True) for edge in edges}
            if old_edge_keys != new_edge_keys:
                raise ValidationError("quality repair cannot change graph structure")
        if plan.mode in {"consolidate", "topics"}:
            validate_materialized_topic_contracts(nodes, lookup)
        raw_documents = {} if plan.mode == "topics" else build_raw_annotations(repo, plan, list(lookup.values()), edges)

        pending = read_pending(repo)
        consumed = [entry.id for entry in entries] if plan.mode in {"incremental", "rebuild"} else []
        if plan.mode in {"incremental", "rebuild"}:
            next_pending = [row for row in pending if row["raw_id"] not in set(consumed)]
            compiled_raw = sorted(set(old_manifest.get("compiled_raw", [])) | set(consumed))
        else:
            next_pending = pending
            compiled_raw = sorted(set(old_manifest.get("compiled_raw", [])))

        tip = None
        tips_seen = sorted(set(str(value) for value in old_manifest.get("tips_seen", [])))
        if consumed:
            tip, tips_seen = next_tip(tips_seen)

        consolidation = next_consolidation_state(old_manifest, plan, consumed, entries)
        resolved_candidate_targets = {
            str(action.get("target_id"))
            for action in plan.node_actions
            if action.get("action") == "split" and action.get("target_id")
        }
        normalized_candidates = normalize_plan_candidates(
            plan.candidates,
            refs,
            redirects,
            nodes,
            resolved_candidate_targets,
        )
        if plan.mode == "topics":
            retained_candidates = [
                item
                for item in old_manifest.get("candidates", [])
                if not ({str(value) for value in item.get("node_ids", [])} & old_topic_ids)
            ]
        else:
            retained_candidates = list(old_manifest.get("candidates", []))
        retained_candidates = normalize_retained_candidates(
            retained_candidates,
            redirects,
            nodes,
            resolved_candidate_targets,
        )
        candidates = next_candidates(
            {**old_manifest, "candidates": retained_candidates},
            normalized_candidates,
        )
        rebuild = next_rebuild_manifest_state(old_manifest, plan, consumed)
        tx = KnowledgeTransaction(repo, plan.session_id)
        tx.prepare()
        committed = False
        try:
            index, counts = render_graph(repo, tx.wiki_next, nodes, edges, lookup)
            manifest = build_manifest(
                repo,
                tx.wiki_next,
                compiled_raw=compiled_raw,
                edges=edges,
                redirects=redirects,
                candidates=candidates,
                consolidation=consolidation,
                tips_seen=tips_seen,
                rebuild=rebuild,
                session_id=plan.session_id,
                raw_entries=lookup,
            )
            tx.stage_metadata(index=index, manifest=manifest, pending_rows=next_pending)
            for relative, content in raw_documents.items():
                tx.stage_raw(relative, content)
            tx.promote()
            paths = ["wiki", "index.md", ".kb/manifest.json", ".kb/pending.jsonl", *sorted(raw_documents)]
            commit = store.commit_paths(commit_message(command, plan.mode, consumed, sorted(affected)), paths)
            committed = True
            tx.mark_committed(commit)
            tx.finalize()
        except Exception:
            if not committed:
                tx.rollback()
                if isinstance(store, GitStorage):
                    store.unstage_paths(paths if "paths" in locals() else [])
            raise

        result: dict[str, Any] = {
            "mode": plan.mode,
            "session_id": plan.session_id,
            "raw_ids": consumed,
            "updated_pages": sorted(affected),
            "commit": commit,
            "pending": len(next_pending),
            "counts": counts,
            "candidates": candidates,
            "consolidation_due": len(consolidation["pending_raw"]) >= CONSOLIDATION_BATCH_SIZE,
            "consolidation_pending": len(consolidation["pending_raw"]),
            "stale_session": False,
            "transaction_recovery": recovery,
            "rebuild": rebuild,
            "quality_repair": quality_repair,
        }
        if plan.mode == "topics":
            result["removed_topics"] = sorted(removed_topic_ids)
            result["replaced_topics"] = sorted(replaced_topic_ids_by_refresh)
        if consumed:
            from .recap import build_recap_request

            result["recap_request"] = build_recap_request(repo, consumed, sorted(affected))
        if tip:
            result["tip"] = tip
        return result


def apply_rebuild_response(repo: Path, response: dict[str, Any]) -> dict[str, Any]:
    value = unwrap_response(response)
    plan = CompilePlan.from_dict(value)
    workspace = rebuild_workspace(repo)
    if plan.mode == "rebuild":
        if not (workspace / ".kb" / "manifest.json").exists():
            request = build_rebuild_request(repo)
            if request is None or plan.session_id != str(request["context"]["session_id"]):
                expected = request["context"]["session_id"] if request else "none"
                raise StaleSessionError(f"stale session: expected {expected}, got {plan.session_id or 'empty'}")
            initialize_rebuild_workspace(repo)
        else:
            validate_rebuild_source(repo, workspace)
        result = apply_response(workspace, response, command="rebuild")
    elif plan.mode == "consolidate":
        if not (workspace / ".kb" / "manifest.json").exists():
            raise ValidationError("no active rebuild workspace")
        validate_rebuild_source(repo, workspace)
        result = apply_response(workspace, response, command="consolidate")
    else:
        raise ValidationError(f"rebuild cannot apply mode={plan.mode}")

    state = rebuild_state(repo)
    consolidation = consolidation_state(load_manifest(workspace))
    ready = state["phase"] == "consolidate" and not consolidation["pending_raw"]
    if ready:
        final = finalize_rebuild(repo)
        return {**result, **final, "rebuild_complete": True}
    return {
        **result,
        "rebuild_complete": False,
        "rebuild_progress": state,
        "next_phase": "consolidate" if state["phase"] == "consolidate" else "replay",
    }


def finalize_rebuild(repo: Path) -> dict[str, Any]:
    workspace = rebuild_workspace(repo)
    manifest_path = workspace / ".kb" / "manifest.json"
    if not manifest_path.exists():
        raise ValidationError("no rebuild workspace to finalize")
    with RepoLock(repo):
        recovery = recover_transaction(repo)
        workspace_manifest = load_manifest(workspace)
        state = rebuild_manifest_state(workspace_manifest)
        consolidation = consolidation_state(workspace_manifest)
        if state["phase"] != "consolidate" or state["cursor"] != state["total"]:
            raise ValidationError("rebuild replay is not complete")
        if consolidation["pending_raw"]:
            raise ValidationError("rebuild consolidation queue must be empty before promotion")

        validate_rebuild_source(repo, workspace)
        source_raw = raw_lookup(repo)
        rebuilt_raw = raw_lookup(workspace)
        if set(source_raw) != set(rebuilt_raw):
            raise ValidationError("raw archive changed during rebuild")
        for raw_id, entry in source_raw.items():
            rebuilt = rebuilt_raw[raw_id]
            if sha256_text(entry.body) != sha256_text(rebuilt.body):
                raise ValidationError(f"raw body changed during rebuild: {raw_id}")
            if raw_source_metadata(entry) != raw_source_metadata(rebuilt):
                raise ValidationError(f"raw metadata changed during rebuild: {raw_id}")

        source_config = load_config(repo)
        source_backend = "git" if (repo / ".git").exists() else "plain"
        final_config = default_config(
            repo,
            str(source_config.get("scope", "shared")),
            str(source_config.get("agent") or "") or None,
            source_backend,
        )
        final_manifest = {
            **workspace_manifest,
            "rebuild": {**state, "phase": "complete"},
        }
        session_id = str(workspace_manifest.get("applied_session_id") or state.get("last_session_id") or "rebuild-finalize")
        store = storage_for(repo)
        if isinstance(store, GitStorage):
            store.assert_only_known_changes()
        tx = KnowledgeTransaction(repo, session_id)
        tx.prepare(REBUILD_CONTROL_PATHS)
        committed = False
        raw_documents: dict[str, str] = {}
        try:
            shutil.copytree(workspace / "wiki", tx.wiki_next, dirs_exist_ok=True)
            tx.stage_metadata(
                index=(workspace / "index.md").read_text(encoding="utf-8"),
                manifest=final_manifest,
                pending_rows=read_pending(workspace),
            )
            tx.stage_control(".gitignore", DEFAULT_GITIGNORE)
            tx.stage_control("AGENTS.md", default_agents_rules())
            tx.stage_control(".kb/config.yaml", frontmatter.dump_mapping(final_config))
            for path in sorted((workspace / "raw").rglob("*.md")):
                relative = relpath(path, workspace)
                content = path.read_text(encoding="utf-8")
                if content != (repo / relative).read_text(encoding="utf-8"):
                    raw_documents[relative] = content
                    tx.stage_raw(relative, content)
            tx.promote()
            paths = [
                "wiki",
                "index.md",
                ".kb/manifest.json",
                ".kb/pending.jsonl",
                *REBUILD_CONTROL_PATHS,
                *sorted(raw_documents),
            ]
            message = (
                "chore(rebuild): 从 raw 顺序重建 v2.4 图谱\n\n"
                f"raw-count: {state['total']}\n"
                f"consolidation-pending: {len(consolidation['pending_raw'])}\n"
                f"kb-version: {KB_VERSION}\n"
            )
            commit = store.commit_paths(message, paths)
            committed = True
            tx.mark_committed(commit)
            tx.finalize()
        except Exception:
            if not committed:
                tx.rollback()
                if isinstance(store, GitStorage):
                    store.unstage_paths(paths if "paths" in locals() else [])
            raise
        shutil.rmtree(workspace)
    return {
        "mode": "rebuild",
        "commit": commit,
        "counts": graph_stats(repo),
        "rebuild_progress": {
            "active": False,
            "phase": "complete",
            "processed": state["total"],
            "total": state["total"],
            "remaining": 0,
            "workspace": str(workspace),
        },
        "transaction_recovery": recovery,
    }


def unwrap_response(response: dict[str, Any]) -> dict[str, Any]:
    if "data" in response and isinstance(response["data"], dict) and "llm_response" in response["data"]:
        return response["data"]["llm_response"]
    if "llm_response" in response:
        return response["llm_response"]
    return response


def validate_compile_plan(
    plan: CompilePlan,
    entries: list[RawEntry],
    lookup: dict[str, RawEntry],
    existing_nodes: dict[str, Node] | None = None,
) -> None:
    if plan.schema_version != 2:
        raise ValidationError("schema_version must be 2")
    scope_ids = {entry.id for entry in entries}
    annotations = {str(item.get("raw_id")): item for item in plan.raw_annotations}
    if plan.mode in {"incremental", "rebuild"} and set(annotations) != scope_ids:
        raise ValidationError("raw_annotations must cover the exact compile scope")
    if plan.mode in {"consolidate", "topics"} and annotations:
        raise ValidationError(f"{plan.mode} raw_annotations must be empty")
    for raw_id, annotation in annotations.items():
        if raw_id not in lookup or not str(annotation.get("summary", "")).strip():
            raise ValidationError(f"invalid raw annotation: {raw_id}")
        importance = annotation.get("importance")
        if not isinstance(importance, int) or not 1 <= importance <= 5:
            raise ValidationError(f"importance must be an integer from 1 to 5: {raw_id}")
        for field in ["mentions", "occurrences", "claims"]:
            if not isinstance(annotation.get(field), list):
                raise ValidationError(f"raw annotation {field} must be an array: {raw_id}")
        validate_raw_annotation_channels(annotation, raw_id)
    allowed_sources = set(lookup) if plan.mode in {"consolidate", "topics"} else scope_ids
    refs: set[str] = set()
    action_sources: set[tuple[str, str]] = set()
    existing_nodes = existing_nodes or {}
    for action in plan.node_actions:
        operation = str(action.get("action", ""))
        if operation not in NODE_ACTIONS:
            raise ValidationError(f"invalid node action: {operation}")
        if operation == "create":
            ref = str(action.get("ref", ""))
            if not ref or ref in refs or action.get("target_id"):
                raise ValidationError("create requires a unique ref and no target_id")
            if ref in existing_nodes or ref in lookup:
                raise ValidationError("create plan-local ref cannot reuse an existing node or raw ID")
            refs.add(ref)
            if str(action.get("type", "")) not in NODE_TYPES:
                raise ValidationError("create requires a valid node type")
            if action.get("type") != "topic" and not action.get("source_ids", action.get("sources", [])):
                raise ValidationError("new non-topic nodes require source_ids")
        elif not action.get("target_id"):
            raise ValidationError(f"{operation} requires target_id")
        target = existing_nodes.get(str(action.get("target_id", "")))
        if operation != "create" and operation not in {"merge", "split"} and target is None:
            raise ValidationError(f"unknown target_id: {action.get('target_id')}")
        if target and action.get("type") and str(action["type"]) != target.type:
            raise ValidationError("node action type cannot change an existing node")
        if plan.mode in {"incremental", "rebuild"} and (operation in {"merge", "split"} or action.get("type") == "topic"):
            label = "rebuild replay" if plan.mode == "rebuild" else "incremental"
            raise ValidationError(f"{label} plans cannot create topics or execute merge/split")
        if plan.mode == "topics" and not (operation == "create" and action.get("type") == "topic"):
            raise ValidationError("topics refresh accepts create topic actions only")
        declared_sources = action.get("source_ids", action.get("sources", []))
        validate_source_ids(declared_sources, allowed_sources)
        node_type = str(action.get("type") or (target.type if target else ""))
        source_only_reinforce = operation == "reinforce" and target is not None and "content" not in action
        if source_only_reinforce:
            if node_type == "topic":
                raise ValidationError("topic reinforce requires complete content and membership")
            if plan.mode not in {"incremental", "rebuild"}:
                raise ValidationError("source-only reinforce is available in incremental or rebuild mode only")
            if "sources" in action:
                raise ValidationError("source-only reinforce accepts source_ids only")
            allowed_fields = {"action", "target_id", "type", "source_ids"}
            mutated = sorted(set(action) - allowed_fields)
            if mutated:
                raise ValidationError(
                    "source-only reinforce cannot mutate node fields: " + ", ".join(mutated)
                )
            if not declared_sources:
                raise ValidationError("source-only reinforce requires source_ids")
        if operation in {"relate", "archive"}:
            forbidden = {
                "title",
                "aliases",
                "summary",
                "source_ids",
                "sources",
                "content",
                "entity_kind",
                "event_kind",
                "status",
                "event_date",
                "semantics",
                "current_state",
                "effective_date",
                "evolution",
                "attrs",
            }
            mutated = sorted(key for key in forbidden if key in action)
            if mutated:
                raise ValidationError(f"{operation} cannot mutate node fields: {', '.join(mutated)}")
        source_grounded = operation in {"create", "reinforce", "refine", "change", "supersede", "merge"}
        content_mutating = source_grounded and not source_only_reinforce
        if plan.mode in {"incremental", "rebuild"} and source_grounded and node_type != "topic":
            source_values = [str(value) for value in declared_sources or []]
            if not source_values:
                raise ValidationError(f"{operation} requires source_ids from the compile scope")
            action_ref = str(action.get("ref") if operation == "create" else action.get("target_id"))
            action_sources.update((raw_id, action_ref) for raw_id in source_values)
        if plan.mode in {"incremental", "rebuild"} and node_type == "statement" and action.get("evolution"):
            raise ValidationError("statement evolution is CLI-managed during incremental and rebuild apply")
        content_sources = set(str(value) for value in declared_sources or [])
        if target:
            content_sources.update(target.sources)
        if operation == "merge":
            for node_id in action.get("absorbed_ids", []):
                absorbed = existing_nodes.get(str(node_id))
                if absorbed:
                    content_sources.update(absorbed.sources)
        if node_type == "topic" and plan.mode in {"consolidate", "topics"}:
            content_sources.update(allowed_sources)
        if content_mutating:
            validate_node_content(action, node_type, content_sources)
        if node_type == "entity" and action.get("entity_kind") and action.get("entity_kind") not in ENTITY_KINDS:
            raise ValidationError(f"invalid entity_kind: {action.get('entity_kind')}")
        if node_type == "event" and content_mutating:
            validate_event_action(action, target, content_sources)
        if plan.mode in {"incremental", "rebuild"} and source_grounded and node_type != "topic":
            channel = {"entity": "mentions", "event": "occurrences", "statement": "claims"}.get(node_type)
            if channel:
                missing_channel = [
                    raw_id
                    for raw_id in source_values
                    if not annotation_supports_action(annotations.get(raw_id, {}), channel, action, target)
                ]
                if missing_channel:
                    raise ValidationError(
                        f"{node_type} actions require matching raw annotation {channel}: "
                        + ", ".join(sorted(missing_channel))
                    )
        for evolution in action.get("evolution", []):
            validate_source_ids(evolution.get("source_ids", evolution.get("sources", [])), allowed_sources)
        for replacement in action.get("replacements", []):
            validate_source_ids(replacement.get("source_ids", replacement.get("sources", [])), allowed_sources)
            replacement_type = str(replacement.get("type") or (target.type if target else ""))
            validate_node_content(replacement, replacement_type, allowed_sources)
            if replacement_type == "entity" and replacement.get("entity_kind") not in ENTITY_KINDS:
                raise ValidationError("split entity replacement requires valid entity_kind")
            if replacement_type == "event":
                validate_event_action(replacement, None, allowed_sources)
    for candidate in plan.candidates:
        kind = candidate.get("kind")
        if kind not in {"merge", "split", "topic"} or not candidate.get("node_ids"):
            raise ValidationError("candidate requires kind=merge|split|topic and node_ids")
        if kind == "topic":
            if (
                candidate.get("topic_kind") not in {"life_domain", "cross_domain_pattern", "longitudinal_arc"}
                or candidate.get("status") not in {"pending", "watching", "rejected", "materialized"}
                or not str(candidate.get("title", "")).strip()
                or not str(candidate.get("reason", "")).strip()
            ):
                raise ValidationError("topic candidate requires title, topic_kind, status, and reason")
        confidence = candidate.get("confidence", 0)
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValidationError("candidate confidence must be between 0 and 1")
    for edge in plan.out_edges:
        if not edge.get("source_ref") or not edge.get("target_ref") or not edge.get("type"):
            raise ValidationError("every out_edge requires source_ref, target_ref, and type")
    if plan.mode in {"incremental", "rebuild"}:
        belongs_to_edges = [edge for edge in plan.out_edges if edge.get("type") == "belongs_to"]
        belongs_to_pairs = {
            (str(edge.get("source_ref")), str(edge.get("target_ref")))
            for edge in belongs_to_edges
        }
        linked_raw = {str(edge.get("source_ref")) for edge in belongs_to_edges}
        annotated_raw = {
            raw_id
            for raw_id, annotation in annotations.items()
            if any(annotation.get(channel) for channel in ["mentions", "occurrences", "claims"])
        }
        missing = annotated_raw - linked_raw
        if missing:
            raise ValidationError("compiled raw entries require belongs_to edges: " + ", ".join(sorted(missing)))
        for edge in belongs_to_edges:
            pair = (str(edge.get("source_ref")), str(edge.get("target_ref")))
            if pair not in action_sources:
                raise ValidationError("belongs_to edges must match a source-grounded node action")
        missing_edges = sorted(action_sources - belongs_to_pairs)
        if missing_edges:
            raise ValidationError(
                "source-grounded node actions require matching belongs_to edges: "
                + ", ".join(f"{raw_id}->{node_ref}" for raw_id, node_ref in missing_edges)
            )
    if plan.mode in {"consolidate", "topics"}:
        validate_topic_plan(
            plan.node_actions,
            plan.out_edges,
            existing_nodes,
            lookup,
            replace_all=plan.mode == "topics",
            candidates=plan.candidates,
        )
    if len(plan.consolidation_memo) > 2000:
        raise ValidationError("consolidation_memo exceeds 2000 characters")


def validate_node_content(action: dict[str, Any], node_type: str, allowed_sources: set[str]) -> None:
    content = action.get("content")
    if not isinstance(content, dict):
        raise ValidationError("node action requires content")
    summary = str(content.get("summary", "")).strip()
    if not summary or summary != str(action.get("summary", "")).strip():
        raise ValidationError("content.summary must be non-empty and equal action.summary")
    key_points = content.get("key_points")
    if not isinstance(key_points, list):
        raise ValidationError("content.key_points must be an array")
    content_issues = node_content_quality_issues(
        node_type,
        summary,
        str(content.get("detail", "")).strip(),
        key_points,
    )
    if content_issues:
        raise ValidationError("weak node content: " + content_issues[0])
    evidence = content.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValidationError("content.evidence requires at least one source-grounded claim")
    for item in evidence:
        if not isinstance(item, dict) or not str(item.get("claim", "")).strip():
            raise ValidationError("every content evidence item requires a claim")
        if not str(item.get("source_id", "")).strip():
            raise ValidationError("every content evidence item requires source_id")
        validate_source_ids([item.get("source_id")], allowed_sources)
    uncertainties = content.get("uncertainties")
    if not isinstance(uncertainties, list) or any(not str(value).strip() for value in uncertainties):
        raise ValidationError("content.uncertainties must be an array of non-empty strings")


def validate_event_action(action: dict[str, Any], target: Node | None, allowed_sources: set[str]) -> None:
    semantics = action.get("semantics")
    if not isinstance(semantics, dict):
        raise ValidationError("event action requires semantics")
    required = {
        "subject_role",
        "action",
        "event_basis",
        "standalone_reason",
        "object_refs",
        "started_at",
        "ended_at",
        "time_precision",
        "factuality",
        "location_ref",
        "confidence",
        "evidence",
    }
    missing = required - set(semantics)
    if missing:
        raise ValidationError("event semantics missing fields: " + ", ".join(sorted(missing)))
    if not isinstance(semantics.get("object_refs"), list):
        raise ValidationError("event semantics object_refs must be an array")
    if semantics.get("ended_at") is not None and not str(semantics.get("ended_at", "")).strip():
        raise ValidationError("event semantics ended_at must be null or a temporal value")
    if semantics.get("location_ref") is not None and not str(semantics.get("location_ref", "")).strip():
        raise ValidationError("event semantics location_ref must be null or a node reference")
    if semantics.get("subject_role") not in EVENT_SUBJECT_ROLES:
        raise ValidationError("event semantics requires user-centered subject_role")
    if not str(semantics.get("action", "")).strip():
        raise ValidationError("event semantics requires a concrete action or state change")
    title = str(action.get("title") or (target.title if target else ""))
    contract_issues = event_contract_issues(title, semantics)
    if contract_issues:
        raise ValidationError("event occurrence contract failed: " + contract_issues[0])
    started_at = str(semantics.get("started_at", ""))
    if not started_at:
        raise ValidationError("event semantics requires started_at")
    try:
        start_value, start_has_time = parse_temporal_anchor(started_at)
    except ValueError as exc:
        raise ValidationError(f"invalid event semantics started_at: {started_at}") from exc
    end_value = None
    if semantics.get("ended_at"):
        ended_at = str(semantics["ended_at"])
        try:
            end_value, end_has_time = parse_temporal_anchor(ended_at)
        except ValueError as exc:
            raise ValidationError(f"invalid event semantics ended_at: {ended_at}") from exc
        if end_has_time != start_has_time:
            raise ValidationError("event semantics started_at and ended_at must use compatible precision")
        try:
            ends_before_start = end_value < start_value
        except TypeError as exc:
            raise ValidationError("event semantics temporal anchors must use compatible timezone forms") from exc
        if ends_before_start:
            raise ValidationError("event semantics ended_at cannot precede started_at")
    time_precision = semantics.get("time_precision")
    if time_precision not in EVENT_TIME_PRECISIONS:
        raise ValidationError("event semantics requires valid time_precision")
    if time_precision == "minute" and not start_has_time:
        raise ValidationError("minute precision requires an ISO datetime started_at")
    if time_precision in {"day", "week", "month", "year"} and start_has_time:
        raise ValidationError(f"{time_precision} precision requires a date-only started_at")
    if time_precision == "range" and end_value is None:
        raise ValidationError("range precision requires ended_at")
    factuality = str(semantics.get("factuality", ""))
    if factuality not in EVENT_FACTUALITIES:
        raise ValidationError("event semantics requires occurred, ongoing, or planned factuality")
    confidence = semantics.get("confidence")
    if not isinstance(confidence, (int, float)) or not EVENT_CONFIDENCE_THRESHOLD <= confidence <= 1:
        raise ValidationError(f"event confidence must be between {EVENT_CONFIDENCE_THRESHOLD} and 1")
    evidence = semantics.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValidationError("event semantics requires factual evidence")
    for item in evidence:
        if not isinstance(item, dict) or not str(item.get("claim", "")).strip():
            raise ValidationError("every event evidence item requires a claim")
        if not str(item.get("source_id", "")).strip():
            raise ValidationError("every event evidence item requires source_id")
        validate_source_ids([item.get("source_id")], allowed_sources)
    event_date = str(action.get("event_date") or (target.event_date if target else ""))
    status = str(action.get("status") or (target.status if target else ""))
    if not event_date or status not in EVENT_STATUSES:
        raise ValidationError("event action requires event_date and valid status")
    if event_date != start_value.date().isoformat():
        raise ValidationError("event_date must match semantics.started_at")
    if status in {"planned", "ongoing", "occurred"} and status != factuality:
        raise ValidationError("event status must match semantics.factuality")


def validate_source_ids(values: Any, allowed: set[str]) -> None:
    for value in values or []:
        if str(value) not in allowed:
            raise ValidationError(f"source is outside compile scope: {value}")


def validate_raw_annotation_channels(annotation: dict[str, Any], raw_id: str) -> None:
    for mention in annotation["mentions"]:
        if (
            not isinstance(mention, dict)
            or not str(mention.get("text", "")).strip()
            or mention.get("kind") not in ENTITY_KINDS
            or not valid_confidence(mention.get("confidence"))
            or (mention.get("target_id") is not None and not str(mention.get("target_id", "")).strip())
        ):
            raise ValidationError(f"invalid raw annotation mention: {raw_id}")
    for occurrence in annotation["occurrences"]:
        if not isinstance(occurrence, dict):
            raise ValidationError(f"invalid raw annotation occurrence: {raw_id}")
        try:
            parse_temporal_anchor(str(occurrence.get("started_at", "")))
        except ValueError as exc:
            raise ValidationError(f"invalid raw annotation occurrence: {raw_id}") from exc
        if (
            not str(occurrence.get("action", "")).strip()
            or occurrence.get("subject_role") not in EVENT_SUBJECT_ROLES
            or occurrence.get("factuality") not in EVENT_FACTUALITIES
            or occurrence.get("event_basis") not in EVENT_BASES
            or len("".join(str(occurrence.get("standalone_reason", "")).split())) < 20
            or not valid_confidence(occurrence.get("confidence"), EVENT_CONFIDENCE_THRESHOLD)
        ):
            raise ValidationError(f"invalid raw annotation occurrence: {raw_id}")
        occurrence_issues = event_contract_issues(str(occurrence.get("action", "")), occurrence)
        if occurrence_issues:
            raise ValidationError(f"invalid raw annotation occurrence: {raw_id}: {occurrence_issues[0]}")
    allowed_claim_kinds = {"preference", "goal", "belief", "plan", "decision", "feeling", "method", "insight"}
    for claim in annotation["claims"]:
        if not isinstance(claim, dict) or claim.get("kind") not in allowed_claim_kinds or not str(claim.get("text", "")).strip():
            raise ValidationError(f"invalid raw annotation claim: {raw_id}")


def annotation_supports_action(
    annotation: dict[str, Any],
    channel: str,
    action: dict[str, Any],
    target: Node | None,
) -> bool:
    items = list(annotation.get(channel, []))
    if channel == "mentions":
        if target and any(str(item.get("target_id", "")) == target.id for item in items):
            return True
        names = [
            str(action.get("title") or (target.title if target else "")),
            *[str(value) for value in action.get("aliases", [])],
            *([*target.aliases] if target else []),
        ]
        inherited = target.title if target else None
        return any(
            any(semantic_overlap(str(item["text"]), name, inherited=inherited) for name in names if name)
            for item in items
        )
    if channel == "occurrences":
        semantics = action.get("semantics") or (target.semantics if target else {})
        if target and not event_material_facts_compatible(target.semantics, semantics):
            return False
        try:
            action_time, action_has_time = parse_temporal_anchor(str(semantics.get("started_at", "")))
        except ValueError:
            return False
        for item in items:
            try:
                occurrence_time, occurrence_has_time = parse_temporal_anchor(str(item.get("started_at", "")))
            except ValueError:
                continue
            if (
                item.get("subject_role") == semantics.get("subject_role")
                and item.get("factuality") == semantics.get("factuality")
                and item.get("event_basis") == semantics.get("event_basis")
                and semantic_overlap(
                    str(item.get("standalone_reason", "")),
                    str(semantics.get("standalone_reason", "")),
                    minimum_ratio=0.25,
                )
                and occurrence_has_time == action_has_time
                and occurrence_time == action_time
                and semantic_overlap(
                    str(item.get("action", "")),
                    str(semantics.get("action", "")),
                    inherited=str(target.semantics.get("action", "")) if target else None,
                )
            ):
                return True
        return False
    if channel == "claims":
        values = [
            str(action.get("current_state") or (target.current_state if target else "")),
            str(action.get("summary") or (target.summary if target else "")),
            str(action.get("title") or (target.title if target else "")),
        ]
        return any(
            any(
                semantic_overlap(
                    str(item["text"]),
                    value,
                    minimum_ratio=0.25,
                    inherited=target.current_state if target else None,
                )
                for value in values
                if value
            )
            for item in items
        )
    return False


def semantic_overlap(
    left: str,
    right: str,
    *,
    minimum_ratio: float = 0.5,
    inherited: str | None = None,
) -> bool:
    first = "".join(char for char in normalize_name(left) if char.isalnum())
    second = "".join(char for char in normalize_name(right) if char.isalnum())
    if not first or not second:
        return False
    if not material_facts_compatible(first, second, inherited=inherited):
        return False
    if has_explicit_negation(first) != has_explicit_negation(second):
        return False
    if first in second or second in first:
        return True
    first_pairs = {first[index : index + 2] for index in range(len(first) - 1)}
    second_pairs = {second[index : index + 2] for index in range(len(second) - 1)}
    shared = first_pairs & second_pairs
    smaller = min(len(first_pairs), len(second_pairs))
    return len(shared) >= 2 and smaller > 0 and len(shared) / smaller >= minimum_ratio


def material_facts_compatible(left: str, right: str, *, inherited: str | None = None) -> bool:
    first = material_fact_tokens(left)
    second = material_fact_tokens(right)
    if not first and not second:
        return True
    if not first or not second:
        if not inherited:
            return False
        inherited_facts = material_fact_tokens(inherited)
        return bool(inherited_facts) and (first or second) <= inherited_facts
    return first <= second or second <= first


def material_fact_tokens(value: str) -> set[str]:
    normalized = "".join(char for char in normalize_name(value) if char.isalnum())
    pattern = re.compile(r"[a-z]+[a-z0-9]*\d+[a-z0-9]*|(?<![a-z])\d+(?:\.\d+)?|[零〇一二三四五六七八九十百千万两]+")
    return set(pattern.findall(normalized))


def event_material_facts_compatible(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_facts = material_fact_tokens(str(previous.get("action", ""))) - temporal_fact_tokens(previous)
    current_facts = material_fact_tokens(str(current.get("action", ""))) - temporal_fact_tokens(current)
    if not previous_facts and not current_facts:
        return True
    if not previous_facts or not current_facts:
        return False
    return previous_facts <= current_facts or current_facts <= previous_facts


def temporal_fact_tokens(semantics: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for field in ("started_at", "ended_at"):
        value = str(semantics.get(field) or "")
        parts = re.findall(r"\d+", value)
        tokens.update(parts)
        tokens.update(part.lstrip("0") or "0" for part in parts)
        if parts:
            tokens.add("".join(parts))
            tokens.add("".join(part.lstrip("0") or "0" for part in parts))
    return tokens


def has_explicit_negation(value: str) -> bool:
    terms = ("没", "未能", "未曾", "尚未", "未完成", "未发生", "未参加", "未支付", "未开始", "未结束", "无", "不", "取消", "拒绝", "停止", "放弃", "not", "never", "without", "cancel")
    return any(term in value for term in terms)


def valid_confidence(value: Any, minimum: float = 0.0) -> bool:
    return isinstance(value, (int, float)) and minimum <= value <= 1


def follow_redirect(node_id: str, redirects: dict[str, str]) -> str:
    seen: set[str] = set()
    while node_id in redirects and node_id not in seen:
        seen.add(node_id)
        node_id = redirects[node_id]
    return node_id


def redirects_without_removed_topics(
    redirects: dict[str, str],
    removed_topic_ids: set[str],
    live_topic_ids: set[str],
) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for source, target in redirects.items():
        if source in live_topic_ids:
            continue
        cursor = str(source)
        seen: set[str] = set()
        touches_removed = False
        while cursor in redirects and cursor not in seen:
            if cursor in removed_topic_ids:
                touches_removed = True
                break
            seen.add(cursor)
            cursor = str(redirects[cursor])
        if cursor in removed_topic_ids:
            touches_removed = True
        if not touches_removed:
            cleaned[str(source)] = str(target)
    return cleaned


def build_raw_annotations(repo: Path, plan: CompilePlan, entries: list[RawEntry], edges: list[dict[str, Any]]) -> dict[str, str]:
    annotations = {str(item["raw_id"]): item for item in plan.raw_annotations}
    belongs: dict[str, list[str]] = {}
    for edge in edges:
        if edge["type"] == "belongs_to":
            belongs.setdefault(str(edge["source"]), []).append(str(edge["target"]))
    staged: dict[str, str] = {}
    for entry in entries:
        annotation = annotations.get(entry.id)
        meta, body = frontmatter.read_document(entry.path)
        before = sha256_text(body)
        if annotation:
            meta["summary"] = str(annotation["summary"])
            meta["importance"] = int(annotation["importance"])
            if annotation.get("emotion"):
                meta["emotion"] = str(annotation["emotion"])
            else:
                meta.pop("emotion", None)
            meta["mentions"] = list(annotation["mentions"])
            meta["occurrences"] = list(annotation["occurrences"])
            meta["claims"] = list(annotation["claims"])
            meta["compiled"] = True
        meta["belongs_to"] = sorted(set(belongs.get(entry.id, [])))
        content = frontmatter.dump_document(meta, body)
        _, after_body = frontmatter.parse_document(content)
        if sha256_text(after_body) != before:
            raise ValidationError(f"raw body changed while annotating {entry.id}")
        if content != entry.path.read_text(encoding="utf-8"):
            staged[relpath(entry.path, repo)] = content
    return staged


def build_manifest(
    repo: Path,
    wiki_root: Path,
    *,
    compiled_raw: list[str],
    edges: list[dict[str, Any]],
    redirects: dict[str, str],
    candidates: list[dict[str, Any]],
    consolidation: dict[str, Any],
    tips_seen: list[str],
    rebuild: dict[str, Any],
    session_id: str,
    raw_entries: dict[str, RawEntry],
) -> dict[str, Any]:
    pages: dict[str, dict[str, Any]] = {}
    for path in sorted(wiki_root.rglob("*.md")):
        meta, _ = frontmatter.read_document(path)
        if meta.get("id"):
            final_relative = Path("wiki") / path.relative_to(wiki_root)
            pages[str(meta["id"])] = {
                "path": final_relative.as_posix(),
                "type": str(meta.get("type", "")),
                "title": str(meta.get("title", meta["id"])),
                "summary": str(meta.get("summary", "")),
                "aliases": sorted(set(str(value) for value in meta.get("aliases", []))),
                "sources": sorted(set(str(value) for value in meta.get("sources", []))),
                "content_hash": sha256_text(path.read_text(encoding="utf-8")),
            }
    raw_hashes = {
        raw_id: {"path": relpath(entry.path, repo), "body_hash": sha256_text(entry.body)}
        for raw_id, entry in sorted(raw_entries.items())
    }
    return {
        "schema": 2,
        "kb_version": KB_VERSION,
        "compiled_raw": sorted(set(compiled_raw)),
        "raw_hashes": raw_hashes,
        "pages": pages,
        "edges": edges,
        "redirects": dict(sorted(redirects.items())),
        "candidates": candidates,
        "consolidation": consolidation,
        "tips_seen": sorted(set(tips_seen)),
        "rebuild": rebuild,
        "applied_session_id": session_id,
    }


def next_consolidation_state(old_manifest: dict[str, Any], plan: CompilePlan, consumed: list[str], entries: list[RawEntry]) -> dict[str, Any]:
    state = consolidation_state(old_manifest)
    if plan.mode == "topics":
        return state
    queue = list(state["pending_raw"])
    memo = str(state["memo"])
    last_session = state.get("last_session_id")
    if plan.mode in {"incremental", "rebuild"}:
        queue = list(dict.fromkeys([*queue, *consumed]))
    else:
        batch_ids = [entry.id for entry in entries]
        if queue[: len(batch_ids)] != batch_ids:
            raise StaleSessionError("consolidation queue changed before apply")
        queue = queue[len(batch_ids) :]
        memo = plan.consolidation_memo
        last_session = plan.session_id
    return {"pending_raw": queue, "memo": memo, "last_session_id": last_session}


def next_rebuild_manifest_state(old_manifest: dict[str, Any], plan: CompilePlan, consumed: list[str]) -> dict[str, Any]:
    state = rebuild_manifest_state(old_manifest)
    if plan.mode != "rebuild":
        return state
    if state["phase"] != "replay":
        raise ValidationError("rebuild replay state is not active")
    if state["total"] == 0 and not consumed:
        return {**state, "phase": "consolidate", "cursor": 0, "last_session_id": plan.session_id}
    if len(consumed) != 1:
        raise ValidationError("rebuild must consume exactly one raw entry per step")
    expected = state["ordered_raw_ids"][state["cursor"] : state["cursor"] + 1]
    if consumed != expected:
        raise StaleSessionError("rebuild cursor changed before apply")
    cursor = state["cursor"] + 1
    return {
        **state,
        "phase": "consolidate" if cursor >= state["total"] else "replay",
        "cursor": cursor,
        "last_session_id": plan.session_id,
    }


def normalize_plan_candidates(
    candidates: list[dict[str, Any]],
    refs: dict[str, str],
    redirects: dict[str, str],
    nodes: dict[str, Node],
    resolved_targets: set[str] | None = None,
) -> list[dict[str, Any]]:
    normalized = []
    for item in candidates:
        if {str(value) for value in item.get("node_ids", [])} & (resolved_targets or set()):
            continue
        node_ids = [follow_redirect(refs.get(str(value), str(value)), redirects) for value in item.get("node_ids", [])]
        unknown = [node_id for node_id in node_ids if node_id not in nodes]
        if unknown:
            raise ValidationError("candidate references unknown nodes: " + ", ".join(sorted(unknown)))
        value = {**item, "node_ids": list(dict.fromkeys(node_ids))}
        if value.get("kind") == "merge" and len(value["node_ids"]) < 2:
            continue
        value["candidate_id"] = str(item.get("candidate_id") or candidate_identity(value))
        normalized.append(value)
    return normalized


def normalize_retained_candidates(
    candidates: list[dict[str, Any]],
    redirects: dict[str, str],
    nodes: dict[str, Node],
    resolved_targets: set[str],
) -> list[dict[str, Any]]:
    retained = [
        item
        for item in candidates
        if not ({str(value) for value in item.get("node_ids", [])} & resolved_targets)
    ]
    return normalize_plan_candidates(retained, {}, redirects, nodes)


def next_candidates(old_manifest: dict[str, Any], plan_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = [*old_manifest.get("candidates", []), *plan_candidates]
    unique: dict[str, dict[str, Any]] = {}
    semantic_keys: dict[str, str] = {}
    for item in values:
        semantic_key = candidate_identity(item)
        key = str(item.get("candidate_id") or semantic_key)
        previous_key = semantic_keys.get(semantic_key)
        if previous_key and previous_key != key:
            unique.pop(previous_key, None)
        unique[key] = {**item, "candidate_id": key}
        semantic_keys[semantic_key] = key
    return [unique[key] for key in sorted(unique)]


def candidate_identity(item: dict[str, Any]) -> str:
    payload = {
        "kind": str(item.get("kind", "")),
        "node_ids": sorted(set(str(value) for value in item.get("node_ids", []))),
        "title": str(item.get("title", "")),
        "topic_kind": str(item.get("topic_kind", "")),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "candidate-" + short_hash(canonical, length=16)


def candidates_payload(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**item, "candidate_id": str(item.get("candidate_id") or candidate_identity(item))}
        for item in candidates
    ]


def load_manifest(repo: Path) -> dict[str, Any]:
    path = repo / ".kb" / "manifest.json"
    if not path.exists():
        return empty_manifest()
    return json.loads(path.read_text(encoding="utf-8"))


def consolidation_state(manifest: dict[str, Any]) -> dict[str, Any]:
    value = manifest.get("consolidation", {})
    return {
        "pending_raw": list(value.get("pending_raw", [])),
        "memo": str(value.get("memo", "")),
        "last_session_id": value.get("last_session_id"),
    }


def list_index_pages(repo: Path) -> list[Node]:
    nodes, _ = load_nodes(repo)
    return sorted(nodes.values(), key=lambda node: node.id)


def list_compiled_pages(repo: Path) -> list[Node]:
    nodes, _ = load_nodes(repo)
    pages = list(nodes.values())
    timeline = repo / "wiki" / "timeline"
    if timeline.exists():
        for path in sorted(timeline.glob("*.md")):
            meta, body = frontmatter.read_document(path)
            if meta.get("id"):
                pages.append(Node(
                    id=str(meta["id"]),
                    type="timeline",
                    title=str(meta.get("event_date") or meta["id"]),
                    summary=first_body_line(body),
                    path=path,
                    sources=list(meta.get("sources", [])),
                    event_date=str(meta.get("event_date", "")),
                    body=body,
                ))
    return pages


def node_payload(node: Node, *, include_edges: bool = False, include_content: bool = True) -> dict[str, Any]:
    payload = {
        "id": node.id,
        "type": node.type,
        "title": node.title,
        "aliases": node.aliases,
        "summary": node.summary,
        "sources": node.sources,
        "entity_kind": node.entity_kind,
        "event_kind": node.event_kind,
        "status": node.status,
        "event_date": node.event_date,
        "current_state": node.current_state,
        "evolution": node.evolution,
    }
    if include_content:
        payload["content"] = {
            "summary": node.summary,
            "detail": node.detail,
            "key_points": node.key_points,
            "evidence": node.evidence,
            "uncertainties": node.uncertainties,
        }
        payload["semantics"] = node.semantics
    elif node.semantics:
        payload["semantics"] = {
            key: node.semantics.get(key)
            for key in ["subject_role", "action", "event_basis", "standalone_reason", "object_refs", "started_at", "time_precision", "factuality", "location_ref", "confidence"]
        }
    if include_edges:
        payload["attrs"] = node.attrs
        payload["out_edges"] = node.out_edges
        payload["backrefs"] = node.backrefs
    return payload


def raw_payload(entry: RawEntry, *, include_body: bool, include_annotations: bool = True) -> dict[str, Any]:
    payload = {
        "id": entry.id,
        "title": entry.title,
        "created": entry.created,
        "event_date": entry.event_date,
        "tags": entry.tags,
        "annotations": entry.annotations if include_annotations else {},
    }
    if include_body:
        payload["body"] = entry.body
    return payload


def skill_code_commit() -> str | None:
    root = skill_repo_root()
    if not (root / ".git").exists():
        return None
    return GitStorage(root).full_commit()


def version_drift(repo: Path) -> bool:
    return load_manifest(repo).get("kb_version") != KB_VERSION


def manifest_drift(repo: Path) -> list[str]:
    manifest = load_manifest(repo)
    drift: list[str] = []
    for page_id, info in manifest.get("pages", {}).items():
        path = repo / info["path"]
        if not path.exists() or sha256_text(path.read_text(encoding="utf-8")) != info.get("content_hash"):
            drift.append(str(page_id))
    lookup = raw_lookup(repo)
    for raw_id, info in manifest.get("raw_hashes", {}).items():
        if raw_id not in lookup or sha256_text(lookup[raw_id].body) != info.get("body_hash"):
            drift.append(f"raw:{raw_id}")
    return sorted(drift)


def graph_stats(repo: Path) -> dict[str, int]:
    nodes, _ = load_nodes(repo)
    counts = {node_type: sum(1 for node in nodes.values() if node.type == node_type) for node_type in sorted(NODE_TYPES)}
    counts["edges"] = len(load_manifest(repo).get("edges", []))
    counts["timeline"] = len(list((repo / "wiki" / "timeline").glob("*.md"))) if (repo / "wiki" / "timeline").exists() else 0
    return counts


def determine_update_mode(repo: Path) -> dict[str, Any]:
    pending = read_pending(repo)
    drift = manifest_drift(repo)
    version_changed = version_drift(repo)
    consolidation = consolidation_state(load_manifest(repo))
    rebuild = rebuild_state(repo)
    quality_repair = False
    repair_issues = {"weak_detail_node_ids": [], "weak_evidence_node_ids": []}
    if rebuild["active"] or version_changed or drift:
        mode = "rebuild"
    elif pending:
        mode = "incremental"
    elif len(consolidation["pending_raw"]) >= CONSOLIDATION_BATCH_SIZE or is_promoted_rebuild_tail_recovery(repo):
        mode = "consolidate"
    elif is_quality_repair_due(repo):
        mode = "consolidate"
        quality_repair = True
        repair_issues = quality_repair_issues(repo)
    else:
        mode = "noop"
    return {
        "mode": mode,
        "pending": len(pending),
        "drift": drift,
        "version_changed": version_changed,
        "consolidation_pending": len(consolidation["pending_raw"]),
        "rebuild": rebuild,
        "quality_repair": quality_repair,
        **repair_issues,
    }


def commit_message(command: str, mode: str, raw_ids: list[str], pages: list[str]) -> str:
    if mode == "rebuild":
        subject = "chore(rebuild): 重建 v2.4 编译图谱"
    elif mode == "topics":
        subject = "feat(memory): 重建高维主题组织"
    elif mode == "consolidate":
        subject = "feat(memory): 整理稳定主题与图谱关系"
    else:
        subject = "feat(memory): 编译新增个人记忆"
    return (
        f"{subject}\n\n"
        f"command: {command}\n"
        f"mode: {mode}\n"
        f"raw-ids: {', '.join(raw_ids) if raw_ids else 'none'}\n"
        f"nodes: {', '.join(pages) if pages else 'none'}\n"
        f"kb-version: {KB_VERSION}\n"
    )


def set_readonly(path: Path) -> None:
    try:
        os.chmod(path, 0o444)
    except OSError:
        pass


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


__all__ = [
    "CONSOLIDATION_BATCH_SIZE",
    "add_raw",
    "all_raw_entries",
    "apply_rebuild_response",
    "apply_response",
    "build_compile_request",
    "build_consolidation_request",
    "build_rebuild_request",
    "build_topic_request",
    "consolidation_state",
    "create_session_id",
    "determine_update_mode",
    "graph_stats",
    "finalize_rebuild",
    "initialize",
    "list_compiled_pages",
    "list_index_pages",
    "load_manifest",
    "manifest_drift",
    "raw_lookup",
    "read_pending",
    "rebuild_state",
    "rebuild_workspace",
    "skill_code_commit",
    "transaction_state",
    "version_drift",
]
