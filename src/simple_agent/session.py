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
from .project_graph import ProjectGraph
from .project_index import ProjectIndex


@dataclass
class RequirementRun:
    """One requirement inside a persistent workspace conversation session."""

    task_id: str
    session_id: str
    request: str
    started_at: str
    context_messages: List[Message]
    memory_summary_ids: List[str]
    knowledge_citations: List[str] = field(default_factory=list)
    project_graph_citations: List[str] = field(default_factory=list)
    project_index_citations: List[str] = field(default_factory=list)


class SessionManager:
    """Manage a multi-requirement conversation over shared project memory."""

    def __init__(
        self,
        store: ProjectMemoryStore,
        context_builder: Optional[ContextBuilder] = None,
        knowledge_base: Optional[KnowledgeBase] = None,
        project_index: Optional[ProjectIndex] = None,
        project_graph: Optional[ProjectGraph] = None,
        session_id: str = "default",
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.store.ensure_session(session_id)
        self.knowledge_base = knowledge_base or KnowledgeBase(store.workspace)
        self.project_index = project_index or ProjectIndex(store.workspace)
        self.project_graph = project_graph or ProjectGraph(
            store.workspace,
            self.project_index,
        )
        self.context_builder = context_builder or ContextBuilder(
            store,
            knowledge_base=self.knowledge_base,
            project_index=self.project_index,
            project_graph=self.project_graph,
        )

    def start_requirement(self, request: str) -> RequirementRun:
        built: BuiltContext = self.context_builder.build(
            request,
            session_id=self.session_id,
        )
        now = datetime.now(timezone.utc)
        task_id = (
            f"task-{now.strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        return RequirementRun(
            task_id=task_id,
            session_id=self.session_id,
            request=request,
            started_at=now.isoformat(),
            context_messages=built.messages,
            memory_summary_ids=built.summary_ids,
            knowledge_citations=built.knowledge_citations,
            project_graph_citations=built.project_graph_citations,
            project_index_citations=built.project_index_citations,
        )

    def start_task(self, request: str) -> RequirementRun:
        """Backward-compatible alias for starting one requirement."""
        return self.start_requirement(request)

    def complete_task(
        self,
        session: RequirementRun,
        result: AgentResult,
    ) -> TaskSummary:
        finished_at = datetime.now(timezone.utc).isoformat()
        files_changed = self._files_changed(result.tool_executions)
        validations = self._validations(result.tool_executions)
        verification = self._verification(result, validations)
        summary = TaskSummary(
            task_id=session.task_id,
            request=session.request,
            status="completed",
            summary=self.store.compact_text(result.content),
            files_changed=files_changed,
            validations=validations,
            started_at=session.started_at,
            finished_at=finished_at,
            session_id=session.session_id,
            verification=verification,
        )
        self.store.write_episode(
            session.task_id,
            {
                "task_id": session.task_id,
                "requirement_id": session.task_id,
                "session_id": session.session_id,
                "request": session.request,
                "status": "completed",
                "started_at": session.started_at,
                "finished_at": finished_at,
                "memory_summary_ids": session.memory_summary_ids,
                "knowledge_citations": session.knowledge_citations,
                "project_graph_citations": session.project_graph_citations,
                "project_index_citations": (
                    session.project_index_citations
                ),
                "final_content": result.content,
                "iterations": result.iterations,
                "compactions": result.compactions,
                "workflow": result.workflow,
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
        self.store.append_requirement_to_session(summary)
        return summary

    def fail_task(
        self,
        session: RequirementRun,
        error: Exception,
    ) -> TaskSummary:
        finished_at = datetime.now(timezone.utc).isoformat()
        error_text = f"{type(error).__name__}: {error}"
        summary = TaskSummary(
            task_id=session.task_id,
            request=session.request,
            status="failed",
            summary=self.store.compact_text(error_text),
            started_at=session.started_at,
            finished_at=finished_at,
            session_id=session.session_id,
            verification="failed",
        )
        self.store.write_episode(
            session.task_id,
            {
                "task_id": session.task_id,
                "requirement_id": session.task_id,
                "session_id": session.session_id,
                "request": session.request,
                "status": "failed",
                "started_at": session.started_at,
                "finished_at": finished_at,
                "memory_summary_ids": session.memory_summary_ids,
                "knowledge_citations": session.knowledge_citations,
                "project_graph_citations": session.project_graph_citations,
                "project_index_citations": (
                    session.project_index_citations
                ),
                "error": error_text,
                "workflow": getattr(error, "workflow", None),
            },
        )
        self.store.append_summary(summary)
        self.store.append_requirement_to_session(summary)
        return summary

    @staticmethod
    def _verification(
        result: AgentResult,
        validations: List[Dict[str, Any]],
    ) -> str:
        if validations:
            if all(item.get("exit_code") == 0 for item in validations):
                return "verified"
            return "failed"
        workflow = result.workflow or {}
        reviews = workflow.get("reviews", [])
        if isinstance(reviews, list) and reviews:
            last = reviews[-1]
            if isinstance(last, dict) and last.get("verdict") == "pass":
                return "verified"
        return "unverified"

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


def write_trace(
    path: Path,
    user_request: str,
    result: AgentResult,
    session_id: str = "",
    requirement_id: str = "",
) -> None:
    """Write a complete, opt-in execution trace as UTF-8 JSON."""
    trace = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "requirement_id": requirement_id,
        "user_request": user_request,
        "iterations": result.iterations,
        "compactions": result.compactions,
        "workflow": result.workflow,
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


# Existing integrations imported TaskSession; keep it as a compatibility alias.
TaskSession = RequirementRun
