from __future__ import annotations

from typing import Any


def compile_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["entities", "topics", "timeline", "index_updates"],
        "properties": {
            "entities": [{
                "op": "upsert",
                "id": "entity-...",
                "entity_kind": "person|project|concept|emotion",
                "title": "页面标题",
                "aliases": ["别名"],
                "summary": "一句话摘要",
                "body_markdown": "## 概述\n...",
                "sources": ["raw-..."],
            }],
            "topics": [{
                "op": "upsert",
                "id": "topic-...",
                "title": "主题标题",
                "summary": "一句话摘要",
                "body_markdown": "## 概述\n...",
                "sources": ["raw-..."],
            }],
            "timeline": [{
                "event_date": "YYYY-MM-DD",
                "entries": [{
                    "time": "HH:MM",
                    "text": "时间线摘要，不复制大段原文",
                    "refs": ["entity-..."],
                }],
                "sources": ["raw-..."],
            }],
            "index_updates": [{
                "id": "entity-...|topic-...",
                "summary": "一句话摘要",
            }],
        },
    }


def search_response_schema() -> dict[str, Any]:
    return {
        "answer_markdown": "基于候选页面的归纳回答",
        "used_pages": ["entity-..."],
        "caveats": ["知识库内容可能是历史视角"],
    }


def review_response_schema() -> dict[str, Any]:
    return {
        "title": "回顾标题",
        "summary_markdown": "结构化回顾",
        "highlighted_pages": ["timeline-YYYY-MM-DD"],
    }


def recap_response_schema() -> dict[str, Any]:
    return {
        "title": "回顾标题",
        "recap_markdown": "面向用户的回顾与关联总结，串联本次记录与历史内容",
        "connections": ["与历史记录的具体关联点"],
        "patterns": ["反复出现的主题、情绪或项目"],
        "related_pages": ["entity-...|topic-...|timeline-YYYY-MM-DD"],
        "suggestions": ["可行动建议或值得追问的问题"],
    }


def merge_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["merges"],
        "properties": {
            "merges": [{
                "canonical": {
                    "op": "upsert",
                    "id": "entity-...|topic-...",
                    "entity_kind": "person|project|concept|emotion",
                    "title": "页面标题",
                    "aliases": ["别名"],
                    "summary": "一句话摘要",
                    "body_markdown": "## 概述\n...",
                    "sources": ["raw-..."],
                },
                "absorbed": ["entity-...|topic-..."],
                "reason": "为什么合并或精炼",
            }],
        },
    }


def llm_request(task: str, agents_rules: str, context: dict[str, Any], instructions: str, response_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": task,
        "agents_rules": agents_rules,
        "context": context,
        "response_schema": response_schema,
        "instructions": instructions,
    }
