"""Persistent incremental source index shared by all workspace sessions."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .workspace import Workspace
from .vector_store import (
    ChromaVectorStore,
    VectorRecord,
    content_fingerprint,
    reciprocal_rank_fusion,
)

INDEX_VERSION = 1
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_CHUNK_CHARS = 4_000
DEFAULT_CHUNK_OVERLAP_LINES = 8
MAX_INDEX_FILES = 200_000

DENIED_NAMES = {".git", ".simple-agent", ".venv", "__pycache__"}
GENERATED_DIRS = {
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
GENERATED_FILES = {".coverage"}
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
INDEXABLE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cfg",
    ".clj",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".dart",
    ".ex",
    ".exs",
    ".go",
    ".graphql",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".kts",
    ".lua",
    ".md",
    ".mdx",
    ".php",
    ".properties",
    ".proto",
    ".py",
    ".rb",
    ".rs",
    ".rst",
    ".scala",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
LANGUAGES = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".dart": "Dart",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scala": "Scala",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
}


@dataclass(frozen=True)
class IndexRefreshResult:
    """Statistics for one full or scoped incremental refresh."""

    scanned_files: int
    indexed_files: int
    unchanged_files: int
    deleted_files: int
    skipped_files: int
    duration_ms: int
    full_refresh: bool
    refreshed_at: str


@dataclass(frozen=True)
class ProjectCodeHit:
    """One source chunk retrieved from the persistent project index."""

    path: str
    chunk_index: int
    start_line: int
    end_line: int
    language: str
    content: str
    score: float

    @property
    def citation(self) -> str:
        return (
            f"project:{self.path}#L{self.start_line}-L{self.end_line}"
        )


@dataclass(frozen=True)
class ProjectSymbol:
    """One indexed code symbol."""

    path: str
    name: str
    kind: str
    line: int
    signature: str


class ProjectIndex:
    """SQLite FTS project map that only rereads changed source files."""

    def __init__(
        self,
        workspace: Workspace,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        vector_store: Optional[ChromaVectorStore] = None,
    ) -> None:
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if chunk_chars < 500:
            raise ValueError("chunk_chars must be at least 500")
        self.workspace = workspace
        self.root = workspace.root / ".simple-agent" / "index"
        self.database_path = self.root / "project-index.db"
        self.map_path = self.root / "repository-map.json"
        self.max_file_bytes = max_file_bytes
        self.chunk_chars = chunk_chars
        self.vector_store = vector_store or ChromaVectorStore.from_env(workspace)
        self.vector_error = ""

    def refresh(
        self,
        paths: Optional[Sequence[str]] = None,
        *,
        sync_vectors: bool = True,
    ) -> IndexRefreshResult:
        started = time.monotonic()
        full_refresh = paths is None
        candidates = list(self._candidate_files(paths))
        if len(candidates) > MAX_INDEX_FILES:
            raise ValueError(
                f"project contains more than {MAX_INDEX_FILES} indexable paths"
            )
        scanned = len(candidates)
        indexed = 0
        unchanged = 0
        skipped = 0
        deleted = 0
        seen = {
            path.relative_to(self.workspace.root).as_posix()
            for path in candidates
            if path.exists()
        }
        refreshed_at = _now()

        with self._connect() as connection:
            existing = {
                row["path"]: row
                for row in connection.execute(
                    """
                    SELECT path, size, mtime_ns, content_hash, indexed
                    FROM files
                    """
                ).fetchall()
            }
            if full_refresh:
                stale = sorted(set(existing) - seen)
            else:
                requested = {
                    self._relative_path(path)
                    for path in (paths or [])
                    if self._is_valid_relative(path)
                }
                stale = sorted(
                    path
                    for path in requested
                    if path in existing
                    and not (self.workspace.root / path).exists()
                )
            for relative in stale:
                self._delete_file(connection, relative)
                deleted += 1

            for path in candidates:
                if not path.exists() or not path.is_file():
                    continue
                relative = path.relative_to(self.workspace.root).as_posix()
                try:
                    stat = path.stat()
                except OSError:
                    skipped += 1
                    continue
                previous = existing.get(relative)
                if (
                    previous is not None
                    and previous["size"] == stat.st_size
                    and previous["mtime_ns"] == stat.st_mtime_ns
                ):
                    unchanged += 1
                    continue
                if not self._can_index(path, stat.st_size):
                    self._store_unindexed(
                        connection,
                        relative,
                        stat.st_size,
                        stat.st_mtime_ns,
                        _language(path),
                        refreshed_at,
                    )
                    skipped += 1
                    continue
                try:
                    raw = path.read_bytes()
                except OSError:
                    skipped += 1
                    continue
                if b"\x00" in raw[:8192]:
                    self._store_unindexed(
                        connection,
                        relative,
                        stat.st_size,
                        stat.st_mtime_ns,
                        _language(path),
                        refreshed_at,
                    )
                    skipped += 1
                    continue
                text = _decode_source(raw)
                content_hash = hashlib.sha256(raw).hexdigest()
                if (
                    previous is not None
                    and previous["indexed"]
                    and previous["content_hash"] == content_hash
                ):
                    connection.execute(
                        """
                        UPDATE files
                        SET size = ?, mtime_ns = ?, indexed_at = ?
                        WHERE path = ?
                        """,
                        (
                            stat.st_size,
                            stat.st_mtime_ns,
                            refreshed_at,
                            relative,
                        ),
                    )
                    unchanged += 1
                    continue
                self._index_file(
                    connection,
                    relative,
                    text,
                    stat.st_size,
                    stat.st_mtime_ns,
                    content_hash,
                    refreshed_at,
                )
                indexed += 1

            connection.execute(
                """
                INSERT INTO metadata (key, value) VALUES ('last_refresh', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (refreshed_at,),
            )
            connection.execute(
                """
                INSERT INTO metadata (key, value) VALUES ('index_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(INDEX_VERSION),),
            )

        result = IndexRefreshResult(
            scanned_files=scanned,
            indexed_files=indexed,
            unchanged_files=unchanged,
            deleted_files=deleted,
            skipped_files=skipped,
            duration_ms=round((time.monotonic() - started) * 1000),
            full_refresh=full_refresh,
            refreshed_at=refreshed_at,
        )
        if indexed or deleted or skipped or not self.map_path.exists():
            self._write_map(result)
        if sync_vectors and (indexed or deleted):
            self.sync_vector_index()
        return result

    def sync_vector_index(self) -> None:
        """Synchronize changed cached chunks with Chroma."""
        try:
            self._sync_vectors()
            self.vector_error = ""
        except Exception as exc:
            self.vector_error = f"{type(exc).__name__}: {exc}"[:2_000]

    def status(self) -> Dict[str, Any]:
        self._ensure_storage_path()
        if not self.database_path.exists():
            return {
                "ready": False,
                "files": 0,
                "indexed_files": 0,
                "chunks": 0,
                "symbols": 0,
                "imports": 0,
                "last_refresh": "",
                "index_version": INDEX_VERSION,
            }
        with self._connect() as connection:
            counts = {
                "files": connection.execute(
                    "SELECT COUNT(*) FROM files"
                ).fetchone()[0],
                "indexed_files": connection.execute(
                    "SELECT COUNT(*) FROM files WHERE indexed = 1"
                ).fetchone()[0],
                "chunks": connection.execute(
                    "SELECT COUNT(*) FROM chunks"
                ).fetchone()[0],
                "symbols": connection.execute(
                    "SELECT COUNT(*) FROM symbols"
                ).fetchone()[0],
                "imports": connection.execute(
                    "SELECT COUNT(*) FROM imports"
                ).fetchone()[0],
            }
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM metadata"
                ).fetchall()
            }
        return {
            "ready": True,
            **counts,
            "last_refresh": metadata.get("last_refresh", ""),
            "index_version": int(
                metadata.get("index_version", INDEX_VERSION)
            ),
            "vector": {
                **self.vector_store.status(),
                "last_error": self.vector_error,
            },
        }

    def overview(
        self,
        max_depth: int = 3,
        max_entries: int = 300,
    ) -> Dict[str, Any]:
        self._ensure_storage_path()
        if self.map_path.exists() and max_entries <= 300:
            try:
                cached = json.loads(
                    self.map_path.read_text(encoding="utf-8")
                ).get("overview")
            except (OSError, json.JSONDecodeError):
                cached = None
            if (
                isinstance(cached, dict)
                and cached.get("tree_depth", 0) >= max_depth
                and isinstance(cached.get("tree"), list)
            ):
                candidates = [
                    path
                    for path in cached["tree"]
                    if isinstance(path, str)
                    and len(Path(path).parts) <= max_depth
                ]
                return {
                    **cached,
                    **self.status(),
                    "tree_depth": max_depth,
                    "tree_truncated": (
                        bool(cached.get("tree_truncated"))
                        or len(candidates) > max_entries
                    ),
                    "tree": candidates[:max_entries],
                }
        return self._build_overview(max_depth, max_entries)

    def _build_overview(
        self,
        max_depth: int = 3,
        max_entries: int = 300,
    ) -> Dict[str, Any]:
        if not self.database_path.exists():
            return {**self.status(), "tree": []}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT path, language, indexed
                FROM files ORDER BY path
                """
            ).fetchall()
            symbol_counts = {
                row["path"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT path, COUNT(*) AS count
                    FROM symbols GROUP BY path
                    """
                ).fetchall()
            }
            import_counts = {
                row["path"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT path, COUNT(*) AS count
                    FROM imports GROUP BY path
                    """
                ).fetchall()
            }
        paths = [row["path"] for row in rows]
        extensions = Counter(
            Path(path).suffix.lower() or "[no extension]" for path in paths
        )
        languages = Counter(
            row["language"] for row in rows if row["language"]
        )
        top_level = Counter(
            Path(path).parts[0] if len(Path(path).parts) > 1 else "[root]"
            for path in paths
        )
        tree_candidates = [
            path
            for path in paths
            if len(Path(path).parts) <= max_depth
        ]
        tree = tree_candidates[:max_entries]
        module_data: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            module = (
                Path(row["path"]).parts[0]
                if len(Path(row["path"]).parts) > 1
                else "[root]"
            )
            record = module_data.setdefault(
                module,
                {
                    "path": module,
                    "files": 0,
                    "languages": Counter(),
                    "symbols": 0,
                    "imports": 0,
                },
            )
            record["files"] += 1
            if row["language"]:
                record["languages"][row["language"]] += 1
            record["symbols"] += symbol_counts.get(row["path"], 0)
            record["imports"] += import_counts.get(row["path"], 0)
        modules = []
        for record in sorted(
            module_data.values(),
            key=lambda item: (-item["files"], item["path"]),
        )[:30]:
            modules.append(
                {
                    **record,
                    "languages": record["languages"].most_common(5),
                }
            )
        return {
            **self.status(),
            "top_file_extensions": extensions.most_common(20),
            "languages": languages.most_common(20),
            "top_level_file_counts": top_level.most_common(30),
            "modules": modules,
            "manifests": [
                path for path in paths if Path(path).name in MANIFEST_NAMES
            ][:100],
            "possible_entrypoints": [
                path for path in paths if Path(path).name in ENTRYPOINT_NAMES
            ][:100],
            "tree_depth": max_depth,
            "tree_truncated": len(tree_candidates) > max_entries,
            "tree": tree,
        }

    def search(
        self,
        query: str,
        limit: int = 8,
    ) -> List[ProjectCodeHit]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("project index query must be non-empty")
        if not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("project index search limit must be from 1 to 30")
        if not self.database_path.exists():
            return []
        terms = _query_terms(query)[:64]
        if not terms:
            return []
        expression = " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.path, c.chunk_index, c.start_line, c.end_line,
                       f.language, c.content, bm25(code_fts) AS rank
                FROM code_fts
                JOIN chunks c ON c.id = code_fts.chunk_id
                JOIN files f ON f.path = c.path
                WHERE code_fts MATCH ?
                ORDER BY rank, c.path, c.chunk_index
                LIMIT ?
                """,
                (expression, limit),
            ).fetchall()
        return [
            ProjectCodeHit(
                path=row["path"],
                chunk_index=row["chunk_index"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                language=row["language"],
                content=row["content"],
                score=round(-float(row["rank"]), 6),
            )
            for row in rows
        ]

    def search_hybrid(
        self,
        query: str,
        limit: int = 8,
    ) -> List[ProjectCodeHit]:
        """Fuse FTS5/BM25 and Chroma dense-vector rankings."""
        keyword_hits = self.search(query, min(30, max(limit * 3, limit)))
        try:
            vector_hits = self.vector_store.query(
                "code_chunks",
                query,
                min(30, max(limit * 3, limit)),
            )
            self.vector_error = ""
        except Exception as exc:
            self.vector_error = f"{type(exc).__name__}: {exc}"[:2_000]
            vector_hits = []
        keyword_ids = [
            f"{hit.path}:{hit.chunk_index}" for hit in keyword_hits
        ]
        vector_ids = [
            f"{hit.metadata.get('path', '')}:"
            f"{int(hit.metadata.get('chunk_index', 0))}"
            for hit in vector_hits
        ]
        fused = reciprocal_rank_fusion([keyword_ids, vector_ids])
        candidates = {
            f"{hit.path}:{hit.chunk_index}": hit for hit in keyword_hits
        }
        for hit in vector_hits:
            path = str(hit.metadata.get("path", ""))
            chunk_index = int(hit.metadata.get("chunk_index", 0))
            key = f"{path}:{chunk_index}"
            if key in candidates or not path or chunk_index < 0:
                continue
            candidates[key] = ProjectCodeHit(
                path=path,
                chunk_index=chunk_index,
                start_line=int(hit.metadata.get("start_line", 1)),
                end_line=int(hit.metadata.get("end_line", 1)),
                language=str(hit.metadata.get("language", "")),
                content=hit.text,
                score=0.0,
            )
        ordered = sorted(
            candidates.items(),
            key=lambda item: (-fused.get(item[0], 0.0), item[0]),
        )
        return [
            ProjectCodeHit(
                path=hit.path,
                chunk_index=hit.chunk_index,
                start_line=hit.start_line,
                end_line=hit.end_line,
                language=hit.language,
                content=hit.content,
                score=round(fused.get(key, 0.0), 8),
            )
            for key, hit in ordered[:limit]
        ]

    def search_symbols(
        self,
        query: str,
        limit: int = 30,
    ) -> List[ProjectSymbol]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("symbol query must be non-empty")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("symbol search limit must be from 1 to 100")
        if not self.database_path.exists():
            return []
        escaped = (
            query.strip().replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT path, name, kind, line, signature
                FROM symbols
                WHERE name LIKE ? ESCAPE '\\'
                ORDER BY
                    CASE WHEN lower(name) = lower(?) THEN 0 ELSE 1 END,
                    length(name), path, line
                LIMIT ?
                """,
                (f"%{escaped}%", query.strip(), limit),
            ).fetchall()
        return [ProjectSymbol(**dict(row)) for row in rows]

    def file_evidence_records(self) -> List[Dict[str, Any]]:
        """Return cached source evidence used by higher-level analyzers."""
        if not self.database_path.exists():
            return []
        with self._connect() as connection:
            files = connection.execute(
                """
                SELECT path, content_hash, language, line_count
                FROM files WHERE indexed = 1 ORDER BY path
                """
            ).fetchall()
            records = []
            for file_row in files:
                path = file_row["path"]
                symbols = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT name, kind, line, signature FROM symbols
                        WHERE path = ? ORDER BY line, name
                        """,
                        (path,),
                    )
                ]
                imports = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT target, line, kind FROM imports
                        WHERE path = ? ORDER BY line, target
                        """,
                        (path,),
                    )
                ]
                chunks = connection.execute(
                    """
                    SELECT content FROM chunks WHERE path = ?
                    ORDER BY chunk_index LIMIT 2
                    """,
                    (path,),
                ).fetchall()
                records.append(
                    {
                        **dict(file_row),
                        "symbols": symbols,
                        "imports": imports,
                        "leading_content": "\n".join(
                            row["content"] for row in chunks
                        )[:12_000],
                    }
                )
            return records

    def find_references(
        self,
        symbol: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("reference symbol must be non-empty")
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("reference limit must be from 1 to 200")
        if not self.database_path.exists():
            return []
        name = symbol.strip()
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        results: List[Dict[str, Any]] = []
        seen = set()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT path, start_line, content
                FROM chunks
                WHERE instr(content, ?) > 0
                ORDER BY path, start_line
                LIMIT ?
                """,
                (name, min(limit * 10, 2_000)),
            ).fetchall()
        for row in rows:
            for offset, line in enumerate(row["content"].splitlines()):
                if not pattern.search(line):
                    continue
                key = (row["path"], row["start_line"] + offset)
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    {
                        "path": row["path"],
                        "line": key[1],
                        "text": line.strip()[:500],
                    }
                )
                if len(results) >= limit:
                    return results
        return results

    def list_imports(
        self,
        path: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("import limit must be from 1 to 500")
        if not self.database_path.exists():
            return []
        query = "SELECT path, target, line, kind FROM imports"
        parameters: Tuple[Any, ...] = ()
        if path:
            query += " WHERE path = ?"
            parameters = (self._relative_path(path),)
        query += " ORDER BY path, line LIMIT ?"
        parameters += (limit,)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def _candidate_files(
        self,
        paths: Optional[Sequence[str]],
    ) -> Iterator[Path]:
        if paths is None:
            yield from self._walk(self.workspace.root)
            return
        seen = set()
        for relative in paths:
            if not isinstance(relative, str) or not relative.strip():
                raise ValueError("refresh paths must be non-empty strings")
            path = self.workspace.resolve(relative)
            if self._is_denied(path):
                continue
            if path.is_dir():
                candidates = self._walk(path)
            else:
                candidates = [path]
            for candidate in candidates:
                key = str(candidate)
                if key not in seen:
                    seen.add(key)
                    yield candidate

    def _walk(self, root: Path) -> Iterator[Path]:
        for current_text, dirnames, filenames in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_text)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if not self._skip_name(name, is_directory=True)
                and not (current / name).is_symlink()
            ]
            for name in sorted(filenames):
                path = current / name
                if self._skip_name(name, is_directory=False):
                    continue
                if path.is_symlink() or self._is_denied(path):
                    continue
                yield path

    def _can_index(self, path: Path, size: int) -> bool:
        if size > self.max_file_bytes:
            return False
        return (
            path.suffix.lower() in INDEXABLE_EXTENSIONS
            or path.name in MANIFEST_NAMES
            or path.name.lower() in {"license", "readme"}
        )

    def _index_file(
        self,
        connection: sqlite3.Connection,
        relative: str,
        text: str,
        size: int,
        mtime_ns: int,
        content_hash: str,
        indexed_at: str,
    ) -> None:
        language = _language(Path(relative))
        self._delete_file(connection, relative, keep_file=True)
        connection.execute(
            """
            INSERT INTO files (
                path, size, mtime_ns, content_hash, language,
                line_count, indexed, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                content_hash = excluded.content_hash,
                language = excluded.language,
                line_count = excluded.line_count,
                indexed = 1,
                indexed_at = excluded.indexed_at
            """,
            (
                relative,
                size,
                mtime_ns,
                content_hash,
                language,
                text.count("\n") + (1 if text else 0),
                indexed_at,
            ),
        )
        symbols = _extract_symbols(relative, text)
        for symbol in symbols:
            connection.execute(
                """
                INSERT INTO symbols (
                    path, name, kind, line, signature
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    relative,
                    symbol.name,
                    symbol.kind,
                    symbol.line,
                    symbol.signature,
                ),
            )
        for target, line, kind in _extract_imports(relative, text):
            connection.execute(
                """
                INSERT INTO imports (path, target, line, kind)
                VALUES (?, ?, ?, ?)
                """,
                (relative, target, line, kind),
            )
        symbol_names = " ".join(symbol.name for symbol in symbols)
        for chunk_index, start, end, content in _chunks(
            text,
            self.chunk_chars,
        ):
            cursor = connection.execute(
                """
                INSERT INTO chunks (
                    path, chunk_index, start_line, end_line, content
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (relative, chunk_index, start, end, content),
            )
            chunk_id = cursor.lastrowid
            terms = " ".join(
                sorted(_index_terms(f"{relative} {symbol_names} {content}"))
            )
            connection.execute(
                """
                INSERT INTO code_fts (path, chunk_id, terms, content)
                VALUES (?, ?, ?, ?)
                """,
                (relative, chunk_id, terms, content),
            )

    def _store_unindexed(
        self,
        connection: sqlite3.Connection,
        relative: str,
        size: int,
        mtime_ns: int,
        language: str,
        indexed_at: str,
    ) -> None:
        self._delete_file(connection, relative, keep_file=True)
        connection.execute(
            """
            INSERT INTO files (
                path, size, mtime_ns, content_hash, language,
                line_count, indexed, indexed_at
            ) VALUES (?, ?, ?, '', ?, 0, 0, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                content_hash = '',
                language = excluded.language,
                line_count = 0,
                indexed = 0,
                indexed_at = excluded.indexed_at
            """,
            (relative, size, mtime_ns, language, indexed_at),
        )

    def _sync_vectors(self) -> None:
        if not self.vector_store.available or not self.database_path.exists():
            return
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.path, c.chunk_index, c.start_line, c.end_line,
                       c.content, f.content_hash, f.language
                FROM chunks c
                JOIN files f ON f.path = c.path
                ORDER BY c.path, c.chunk_index
                """
            ).fetchall()
        records = [
            VectorRecord(
                id=content_fingerprint(
                    "code",
                    row["path"],
                    str(row["chunk_index"]),
                ),
                text=row["content"],
                content_hash=content_fingerprint(
                    row["content_hash"],
                    str(row["chunk_index"]),
                    row["content"],
                ),
                metadata={
                    "path": row["path"],
                    "chunk_index": row["chunk_index"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                    "language": row["language"],
                },
            )
            for row in rows
        ]
        self.vector_store.sync_namespace("code_chunks", records)

    @staticmethod
    def _delete_file(
        connection: sqlite3.Connection,
        relative: str,
        keep_file: bool = False,
    ) -> None:
        connection.execute("DELETE FROM code_fts WHERE path = ?", (relative,))
        connection.execute("DELETE FROM chunks WHERE path = ?", (relative,))
        connection.execute("DELETE FROM symbols WHERE path = ?", (relative,))
        connection.execute("DELETE FROM imports WHERE path = ?", (relative,))
        if not keep_file:
            connection.execute("DELETE FROM files WHERE path = ?", (relative,))

    def _write_map(self, refresh: IndexRefreshResult) -> None:
        data = {
            "version": INDEX_VERSION,
            "refresh": asdict(refresh),
            "overview": self._build_overview(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=".repository-map-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(data, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        temporary_path.replace(self.map_path)

    def _connect(self) -> sqlite3.Connection:
        self._ensure_storage_path()
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                language TEXT NOT NULL,
                line_count INTEGER NOT NULL,
                indexed INTEGER NOT NULL,
                indexed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                content TEXT NOT NULL,
                UNIQUE(path, chunk_index)
            );
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                line INTEGER NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS symbols_name_idx
            ON symbols(name);
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                target TEXT NOT NULL,
                line INTEGER NOT NULL,
                kind TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS imports_target_idx
            ON imports(target);
            CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
                path UNINDEXED,
                chunk_id UNINDEXED,
                terms,
                content,
                tokenize = 'unicode61'
            );
            """
        )
        return connection

    def _ensure_storage_path(self) -> None:
        paths = [
            self.workspace.root / ".simple-agent",
            self.root,
            self.database_path,
            self.map_path,
        ]
        if any(path.is_symlink() for path in paths if path.exists()):
            raise ValueError("project index paths cannot contain symbolic links")
        try:
            self.database_path.resolve(strict=False).relative_to(
                self.workspace.root
            )
        except ValueError as exc:
            raise ValueError("project index path is outside the workspace") from exc

    def _is_denied(self, path: Path) -> bool:
        try:
            parts = path.relative_to(self.workspace.root).parts
        except ValueError:
            return True
        return any(
            part in DENIED_NAMES
            or part in GENERATED_DIRS
            or part.endswith(".egg-info")
            or _is_sensitive_name(part)
            for part in parts
        )

    @staticmethod
    def _skip_name(name: str, is_directory: bool) -> bool:
        if name in DENIED_NAMES or _is_sensitive_name(name):
            return True
        if is_directory and (
            name in GENERATED_DIRS or name.endswith(".egg-info")
        ):
            return True
        return (
            not is_directory
            and (
                name in GENERATED_FILES
                or name.endswith((".min.js", ".min.css", ".pyc"))
            )
        )

    def _relative_path(self, path: str) -> str:
        resolved = self.workspace.resolve(path)
        if self._is_denied(resolved):
            raise ValueError(f"cannot index sensitive path: {path}")
        return resolved.relative_to(self.workspace.root).as_posix()

    def _is_valid_relative(self, path: str) -> bool:
        try:
            self._relative_path(path)
            return True
        except (TypeError, ValueError):
            return False


def project_hit_to_dict(
    hit: ProjectCodeHit,
    include_content: bool = True,
) -> Dict[str, Any]:
    data = {
        "citation": hit.citation,
        "path": hit.path,
        "chunk_index": hit.chunk_index,
        "start_line": hit.start_line,
        "end_line": hit.end_line,
        "language": hit.language,
        "score": hit.score,
    }
    if include_content:
        data["content"] = hit.content
    return data


def _chunks(
    text: str,
    max_chars: int,
) -> Iterator[Tuple[int, int, int, str]]:
    lines = text.splitlines()
    if not lines and text:
        lines = [text]
    start = 0
    chunk_index = 1
    while start < len(lines):
        end = start
        chars = 0
        while end < len(lines):
            addition = len(lines[end]) + 1
            if end > start and chars + addition > max_chars:
                break
            chars += addition
            end += 1
        if end == start:
            end += 1
        content = "\n".join(lines[start:end])
        yield chunk_index, start + 1, end, content
        chunk_index += 1
        if end >= len(lines):
            break
        start = max(start + 1, end - DEFAULT_CHUNK_OVERLAP_LINES)


def _extract_symbols(path: str, text: str) -> List[ProjectSymbol]:
    suffix = Path(path).suffix.lower()
    patterns: List[Tuple[str, re.Pattern[str]]] = []
    if suffix == ".py":
        patterns = [
            (
                "class",
                re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:"),
            ),
            (
                "function",
                re.compile(
                    r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\([^)]*\)"
                ),
            ),
        ]
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        patterns = [
            (
                "class",
                re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)"),
            ),
            (
                "interface",
                re.compile(
                    r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)"
                ),
            ),
            (
                "function",
                re.compile(
                    r"^\s*(?:export\s+)?(?:async\s+)?function\s+"
                    r"([A-Za-z_$][\w$]*)"
                ),
            ),
            (
                "function",
                re.compile(
                    r"^\s*(?:export\s+)?(?:const|let)\s+"
                    r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("
                ),
            ),
        ]
    else:
        patterns = [
            (
                "class",
                re.compile(
                    r"^\s*(?:public\s+|private\s+|export\s+)?"
                    r"(?:class|struct|interface|trait|enum)\s+"
                    r"([A-Za-z_]\w*)"
                ),
            ),
            (
                "function",
                re.compile(
                    r"^\s*(?:pub\s+|public\s+|private\s+|static\s+)*"
                    r"(?:fn|func|function)\s+([A-Za-z_]\w*)"
                ),
            ),
        ]
    symbols = []
    seen = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            key = (match.group(1), kind, line_number)
            if key in seen:
                continue
            seen.add(key)
            symbols.append(
                ProjectSymbol(
                    path=path,
                    name=match.group(1),
                    kind=kind,
                    line=line_number,
                    signature=line.strip()[:500],
                )
            )
    return symbols


def _extract_imports(
    path: str,
    text: str,
) -> List[Tuple[str, int, str]]:
    suffix = Path(path).suffix.lower()
    patterns: List[Tuple[str, re.Pattern[str]]] = []
    if suffix == ".py":
        patterns = [
            (
                "from",
                re.compile(
                    r"^\s*from\s+(\.*[A-Za-z_][\w.]*)\s+import\s+"
                ),
            ),
            (
                "import",
                re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)"),
            ),
        ]
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        patterns = [
            (
                "import",
                re.compile(r"""(?:from\s+|import\s*)['"]([^'"]+)['"]"""),
            ),
            (
                "require",
                re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
            ),
        ]
    elif suffix == ".go":
        patterns = [
            ("import", re.compile(r"""^\s*import\s+["']([^"']+)["']""")),
        ]
    elif suffix == ".rs":
        patterns = [
            ("use", re.compile(r"^\s*use\s+([A-Za-z_][\w:]*)")),
        ]
    imports = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in patterns:
            match = pattern.search(line)
            if match:
                imports.append((match.group(1), line_number, kind))
    return imports


def _index_terms(text: str) -> set:
    lowered = text.lower()
    terms = set(re.findall(r"[a-z_][a-z0-9_.$:/-]{1,127}", lowered))
    for sequence in re.findall(r"[\u3400-\u9fff]+", lowered):
        if len(sequence) == 1:
            terms.add(sequence)
        else:
            terms.update(
                sequence[index : index + 2]
                for index in range(len(sequence) - 1)
            )
    return terms


def _query_terms(query: str) -> List[str]:
    terms = _index_terms(query)
    return sorted(terms, key=lambda item: (-len(item), item))


def _language(path: Path) -> str:
    return LANGUAGES.get(path.suffix.lower(), path.suffix.lower().lstrip("."))


def _decode_source(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _is_sensitive_name(name: str) -> bool:
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return Path(name).suffix.lower() in {".key", ".pem", ".p12", ".pfx"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
