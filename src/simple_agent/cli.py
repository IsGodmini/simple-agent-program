"""Command-line interface."""

import argparse
from pathlib import Path

from .agent import Agent
from .config import Settings
from .context import ContextBudget, ContextManager
from .llm import OpenAICompatibleLLM
from .session import write_trace
from .tools import (
    ApplyPatchTool,
    ListFilesTool,
    ReadFileTool,
    RunCommandTool,
    ToolRegistry,
)
from .workspace import Workspace


def build_agent(workspace_path: Path) -> Agent:
    settings = Settings.from_env()
    workspace = Workspace(workspace_path)
    tools = ToolRegistry(
        [
            ListFilesTool(workspace),
            ReadFileTool(workspace),
            ApplyPatchTool(workspace),
            RunCommandTool(workspace),
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
    result = build_agent(args.workspace).run(request)
    if args.trace_file:
        write_trace(args.trace_file, request, result)
    print(result.content)
