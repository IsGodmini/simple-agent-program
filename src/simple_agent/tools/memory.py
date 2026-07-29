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
        "搜索当前工作区所有会话共享的场景记忆摘要。当前需求可能依赖其他"
        "会话中的历史决策或修改时使用。结果包含 session_id；记忆可能过期，"
        "修改前必须核对当前文件。"
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
            "session_id": {
                "type": "string",
                "description": (
                    "可选；只搜索指定会话。不提供时搜索整个工作区。"
                ),
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
        session_id = arguments.get("session_id")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer from 1 to 10")
        if session_id is not None:
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("session_id must be a non-empty string")
            self.store.get_session(session_id)

        summaries = self.store.search_summaries(
            query,
            limit,
            session_id=session_id,
        )
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
        "根据 search_memory 或项目上下文返回的 task_id，读取当前工作区中"
        "任意会话的一条详细场景记忆。内容可能包含旧工具结果，使用前必须"
        "核对当前代码和测试状态。"
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
