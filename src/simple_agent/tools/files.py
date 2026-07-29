"""Scalable, read-only filesystem tools for large source repositories."""

import fnmatch
import json
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from ..workspace import Workspace
from .base import Tool
from .safety import DENIED_PATH_NAMES, ensure_safe_path, is_sensitive_name

DEFAULT_GENERATED_DIRS = {
    ".cache",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
DEFAULT_GENERATED_FILES = {
    ".coverage",
}
MANIFEST_NAMES = {
    "Cargo.toml",
    "Dockerfile",
    "Gemfile",
    "Makefile",
    "README.md",
    "compose.yaml",
    "docker-compose.yml",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
    "settings.gradle",
}
ENTRYPOINT_NAMES = {
    "__main__.py",
    "app.py",
    "cli.py",
    "index.js",
    "index.ts",
    "main.go",
    "main.js",
    "main.py",
    "main.rs",
    "main.ts",
    "server.js",
    "server.py",
}
RG_EXCLUDE_GLOBS = [
    "!**/.git/**",
    "!**/.simple-agent/**",
    "!**/.venv/**",
    "!**/__pycache__/**",
    "!**/.env",
    "!**/.env.*",
    "!**/*.key",
    "!**/*.pem",
    "!**/*.p12",
    "!**/*.pfx",
]


class RepositoryScanner:
    """Walk a workspace with deterministic ordering and directory pruning."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def iter_paths(
        self,
        relative_path: str = ".",
        max_depth: int = 6,
        include_generated: bool = False,
        include_hidden: bool = True,
    ) -> Iterator[Path]:
        directory = self.workspace.resolve(relative_path)
        if not directory.exists():
            raise ValueError(f"directory does not exist: {relative_path}")
        if not directory.is_dir():
            raise ValueError(f"path is not a directory: {relative_path}")

        for current_text, dirnames, filenames in os.walk(
            directory,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_text)
            current_depth = len(current.relative_to(directory).parts)
            visible_directories = []
            for name in sorted(dirnames):
                candidate = current / name
                if self._skip(
                    candidate,
                    name,
                    include_generated,
                    include_hidden,
                    is_directory=True,
                ):
                    continue
                if current_depth + 1 <= max_depth:
                    visible_directories.append(name)
                    yield candidate
            dirnames[:] = (
                visible_directories
                if current_depth + 1 < max_depth
                else []
            )

            if current_depth + 1 > max_depth:
                continue
            for name in sorted(filenames):
                candidate = current / name
                if self._skip(
                    candidate,
                    name,
                    include_generated,
                    include_hidden,
                    is_directory=False,
                ):
                    continue
                yield candidate

    def _skip(
        self,
        path: Path,
        name: str,
        include_generated: bool,
        include_hidden: bool,
        is_directory: bool,
    ) -> bool:
        if name in DENIED_PATH_NAMES or is_sensitive_name(name):
            return True
        if not include_hidden and name.startswith("."):
            return True
        if not include_generated:
            if is_directory and name in DEFAULT_GENERATED_DIRS:
                return True
            if not is_directory and (
                name in DEFAULT_GENERATED_FILES
                or name.endswith((".min.js", ".min.css", ".pyc"))
            ):
                return True
        if path.is_symlink():
            try:
                path.resolve().relative_to(self.workspace.root)
            except ValueError:
                return True
        return False


class ListFilesTool(Tool):
    name = "list_files"
    description = (
        "按稳定顺序分页列出项目中的文件和目录。大型项目先使用较小深度查看"
        "结构，再通过 offset 翻页或缩小 path。默认跳过依赖、缓存和构建产物。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "项目相对目录，默认'.'。",
            },
            "pattern": {
                "type": "string",
                "description": "可选 Glob，例如 '*.py' 或 'src/**'。",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "description": "递归深度，默认6。",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "跳过的条目数，用于分页，默认0。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": "本页最大条目数，默认200。",
            },
            "include_generated": {
                "type": "boolean",
                "description": "是否包含依赖和构建产物，默认false。",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "是否包含安全范围内的隐藏文件，默认true。",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.scanner = RepositoryScanner(workspace)

    def execute(self, arguments: Dict[str, Any]) -> str:
        options = _scan_options(arguments)
        pattern = arguments.get("pattern")
        if pattern is not None and not isinstance(pattern, str):
            raise ValueError("pattern must be a string")
        offset, limit = _pagination(arguments, default_limit=200)

        entries: List[str] = []
        matched = 0
        has_more = False
        for path in self.scanner.iter_paths(**options):
            relative = path.relative_to(self.workspace.root).as_posix()
            if pattern and not (
                fnmatch.fnmatch(path.name, pattern)
                or fnmatch.fnmatch(relative, pattern)
            ):
                continue
            if matched < offset:
                matched += 1
                continue
            if len(entries) >= limit:
                has_more = True
                break
            suffix = "@" if path.is_symlink() else "/" if path.is_dir() else ""
            entries.append(f"{relative}{suffix}")
            matched += 1

        return _paged_output(
            entries,
            offset,
            has_more,
            empty_text="(没有匹配的文件或目录)",
        )


class FindFilesTool(Tool):
    name = "find_files"
    description = (
        "按 Glob 在大型项目中查找文件路径，不读取文件内容。适合定位配置、"
        "测试、入口文件或某类源码；结果支持 offset 分页。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "文件名或相对路径 Glob，例如 '*test*.py'。",
            },
            "path": {
                "type": "string",
                "description": "搜索起始目录，默认'.'。",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "最大搜索深度，默认20。",
            },
            "offset": {"type": "integer", "minimum": 0},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": "本页最大结果数，默认200。",
            },
            "include_generated": {"type": "boolean"},
            "include_hidden": {"type": "boolean"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.scanner = RepositoryScanner(workspace)

    def execute(self, arguments: Dict[str, Any]) -> str:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a non-empty string")
        options = _scan_options(arguments, default_max_depth=20)
        offset, limit = _pagination(arguments, default_limit=200)

        results: List[str] = []
        matched = 0
        has_more = False
        for path in self.scanner.iter_paths(**options):
            if not path.is_file():
                continue
            relative = path.relative_to(self.workspace.root).as_posix()
            if not (
                fnmatch.fnmatch(path.name, pattern)
                or fnmatch.fnmatch(relative, pattern)
            ):
                continue
            if matched < offset:
                matched += 1
                continue
            if len(results) >= limit:
                has_more = True
                break
            results.append(relative)
            matched += 1

        return _paged_output(
            results,
            offset,
            has_more,
            empty_text="(没有匹配的文件)",
        )


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "按行读取项目内的 UTF-8 文本文件。大型文件必须使用 start_line、"
        "end_line 或 max_lines 分段读取；返回结果包含续读提示。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "项目相对文件路径。",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "起始行，默认1。",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "可选结束行（包含）。",
            },
            "max_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
                "description": "单次最多读取行数，默认400。",
            },
            "encoding": {
                "type": "string",
                "enum": ["utf-8", "utf-8-sig", "latin-1"],
                "description": "文本编码，默认utf-8。",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        workspace: Workspace,
        max_chars: int = 30_000,
    ) -> None:
        self.workspace = workspace
        self.max_chars = max_chars

    def execute(self, arguments: Dict[str, Any]) -> str:
        relative_path = arguments.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("path must be a non-empty string")
        start_line = arguments.get("start_line", 1)
        end_line = arguments.get("end_line")
        max_lines = arguments.get("max_lines", 400)
        encoding = arguments.get("encoding", "utf-8")
        if not isinstance(start_line, int) or start_line < 1:
            raise ValueError("start_line must be a positive integer")
        if end_line is not None and (
            not isinstance(end_line, int) or end_line < start_line
        ):
            raise ValueError("end_line must be an integer >= start_line")
        if not isinstance(max_lines, int) or not 1 <= max_lines <= 2000:
            raise ValueError("max_lines must be an integer from 1 to 2000")
        if encoding not in {"utf-8", "utf-8-sig", "latin-1"}:
            raise ValueError("unsupported encoding")

        path = self.workspace.resolve(relative_path)
        if not path.exists():
            raise ValueError(f"file does not exist: {relative_path}")
        if not path.is_file():
            raise ValueError(f"path is not a file: {relative_path}")
        ensure_safe_path(path, self.workspace.root)
        if _looks_binary(path):
            raise ValueError(f"binary files are not supported: {relative_path}")

        requested_lines = max_lines
        if end_line is not None:
            requested_lines = min(max_lines, end_line - start_line + 1)
        selected: List[Tuple[int, str]] = []
        has_more = False
        char_count = 0
        with path.open("r", encoding=encoding) as file:
            for line_number, line in enumerate(file, start=1):
                if line_number < start_line:
                    continue
                if end_line is not None and line_number > end_line:
                    has_more = True
                    break
                clean_line = line.rstrip("\r\n")
                rendered_length = len(clean_line) + 10
                if (
                    len(selected) >= requested_lines
                    or char_count + rendered_length > self.max_chars
                ):
                    has_more = True
                    break
                selected.append((line_number, clean_line))
                char_count += rendered_length

        size = path.stat().st_size
        header = (
            f"文件: {relative_path} | 大小: {size} bytes | "
            f"读取起始行: {start_line}"
        )
        if not selected:
            return f"{header}\n(该范围没有内容)"
        numbered = "\n".join(
            f"{line_number:>6} | {line}"
            for line_number, line in selected
        )
        footer = ""
        if has_more:
            footer = (
                f"\n\n[还有更多内容；下一次使用 start_line="
                f"{selected[-1][0] + 1} 继续读取]"
            )
        return f"{header}\n{numbered}{footer}"


class SearchCodeTool(Tool):
    name = "search_code"
    description = (
        "在项目源码中搜索文本或正则表达式，返回文件、行号和匹配行。优先使用"
        "搜索定位符号，再通过 read_file 分段读取上下文。支持分页。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索文本或正则表达式。",
            },
            "path": {
                "type": "string",
                "description": "搜索目录，默认'.'。",
            },
            "glob": {
                "type": "string",
                "description": "可选文件 Glob，例如 '*.py'。",
            },
            "regex": {
                "type": "boolean",
                "description": "是否按正则搜索，默认false。",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "是否区分大小写，默认true。",
            },
            "offset": {"type": "integer", "minimum": 0},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "本页最大匹配数，默认100。",
            },
            "include_generated": {"type": "boolean"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        workspace: Workspace,
        timeout_seconds: int = 20,
        use_ripgrep: bool = True,
    ) -> None:
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds
        self.use_ripgrep = use_ripgrep
        self.scanner = RepositoryScanner(workspace)

    def execute(self, arguments: Dict[str, Any]) -> str:
        query = arguments.get("query")
        relative_path = arguments.get("path", ".")
        glob = arguments.get("glob")
        is_regex = arguments.get("regex", False)
        case_sensitive = arguments.get("case_sensitive", True)
        include_generated = arguments.get("include_generated", False)
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")
        if "\x00" in query or "\n" in query:
            raise ValueError("query cannot contain control characters")
        if not isinstance(relative_path, str):
            raise ValueError("path must be a string")
        if glob is not None and not isinstance(glob, str):
            raise ValueError("glob must be a string")
        if not isinstance(is_regex, bool) or not isinstance(case_sensitive, bool):
            raise ValueError("regex and case_sensitive must be booleans")
        if not isinstance(include_generated, bool):
            raise ValueError("include_generated must be a boolean")
        offset, limit = _pagination(arguments, default_limit=100, max_limit=500)
        search_root = self.workspace.resolve(relative_path)
        if not search_root.is_dir():
            raise ValueError(f"path is not a directory: {relative_path}")

        if self.use_ripgrep:
            try:
                matches, has_more = self._search_with_rg(
                    query,
                    search_root,
                    glob,
                    is_regex,
                    case_sensitive,
                    include_generated,
                    offset,
                    limit,
                )
            except FileNotFoundError:
                matches, has_more = self._search_with_python(
                    query,
                    relative_path,
                    glob,
                    is_regex,
                    case_sensitive,
                    include_generated,
                    offset,
                    limit,
                )
        else:
            matches, has_more = self._search_with_python(
                query,
                relative_path,
                glob,
                is_regex,
                case_sensitive,
                include_generated,
                offset,
                limit,
            )

        return _paged_output(
            matches,
            offset,
            has_more,
            empty_text="(没有匹配结果)",
        )

    def _search_with_rg(
        self,
        query: str,
        search_root: Path,
        glob: Optional[str],
        is_regex: bool,
        case_sensitive: bool,
        include_generated: bool,
        offset: int,
        limit: int,
    ) -> Tuple[List[str], bool]:
        command = ["rg", "--json", "--line-number", "--hidden"]
        if not is_regex:
            command.append("--fixed-strings")
        if not case_sensitive:
            command.append("--ignore-case")
        for exclude in RG_EXCLUDE_GLOBS:
            command.extend(["--glob", exclude])
        if not include_generated:
            for directory in sorted(DEFAULT_GENERATED_DIRS):
                command.extend(["--glob", f"!**/{directory}/**"])
        if glob:
            command.extend(["--glob", glob])
        command.extend(["--", query, str(search_root)])

        process = subprocess.Popen(
            command,
            cwd=self.workspace.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        results: List[str] = []
        seen = 0
        has_more = False
        deadline = time.monotonic() + self.timeout_seconds
        try:
            assert process.stdout is not None
            for raw_record in process.stdout:
                if time.monotonic() > deadline:
                    raise ValueError(
                        f"code search timed out after {self.timeout_seconds} seconds"
                    )
                record = json.loads(raw_record)
                if record.get("type") != "match":
                    continue
                rendered = self._render_rg_match(record["data"])
                if rendered is None:
                    continue
                if seen < offset:
                    seen += 1
                    continue
                if len(results) >= limit:
                    has_more = True
                    process.terminate()
                    break
                results.append(rendered)
                seen += 1
            remaining = max(0.1, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                raise ValueError(
                    f"code search timed out after {self.timeout_seconds} seconds"
                ) from exc
            if not has_more and process.returncode not in {0, 1}:
                assert process.stderr is not None
                error = process.stderr.read().strip()
                raise ValueError(f"ripgrep failed: {error or process.returncode}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        return results, has_more

    def _render_rg_match(self, data: Dict[str, Any]) -> Optional[str]:
        raw_path = data.get("path", {}).get("text")
        if not isinstance(raw_path, str):
            return None
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.workspace.root / path
        path = path.resolve()
        try:
            ensure_safe_path(path, self.workspace.root)
            relative = path.relative_to(self.workspace.root).as_posix()
        except ValueError:
            return None
        line_number = data.get("line_number", "?")
        line = data.get("lines", {}).get("text", "").rstrip("\r\n")
        return f"{relative}:{line_number}: {_truncate_line(line)}"

    def _search_with_python(
        self,
        query: str,
        relative_path: str,
        glob: Optional[str],
        is_regex: bool,
        case_sensitive: bool,
        include_generated: bool,
        offset: int,
        limit: int,
    ) -> Tuple[List[str], bool]:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if is_regex else re.escape(query), flags)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc

        results: List[str] = []
        seen = 0
        has_more = False
        deadline = time.monotonic() + self.timeout_seconds
        for path in self.scanner.iter_paths(
            relative_path=relative_path,
            max_depth=50,
            include_generated=include_generated,
            include_hidden=True,
        ):
            if time.monotonic() > deadline:
                raise ValueError(
                    f"code search timed out after {self.timeout_seconds} seconds"
                )
            if not path.is_file() or _looks_binary(path):
                continue
            relative = path.relative_to(self.workspace.root).as_posix()
            if glob and not (
                fnmatch.fnmatch(path.name, glob)
                or fnmatch.fnmatch(relative, glob)
            ):
                continue
            try:
                with path.open("r", encoding="utf-8") as file:
                    for line_number, line in enumerate(file, start=1):
                        if not pattern.search(line):
                            continue
                        if seen < offset:
                            seen += 1
                            continue
                        if len(results) >= limit:
                            has_more = True
                            return results, has_more
                        results.append(
                            f"{relative}:{line_number}: "
                            f"{_truncate_line(line.rstrip())}"
                        )
                        seen += 1
            except UnicodeDecodeError:
                continue
        return results, has_more


class RepositoryMapTool(Tool):
    name = "repository_map"
    description = (
        "扫描大型仓库并返回紧凑项目地图：文件/目录数量、主要扩展名、顶层模块、"
        "清单文件和可能的入口文件。首次理解陌生大型项目时优先调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "扫描起始目录，默认'.'。",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "最大扫描深度，默认20。",
            },
            "max_files": {
                "type": "integer",
                "minimum": 100,
                "maximum": 100000,
                "description": "最多统计文件数，默认50000。",
            },
            "include_generated": {"type": "boolean"},
        },
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.scanner = RepositoryScanner(workspace)

    def execute(self, arguments: Dict[str, Any]) -> str:
        relative_path = arguments.get("path", ".")
        max_depth = arguments.get("max_depth", 20)
        max_files = arguments.get("max_files", 50_000)
        include_generated = arguments.get("include_generated", False)
        if not isinstance(relative_path, str):
            raise ValueError("path must be a string")
        if not isinstance(max_depth, int) or not 1 <= max_depth <= 50:
            raise ValueError("max_depth must be an integer from 1 to 50")
        if not isinstance(max_files, int) or not 100 <= max_files <= 100_000:
            raise ValueError("max_files must be an integer from 100 to 100000")
        if not isinstance(include_generated, bool):
            raise ValueError("include_generated must be a boolean")

        files = 0
        directories = 0
        truncated = False
        extensions: Counter = Counter()
        top_level: Counter = Counter()
        manifests: List[str] = []
        entrypoints: List[str] = []
        base = self.workspace.resolve(relative_path)
        for path in self.scanner.iter_paths(
            relative_path=relative_path,
            max_depth=max_depth,
            include_generated=include_generated,
            include_hidden=True,
        ):
            relative_to_base = path.relative_to(base)
            relative = path.relative_to(self.workspace.root).as_posix()
            if path.is_dir():
                directories += 1
                continue
            files += 1
            first_part = (
                relative_to_base.parts[0]
                if len(relative_to_base.parts) > 1
                else "[root]"
            )
            top_level[first_part] += 1
            extensions[path.suffix.lower() or "[no extension]"] += 1
            if path.name in MANIFEST_NAMES and len(manifests) < 100:
                manifests.append(relative)
            if path.name in ENTRYPOINT_NAMES and len(entrypoints) < 100:
                entrypoints.append(relative)
            if files >= max_files:
                truncated = True
                break

        data = {
            "path": relative_path,
            "files_scanned": files,
            "directories_scanned": directories,
            "truncated": truncated,
            "top_file_extensions": extensions.most_common(20),
            "top_level_file_counts": top_level.most_common(30),
            "manifests": manifests,
            "possible_entrypoints": entrypoints,
            "defaults": {
                "generated_dependencies_skipped": not include_generated,
                "max_depth": max_depth,
                "max_files": max_files,
            },
        }
        return json.dumps(data, ensure_ascii=False, indent=2)


def _scan_options(
    arguments: Dict[str, Any],
    default_max_depth: int = 6,
) -> Dict[str, Any]:
    relative_path = arguments.get("path", ".")
    max_depth = arguments.get("max_depth", default_max_depth)
    include_generated = arguments.get("include_generated", False)
    include_hidden = arguments.get("include_hidden", True)
    if not isinstance(relative_path, str):
        raise ValueError("path must be a string")
    if not isinstance(max_depth, int) or not 1 <= max_depth <= 50:
        raise ValueError("max_depth must be an integer from 1 to 50")
    if not isinstance(include_generated, bool):
        raise ValueError("include_generated must be a boolean")
    if not isinstance(include_hidden, bool):
        raise ValueError("include_hidden must be a boolean")
    return {
        "relative_path": relative_path,
        "max_depth": max_depth,
        "include_generated": include_generated,
        "include_hidden": include_hidden,
    }


def _pagination(
    arguments: Dict[str, Any],
    default_limit: int,
    max_limit: int = 1000,
) -> Tuple[int, int]:
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", default_limit)
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if not isinstance(limit, int) or not 1 <= limit <= max_limit:
        raise ValueError(f"limit must be an integer from 1 to {max_limit}")
    return offset, limit


def _paged_output(
    entries: Iterable[str],
    offset: int,
    has_more: bool,
    empty_text: str,
    max_chars: int = 30_000,
) -> str:
    values = list(entries)
    if not values:
        return empty_text
    visible: List[str] = []
    char_count = 0
    for value in values:
        added_chars = len(value) + 1
        if visible and char_count + added_chars > max_chars:
            break
        visible.append(value)
        char_count += added_chars
    has_more = has_more or len(visible) < len(values)
    end = offset + len(visible)
    header = f"结果 {offset + 1}-{end}"
    footer = ""
    if has_more:
        footer = f"\n\n[还有更多结果；下一次使用 offset={end}]"
    return f"{header}\n" + "\n".join(visible) + footer


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            sample = file.read(8192)
    except OSError:
        return True
    return b"\x00" in sample


def _truncate_line(line: str, max_chars: int = 500) -> str:
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + "...[line truncated]"
