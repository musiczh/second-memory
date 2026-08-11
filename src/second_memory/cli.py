from __future__ import annotations

import json
import sys
import webbrowser
from pathlib import Path
from typing import Optional

import typer

from .compiler import (
    CONSOLIDATION_BATCH_SIZE,
    add_raw,
    all_raw_entries,
    apply_rebuild_response,
    apply_response,
    build_compile_request,
    build_consolidation_request,
    build_rebuild_request,
    build_topic_request,
    consolidation_state,
    determine_update_mode,
    graph_stats,
    finalize_rebuild,
    initialize,
    list_compiled_pages,
    load_manifest,
    manifest_drift,
    read_pending,
    rebuild_state,
    skill_code_commit,
    transaction_state,
    version_drift,
)
from .config import KB_VERSION, resolve_repo
from .errors import SecondMemoryError, StaleSessionError, ValidationError
from .recap import build_recap_request
from .retriever import search_level1, search_level2_request
from .reviewer import review_request
from .store.git_store import GitStorage
from .utils import json_dumps
from .wiki import build_wiki_html, build_wiki_model

app = typer.Typer(no_args_is_help=True, add_completion=False)


def emit(command: str, data: object, json_output: bool = True) -> None:
    payload = {"ok": True, "command": command, "data": data, "error": None}
    typer.echo(json_dumps(payload) if json_output else payload)


def fail(command: str, exc: Exception, json_output: bool = True) -> None:
    code = exc.code if isinstance(exc, SecondMemoryError) else "error"
    payload = {"ok": False, "command": command, "data": None, "error": {"code": code, "message": str(exc)}}
    typer.echo(json_dumps(payload) if json_output else payload, err=True)
    raise typer.Exit(1)


def read_stdin_text() -> str:
    return sys.stdin.read()


@app.command()
def init(
    repo: Optional[str] = typer.Option(None, "--repo", help="Knowledge-base repository path."),
    scope: str = typer.Option("shared", "--scope", help="shared or agent."),
    agent: Optional[str] = typer.Option(None, "--agent", help="Agent name when scope=agent."),
    backend: str = typer.Option("git", "--backend", help="git or plain."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON envelope."),
) -> None:
    command = "init"
    try:
        target = resolve_repo(repo, for_init=True, scope=scope, agent=agent)
        emit(command, initialize(target, scope, agent, backend), json_output=True)
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def add(
    title: str = typer.Option(..., "--title"),
    event_date: Optional[str] = typer.Option(None, "--event-date"),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
    stdin: bool = typer.Option(False, "--stdin", help="Read content from stdin."),
    content: Optional[str] = typer.Option(None, "--content", help="Inline content."),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "add"
    try:
        body = read_stdin_text() if stdin else (content or "")
        tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
        emit(command, add_raw(resolve_repo(repo), title, body, event_date, tag_list), json_output=True)
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def compile(
    emit_request: bool = typer.Option(False, "--emit-request"),
    apply_response_flag: bool = typer.Option(False, "--apply-response"),
    stdin: bool = typer.Option(False, "--stdin"),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "compile"
    try:
        target = resolve_repo(repo)
        if emit_request:
            request = build_compile_request(target, task="compile", mode="incremental")
            emit(command, {"llm_request": request, "pending": len(request["context"]["raw_entries"])}, json_output=True)
            return
        if apply_response_flag:
            response = json.loads(read_stdin_text() if stdin else sys.stdin.read())
            emit(command, apply_response(target, response, command="compile"), json_output=True)
            return
        raise typer.BadParameter("use --emit-request or --apply-response --stdin")
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def rebuild(
    emit_request: bool = typer.Option(False, "--emit-request"),
    apply_response_flag: bool = typer.Option(False, "--apply-response"),
    stdin: bool = typer.Option(False, "--stdin"),
    finalize: bool = typer.Option(False, "--finalize", help="Finalize a completed rebuild workspace after a recoverable promotion failure."),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "rebuild"
    try:
        target = resolve_repo(repo)
        if emit_request:
            request = build_rebuild_request(target)
            state = rebuild_state(target)
            emit(
                command,
                {
                    "mode": request["context"]["mode"] if request else "finalize",
                    "llm_request": request,
                    "raw_count": state["total"],
                    "rebuild": state,
                    "ready_to_finalize": request is None and state["phase"] == "consolidate",
                },
                json_output=True,
            )
            return
        if apply_response_flag:
            response = json.loads(read_stdin_text() if stdin else sys.stdin.read())
            emit(command, apply_rebuild_response(target, response), json_output=True)
            return
        if finalize:
            emit(command, {**finalize_rebuild(target), "rebuild_complete": True}, json_output=True)
            return
        raise typer.BadParameter("use --emit-request, --apply-response --stdin, or --finalize")
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def consolidate(
    emit_request: bool = typer.Option(False, "--emit-request"),
    apply_response_flag: bool = typer.Option(False, "--apply-response"),
    stdin: bool = typer.Option(False, "--stdin"),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "consolidate"
    try:
        target = resolve_repo(repo)
        if emit_request:
            request = build_consolidation_request(target)
            state = consolidation_state(load_manifest(target))
            emit(
                command,
                {
                    "mode": "consolidate" if request else "noop",
                    "llm_request": request,
                    "pending": len(state["pending_raw"]),
                    "batch_size": CONSOLIDATION_BATCH_SIZE,
                },
                json_output=True,
            )
            return
        if apply_response_flag:
            response = json.loads(read_stdin_text() if stdin else sys.stdin.read())
            emit(command, apply_response(target, response, command="consolidate"), json_output=True)
            return
        raise typer.BadParameter("use --emit-request or --apply-response --stdin")
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def topics(
    emit_request: bool = typer.Option(False, "--emit-request"),
    apply_response_flag: bool = typer.Option(False, "--apply-response"),
    stdin: bool = typer.Option(False, "--stdin"),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "topics"
    try:
        target = resolve_repo(repo)
        if emit_request:
            request = build_topic_request(target)
            emit(
                command,
                {
                    "mode": "topics",
                    "llm_request": request,
                    "statements": len(request["context"]["statement_catalog"]),
                    "existing_topics": len(request["context"]["existing_topics"]),
                },
                json_output=True,
            )
            return
        if apply_response_flag:
            response = json.loads(read_stdin_text() if stdin else sys.stdin.read())
            emit(command, apply_response(target, response, command="topics"), json_output=True)
            return
        raise typer.BadParameter("use --emit-request or --apply-response --stdin")
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def search(
    query: str = typer.Option(..., "--query"),
    level: int = typer.Option(1, "--level"),
    emit_request: bool = typer.Option(False, "--emit-request"),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "search"
    try:
        target = resolve_repo(repo)
        if level == 1:
            emit(command, search_level1(target, query), json_output=True)
            return
        if level == 2 and emit_request:
            emit(command, {"llm_request": search_level2_request(target, query)}, json_output=True)
            return
        raise typer.BadParameter("level 2 requires --emit-request")
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def review(
    range_name: Optional[str] = typer.Option(None, "--range"),
    on_this_day: Optional[str] = typer.Option(None, "--on-this-day"),
    start_date: Optional[str] = typer.Option(None, "--start-date"),
    end_date: Optional[str] = typer.Option(None, "--end-date"),
    emit_request: bool = typer.Option(False, "--emit-request"),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "review"
    try:
        if not emit_request:
            raise typer.BadParameter("review currently emits an LLM request; pass --emit-request")
        emit(command, {"llm_request": review_request(resolve_repo(repo), range_name=range_name, on_this_day=on_this_day, start_date=start_date, end_date=end_date)}, json_output=True)
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def recap(
    raw_id: list[str] = typer.Option([], "--raw-id", help="Focus raw ids; defaults to the most recent raw entry."),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "recap"
    try:
        target = resolve_repo(repo)
        focus = list(raw_id)
        if not focus:
            entries = all_raw_entries(target)
            if entries:
                focus = [sorted(entries, key=lambda e: (e.created, e.id))[-1].id]
        request = build_recap_request(target, focus, []) if focus else None
        emit(command, {"focus_raw_ids": focus, "llm_request": request}, json_output=True)
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def update(
    emit_request: bool = typer.Option(False, "--emit-request"),
    apply_response_flag: bool = typer.Option(False, "--apply-response"),
    stdin: bool = typer.Option(False, "--stdin"),
    finalize: bool = typer.Option(False, "--finalize", help="Finalize a completed rebuild workspace after a recoverable promotion failure."),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "update"
    try:
        target = resolve_repo(repo)
        if emit_request:
            code_commit = skill_code_commit()
            pull = {
                "attempted": False,
                "ok": True,
                "updated": False,
                "before": code_commit,
                "after": code_commit,
                "message": "automatic code pull is disabled; update the Skill repository explicitly",
            }
            decision = determine_update_mode(target)
            if decision["mode"] == "rebuild":
                request = build_rebuild_request(target)
                next_mode = str(request["context"]["mode"]) if request else "finalize"
                emit(
                    command,
                    {
                        **decision,
                        "update_mode": "rebuild",
                        "mode": next_mode,
                        "code_update": pull,
                        "llm_request": request,
                        "ready_to_finalize": request is None,
                    },
                    json_output=True,
                )
                return
            if decision["mode"] == "incremental":
                request = build_compile_request(target, task="update", mode="incremental")
                emit(command, {**decision, "code_update": pull, "llm_request": request}, json_output=True)
                return
            if decision["mode"] == "consolidate":
                request = build_consolidation_request(
                    target,
                    task="update",
                    quality_repair=bool(decision.get("quality_repair")),
                )
                emit(command, {**decision, "code_update": pull, "llm_request": request}, json_output=True)
                return
            emit(command, {**decision, "code_update": pull}, json_output=True)
            return
        if apply_response_flag:
            response = json.loads(read_stdin_text() if stdin else sys.stdin.read())
            mode = str(response.get("mode") or response.get("llm_response", {}).get("mode") or response.get("data", {}).get("llm_response", {}).get("mode") or "")
            outer_mode = determine_update_mode(target)["mode"]
            if outer_mode == "rebuild":
                request = build_rebuild_request(target)
                expected_mode = str(request["context"]["mode"]) if request else "finalize"
                if mode != expected_mode:
                    raise StaleSessionError(f"update mode changed: expected {expected_mode}, got {mode or 'empty'}")
                emit(command, apply_rebuild_response(target, response), json_output=True)
                return
            expected_mode = outer_mode
            if mode != expected_mode:
                raise StaleSessionError(f"update mode changed: expected {expected_mode}, got {mode or 'empty'}")
            if mode in {"incremental", "consolidate"}:
                emit(command, apply_response(target, response, command="update"), json_output=True)
            else:
                raise ValidationError("update has no applicable plan")
            return
        if finalize:
            if determine_update_mode(target)["mode"] != "rebuild" or build_rebuild_request(target) is not None:
                raise ValidationError("update rebuild is not ready to finalize")
            emit(command, {**finalize_rebuild(target), "rebuild_complete": True}, json_output=True)
            return
        raise typer.BadParameter("use --emit-request, --apply-response --stdin, or --finalize")
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def status(
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "status"
    try:
        target = resolve_repo(repo)
        pending = read_pending(target)
        store = GitStorage(target) if (target / ".git").exists() else None
        manifest = load_manifest(target)
        consolidation = consolidation_state(manifest)
        semantic_quality = build_wiki_model(target)["health"]["semantic_quality"]
        data = {
            "repo": str(target),
            "pending": len(pending),
            "pending_raw": pending,
            "pages": len(list_compiled_pages(target)),
            "counts": graph_stats(target),
            "raw": len(all_raw_entries(target)),
            "recent_commit": store.current_commit() if store else None,
            "manifest_drift": manifest_drift(target),
            "code_commit": skill_code_commit(),
            "kb_version": KB_VERSION,
            "compiled_kb_version": manifest.get("kb_version"),
            "version_drift": version_drift(target),
            "candidates": manifest.get("candidates", []),
            "consolidation_pending": len(consolidation["pending_raw"]),
            "consolidation_due": len(consolidation["pending_raw"]) >= CONSOLIDATION_BATCH_SIZE,
            "stale_session": False,
            "transaction_recovery": transaction_state(target),
            "rebuild": rebuild_state(target),
            "semantic_quality": semantic_quality,
        }
        emit(command, data, json_output=True)
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def wiki(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="HTML output path; defaults to <repo>/../wiki.html."),
    open_browser: bool = typer.Option(False, "--open", help="Open the generated HTML in the default browser."),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "wiki"
    try:
        target = resolve_repo(repo)
        output_path = Path(output).expanduser().resolve() if output else target.parent / "wiki.html"
        result = build_wiki_html(target, output_path)
        if open_browser:
            webbrowser.open(output_path.as_uri())
        emit(command, result, json_output=True)
    except Exception as exc:
        fail(command, exc, json_output=True)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
