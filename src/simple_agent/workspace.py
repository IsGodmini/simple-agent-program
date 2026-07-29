"""Workspace path validation."""

from pathlib import Path


class Workspace:
    """Resolve paths while preventing access outside the project root."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"Workspace does not exist: {self.root}")

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                f"Path is outside the workspace: {relative_path}"
            ) from exc
        return candidate
