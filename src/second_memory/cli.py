from __future__ import annotations

import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Optional

import typer

from .compiler import (
    add_raw,
    all_raw_entries,
    apply_merge_response,
    apply_response,
    build_compile_request,
    initialize,
    list_compiled_pages,
    load_manifest,
    manifest_drift,
    pull_skill_code,
    read_pending,
    skill_code_commit,
    version_drift,
)
from .config import KB_VERSION, resolve_repo
from .errors import SecondMemoryError
from .merge import build_merge_request
from .recap import build_recap_request
from .retriever import search_level1, search_level2_request
from .reviewer import review_request
from .store.git_store import GitStorage
from .utils import json_dumps
from .wiki import build_wiki_html

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


# When the code repo is fast-forwarded mid-run we re-exec the CLI so the new code
# loads; this env var carries the original pull result forward and guards the
# re-exec so it can happen at most once.
_REEXEC_PULL_ENV = "SECOND_MEMORY_UPDATE_PULL"


def sync_skill_code() -> dict:
    """Pull the Skill/CLI code repo, then re-exec this command if it advanced.

    A plain in-process pull is not enough: ``KB_VERSION`` and the compile helpers
    are bound at import time, so judging drift right after a fast-forward would
    still use the old code. When the pull moves HEAD we re-exec with the freshly
    pulled code; the original pull result is carried through ``_REEXEC_PULL_ENV``
    so the post-exec run reports it and never pulls (or re-execs) twice.
    """
    carried = os.environ.get(_REEXEC_PULL_ENV)
    if carried is not None:
        return json.loads(carried)
    pull = pull_skill_code()
    if pull.get("updated"):
        env = dict(os.environ)
        env[_REEXEC_PULL_ENV] = json_dumps(pull)
        os.execve(sys.executable, [sys.executable, "-m", "second_memory.cli", *sys.argv[1:]], env)
    return pull


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
            emit(command, apply_response(target, response, command="compile", replace_compiled=False), json_output=True)
            return
        raise typer.BadParameter("use --emit-request or --apply-response --stdin")
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def rebuild(
    emit_request: bool = typer.Option(False, "--emit-request"),
    apply_response_flag: bool = typer.Option(False, "--apply-response"),
    stdin: bool = typer.Option(False, "--stdin"),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "rebuild"
    try:
        target = resolve_repo(repo)
        if emit_request:
            request = build_compile_request(target, task="rebuild", raw_entries=all_raw_entries(target), mode="rebuild")
            emit(command, {"llm_request": request, "raw_count": len(request["context"]["raw_entries"])}, json_output=True)
            return
        if apply_response_flag:
            response = json.loads(read_stdin_text() if stdin else sys.stdin.read())
            emit(command, apply_response(target, response, command="rebuild", replace_compiled=True), json_output=True)
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
def merge(
    emit_request: bool = typer.Option(False, "--emit-request"),
    apply_response_flag: bool = typer.Option(False, "--apply-response"),
    stdin: bool = typer.Option(False, "--stdin"),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "merge"
    try:
        target = resolve_repo(repo)
        if emit_request:
            request = build_merge_request(target)
            pages = len(request["context"]["pages"]) if request else 0
            emit(command, {"llm_request": request, "pages": pages}, json_output=True)
            return
        if apply_response_flag:
            response = json.loads(read_stdin_text() if stdin else sys.stdin.read())
            emit(command, apply_merge_response(target, response, command="merge"), json_output=True)
            return
        raise typer.BadParameter("use --emit-request or --apply-response --stdin")
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def update(
    emit_request: bool = typer.Option(False, "--emit-request"),
    apply_response_flag: bool = typer.Option(False, "--apply-response"),
    stdin: bool = typer.Option(False, "--stdin"),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "update"
    try:
        target = resolve_repo(repo)
        if emit_request:
            pull = sync_skill_code()
            pending = read_pending(target)
            drift = manifest_drift(target)
            version_changed = version_drift(target)
            # Version drift means the compiled layer was built by older rules, so a
            # full rebuild from the raw archive takes priority over consuming pending.
            if version_changed or drift:
                request = build_compile_request(target, task="update", raw_entries=all_raw_entries(target), mode="rebuild")
                emit(command, {"mode": "rebuild", "pending": len(pending), "drift": drift, "code_update": pull, "version_changed": version_changed, "llm_request": request}, json_output=True)
                return
            if pending:
                request = build_compile_request(target, task="update", mode="incremental")
                emit(command, {"mode": "incremental", "pending": len(pending), "drift": drift, "code_update": pull, "version_changed": False, "llm_request": request}, json_output=True)
                return
            emit(command, {"mode": "noop", "pending": 0, "drift": [], "code_update": pull, "version_changed": False}, json_output=True)
            return
        if apply_response_flag:
            response = json.loads(read_stdin_text() if stdin else sys.stdin.read())
            # Rebuild (replace) when the version drifted or pages drifted; only an
            # incremental compile of fresh pending entries keeps the existing wiki.
            replace = version_drift(target) or bool(manifest_drift(target)) or not read_pending(target)
            emit(command, apply_response(target, response, command="update", replace_compiled=replace), json_output=True)
            return
        raise typer.BadParameter("use --emit-request or --apply-response --stdin")
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
        data = {
            "repo": str(target),
            "pending": len(pending),
            "pending_raw": pending,
            "pages": len(list_compiled_pages(target)),
            "raw": len(all_raw_entries(target)),
            "recent_commit": store.current_commit() if store else None,
            "manifest_drift": manifest_drift(target),
            "code_commit": skill_code_commit(),
            "kb_version": KB_VERSION,
            "compiled_kb_version": load_manifest(target).get("kb_version"),
            "version_drift": version_drift(target),
        }
        emit(command, data, json_output=True)
    except Exception as exc:
        fail(command, exc, json_output=True)


@app.command()
def wiki(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="输出 HTML 路径，默认 <repo>/../wiki.html"),
    open_browser: bool = typer.Option(False, "--open", help="生成后用浏览器打开。"),
    repo: Optional[str] = typer.Option(None, "--repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "wiki"
    try:
        target = resolve_repo(repo)
        default_out = target.parent / "wiki.html"
        out_path = Path(output).expanduser().resolve() if output else default_out
        result = build_wiki_html(target, out_path)
        if open_browser:
            webbrowser.open(out_path.as_uri())
        emit(command, result, json_output=True)
    except Exception as exc:
        fail(command, exc, json_output=True)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
