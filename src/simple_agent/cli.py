"""Command-line interface."""

import argparse
from pathlib import Path
from typing import Optional

from .agent import Agent
from .config import Settings
from .context import ContextBudget, ContextManager
from .llm import OpenAICompatibleLLM
from .memory import ProjectMemoryStore
from .session import SessionManager, write_trace
from .tools import (
    ApplyPatchTool,
    FindFilesTool,
    ListFilesTool,
    ReadEpisodeTool,
    ReadFileTool,
    RepositoryMapTool,
    RunCommandTool,
    SearchCodeTool,
    SearchMemoryTool,
    ToolRegistry,
)
from .workspace import Workspace


def build_agent(
    workspace_path: Path,
    memory_store: Optional[ProjectMemoryStore] = None,
) -> Agent:
    settings = Settings.from_env()
    workspace = Workspace(workspace_path)
    memory_store = memory_store or ProjectMemoryStore(workspace)
    tools = ToolRegistry(
        [
            ListFilesTool(workspace),
            FindFilesTool(workspace),
            SearchCodeTool(workspace),
            RepositoryMapTool(workspace),
            ReadFileTool(workspace),
            ApplyPatchTool(workspace),
            RunCommandTool(workspace),
            SearchMemoryTool(memory_store),
            ReadEpisodeTool(memory_store),
        ]
    )
    return Agent(
        llm=OpenAICompatibleLLM(settings),
        tools=tools,
        max_iterations=settings.max_iterations,
        context_manager=ContextManager(
            ContextBudget(
                context_window=settings.context_window,
                max_input_tokens=settings.max_input_tokens,
                max_output_tokens=settings.max_output_tokens,
                compact_at_tokens=settings.compact_at_tokens,
            )
        ),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect a project using a tool-enabled LLM agent."
    )
    parser.add_argument("request", nargs="+", help="Natural-language task")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Project directory (default: current directory)",
    )
    parser.add_argument(
        "--trace-file",
        type=Path,
        help="Optionally save the complete execution trace as JSON.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    request = " ".join(args.request)
    workspace = Workspace(args.workspace)
    memory_store = ProjectMemoryStore(workspace)
    session_manager = SessionManager(memory_store)
    task = session_manager.start_task(request)
    try:
        result = build_agent(
            args.workspace,
            memory_store=memory_store,
        ).run(
            request,
            context_messages=task.context_messages,
        )
    except Exception as exc:
        session_manager.fail_task(task, exc)
        raise
    session_manager.complete_task(task, result)
    if args.trace_file:
        write_trace(args.trace_file, request, result)
    print(result.content)
