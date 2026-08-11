---
name: second-memory
description: Use when the user wants to save personal notes, diary entries, reflections, work reviews, decisions, events, or long-term preferences into a local second-memory knowledge base; when durable personal context is worth offering to save; when answering questions that may benefit from accumulated local knowledge; or when the user asks for recap, review, search, Wiki HTML export, rebuild, consolidation, or maintenance.
---

# Second Memory Skill

This skill connects the host Agent to a local Markdown + Git personal knowledge base through the `second-memory` CLI. The CLI performs deterministic storage, validation, graph projection, and transaction work; the host Agent performs semantic reasoning through the emitted two-stage protocol.

## Core Rules

- Answer the current conversation first. Treat knowledge-base content as the user's historical context, never as authoritative external truth.
- When the user explicitly says “记一下”, “保存”, or “存入知识库”, save directly. For merely durable conversation content, ask for confirmation before running any write command.
- Every successful save must end with a recap based on `data.recap_request`; do not stop after reporting a CLI success.
- Never edit a file under `raw/` directly. Use `add` for new user content. The CLI may add validated metadata while preserving the raw body hash and restoring read-only permissions.
- Never send the whole raw archive for ordinary search or consolidation. Search Level 1 first; request Level 2 only for relevant candidates. Consolidation uses the bounded batch supplied by the CLI.
- The CLI never calls an LLM. For compile, rebuild, consolidate, update, review, and Level-2 search, emit a request, reason over only that request, then return JSON matching its `response_schema`.
- Preserve the `schema_version`, `session_id`, and `mode` from the emitted request. Never reuse an old response or infer a different apply mode.
- Incremental compile and rebuild replay may create or update entity, event, and statement nodes. They may record merge/split candidates, but must not execute structural merge/split actions or create topics.
- Topics are graph-wide reading lenses above individual insights, not large insights and not batch summaries. “Higher-dimensional” means organizing several independent records, times, and node types into a durable human-readable view; it does not mean unrelated domains must be mixed. Use `life_domain` for a recurring field such as AI collaboration, `longitudinal_arc` for an evolving issue such as sleep, and `cross_domain_pattern` only when the same mechanism is genuinely evidenced across domains.
- A topic must directly contain at least five members, including at least two statements and evidence from at least three independent raw capture sessions. Direct members may be raw, entity, event, statement, or child topic; child topics may have multiple parents, and topic depth must not exceed three. Never use an entity, event, raw, or generic statement as quota filler: every member needs a source-grounded facet, reason, and excerpt that independently contributes to the organizing question.
- Topic membership requires one coherent organizing question, a facet assignment and a specific inclusion reason for every member. Sharing a date, source batch, emotion, generic word, or bridge sentence is never enough. Every ten compiled raw entries trigger a graph-wide topic audit, not a ten-record topic boundary or a fixed one-topic quota. Repeated cohorts must become a topic or a stable topic candidate with an explicit `pending|watching|rejected|materialized` status and reason; never let a recurring cohort silently disappear.
- Treat entity, event, and statement as independent extraction channels. One raw may contribute all three; never force the raw into a single primary type.
- Bind every source-grounded entity/event/statement action to a non-empty `mentions`/`occurrences`/`claims` channel on the same raw. Before choosing source-only `reinforce`, compare the new source with the node's complete summary, detail, evidence, uncertainties and history. Use source-only `reinforce` only during incremental or rebuild replay, and only when the mention is genuinely repetitive and the new source would not add history, invalidate an uncertainty, change the synthesis or make any sentence stale. Otherwise return a full `refine` with content synthesized from all old and new sources. Source-only `reinforce` contains only `target_id`, `type`, and `source_ids`—never the legacy `sources` alias—omits `content` and all mutable fields, and emits the matching raw-to-node `belongs_to` edge; it only expands source coverage.
- Resolve every durable entity mention, even when the entity already exists. Emit explicit `event|statement -> entity` `involves|about|instance_of` edges when the source proves that relation. Entity sources are the deterministic union of direct raw mentions and raw sources carried by explicitly linked events or insights; do not infer this closure from mere lexical similarity or shared dates.
- A raw is allowed to compile with empty extraction channels, zero node actions, and zero `belongs_to` edges when it contains no durable referent, timeline-worthy occurrence, or reusable insight. Never invent a micro-event or generic statement merely to make a raw point at a node; the immutable raw remains searchable and auditable.
- Entity is a stable named referent. Extract people, organizations, places, works/books, products, tools, projects, tasks, objects, concepts, and emotions when they have durable reference value. A belief, method, relationship pattern, or evolving practice is a statement, not an entity.
- Event is a timeline-worthy occurrence, not a dated insight. After removing every reflection, interpretation, lesson, intention, and emotional label, the raw must still leave one bounded fact about what the user did, attended, completed, encountered, transacted, or was materially affected by. A routine chat, routine reading session, fleeting feeling, self-observation, generic intention, or recurring habit is not an event merely because it has a date.
- Every event must declare `event_basis=appointment|scheduled_commitment|incident|milestone|transaction|material_change` and a source-grounded `standalone_reason` explaining why the occurrence remains useful on a timeline without its attached insight. Planned events are limited to an appointment, scheduled commitment, or milestone with an explicit commitment and time anchor. Select the basis from the observable occurrence itself, never from the importance of the attached reflection.
- Event titles and `semantics.action` must be the same occurrence-only phrase. Move interpretations such as “觉察／识别／反思／重构／复盘／理解／整合／思考／捕捉／感悟／发现自己的模式／收到启发／发生认知改变” into a statement node and relate it to the event. For example, use event “参加了第 19 次心理咨询” plus a separate insight about the changed reliability standard; do not use “第 19 次心理咨询重构靠谱标准” as an event. “发送周报时把周会时间写错” is a valid incident because an observable error occurred; “与对象聊天时捕捉表达自我监控” is an insight, and “在 7 月 13 日与对象聊天” is still too routine and small to promote. An `incident` needs a concrete observable result, mistake, direct impact, or bounded experience; generic verbs such as “发生／收到／遇到” never prove this by themselves, and calling a routine activity an incident does not make it one.
- Product language calls statement nodes “洞察”. Keep `type=statement` in the storage protocol, but never present the user-facing label “陈述”.
- Every created or substantively updated node must include a source-grounded `content` object with a short summary, detailed synthesis, key points, evidence claims, and uncertainties. The detail must be independently readable rather than an expanded caption: at least two substantive paragraphs, at least four distinct factual or analytical sentences, and three concrete non-duplicative key points. Start the two required paragraphs with the type-specific labels exactly: entity `对象与关系：` then `历史与现状：`; event `发生与背景：` then `结果与关联：`; statement `洞察与依据：` then `演进与影响：`; topic `组织视角：` then `脉络与边界：`. Each paragraph must answer its label directly and synthesize all attached source history. Every sentence must be specific to that node's own sources, evidence, semantic fields, or history. Never reuse the same long generic sentence or paragraph across different nodes; shared short terms and verbatim evidence excerpts are allowed. A topic must additionally return `attrs.topic_reading` with `core_understanding`, source-grounded `evolution`, explicit `contradictions`, `open_questions`, and `confidence`; empty contradiction or question arrays are valid only when the evidence does not support one. Do not reuse a generic paragraph across node types, repeat a sentence to reach a length limit, concatenate raw passages, or invent detail; put unresolved gaps in `uncertainties`.

## Setup

From this repository:

```bash
scripts/setup.sh
```

Use `SECOND_MEMORY_REPO` to select the runtime knowledge base, or pass `--repo` on every command.

```bash
second-memory init --scope shared --json
second-memory status --json
```

## Explicit Save

```bash
printf '%s' "$TEXT" | second-memory add \
  --title "$TITLE" \
  --event-date YYYY-MM-DD \
  --stdin --json

second-memory compile --emit-request --json
```

Read `data.llm_request`. Return one V2.4 contract `CompilePlan v2` matching its schema:

- Annotate each consumed raw with a concise summary, importance, optional emotion, and three explicit arrays: `mentions` for durable referents, `occurrences` for possible user-centered events, and `claims` for possible insight threads. Empty arrays are valid only when no action of the corresponding entity/event/statement type cites that raw; omitting a channel is never valid.
- Returning zero node actions and zero `belongs_to` edges is valid when all three channels are empty. Prefer that outcome over promoting a routine chat, routine reading trigger, momentary state, or generic observation into a durable node.
- Reuse an existing node with `target_id` when the resolver context identifies it.
- Use a plan-local `ref` for a new node. The CLI owns final IDs and paths.
- Use entity only as a stable person/project/concept/emotion anchor.
- Run entity mention extraction independently. Supported entity kinds are `person|organization|place|work|product|tool|project|task|object|concept|emotion`; a book such as `《贫穷的本质》` is a `work` entity.
- Create event only when `semantics` proves all six gates: the subject is the user or directly affects the user; an occurrence remains after removing interpretation; it has an explicit time anchor; factuality is occurred/ongoing or a concrete scheduled commitment; `event_basis` is one allowed timeline category; and `standalone_reason` explains its independent timeline value from source facts. Before creating it, run both counterfactual checks: if the reflection disappeared, would a bounded occurrence still remain; and would the occurrence still be useful in a monthly review without the insight. If either answer is no, keep only a statement.
- An attended consultation, meeting, trip, consequential mistake, completed delivery, transaction, milestone, or material external change may be an event. Routine reading, routine conversation, a momentary internal observation, a decision without execution, and a behavior pattern are statements. Finishing a work or reaching a reading milestone can be an event; merely reading and reflecting cannot.
- Event title and `semantics.action` must be the same normalized phrase and state only what happened, using an observable occurrence verb appropriate to `event_basis`. Never append its interpretation or lesson. A meeting title containing the noun “计划会” remains valid; an intention beginning with “计划做……” is a statement until scheduled. When one raw supports both, create the event and statement independently and connect them with an explicit edge.
- Event temporal anchors must be valid ISO dates or datetimes. `minute` precision requires a datetime, date-only precision must not contain a time, and `range` requires a non-decreasing `ended_at`.
- Use statement for decisions, preferences, goals, beliefs, plans, feelings, methods, and insights. “AI 协作”, “睡前拖延与未完成工作的压力”, and “与妈妈关系中的依赖、愧疚和边界” are statements unless a source separately proves a concrete event.
- Except for source-only `reinforce`, every create/reinforce/refine/change/supersede action returns `content.summary`, `content.detail`, `content.key_points`, `content.evidence`, and `content.uncertainties`. Detail must use the two exact type-specific paragraph labels above, contain at least four distinct substantive sentences, explicitly carry the node summary's central named concept, and cover all attached source history relevant to the node. Return at least three concrete key points of eight or more non-whitespace characters; at least two key points must repeat a central concept from the summary so they cannot be reused for an unrelated node. Every evidence item must reference an allowed raw source and state the supported claim. Never use generic filler such as “当前节点／相关内容／基本信息／未来查阅／后续检索／回顾／关系投影” unless the source itself makes that claim.
- In incremental and rebuild plans, always omit statement `evolution`; provide only `current_state` and `effective_date`. The CLI owns deterministic history append and rejects Agent-supplied evolution in these modes.
- Express relationships in `out_edges`; use raw-to-node `belongs_to` edges to associate source records. A matching `belongs_to` is mandatory for every source-grounded action, including source-only `reinforce`.
- Preserve source lineage. Do not copy large raw passages into summaries.

Apply the exact response:

```bash
printf '%s' "$COMPILE_PLAN_JSON" | \
  second-memory compile --apply-response --stdin --json
```

Report the saved raw ID, affected nodes, commit, and pending count. Then reason over `data.recap_request` and present a short connected recap.

## Opportunistic Save

When content looks durable but the user did not explicitly request storage, answer normally and ask:

> 这段内容看起来适合沉淀到你的第二记忆库。要我帮你存入吗？

Do not run `add`, `compile`, or any other write command until the user confirms. After confirmation, follow the full explicit-save workflow.

## Answer With Personal Context

```bash
second-memory search --query "$QUERY" --level 1 --json
```

If candidates are relevant, cite them as the user's historical notes. Only when deeper content is necessary:

```bash
second-memory search --query "$QUERY" --level 2 --emit-request --json
```

Use only the returned candidate nodes and bounded source snippets. Make every personal-history claim traceable to a returned source.

## Recap And Review

The apply result contains `recap_request` when raw entries were consumed. It bundles the new entries, related prior nodes, and on-this-day projections. Produce a user-facing recap with concrete connections and actionable follow-ups.

To regenerate one:

```bash
second-memory recap --json
second-memory recap --raw-id "$RAW_ID" --json
```

For periodic review:

```bash
second-memory review --range last-week --emit-request --json
second-memory review --on-this-day YYYY-MM-DD --emit-request --json
```

Generate the review only from `data.llm_request.context.timeline_pages` and their source references.

## Consolidation

Compiled raw IDs accumulate in a bounded consolidation queue. A batch becomes due at ten entries.

```bash
second-memory consolidate --emit-request --json
```

When `data.llm_request` is present, reason only over its bounded raw annotations, compact index, aliases, candidates, related one-hop nodes, graph-wide `member_catalog`／`raw_catalog`, and `source_dates`. The full-library catalogs contain compiled node content and raw annotations, never raw bodies. A consolidation plan may:

- create a qualified topic;
- merge nodes that confidently represent the same object;
- split a conflated node into explicit replacements;
- refine existing nodes and relationships;
- retain uncertain cases in `candidates` instead of forcing a structural change.

Consolidation receives the graph-wide member catalog and current topic membership in addition to the bounded batch. Topic actions must use `membership_mode=replace` and return a complete `attrs.topic_contract` containing:

- `topic_kind`: `life_domain|cross_domain_pattern|longitudinal_arc`;
- one durable `organizing_question` that every member helps answer;
- one `facet_relationship` explaining why the facets form a higher-dimensional whole instead of a list of neighboring insights;
- one operational `boundary_rule` that can decide membership without relying on broad shared words;
- at least two `facets`, each containing at least two `member_refs`;
- `member_rationales` covering every contained member exactly, with its facet, non-generic contribution, and a `supporting_excerpt` copied from that member's available raw annotation, entity/event/statement content, or child-topic synthesis; the member's own content and evidence must directly answer the organizing question and support that facet, while the rationale may explain but never invent a missing bridge;
- optional `exclusions` using `member_ref`, `nearby_excerpt`, and a boundary reason for genuinely close non-members.

Every topic action must also return `attrs.topic_reading`: a concrete `core_understanding`; a chronological `evolution` whose source IDs are member-grounded; `contradictions` that cite at least two conflicting members when present and whose source IDs actually cover every cited member; `open_questions` with evidence-based reasons; and numeric `confidence`. A member rationale may quote the member's compiled content, Raw annotation channels, or event semantics such as `standalone_reason`. Do not invent a contradiction or open question merely to populate a field.

Do not create a topic merely because the current ten-entry batch contains loosely related material. A topic must remain coherent against the whole member catalog. Conversely, do not confuse safety with scarcity: a coherent long-running life domain such as AI collaboration, sleep, or psychological themes is valid even when it is not cross-domain. Raw, entity, event, statement, and child-topic members are allowed only when their own evidence directly contributes.

Evaluate membership against the complete compiled statement, including evolution, not only its title or latest summary. For a domain topic, the cited excerpt must directly contain that domain's mechanism or decision variable; a generic mechanism does not belong merely because its source originated in that domain. Never borrow a marginal statement to reach the minimum count—remove or defer the whole topic if direct members fall below the contract.

Before finalizing each member, hide the proposed topic title, facet name, and rationale, then ask whether the member's own available annotation, state, synthesis, key points, semantics, or non-superseded evolution directly contributes to the organizing question. Reject the member if the rationale has to introduce a new domain such as finance, intimate attachment, or AI delivery that is absent from the member itself. Recalculate the member／statement／raw／facet thresholds after every rejection; do not replace a rejected marginal member with a generic item about action, feedback, responsibility, safety, scarcity, or immediate reward.

Treat any exact cross-node repetition of a non-evidence detail sentence with at least 24 normalized characters as weak detail. Treat it as a verbatim evidence excerpt only when, after removing at most one allowlisted prefix (`其直接依据是` or `它参与`), its entire normalized remainder exactly equals one of that node's own `evidence.claim` values. Never use contains or suffix matching. A single node is also weak when its detail substitutes compiler-policy prose such as `当前节点只确认`, `节点仅保留`, `节点不把`, `后续若出现新的实质信息`, or `后续实质变化需要` for source-specific facts; do not reject ordinary factual sentences merely because they contain `节点`. When the same normalized evidence claim is reused by multiple entities, the claim must directly name each entity through its normalized title, alias, or a conservative entity word form; do not infer specificity from broad 2-grams or nearby context.

If `update --emit-request` returns a zero-entry Consolidation with `quality_repair=true`, repair exactly the listed weak-detail and weak-evidence nodes through full source-grounded updates. This mode exists only for cross-node duplicate detail, single-node compiler-policy detail, or shared non-specific entity evidence already stored in the graph, consumes no pending raw, and must eliminate every defect class in the projected graph. Never fabricate an empty manual Consolidation request or use quality repair for ordinary short queues.

For `life_domain`, relationship type is part of the boundary: partner／parent-child attachment and mutual responsibility are not interchangeable with leader／mentor authority, resource access, group belonging, or general social evaluation. A statement can belong to two topics when it independently passes both organizing questions; zero overlap is not an optimization target.

After the forward member check, run a reverse completeness audit over every unassigned statement and every persisted topic candidate. Test complete content and non-superseded evolution against every organizing question. If a recurring cohort is coherent but not ready, return one stable topic candidate containing its strongest node IDs and an explicit status/reason. Exclusions remain local membership boundaries; candidates record graph-wide themes that need more evidence. Neither mechanism permits forcing a node into a topic.

Every topic action must return `source_ids=[]`. Topic sources are the exact union of current contained members' recursive raw sources; never copy old topic sources or append sources from removed members.

Return `raw_annotations=[]` in every consolidation response. Consolidation consumes existing bounded annotations but cannot rewrite any raw annotation.

Apply using the unchanged session and mode:

```bash
printf '%s' "$CONSOLIDATION_PLAN_JSON" | \
  second-memory consolidate --apply-response --stdin --json
```

The oldest ten queue entries are consumed only after a successful transaction. A failure leaves the batch intact.

## Topic Refresh

When the user asks to review, repair, or regenerate topic organization, use the dedicated graph-wide topic refresh instead of replaying raw or editing topic Markdown:

```bash
second-memory topics --emit-request --json
```

The request includes every compiled node with content, source dates, compact raw annotations, existing topic memberships, and persisted candidates. It never includes raw bodies. Audit the old topics, then output a complete replacement topic set using only `create` topic actions and topic `contains` edges whose targets may be raw, entity, event, statement, or a plan-local child topic. Use plan-local refs instead of copying old IDs; the CLI may reuse a deterministic topic ID when the regenerated title is stable, but its content and membership are still fully replaced. Returning no topic is valid only with explicit candidate dispositions explaining the recurring cohorts reviewed. Before apply, perform both the member direct-contribution pass and the graph-wide recurring-cohort pass.

Apply the unchanged response:

```bash
printf '%s' "$TOPIC_PLAN_JSON" | \
  second-memory topics --apply-response --stdin --json
```

The CLI atomically removes the old topic layer and every edge involving an old topic, validates the complete replacement set, and reprojects the Wiki. Entity, event, statement, raw, pending, Consolidation queue, candidates and redirects unrelated to old topics, and statement evolution must remain unchanged. Candidates or redirects that reference a removed topic are part of the replaced topic layer and must be removed to prevent dangling governance references.

## Raw-only Sequential Rebuild

`rebuild` never migrates or exposes compiled v1/v2 nodes. It reconstructs the compiled layer only from immutable raw entries by replaying the normal intake semantics one record at a time.

```bash
second-memory rebuild --emit-request --json
```

The request contains exactly one raw entry. On the first step, `existing_nodes`, redirects, candidates, and consolidation memo are empty even when the source repository contains legacy compiled pages. Raw entries are ordered by `created` and then `raw_id`; `event_date` remains semantic event time and does not change replay order.

For every step:

- reason only over the current raw and the nodes created by earlier steps of the same rebuild;
- produce entity, event, and statement actions using the same rules as incremental compile;
- do not create topics or execute merge/split;
- preserve the request `session_id` and `mode=rebuild`;
- apply the response before requesting the next raw.

```bash
printf '%s' "$REBUILD_PLAN_JSON" | \
  second-memory rebuild --apply-response --stdin --json
```

The first successful apply creates an isolated rebuild workspace and clears compiler-owned raw annotations only inside that workspace while preserving every raw body byte-for-byte and every non-compiler frontmatter field. The source knowledge base remains unchanged during replay. After every ten newly queued durable raw entries, the next rebuild request switches to one bounded Consolidation batch before raw replay continues; this trigger permits topic review but never guarantees that a topic must be created. After all raw entries have replayed, a final tail of one to nine queued entries is emitted once as `mode=consolidate` with its actual `batch_size` and `final_tail=true`; this partial batch is valid only inside the completed rebuild workspace, while ordinary Consolidation still requires exactly ten entries. Continue emit/apply until the Consolidation queue is empty. A failed or stale tail response leaves the workspace and queue unpromoted. If `update --emit-request` returns `mode=finalize` and `ready_to_finalize=true`, the last successful apply reached the promotion boundary but did not switch the source repository; run `second-memory update --finalize` and never replay the old response. The CLI then revalidates the complete raw archive under the source lock, transactionally promotes the workspace, and returns `rebuild_complete=true`. A rebuilt graph must not contain redirects, candidates, nodes, edges, topic structure, compiler rules, or schema-1 configuration inherited from the pre-rebuild layer; `AGENTS.md`, `.gitignore`, and `.kb/config.yaml` are regenerated from v2 defaults while preserving only deployment scope/agent/backend settings.

One compatibility case may emit the same final-tail request from an already promoted repository created by the older rebuild behavior: rebuild must be `complete` at its final cursor, compiled raw must equal the recorded ordered rebuild set, the current raw archive must still equal that ordered list, the one-to-nine queued IDs must be the exact ordered suffix, and there must be no pending compile input. This is a one-shot recovery only; an ordinary short queue or any raw added or incrementally compiled after rebuild remains ineligible until the normal ten-entry threshold.

## Browse As Wiki HTML

When the user asks to browse or export the whole knowledge base, use the deterministic read-only renderer:

```bash
second-memory wiki --json
second-memory wiki --output /path/to/wiki.html --json
second-memory wiki --open --json
```

The default output is `<repo>/../wiki.html`, outside the knowledge-base Git repository. The command does not call an LLM and does not mutate raw, wiki, manifest, pending, or Git state. Report the absolute output path and counts.

Only report the generated `data.output` file as the deliverable; never point the user at `src/second_memory/templates/wiki.html`. The generated file must contain a pre-rendered static overview of the compiled result so local viewers that disable JavaScript still show knowledge-base content.

The self-contained page must expose all v2 projections: entity, event, insight (`type=statement`), topic, event timeline, insight evolution, event status/date history, detailed node synthesis, evidence, explicit edges and backrefs, source raw annotations, redirects, unresolved candidates, Consolidation state, drift, and transaction recovery. It may read the complete raw archive only because the user explicitly requested a local full-library export; do not reuse the exported payload as ordinary search or Consolidation context.

## Maintenance

```bash
second-memory update --emit-request --json
```

The returned mode is authoritative:

- `rebuild`: knowledge-base version or compiled content drift requires a raw-only sequential rebuild, or an existing rebuild is still in progress.
- `incremental`: uncompiled raw entries must be consumed.
- `consolidate`: at least ten compiled raw entries are waiting for consolidation.
- `noop`: nothing needs to change.

When `llm_request` is present, produce its exact `CompilePlan v2` and apply it without changing `mode` or `session_id`:

```bash
printf '%s' "$PLAN_JSON" | second-memory update --apply-response --stdin --json
```

Use `second-memory status --json` to inspect node/edge counts, pending raw, candidates, consolidation state, drift, stale-session rejection, and transaction recovery state.
