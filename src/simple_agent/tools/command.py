"""Allowlisted command execution without a shell."""

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from ..workspace import Workspace
from .base import Tool

PYTHON_MODULES = {"compileall", "mypy", "pytest", "ruff", "unittest"}
GIT_SUBCOMMANDS = {"diff", "log", "show", "status"}
GO_SUBCOMMANDS = {"build", "test", "vet"}
CARGO_SUBCOMMANDS = {"build", "check", "clippy", "fmt", "test"}
PACKAGE_MANAGER_SUBCOMMANDS = {"run", "test"}


class RunCommandTool(Tool):
    """Run common test and build commands inside the workspace."""

    name = "run_command"
    description = (
        "Run an allowlisted test, build, static-analysis, formatting, or "
        "read-only Git command in the project. Pass arguments as an array; "
        "shell syntax and arbitrary commands are not supported."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    "Command and arguments, for example "
                    "['python3', '-m', 'unittest', 'discover', '-s', 'tests']."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 300,
                "description": "Timeout in seconds. Defaults to 120.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        workspace: Workspace,
        default_timeout: int = 120,
        max_output_chars: int = 30_000,
    ) -> None:
        self.workspace = workspace
        self.default_timeout = default_timeout
        self.max_output_chars = max_output_chars

    def execute(self, arguments: Dict[str, Any]) -> str:
        command = arguments.get("command")
        timeout = arguments.get("timeout_seconds", self.default_timeout)
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part for part in command)
        ):
            raise ValueError("command must be a non-empty array of strings")
        if not isinstance(timeout, int) or not 1 <= timeout <= 300:
            raise ValueError("timeout_seconds must be an integer from 1 to 300")

        self._validate_command(command)
        try:
            completed = subprocess.run(
                command,
                cwd=self.workspace.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._safe_environment(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            return self._format_result(command, None, stdout, stderr, timeout)
        except FileNotFoundError as exc:
            raise ValueError(f"executable not found: {command[0]}") from exc

        return self._format_result(
            command,
            completed.returncode,
            completed.stdout,
            completed.stderr,
            timeout,
        )

    def _validate_command(self, command: List[str]) -> None:
        executable_path = Path(command[0])
        executable = executable_path.name
        if executable_path.is_absolute():
            resolved = executable_path.resolve()
            try:
                resolved.relative_to(self.workspace.root)
            except ValueError as exc:
                raise ValueError(
                    "absolute executables outside the workspace are not allowed"
                ) from exc

        for argument in command[1:]:
            if "\x00" in argument or "\n" in argument:
                raise ValueError("command arguments cannot contain control characters")
            path_value = argument.split("=", 1)[-1]
            if Path(path_value).is_absolute() or ".." in Path(path_value).parts:
                raise ValueError("command paths must stay inside the workspace")

        if executable.startswith("python"):
            self._validate_python(command)
        elif executable in {"pytest", "ruff", "mypy"}:
            return
        elif executable == "git":
            self._require_subcommand(command, GIT_SUBCOMMANDS)
        elif executable == "go":
            self._require_subcommand(command, GO_SUBCOMMANDS)
        elif executable == "cargo":
            self._require_subcommand(command, CARGO_SUBCOMMANDS)
        elif executable in {"npm", "pnpm", "yarn"}:
            self._require_subcommand(command, PACKAGE_MANAGER_SUBCOMMANDS)
        else:
            raise ValueError(f"command is not allowlisted: {command[0]}")

    @staticmethod
    def _validate_python(command: List[str]) -> None:
        if command[1:] in (["--version"], ["-V"]):
            return
        if len(command) < 3 or command[1] != "-m":
            raise ValueError("Python commands must use an allowlisted '-m module'")
        if command[2] not in PYTHON_MODULES:
            raise ValueError(f"Python module is not allowlisted: {command[2]}")

    @staticmethod
    def _require_subcommand(command: List[str], allowed: set) -> None:
        if len(command) < 2 or command[1] not in allowed:
            allowed_text = ", ".join(sorted(allowed))
            raise ValueError(
                f"{command[0]} subcommand must be one of: {allowed_text}"
            )

    @staticmethod
    def _safe_environment() -> Dict[str, str]:
        allowed_names = {"LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TMPDIR"}
        return {
            name: value
            for name, value in os.environ.items()
            if name in allowed_names
        }

    def _format_result(
        self,
        command: List[str],
        exit_code: Any,
        stdout: str,
        stderr: str,
        timeout: int,
    ) -> str:
        if exit_code is None:
            status = f"Timed out after {timeout} seconds"
        else:
            status = f"Exit code: {exit_code}"
        output = (
            f"Command: {command!r}\n"
            f"{status}\n"
            f"STDOUT:\n{stdout or '(empty)'}\n"
            f"STDERR:\n{stderr or '(empty)'}"
        )
        if len(output) > self.max_output_chars:
            output = (
                output[: self.max_output_chars]
                + f"\n... output truncated after {self.max_output_chars} characters"
            )
        return output
