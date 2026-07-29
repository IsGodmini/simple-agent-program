"""Optional JSON trace persistence."""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .agent import AgentResult


def write_trace(path: Path, user_request: str, result: AgentResult) -> None:
    """Write a complete, opt-in execution trace as UTF-8 JSON."""
    trace = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_request": user_request,
        "iterations": result.iterations,
        "compactions": result.compactions,
        "final_content": result.content,
        "tool_executions": [
            asdict(execution) for execution in result.tool_executions
        ],
        "messages": result.messages,
    }
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
