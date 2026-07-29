"""Shared safety checks for workspace tools."""

from pathlib import Path

DENIED_PATH_NAMES = {".git", ".venv", "__pycache__"}


def is_sensitive_name(name: str) -> bool:
    """Return whether a filename commonly contains credentials."""
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return Path(name).suffix.lower() in {".key", ".pem", ".p12", ".pfx"}


def ensure_safe_path(path: Path, root: Path) -> None:
    """Reject internal, generated, and credential-bearing paths."""
    relative_parts = path.relative_to(root).parts
    if any(part in DENIED_PATH_NAMES for part in relative_parts) or any(
        is_sensitive_name(part) for part in relative_parts
    ):
        raise ValueError(f"access to sensitive path is denied: {path.relative_to(root)}")
