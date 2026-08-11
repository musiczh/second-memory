from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)


def now_local() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def parse_date(value: str | None) -> date:
    if not value:
        return now_local().date()
    return date.fromisoformat(value)


def parse_temporal_anchor(value: str) -> tuple[datetime, bool]:
    pattern = r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?"
    if not re.fullmatch(pattern, value):
        raise ValueError(value)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized), "T" in value


def date_range_last_week() -> tuple[date, date]:
    end = now_local().date()
    return end - timedelta(days=6), end


def slugify(text: str, fallback: str = "note", max_len: int = 64) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().lower()
    chars: list[str] = []
    last_dash = False
    for ch in normalized:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            chars.append(ch)
            last_dash = False
        elif ch in {" ", "-", "_", "/", "\\", "."} and not last_dash:
            chars.append("-")
            last_dash = True
    slug = "".join(chars).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug or fallback)[:max_len].strip("-") or fallback


def short_hash(*parts: str, length: int = 8) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:length]


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def relpath(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
