from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _parse_value(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "None"}:
        return None
    if value.startswith("[") or value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value.strip('"')
    return value


def parse_mapping(text: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = _parse_value(value)
    return meta


def dump_mapping(meta: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in meta.items():
        if isinstance(value, list):
            rendered = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif value is None:
            rendered = "null"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = json.dumps(str(value), ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines) + "\n"


def parse_document(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw_meta = text[4:end]
    body = text[end + 5 :]
    return parse_mapping(raw_meta), body.lstrip("\n")


def dump_document(meta: dict[str, Any], body: str) -> str:
    body = body.rstrip() + "\n"
    return f"---\n{dump_mapping(meta)}---\n\n{body}"


def read_document(path: Path) -> tuple[dict[str, Any], str]:
    return parse_document(path.read_text(encoding="utf-8"))


def write_document(path: Path, meta: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_document(meta, body), encoding="utf-8")
