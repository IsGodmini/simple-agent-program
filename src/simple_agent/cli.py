"""Command-line interface."""

import argparse
from pathlib import Path

from .agent import Agent
from .config import Settings
from .llm import OpenAICompatibleLLM
from .tools import ListFilesTool, ReadFileTool, ToolRegistry
from .workspace import Workspace


def build_agent(workspace_path: Path) -> Agent:
    settings = Settings.from_env()
    workspace = Workspace(workspace_path)
    tools = ToolRegistry(
        [
            ListFilesTool(workspace),
            ReadFileTool(workspace),
        ]
    )
    return Agent(
        llm=OpenAICompatibleLLM(settings),
        tools=tools,
        max_iterations=settings.max_iterations,
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = build_agent(args.workspace).run(" ".join(args.request))
    print(result.content)
