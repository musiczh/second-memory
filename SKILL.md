---
name: second-memory
description: Use when the user wants to save personal notes, diary entries, reflections, work reviews, or reusable thoughts into a local second-memory knowledge base; when a conversation may contain durable personal context worth asking to save; when answering questions that may benefit from the user's accumulated local knowledge; or when the user asks for weekly review, on-this-day recall, knowledge-base search, rebuild, or maintenance.
---

# Second Memory Skill

This skill connects Codex to a local Markdown knowledge base through the `second-memory` CLI. The knowledge base is a personal memory aid: it records what the user wrote and helps with recall and association. It is not an authoritative source of external truth.

## Core Rules

- Answer in the current conversation first. Use the knowledge base as personal context, not as the only source.
- Before saving content that was not explicitly requested with phrases like "记一下" or "存入知识库", ask the user for confirmation.
- Never send the whole raw archive to the model. Use `search --level 1` first, then only request deeper context when needed.
- `index.md` is the compact semantic entry point for entities and topics. Daily timeline pages are used by `review`, not by the level-1 index.
- The CLI never calls an LLM. For compile, rebuild, review, update, and level-2 search, run `--emit-request`, perform the requested reasoning yourself, then pass the structured JSON back with `--apply-response --stdin` when the command supports it.
- Raw pages are immutable. Do not edit files under `raw/`; add a new record instead.

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

Report the saved `raw_id`, updated pages, and commit.

### Opportunistic Save

When the user says something durable but did not explicitly ask to save it, ask first:

> 这段内容看起来适合沉淀到你的第二记忆库。要我帮你存入吗？

Only after confirmation run the explicit save workflow.

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

### Maintenance

Daily or manual update:

```bash
second-memory update --emit-request --json
```

If the result contains `llm_request`, produce the response JSON and apply it:

```bash
printf '%s' "$LLM_JSON" | second-memory update --apply-response --stdin --json
```

Use `second-memory status --json` after maintenance.
