"""Tools for selectively retrieving persistent episodic memory."""

import json
from dataclasses import asdict
from typing import Any, Dict

from ..memory import ProjectMemoryStore
from .base import Tool


class SearchMemoryTool(Tool):
    """Search compact task summaries without loading raw prior transcripts."""

    name = "search_memory"
    description = (
        "Search summaries of earlier project tasks. Use this when the current "
        "request may depend on a past decision or change. Memory may be stale; "
        "verify current files before editing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords, feature name, symbol, or file path.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Maximum summaries to return. Defaults to 3.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, store: ProjectMemoryStore) -> None:
        self.store = store

    def execute(self, arguments: Dict[str, Any]) -> str:
        query = arguments.get("query")
        limit = arguments.get("limit", 3)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer from 1 to 10")

        summaries = self.store.search_summaries(query, limit)
        if not summaries:
            return "No matching project memories found."
        return json.dumps(
            [asdict(summary) for summary in summaries],
            ensure_ascii=False,
            indent=2,
        )


class ReadEpisodeTool(Tool):
    """Read one detailed prior task only after its ID is known."""

    name = "read_episode"
    description = (
        "Read the detailed episodic record for one earlier task ID returned by "
        "project context or search_memory. This may contain old tool results, "
        "so verify all current code and test state before relying on it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Exact task ID, such as task-20260729T120000Z-ab12cd34.",
            }
        },
        "required": ["task_id"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        store: ProjectMemoryStore,
        max_chars: int = 30_000,
    ) -> None:
        self.store = store
        self.max_chars = max_chars

    def execute(self, arguments: Dict[str, Any]) -> str:
        task_id = arguments.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be a non-empty string")
        content = json.dumps(
            self.store.read_episode(task_id),
            ensure_ascii=False,
            indent=2,
        )
        if len(content) > self.max_chars:
            return (
                content[: self.max_chars]
                + f"\n... episode truncated after {self.max_chars} characters"
            )
        return content
