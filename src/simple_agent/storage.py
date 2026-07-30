"""Project-scoped persistent storage outside source workspaces."""

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from dotenv import load_dotenv

from .workspace import Workspace

STORAGE_HOME_ENV = "SIMPLE_AGENT_HOME"


class ProjectStorage:
    """Resolve one isolated system storage region for a workspace."""

    def __init__(self, workspace: Workspace) -> None:
        load_dotenv()
        self.workspace = workspace
        configured = os.getenv(STORAGE_HOME_ENV, "").strip()
        self.customized = bool(configured)
        candidate = (
            Path(configured).expanduser()
            if configured
            else Path.home() / ".simple-agent"
        )
        if candidate.exists() and candidate.is_symlink():
            raise ValueError("simple-agent storage home cannot be a symlink")
        self.home = candidate.resolve()
        try:
            self.home.relative_to(workspace.root)
        except ValueError:
            pass
        else:
            raise ValueError(
                "simple-agent storage home must be outside the workspace"
            )
        self.projects_root = self.home / "projects"
        self.project_id = hashlib.sha256(
            str(workspace.root).encode("utf-8")
        ).hexdigest()[:24]
        self.root = self.projects_root / self.project_id
        self.legacy_root = workspace.root / ".simple-agent"
        self._migrate_legacy()

    def status(self) -> dict:
        return {
            "home": str(self.home),
            "project_id": self.project_id,
            "project_root": str(self.root),
            "customized": self.customized,
            "legacy_storage_present": self.legacy_root.exists(),
        }

    def ensure_path(self, path: Path, label: str = "storage") -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                f"{label} path is outside the project storage root"
            ) from exc
        current = self.home
        paths = [current]
        for part in Path("projects", self.project_id, *relative.parts).parts:
            current = current / part
            paths.append(current)
        if any(item.is_symlink() for item in paths if item.exists()):
            raise ValueError(f"{label} paths cannot contain symbolic links")
        try:
            path.resolve(strict=False).relative_to(self.home)
        except ValueError as exc:
            raise ValueError(
                f"{label} path is outside the simple-agent storage home"
            ) from exc

    def _migrate_legacy(self) -> None:
        if self.root.exists() or not self.legacy_root.exists():
            return
        if self.legacy_root.is_symlink():
            raise ValueError(
                "legacy .simple-agent storage cannot contain symbolic links"
            )
        if any(path.is_symlink() for path in self.legacy_root.rglob("*")):
            raise ValueError(
                "legacy .simple-agent storage cannot contain symbolic links"
            )
        self.projects_root.mkdir(parents=True, exist_ok=True)
        temporary = self.projects_root / (
            f".migrate-{self.project_id}-{uuid.uuid4().hex}"
        )
        try:
            shutil.copytree(self.legacy_root, temporary)
            try:
                temporary.replace(self.root)
            except FileExistsError:
                pass
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
