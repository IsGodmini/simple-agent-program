"""Read-only filesystem tools for the first development phase."""

from pathlib import Path
from typing import Any, Dict, List

from ..workspace import Workspace
from .base import Tool

IGNORED_NAMES = {".git", ".venv", "__pycache__"}


def _is_sensitive_name(name: str) -> bool:
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return Path(name).suffix.lower() in {".key", ".pem", ".p12", ".pfx"}


class ListFilesTool(Tool):
    name = "list_files"
    description = (
        "List files and directories inside the project. "
        "Use this before reading files to understand the repository structure."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Project-relative directory path. Defaults to '.'.",
            }
        },
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace, max_entries: int = 200) -> None:
        self.workspace = workspace
        self.max_entries = max_entries

    def execute(self, arguments: Dict[str, Any]) -> str:
        relative_path = arguments.get("path", ".")
        if not isinstance(relative_path, str):
            raise ValueError("path must be a string")

        directory = self.workspace.resolve(relative_path)
        if not directory.exists():
            raise ValueError(f"directory does not exist: {relative_path}")
        if not directory.is_dir():
            raise ValueError(f"path is not a directory: {relative_path}")

        entries: List[str] = []
        for path in sorted(directory.rglob("*")):
            relative_parts = path.relative_to(directory).parts
            if any(part in IGNORED_NAMES for part in relative_parts) or any(
                _is_sensitive_name(part) for part in relative_parts
            ):
                continue
            suffix = "/" if path.is_dir() else ""
            entries.append(f"{path.relative_to(self.workspace.root)}{suffix}")
            if len(entries) >= self.max_entries:
                entries.append(
                    f"... output truncated after {self.max_entries} entries"
                )
                break

        return "\n".join(entries) if entries else "(empty directory)"


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file inside the project. "
        "The output includes line numbers for precise discussion."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Project-relative file path.",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace, max_chars: int = 30_000) -> None:
        self.workspace = workspace
        self.max_chars = max_chars

    def execute(self, arguments: Dict[str, Any]) -> str:
        relative_path = arguments.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("path must be a non-empty string")

        path = self.workspace.resolve(relative_path)
        if not path.exists():
            raise ValueError(f"file does not exist: {relative_path}")
        if not path.is_file():
            raise ValueError(f"path is not a file: {relative_path}")
        relative_parts = path.relative_to(self.workspace.root).parts
        if any(part in IGNORED_NAMES for part in relative_parts) or any(
            _is_sensitive_name(part) for part in relative_parts
        ):
            raise ValueError(f"access to sensitive path is denied: {relative_path}")

        text = path.read_text(encoding="utf-8")
        truncated = len(text) > self.max_chars
        if truncated:
            text = text[: self.max_chars]

        numbered = "\n".join(
            f"{line_number:>4} | {line}"
            for line_number, line in enumerate(text.splitlines(), start=1)
        )
        if truncated:
            numbered += f"\n... output truncated after {self.max_chars} characters"
        return numbered or "(empty file)"
