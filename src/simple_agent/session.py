"""Task lifecycle, episodic-memory persistence, and optional JSON traces."""

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent import AgentResult, ToolExecution
from .knowledge import KnowledgeBase
from .llm import Message
from .memory import BuiltContext, ContextBuilder, ProjectMemoryStore, TaskSummary


@dataclass
class TaskSession:
    """One natural-language requirement and its cross-task context."""

    task_id: str
    request: str
    started_at: str
    context_messages: List[Message]
    memory_summary_ids: List[str]
    knowledge_citations: List[str] = field(default_factory=list)


class SessionManager:
    """Separate per-task working context from persistent project memory."""

    def __init__(
        self,
        store: ProjectMemoryStore,
        context_builder: Optional[ContextBuilder] = None,
        knowledge_base: Optional[KnowledgeBase] = None,
    ) -> None:
        self.store = store
        self.knowledge_base = knowledge_base or KnowledgeBase(store.workspace)
        self.context_builder = context_builder or ContextBuilder(
            store,
            knowledge_base=self.knowledge_base,
        )

    def start_task(self, request: str) -> TaskSession:
        built: BuiltContext = self.context_builder.build(request)
        now = datetime.now(timezone.utc)
        task_id = (
            f"task-{now.strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        return TaskSession(
            task_id=task_id,
            request=request,
            started_at=now.isoformat(),
            context_messages=built.messages,
            memory_summary_ids=built.summary_ids,
            knowledge_citations=built.knowledge_citations,
        )

    def complete_task(
        self,
        session: TaskSession,
        result: AgentResult,
    ) -> TaskSummary:
        finished_at = datetime.now(timezone.utc).isoformat()
        files_changed = self._files_changed(result.tool_executions)
        validations = self._validations(result.tool_executions)
        summary = TaskSummary(
            task_id=session.task_id,
            request=session.request,
            status="completed",
            summary=self.store.compact_text(result.content),
            files_changed=files_changed,
            validations=validations,
            started_at=session.started_at,
            finished_at=finished_at,
        )
        self.store.write_episode(
            session.task_id,
            {
                "task_id": session.task_id,
                "request": session.request,
                "status": "completed",
                "started_at": session.started_at,
                "finished_at": finished_at,
                "memory_summary_ids": session.memory_summary_ids,
                "knowledge_citations": session.knowledge_citations,
                "final_content": result.content,
                "iterations": result.iterations,
                "compactions": result.compactions,
                "files_changed": files_changed,
                "validations": validations,
                "tool_executions": [
                    asdict(execution) for execution in result.tool_executions
                ],
                "messages": [
                    message
                    for message in result.messages
                    if message.get("role") != "system"
                ],
            },
        )
        self.store.append_summary(summary)
        return summary

    def fail_task(self, session: TaskSession, error: Exception) -> TaskSummary:
        finished_at = datetime.now(timezone.utc).isoformat()
        error_text = f"{type(error).__name__}: {error}"
        summary = TaskSummary(
            task_id=session.task_id,
            request=session.request,
            status="failed",
            summary=self.store.compact_text(error_text),
            started_at=session.started_at,
            finished_at=finished_at,
        )
        self.store.write_episode(
            session.task_id,
            {
                "task_id": session.task_id,
                "request": session.request,
                "status": "failed",
                "started_at": session.started_at,
                "finished_at": finished_at,
                "memory_summary_ids": session.memory_summary_ids,
                "knowledge_citations": session.knowledge_citations,
                "error": error_text,
            },
        )
        self.store.append_summary(summary)
        return summary

    @staticmethod
    def _files_changed(executions: List[ToolExecution]) -> List[str]:
        files: List[str] = []
        for execution in executions:
            if execution.name != "apply_patch":
                continue
            arguments = SessionManager._arguments(execution.arguments)
            path = arguments.get("path")
            if isinstance(path, str) and path not in files:
                files.append(path)
        return files

    @staticmethod
    def _validations(executions: List[ToolExecution]) -> List[Dict[str, Any]]:
        validations = []
        for execution in executions:
            if execution.name != "run_command":
                continue
            arguments = SessionManager._arguments(execution.arguments)
            command = arguments.get("command")
            match = re.search(r"Exit code: (-?\d+)", execution.result)
            validations.append(
                {
                    "command": command,
                    "exit_code": int(match.group(1)) if match else None,
                }
            )
        return validations

    @staticmethod
    def _arguments(raw_arguments: str) -> Dict[str, Any]:
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        return arguments if isinstance(arguments, dict) else {}


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
