"""Command-line interface."""

import argparse
import json
from pathlib import Path
from typing import List, Optional

from .agent import Agent
from .config import Settings
from .context import ContextBudget, ContextManager
from .knowledge import KnowledgeBase, document_to_dict, is_supported_document
from .llm import OpenAICompatibleLLM
from .memory import ProjectMemoryStore
from .session import SessionManager, write_trace
from .tools import (
    ApplyPatchTool,
    FindFilesTool,
    ListFilesTool,
    ListKnowledgeTool,
    ReadEpisodeTool,
    ReadFileTool,
    ReadKnowledgeTool,
    RepositoryMapTool,
    RunCommandTool,
    SearchCodeTool,
    SearchKnowledgeTool,
    SearchMemoryTool,
    ToolRegistry,
)
from .workspace import Workspace


def build_agent(
    workspace_path: Path,
    memory_store: Optional[ProjectMemoryStore] = None,
    knowledge_base: Optional[KnowledgeBase] = None,
) -> Agent:
    settings = Settings.from_env()
    workspace = Workspace(workspace_path)
    memory_store = memory_store or ProjectMemoryStore(workspace)
    knowledge_base = knowledge_base or KnowledgeBase(workspace)
    tools = ToolRegistry(
        [
            ListFilesTool(workspace),
            FindFilesTool(workspace),
            SearchCodeTool(workspace),
            RepositoryMapTool(workspace),
            ReadFileTool(workspace),
            ApplyPatchTool(workspace),
            RunCommandTool(workspace),
            SearchKnowledgeTool(knowledge_base),
            ReadKnowledgeTool(knowledge_base),
            ListKnowledgeTool(knowledge_base),
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
    parser.add_argument("request", nargs="*", help="Natural-language task")
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
    parser.add_argument(
        "--knowledge-file",
        type=Path,
        action="append",
        default=[],
        help="Import one knowledge document; may be repeated.",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        action="append",
        default=[],
        help="Recursively import supported documents from a directory.",
    )
    parser.add_argument(
        "--list-knowledge",
        action="store_true",
        help="List indexed knowledge documents without calling the LLM.",
    )
    parser.add_argument(
        "--remove-knowledge",
        action="append",
        default=[],
        metavar="DOCUMENT_ID",
        help="Remove an indexed document by ID; may be repeated.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workspace = Workspace(args.workspace)
    memory_store = ProjectMemoryStore(workspace)
    knowledge_base = KnowledgeBase(workspace)
    knowledge_output = _handle_knowledge_actions(args, knowledge_base)
    if knowledge_output:
        print(knowledge_output)

    request = " ".join(args.request).strip()
    if not request:
        if (
            args.knowledge_file
            or args.knowledge_dir
            or args.list_knowledge
            or args.remove_knowledge
        ):
            return
        raise ValueError(
            "provide a natural-language request or a knowledge management option"
        )

    session_manager = SessionManager(
        memory_store,
        knowledge_base=knowledge_base,
    )
    task = session_manager.start_task(request)
    try:
        result = build_agent(
            args.workspace,
            memory_store=memory_store,
            knowledge_base=knowledge_base,
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


def _handle_knowledge_actions(
    args: argparse.Namespace,
    knowledge_base: KnowledgeBase,
) -> str:
    output: List[str] = []
    sources = list(args.knowledge_file)
    for directory in args.knowledge_dir:
        resolved = directory.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"knowledge directory does not exist: {directory}")
        sources.extend(
            path
            for path in sorted(resolved.rglob("*"))
            if path.is_file() and is_supported_document(path)
        )

    if sources:
        documents = knowledge_base.ingest_many(sources)
        output.append(
            "已导入知识文档：\n"
            + json.dumps(
                [document_to_dict(document) for document in documents],
                ensure_ascii=False,
                indent=2,
            )
        )

    for document_id in args.remove_knowledge:
        removed = knowledge_base.remove(document_id)
        output.append(
            f"{'已删除' if removed else '未找到'}知识文档：{document_id}"
        )

    if args.list_knowledge:
        output.append(
            "当前知识文档：\n"
            + json.dumps(
                [
                    document_to_dict(document)
                    for document in knowledge_base.list_documents()
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    return "\n".join(output)
