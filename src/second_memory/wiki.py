from __future__ import annotations

import html as html_lib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import frontmatter
from .compiler import list_compiled_pages, raw_lookup
from .config import load_config
from .models import Page
from .utils import now_local

TEMPLATE_PATH = Path(__file__).parent / "templates" / "wiki.html"
DATA_PLACEHOLDER = "__WIKI_DATA__"

# How many related entity/topic pages to surface on a detail page.
MAX_RELATED = 8

# Stable display order for entity kinds; unknown kinds sort last.
ENTITY_KIND_RANK = {"person": 0, "project": 1, "concept": 2, "emotion": 3}


# --------------------------------------------------------------------------- #
# Minimal Markdown -> HTML                                                     #
# --------------------------------------------------------------------------- #
# The project deliberately carries a single dependency (typer) and hand-parses
# its own frontmatter, so we render the small Markdown subset the compiler and
# raw entries actually use rather than pull in a Markdown library. Text is HTML
# -escaped first, then inline tags are inserted, so user content cannot inject
# markup.

_CODE = re.compile(r"`([^`]+?)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"\*(.+?)\*")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_ULIST = re.compile(r"^[-*]\s+(.*)$")
_OLIST = re.compile(r"^\d+\.\s+(.*)$")


def _safe_href(url: str) -> str:
    low = url.lower()
    if low.startswith(("http://", "https://", "mailto:", "/", "#")):
        return url.replace('"', "%22")
    return ""


def _inline(text: str) -> str:
    """Apply inline formatting to an already HTML-escaped string."""
    text = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", text)

    def link_sub(m: re.Match[str]) -> str:
        href = _safe_href(m.group(2).strip())
        return f'<a href="{href}" rel="noopener noreferrer">{m.group(1)}</a>' if href else m.group(1)

    text = _LINK.sub(link_sub, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return text


def render_markdown(text: str) -> str:
    if not text or not text.strip():
        return ""
    out: list[str] = []
    para: list[str] = []
    list_items: list[str] = []
    list_tag: str | None = None

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(html_lib.escape(line)) for line in para) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            body = "".join(f"<li>{item}</li>" for item in list_items)
            out.append(f"<{list_tag}>{body}</{list_tag}>")
            list_items.clear()
        list_tag = None

    for raw_line in text.replace("\r\n", "\n").split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            flush_para()
            flush_list()
            continue
        heading = _HEADING.match(stripped)
        if heading:
            flush_para()
            flush_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(html_lib.escape(heading.group(2)))}</h{level}>")
            continue
        ordered = _OLIST.match(stripped)
        unordered = None if ordered else _ULIST.match(stripped)
        if unordered or ordered:
            flush_para()
            tag = "ul" if unordered else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            content = (unordered or ordered).group(1)
            list_items.append(_inline(html_lib.escape(content)))
            continue
        flush_list()
        para.append(stripped)

    flush_para()
    flush_list()
    return "".join(out)


# --------------------------------------------------------------------------- #
# Timeline line parsing                                                        #
# --------------------------------------------------------------------------- #
_TIME_PREFIX = re.compile(r"^(\d{1,2}:\d{2})\s+(.*)$")


def _parse_timeline_line(line: str) -> tuple[str, str, list[str]]:
    """Split a timeline bullet into (time, text, ref_ids).

    Lines look like ``- HH:MM 摘要 -> entity-a, topic-b``; time and refs are
    both optional and handled defensively.
    """
    body = line.strip()
    if body.startswith("-"):
        body = body[1:].strip()
    refs: list[str] = []
    if " -> " in body:
        body, _, ref_part = body.rpartition(" -> ")
        refs = [ref.strip() for ref in ref_part.split(",") if ref.strip()]
    match = _TIME_PREFIX.match(body.strip())
    if match:
        return match.group(1), match.group(2).strip(), refs
    return "", body.strip(), refs


# --------------------------------------------------------------------------- #
# Model assembly                                                               #
# --------------------------------------------------------------------------- #
def _ref_object(ref_id: str, page_by_id: dict[str, Page]) -> dict[str, Any]:
    page = page_by_id.get(ref_id)
    if page:
        return {"id": ref_id, "title": page.title, "type": page.type}
    return {"id": ref_id, "title": ref_id, "type": "unknown"}


def _related_for(
    page_id: str,
    page_by_id: dict[str, Page],
    page_sources: dict[str, set[str]],
    cooccurrence: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Rank other entity/topic pages related to ``page_id``.

    Relations are derived, not stored: two pages relate when they share a source
    raw (weighted by overlap) or co-occur in the same timeline entry.
    """
    own_sources = page_sources.get(page_id, set())
    scores: dict[str, int] = {}
    for other_id in page_by_id:
        if other_id == page_id:
            continue
        score = 3 * len(own_sources & page_sources.get(other_id, set()))
        score += 2 * cooccurrence.get(page_id, {}).get(other_id, 0)
        if score:
            scores[other_id] = score
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_RELATED]
    related: list[dict[str, Any]] = []
    for other_id, _ in ranked:
        page = page_by_id[other_id]
        related.append({
            "id": other_id,
            "title": page.title,
            "type": page.type,
            "summary": page.summary,
            "entity_kind": page.entity_kind,
        })
    return related


def _entry_raw_ids(
    time: str,
    ref_ids: list[str],
    day_sources: list[str],
    page_sources: dict[str, set[str]],
) -> list[str]:
    """Derive which raw entries a single timeline entry came from.

    The stored timeline line records entity/topic refs but not its own raw, so we
    attribute day-level sources to the entry with a layered fallback:
      1. exact minute match — a day source whose id ``HHMM`` equals the entry time
         (precise for entries carrying a real timestamp);
      2. reference overlap — day sources cited by the pages this entry references
         (handles ``00:00`` bulk-imported entries that share a day);
      3. all day sources — so every entry still links back to its origin.
    """
    hhmm = time.replace(":", "") if time else ""
    if hhmm:
        exact = [s for s in day_sources if s.split("-")[2:3] == [hhmm]]
        if exact:
            return exact
    overlap = [s for s in day_sources if any(s in page_sources.get(r, set()) for r in ref_ids)]
    if overlap:
        return overlap
    return list(day_sources)


def build_wiki_model(repo: Path) -> dict[str, Any]:
    load_config(repo)
    pages = list_compiled_pages(repo)
    raws = raw_lookup(repo)

    entities = [p for p in pages if p.type == "entity"]
    topics = [p for p in pages if p.type == "topic"]
    timelines = [p for p in pages if p.type == "timeline"]
    page_by_id = {p.id: p for p in pages if p.type in {"entity", "topic"}}
    page_sources = {p.id: set(p.sources) for p in page_by_id.values()}

    # Parse timelines once; collect per-page appearances and co-occurrence.
    cooccurrence: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    appearances: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parsed_timeline: list[dict[str, Any]] = []
    for tp in sorted(timelines, key=lambda p: p.title, reverse=True):
        date = tp.title  # timeline pages carry event_date as their title
        entries: list[dict[str, Any]] = []
        for line in tp.body.splitlines():
            if not line.strip():
                continue
            time, text, ref_ids = _parse_timeline_line(line)
            refs = [_ref_object(rid, page_by_id) for rid in ref_ids]
            entry_raw_ids = _entry_raw_ids(time, ref_ids, list(tp.sources), page_sources)
            entry_raws = [
                {"id": rid, "title": raws[rid].title}
                for rid in entry_raw_ids
                if rid in raws
            ]
            entries.append({"time": time, "text": text, "refs": refs, "raws": entry_raws})
            present = [r["id"] for r in refs if r["type"] != "unknown"]
            for pid in present:
                appearances[pid].append({"date": date, "time": time, "text": text})
            for i, left in enumerate(present):
                for right in present[i + 1:]:
                    cooccurrence[left][right] += 1
                    cooccurrence[right][left] += 1
        # Sort entries within a day by time descending, matching the day-level
        # order so the whole timeline reads newest-first top to bottom. Times are
        # "HH:MM" strings (lexicographic == chronological); timeless entries sort
        # last, and the stable sort preserves original order among equal times.
        entries.sort(key=lambda e: e["time"], reverse=True)
        parsed_timeline.append({"id": tp.id, "date": date, "sources": list(tp.sources), "entries": entries})

    # created/updated live in frontmatter but not on Page; read them for detail pages.
    def page_dict(page: Page) -> dict[str, Any]:
        meta, _ = frontmatter.read_document(page.path)
        return {
            "id": page.id,
            "type": page.type,
            "title": page.title,
            "summary": page.summary,
            "entity_kind": page.entity_kind,
            "aliases": list(page.aliases),
            "body_html": render_markdown(page.body),
            "created": str(meta.get("created", "")),
            "updated": str(meta.get("updated", "")),
            "sources": list(page.sources),
            "related": _related_for(page.id, page_by_id, page_sources, cooccurrence),
            "timeline_appearances": sorted(
                appearances.get(page.id, []), key=lambda a: (a["date"], a["time"]), reverse=True
            ),
        }

    entity_dicts = sorted(
        (page_dict(p) for p in entities),
        key=lambda d: (ENTITY_KIND_RANK.get(d["entity_kind"] or "", 99), d["title"]),
    )
    topic_dicts = sorted((page_dict(p) for p in topics), key=lambda d: d["title"])

    raw_dicts = {
        entry.id: {
            "id": entry.id,
            "title": entry.title,
            "event_date": entry.event_date,
            "created": entry.created,
            "tags": list(entry.tags),
            "body_html": render_markdown(entry.body),
        }
        for entry in raws.values()
    }

    return {
        "generated_at": now_local().isoformat(),
        "repo": str(repo),
        "counts": {
            "entities": len(entity_dicts),
            "topics": len(topic_dicts),
            "timeline": len(parsed_timeline),
            "raw": len(raw_dicts),
        },
        "entities": entity_dicts,
        "topics": topic_dicts,
        "timeline": parsed_timeline,
        "raws": raw_dicts,
    }


# --------------------------------------------------------------------------- #
# HTML rendering                                                               #
# --------------------------------------------------------------------------- #
def _embed_json(model: dict[str, Any]) -> str:
    """Serialize the model for a <script type="application/json"> block.

    Escaping ``< > &`` as unicode escapes keeps the payload valid JSON while
    ensuring a stray ``</script>`` inside body text cannot close the block.
    """
    payload = json.dumps(model, ensure_ascii=False)
    return payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def render_html(model: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.replace(DATA_PLACEHOLDER, _embed_json(model))


def build_wiki_html(repo: Path, output_path: Path) -> dict[str, Any]:
    model = build_wiki_model(repo)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_html(model), encoding="utf-8")
    return {"output": str(output_path), "counts": model["counts"], "generated_at": model["generated_at"]}
