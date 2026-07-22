---
name: second-memory
description: Use when the user wants to save personal notes, diary entries, reflections, work reviews, or long-term preferences into a local second-memory knowledge base (e.g. "记一下" / "存入知识库"); when a conversation contains durable personal context worth offering to save; when answering questions that may benefit from the user's accumulated local knowledge; when the user wants a recap that connects newly saved content to their history; or when the user asks for weekly review, on-this-day recall, knowledge-base search, rebuild, or maintenance.
---

# Second Memory Skill

This skill connects the host agent to a local Markdown knowledge base through the `second-memory` CLI. The knowledge base is a personal memory aid: it records what the user wrote and helps with recall and association. It is not an authoritative source of external truth.

## Core Rules

- Answer in the current conversation first. Use the knowledge base as personal context, not as the only source.
- Before saving content that was not explicitly requested with phrases like "记一下" or "存入知识库", ask the user for confirmation.
- After every successful save, do not stop at "已入库". Give the user a short recap that connects the new entry to their accumulated history (recurring themes, related people/projects, on-this-day echoes, actionable follow-ups). The `compile`/`update` apply step returns a `recap_request`; reason over it and present the recap.
- After the recap, if the apply result contains a `merge_request`, reason over it to consolidate entities and topics. Only apply a merge when you are confident two pages refer to the same thing (or a topic clearly needs refining); run `merge --apply-response` and then briefly tell the user what was merged. If nothing is confidently mergeable, skip it — do not force merges.
- Never send the whole raw archive to the model. Use `search --level 1` first, then only request deeper context when needed.
- `index.md` is the compact semantic entry point for entities and topics. Daily timeline pages are used by `review`, not by the level-1 index.
- The CLI never calls an LLM. For compile, rebuild, review, update, and level-2 search, run `--emit-request`, perform the requested reasoning yourself, then pass the structured JSON back with `--apply-response --stdin` when the command supports it.
- Raw pages are immutable. Do not edit files under `raw/`; add a new record instead.
- When a CLI envelope carries a top-level `tip` field, pass that usage suggestion on to the user in a natural tone as part of your reply. Do not explain the mechanism; just relay the tip.

## Setup

From this repository:

```bash
scripts/setup.sh
```

Use `SECOND_MEMORY_REPO` to point at the user's runtime knowledge base, or pass `--repo` explicitly.

```bash
second-memory init --scope shared
second-memory status --json
```

## Workflows

### Explicit Save

When the user explicitly asks to save text:

```bash
printf '%s' "$TEXT" | second-memory add --title "$TITLE" --event-date YYYY-MM-DD --stdin --json
second-memory compile --emit-request --json
```

Read `data.llm_request`, produce JSON matching `response_schema`, then:

```bash
printf '%s' "$LLM_JSON" | second-memory compile --apply-response --stdin --json
```

Report the saved `raw_id`, updated pages, and commit. Then run the post-save recap below.

### Post-Save Recap

Every save must end with a recap that reviews the new content against the user's history, not just a "已入库" confirmation.

The `compile --apply-response` (and `update --apply-response`) result contains a `recap_request` whenever raw entries were consumed. Read `data.recap_request`, produce JSON matching its `response_schema`, and present the recap to the user. The request bundles:

- `focus`: the entries just saved.
- `related_history`: related entity/topic pages that already carried earlier sources.
- `on_this_day`: prior timeline pages sharing the same month-day.
- `history_available`: false when this is the first record on the topic — in that case give a light recap and a direction to watch.

If you need to (re)generate a recap outside the apply step, or for the most recent entry:

```bash
second-memory recap --json                 # most recent raw entry
second-memory recap --raw-id "$RAW_ID" --json
```

Reason over `data.llm_request` the same way, then present the recap. When `llm_request` is null there is nothing to recap.

### Entity/Topic Merge(实体与主题合并)

The knowledge base grows one page per distinct name, so over time the same
person/project/concept ends up split across several entity pages and topics
fragment. Consolidation folds co-referent entities together (e.g. 5 pages → 2),
merges mergeable topics, and refines topic definitions to fit accumulated content.

This runs automatically after each save: `compile --apply-response` (and
`update --apply-response`) attaches a `merge_request` **only when that apply added a new
entity/topic page** and there are at least two entity/topic pages. A save that merely
refines existing pages or only writes timeline entries carries no `merge_request`, so no
needless full-page merge round trip happens. Read `data.merge_request`, produce JSON
matching its `response_schema`, and apply only the confident merges:

```bash
printf '%s' "$MERGE_JSON" | second-memory merge --apply-response --stdin --json
```

To run a full-library consolidation on demand (independent of a save):

```bash
second-memory merge --emit-request --json     # bundles every entity/topic page
printf '%s' "$MERGE_JSON" | second-memory merge --apply-response --stdin --json
```

Each element of `merges` is one `canonical` page plus the ids it `absorbed`:

- **Merge**: `canonical` is the surviving page (any of the group's ids), `absorbed`
  lists the pages folded into it (their files are deleted).
- **Refine only**: `absorbed` is `[]`; rewrite the same id's title/summary/body/aliases.
- **Rename**: `canonical.id` is the new id, `absorbed` lists the old id.

Rules: `absorbed` ids must be existing pages of the same type as `canonical`;
`canonical.sources` must union in every absorbed page's sources (the CLI also unions
defensively); ids must not repeat across groups; base decisions only on the given
pages. Report `data.merged_groups`, `canonical_pages`, and `deleted_pages`.

Consolidation is **not persisted as an alias ledger**: a later `rebuild`/`update`
that recompiles the wiki from raw will re-split the merged pages, but that apply
step will again carry a `merge_request`, so re-running the merge restores the
consolidation. Nothing is lost — it just costs one more round trip.

### Opportunistic Save

When the user says something durable but did not explicitly ask to save it, ask first:

> 这段内容看起来适合沉淀到你的第二记忆库。要我帮你存入吗？

Only after confirmation run the explicit save workflow, including the post-save recap.

### Answer With Personal Context

For questions that may depend on the user's history:

```bash
second-memory search --query "$QUERY" --level 1 --json
```

If candidates are useful, cite them as the user's historical notes. If deeper context is needed:

```bash
second-memory search --query "$QUERY" --level 2 --emit-request --json
```

Use only the returned candidate pages and raw snippets.

### Reviews

Weekly review:

```bash
second-memory review --range last-week --emit-request --json
```

On-this-day:

```bash
second-memory review --on-this-day YYYY-MM-DD --emit-request --json
```

Generate the review from `data.llm_request.context.timeline_pages`.

### Wiki 浏览

When the user wants to browse the whole knowledge base "像看书一样" (as a readable,
clickable page), render the compiled `wiki/` layer and `raw/` archive into a single
self-contained HTML file:

```bash
second-memory wiki --json            # writes <repo>/../wiki.html by default
second-memory wiki --open --json     # also open it in the default browser
second-memory wiki --output /path/to/memory.html --json
```

Unlike `compile`/`review`/`update`, this is a **single deterministic command** — no LLM
reasoning and no `--emit-request` / `--apply-response` round trip. Report the returned
`data.output` path and `data.counts` (entities / topics / timeline / raw). The HTML embeds
its data and needs no server: it opens on double-click and offers three views — timeline,
entities (grouped by kind), and topics — each entity/topic detail page linking its summary,
related entities/topics, timeline appearances, and source raw entries.

The output defaults to **outside the knowledge-base git repo** (`<repo>/../wiki.html`), so
it never trips the `compile`/`update` worktree guard. It is a pure build artifact: re-run
`second-memory wiki` after new entries are saved to refresh it; nothing regenerates it
automatically.

### Maintenance

Daily or manual update. This is the entry point an integrating agent calls to stay
current: `update --emit-request` first fast-forwards the Skill/CLI **code** repo
(`git pull --ff-only`); if that advances HEAD it re-execs once so the run uses the
freshly pulled code, then decides what to recompile.

```bash
second-memory update --emit-request --json
```

The result reports `code_update` (the git pull outcome) and a `mode`:

- `rebuild` — the knowledge-base version changed (`version_changed`) or compiled
  pages drifted (`drift`); the wiki layer is rebuilt from the raw archive with the
  latest rules. Version/page drift takes priority over pending entries, so a rule
  change is never silently downgraded to an incremental pass.
- `incremental` — no drift, but pending raw entries are waiting; compile them.
- `noop` — nothing to do: version current and no pending/drift.

The knowledge-base version (`KB_VERSION`, default `1.0.0`) is bumped in code only
when the wiki organization or compile rules change. A plain code update that does
not bump it will pull but stay `noop`, so no needless recompile happens.

When the result contains `llm_request`, produce the response JSON and apply it:

```bash
printf '%s' "$LLM_JSON" | second-memory update --apply-response --stdin --json
```

Applying records the knowledge-base version the wiki was built against, so the next
`update` only rebuilds when the version actually changes. Use `second-memory status
--json` (`kb_version` / `compiled_kb_version` / `version_drift`) after maintenance.
