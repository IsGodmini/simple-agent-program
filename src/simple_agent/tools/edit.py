"""Controlled text-file editing tool."""

from typing import Any, Callable, Dict, Optional

from ..workspace import Workspace
from .base import Tool
from .safety import ensure_safe_path


class ApplyPatchTool(Tool):
    """Create a text file or replace one exact, unique text block."""

    name = "apply_patch"
    description = (
        "Modify one UTF-8 text file in the project. Use mode='replace' with an "
        "exact, unique old_text block, or mode='create' for a new file. "
        "Read existing files before replacing text."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Project-relative file path.",
            },
            "mode": {
                "type": "string",
                "enum": ["replace", "create"],
                "description": "Whether to replace text or create a new file.",
            },
            "old_text": {
                "type": "string",
                "description": "Exact text to replace. Required in replace mode.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text or complete new file content.",
            },
        },
        "required": ["path", "mode", "new_text"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        workspace: Workspace,
        on_change: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.workspace = workspace
        self.on_change = on_change

    def execute(self, arguments: Dict[str, Any]) -> str:
        relative_path = arguments.get("path")
        mode = arguments.get("mode")
        new_text = arguments.get("new_text")
        old_text = arguments.get("old_text")

        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("path must be a non-empty string")
        if mode not in {"replace", "create"}:
            raise ValueError("mode must be 'replace' or 'create'")
        if not isinstance(new_text, str):
            raise ValueError("new_text must be a string")

        path = self.workspace.resolve(relative_path)
        ensure_safe_path(path, self.workspace.root)

        if mode == "create":
            if path.exists():
                raise ValueError(f"file already exists: {relative_path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text, encoding="utf-8")
            self._notify_change(relative_path)
            return f"Created {relative_path} ({len(new_text)} characters)"

        if not isinstance(old_text, str) or not old_text:
            raise ValueError("old_text must be a non-empty string in replace mode")
        if not path.exists():
            raise ValueError(f"file does not exist: {relative_path}")
        if not path.is_file():
            raise ValueError(f"path is not a file: {relative_path}")

        content = path.read_text(encoding="utf-8")
        occurrences = content.count(old_text)
        if occurrences != 1:
            raise ValueError(
                f"old_text must occur exactly once; found {occurrences} occurrences"
            )

        updated = content.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        self._notify_change(relative_path)
        return (
            f"Updated {relative_path}: replaced {len(old_text)} characters "
            f"with {len(new_text)} characters"
        )

    def _notify_change(self, relative_path: str) -> None:
        if self.on_change is not None:
            try:
                self.on_change(relative_path)
            except Exception:
                # The source edit is already durable; index refresh can retry
                # automatically at the beginning of the next requirement.
                return
