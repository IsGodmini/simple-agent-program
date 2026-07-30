"""Persistent project knowledge graph and versioned file-function profiles."""

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .project_index import ProjectIndex
from .workspace import Workspace

GRAPH_VERSION = 1
PROFILE_VERSION = 1
DEFAULT_PROFILE_LIMIT = 6
MAX_PROFILE_RESULTS = 30
MAX_GRAPH_NODES = 500_000
MAX_GRAPH_EDGES = 1_000_000


@dataclass(frozen=True)
class ProjectGraphConfig:
    """Neo4j-first graph configuration with a local SQLite fallback."""

    backend: str = "neo4j"
    neo4j_uri: str = ""
    neo4j_username: str = ""
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"

    def __post_init__(self) -> None:
        if self.backend not in {"sqlite", "neo4j"}:
            raise ValueError(
                "PROJECT_GRAPH_BACKEND must be sqlite or neo4j"
            )
    @property
    def missing_neo4j_settings(self) -> List[str]:
        return [
            name
            for name, value in (
                ("NEO4J_URI", self.neo4j_uri),
                ("NEO4J_USERNAME", self.neo4j_username),
                ("NEO4J_PASSWORD", self.neo4j_password),
            )
            if not value
        ]

    @property
    def neo4j_configured(self) -> bool:
        return (
            self.backend == "neo4j"
            and not self.missing_neo4j_settings
        )

    @classmethod
    def from_env(cls) -> "ProjectGraphConfig":
        return cls(
            backend=os.getenv("PROJECT_GRAPH_BACKEND", "neo4j").lower(),
            neo4j_uri=os.getenv("NEO4J_URI", ""),
            neo4j_username=os.getenv("NEO4J_USERNAME", ""),
            neo4j_password=os.getenv("NEO4J_PASSWORD", ""),
            neo4j_database=os.getenv("NEO4J_DATABASE", "neo4j"),
        )


@dataclass(frozen=True)
class FileProfile:
    """One content-hash-bound explanation of a project file."""

    path: str
    content_hash: str
    language: str
    line_count: int
    purpose: str
    responsibilities: List[str]
    public_symbols: List[Dict[str, Any]]
    imports: List[str]
    related_tests: List[str]
    confidence: float
    evidence: List[str]
    stale: bool
    profile_version: int
    updated_at: str

    @property
    def citation(self) -> str:
        return f"graph:{self.path}"


@dataclass(frozen=True)
class GraphRefreshResult:
    """Statistics for one graph synchronization."""

    scanned_files: int
    updated_profiles: int
    unchanged_profiles: int
    deleted_profiles: int
    nodes: int
    edges: int
    duration_ms: int
    backend: str
    requested_backend: str
    fallback_reason: str
    neo4j_synced: bool
    refreshed_at: str


class Neo4jGraphMirror:
    """Mirror one workspace graph into Neo4j using the official driver."""

    def __init__(
        self,
        config: ProjectGraphConfig,
        driver_factory: Optional[Any] = None,
    ) -> None:
        self.config = config
        if driver_factory is None:
            try:
                from neo4j import GraphDatabase
            except ImportError as exc:
                raise RuntimeError(
                    "Neo4j backend requires the 'neo4j' package; "
                    "install project dependencies with 'pip install -e .'"
                ) from exc
            driver_factory = GraphDatabase.driver
        self.driver = driver_factory(
            config.neo4j_uri,
            auth=(config.neo4j_username, config.neo4j_password),
        )

    def close(self) -> None:
        self.driver.close()

    def sync_snapshot(
        self,
        workspace_id: str,
        workspace_path: str,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> None:
        """Atomically replace this workspace snapshot in Neo4j."""

        self.driver.verify_connectivity()
        self.driver.execute_query(
            """
            CREATE CONSTRAINT simple_agent_project_node_key IF NOT EXISTS
            FOR (n:SimpleAgentNode)
            REQUIRE (n.workspace_id, n.node_key) IS UNIQUE
            """,
            database_=self.config.neo4j_database,
        )
        self.driver.execute_query(
            """
            OPTIONAL MATCH (
                old:SimpleAgentNode {workspace_id: $workspace_id}
            )
            WITH
                [node IN collect(old) WHERE node IS NOT NULL] AS old_nodes,
                $nodes AS nodes,
                $edges AS edges
            FOREACH (old IN old_nodes | DETACH DELETE old)
            WITH nodes, edges
            UNWIND nodes AS item
            CREATE (n:SimpleAgentNode {
                workspace_id: $workspace_id,
                workspace_path: $workspace_path,
                node_key: item.node_key,
                node_type: item.node_type,
                name: item.name,
                path: item.path,
                purpose: item.purpose,
                content_hash: item.content_hash,
                properties_json: item.properties_json
            })
            WITH edges
            UNWIND edges AS edge
            MATCH (source:SimpleAgentNode {
                workspace_id: $workspace_id,
                node_key: edge.source_key
            })
            MATCH (target:SimpleAgentNode {
                workspace_id: $workspace_id,
                node_key: edge.target_key
            })
            CREATE (source)-[:SIMPLE_AGENT_RELATION {
                kind: edge.edge_type,
                evidence_json: edge.evidence_json
            }]->(target)
            """,
            parameters_={
                "workspace_id": workspace_id,
                "workspace_path": workspace_path,
                "nodes": nodes,
                "edges": edges,
            },
            database_=self.config.neo4j_database,
        )


class ProjectGraph:
    """Incremental graph derived from the persistent project source index."""

    def __init__(
        self,
        workspace: Workspace,
        project_index: Optional[ProjectIndex] = None,
        config: Optional[ProjectGraphConfig] = None,
    ) -> None:
        self.workspace = workspace
        self.project_index = project_index or ProjectIndex(workspace)
        self.config = config or ProjectGraphConfig.from_env()
        self.root = workspace.root / ".simple-agent" / "graph"
        self.database_path = self.root / "project-graph.db"
        self.workspace_id = hashlib.sha256(
            str(workspace.root).encode("utf-8")
        ).hexdigest()[:24]

    def refresh(
        self,
        paths: Optional[Sequence[str]] = None,
    ) -> GraphRefreshResult:
        """Refresh source index, changed profiles, graph nodes, and relations."""

        started = time.monotonic()
        index_result = self.project_index.refresh(paths)
        refreshed_at = _now()
        records = self._index_records()
        current_paths = {record["path"] for record in records}
        updated = 0
        unchanged = 0
        deleted = 0

        with self._connect() as connection:
            existing = {
                row["path"]: row
                for row in connection.execute(
                    """
                    SELECT path, content_hash, profile_version
                    FROM file_profiles
                    """
                ).fetchall()
            }
            stale_paths = sorted(set(existing) - current_paths)
            for path in stale_paths:
                connection.execute(
                    "DELETE FROM file_profiles WHERE path = ?",
                    (path,),
                )
                connection.execute(
                    "DELETE FROM profile_fts WHERE path = ?",
                    (path,),
                )
                deleted += 1

            related_tests = _related_test_map(records)
            for record in records:
                previous = existing.get(record["path"])
                if (
                    previous is not None
                    and previous["content_hash"] == record["content_hash"]
                    and previous["profile_version"] == PROFILE_VERSION
                ):
                    unchanged += 1
                    continue
                profile = _build_profile(
                    record,
                    related_tests.get(record["path"], []),
                    refreshed_at,
                )
                self._store_profile(connection, profile)
                updated += 1

            graph_is_empty = (
                connection.execute(
                    "SELECT COUNT(*) FROM graph_nodes"
                ).fetchone()[0]
                == 0
            )
            if updated or deleted or graph_is_empty:
                self._rebuild_graph(
                    connection,
                    records,
                    related_tests,
                    refreshed_at,
                )
            self._set_metadata(connection, "graph_version", str(GRAPH_VERSION))
            self._set_metadata(connection, "last_refresh", refreshed_at)
            self._set_metadata(
                connection,
                "last_index_refresh",
                json.dumps(asdict(index_result), ensure_ascii=False),
            )
            counts = self._counts(connection)

        neo4j_synced = False
        graph_status = self.status()
        needs_neo4j_sync = (
            self.config.neo4j_configured
            and (
                updated
                or deleted
                or not graph_status.get("neo4j_last_sync")
                or bool(graph_status.get("neo4j_last_error"))
            )
        )
        if needs_neo4j_sync:
            try:
                self._sync_neo4j()
                neo4j_synced = True
                self._record_mirror_status("", refreshed_at)
            except Exception as exc:
                self._record_mirror_status(
                    self._safe_mirror_error(exc),
                    refreshed_at,
                )
        elif self.config.neo4j_configured:
            neo4j_synced = bool(graph_status.get("neo4j_last_sync"))

        final_status = self.status()
        return GraphRefreshResult(
            scanned_files=len(records),
            updated_profiles=updated,
            unchanged_profiles=unchanged,
            deleted_profiles=deleted,
            nodes=counts["nodes"],
            edges=counts["edges"],
            duration_ms=round((time.monotonic() - started) * 1000),
            backend=final_status["backend"],
            requested_backend=self.config.backend,
            fallback_reason=final_status["fallback_reason"],
            neo4j_synced=neo4j_synced,
            refreshed_at=refreshed_at,
        )

    def status(self) -> Dict[str, Any]:
        self._ensure_storage_path()
        if not self.database_path.exists():
            backend = self._backend_state({})
            return {
                "ready": False,
                **backend,
                "workspace_id": self.workspace_id,
                "profiles": 0,
                "nodes": 0,
                "edges": 0,
            }
        with self._connect() as connection:
            counts = self._counts(connection)
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM metadata"
                ).fetchall()
            }
        backend = self._backend_state(metadata)
        return {
            "ready": counts["profiles"] > 0,
            **backend,
            "workspace_id": self.workspace_id,
            **counts,
            "last_refresh": metadata.get("last_refresh", ""),
            "graph_version": int(metadata.get("graph_version", "0")),
            "neo4j_last_sync": metadata.get("neo4j_last_sync", ""),
            "neo4j_last_error": metadata.get("neo4j_last_error", ""),
        }

    def _backend_state(self, metadata: Dict[str, str]) -> Dict[str, Any]:
        requested = self.config.backend
        if requested == "sqlite":
            return {
                "backend": "sqlite",
                "requested_backend": "sqlite",
                "fallback_active": False,
                "fallback_reason": "",
                "neo4j_configured": False,
            }
        missing = self.config.missing_neo4j_settings
        if missing:
            return {
                "backend": "sqlite",
                "requested_backend": "neo4j",
                "fallback_active": True,
                "fallback_reason": (
                    "Neo4j configuration incomplete: " + ", ".join(missing)
                ),
                "neo4j_configured": False,
            }
        error = metadata.get("neo4j_last_error", "")
        if error:
            return {
                "backend": "sqlite",
                "requested_backend": "neo4j",
                "fallback_active": True,
                "fallback_reason": error,
                "neo4j_configured": True,
            }
        if metadata.get("neo4j_last_sync"):
            return {
                "backend": "neo4j",
                "requested_backend": "neo4j",
                "fallback_active": False,
                "fallback_reason": "",
                "neo4j_configured": True,
            }
        return {
            "backend": "sqlite",
            "requested_backend": "neo4j",
            "fallback_active": True,
            "fallback_reason": "Neo4j has not completed its first sync",
            "neo4j_configured": True,
        }

    def overview(
        self,
        max_profiles: int = 30,
    ) -> Dict[str, Any]:
        if not 1 <= max_profiles <= 200:
            raise ValueError("max_profiles must be from 1 to 200")
        status = self.status()
        if not status["ready"]:
            return status
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT path, language, purpose, confidence, stale
                FROM file_profiles
                ORDER BY
                    CASE
                        WHEN path LIKE 'src/%' THEN 0
                        WHEN path LIKE 'tests/%' THEN 2
                        ELSE 1
                    END,
                    path
                LIMIT ?
                """,
                (max_profiles,),
            ).fetchall()
            edge_types = connection.execute(
                """
                SELECT edge_type, COUNT(*) AS count
                FROM graph_edges
                GROUP BY edge_type
                ORDER BY count DESC, edge_type
                """
            ).fetchall()
        return {
            **status,
            "edge_types": [dict(row) for row in edge_types],
            "representative_files": [dict(row) for row in rows],
        }

    def get_profile(self, path: str) -> Optional[FileProfile]:
        relative = self._relative_path(path)
        self._ensure_storage_path()
        if not self.database_path.exists():
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM file_profiles WHERE path = ?",
                (relative,),
            ).fetchone()
        return _profile_from_row(row) if row is not None else None

    def search_profiles(
        self,
        query: str,
        limit: int = DEFAULT_PROFILE_LIMIT,
    ) -> List[FileProfile]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("file profile query must be non-empty")
        if not 1 <= limit <= MAX_PROFILE_RESULTS:
            raise ValueError(
                f"profile search limit must be from 1 to {MAX_PROFILE_RESULTS}"
            )
        self._ensure_storage_path()
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
                SELECT p.*
                FROM profile_fts
                JOIN file_profiles p ON p.path = profile_fts.path
                WHERE profile_fts MATCH ?
                ORDER BY bm25(profile_fts), p.path
                LIMIT ?
                """,
                (expression, limit),
            ).fetchall()
        return [_profile_from_row(row) for row in rows]

    def neighbors(
        self,
        path: str,
        depth: int = 1,
        limit: int = 100,
    ) -> Dict[str, Any]:
        relative = self._relative_path(path)
        if not 1 <= depth <= 4:
            raise ValueError("graph depth must be from 1 to 4")
        if not 1 <= limit <= 500:
            raise ValueError("graph result limit must be from 1 to 500")
        start = f"file:{relative}"
        self._ensure_storage_path()
        if not self.database_path.exists():
            return {"path": relative, "nodes": [], "edges": []}
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH RECURSIVE reachable(node_key, depth) AS (
                    SELECT ?, 0
                    UNION
                    SELECT
                        CASE
                            WHEN e.source_key = reachable.node_key
                            THEN e.target_key
                            ELSE e.source_key
                        END,
                        reachable.depth + 1
                    FROM reachable
                    JOIN graph_edges e
                      ON e.source_key = reachable.node_key
                      OR e.target_key = reachable.node_key
                    WHERE reachable.depth < ?
                )
                SELECT DISTINCT n.*
                FROM reachable
                JOIN graph_nodes n ON n.node_key = reachable.node_key
                ORDER BY reachable.depth, n.node_type, n.node_key
                LIMIT ?
                """,
                (start, depth, limit),
            ).fetchall()
            node_keys = [row["node_key"] for row in rows]
            edges = self._edges_for_keys(connection, node_keys, limit * 3)
        return {
            "path": relative,
            "depth": depth,
            "nodes": [_node_to_dict(row) for row in rows],
            "edges": edges,
        }

    def impact_analysis(
        self,
        path: str,
        depth: int = 2,
        limit: int = 100,
    ) -> Dict[str, Any]:
        graph = self.neighbors(path, depth, limit)
        relative = graph["path"]
        affected_files = []
        related_tests = []
        for node in graph["nodes"]:
            node_path = node.get("path")
            if (
                node["node_type"] == "File"
                and node_path
                and node_path != relative
            ):
                if _is_test_path(node_path):
                    related_tests.append(node_path)
                else:
                    affected_files.append(node_path)
        incoming = [
            edge
            for edge in graph["edges"]
            if edge["target_key"] == f"file:{relative}"
        ]
        return {
            "target": relative,
            "profile": (
                profile_to_dict(self.get_profile(relative))
                if self.get_profile(relative)
                else None
            ),
            "direct_incoming_relations": incoming,
            "affected_files": sorted(set(affected_files)),
            "related_tests": sorted(set(related_tests)),
            "graph": graph,
        }

    def mark_stale(self, paths: Sequence[str]) -> int:
        relative_paths = [self._relative_path(path) for path in paths]
        self._ensure_storage_path()
        if not relative_paths or not self.database_path.exists():
            return 0
        with self._connect() as connection:
            placeholders = ",".join("?" for _ in relative_paths)
            cursor = connection.execute(
                f"""
                UPDATE file_profiles SET stale = 1
                WHERE path IN ({placeholders})
                """,
                tuple(relative_paths),
            )
            return cursor.rowcount

    def _index_records(self) -> List[Dict[str, Any]]:
        if not self.project_index.database_path.exists():
            return []
        connection = sqlite3.connect(self.project_index.database_path)
        connection.row_factory = sqlite3.Row
        try:
            files = connection.execute(
                """
                SELECT path, content_hash, language, line_count
                FROM files
                WHERE indexed = 1
                ORDER BY path
                """
            ).fetchall()
            records = []
            for file_row in files:
                path = file_row["path"]
                symbols = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT name, kind, line, signature
                        FROM symbols
                        WHERE path = ?
                        ORDER BY line, name
                        """,
                        (path,),
                    ).fetchall()
                ]
                imports = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT target, line, kind
                        FROM imports
                        WHERE path = ?
                        ORDER BY line, target
                        """,
                        (path,),
                    ).fetchall()
                ]
                chunk = connection.execute(
                    """
                    SELECT content
                    FROM chunks
                    WHERE path = ?
                    ORDER BY chunk_index
                    LIMIT 1
                    """,
                    (path,),
                ).fetchone()
                records.append(
                    {
                        **dict(file_row),
                        "symbols": symbols,
                        "imports": imports,
                        "leading_content": chunk["content"] if chunk else "",
                    }
                )
            return records
        finally:
            connection.close()

    def _store_profile(
        self,
        connection: sqlite3.Connection,
        profile: FileProfile,
    ) -> None:
        connection.execute(
            """
            INSERT INTO file_profiles (
                path, content_hash, language, line_count, purpose,
                responsibilities_json, public_symbols_json, imports_json,
                related_tests_json, confidence, evidence_json, stale,
                profile_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                content_hash = excluded.content_hash,
                language = excluded.language,
                line_count = excluded.line_count,
                purpose = excluded.purpose,
                responsibilities_json = excluded.responsibilities_json,
                public_symbols_json = excluded.public_symbols_json,
                imports_json = excluded.imports_json,
                related_tests_json = excluded.related_tests_json,
                confidence = excluded.confidence,
                evidence_json = excluded.evidence_json,
                stale = excluded.stale,
                profile_version = excluded.profile_version,
                updated_at = excluded.updated_at
            """,
            (
                profile.path,
                profile.content_hash,
                profile.language,
                profile.line_count,
                profile.purpose,
                json.dumps(profile.responsibilities, ensure_ascii=False),
                json.dumps(profile.public_symbols, ensure_ascii=False),
                json.dumps(profile.imports, ensure_ascii=False),
                json.dumps(profile.related_tests, ensure_ascii=False),
                profile.confidence,
                json.dumps(profile.evidence, ensure_ascii=False),
                int(profile.stale),
                profile.profile_version,
                profile.updated_at,
            ),
        )
        connection.execute(
            "DELETE FROM profile_fts WHERE path = ?",
            (profile.path,),
        )
        terms = " ".join(
            sorted(
                _index_terms(
                    " ".join(
                        [
                            profile.path,
                            profile.purpose,
                            *profile.responsibilities,
                            *[
                                symbol["name"]
                                for symbol in profile.public_symbols
                            ],
                            *profile.imports,
                        ]
                    )
                )
            )
        )
        connection.execute(
            """
            INSERT INTO profile_fts (
                path, purpose, responsibilities, symbols, terms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                profile.path,
                profile.purpose,
                "\n".join(profile.responsibilities),
                " ".join(
                    symbol["name"] for symbol in profile.public_symbols
                ),
                terms,
            ),
        )

    def _rebuild_graph(
        self,
        connection: sqlite3.Connection,
        records: List[Dict[str, Any]],
        related_tests: Dict[str, List[str]],
        refreshed_at: str,
    ) -> None:
        connection.execute("DELETE FROM graph_edges")
        connection.execute("DELETE FROM graph_nodes")
        workspace_key = "workspace:."
        self._insert_node(
            connection,
            workspace_key,
            "Workspace",
            self.workspace.root.name,
            "",
            "",
            "",
            {"root": str(self.workspace.root)},
            refreshed_at,
        )
        aliases = _module_aliases(records)
        module_targets: Set[str] = set()
        for record in records:
            path = record["path"]
            profile_row = connection.execute(
                "SELECT purpose FROM file_profiles WHERE path = ?",
                (path,),
            ).fetchone()
            purpose = profile_row["purpose"] if profile_row else ""
            file_key = f"file:{path}"
            self._insert_node(
                connection,
                file_key,
                "File",
                Path(path).name,
                path,
                purpose,
                record["content_hash"],
                {
                    "language": record["language"],
                    "line_count": record["line_count"],
                    "is_test": _is_test_path(path),
                },
                refreshed_at,
            )
            self._insert_edge(
                connection,
                workspace_key,
                file_key,
                "CONTAINS",
                {"path": path},
                refreshed_at,
            )
            for symbol in record["symbols"]:
                symbol_key = (
                    f"symbol:{path}:{symbol['name']}:{symbol['line']}"
                )
                self._insert_node(
                    connection,
                    symbol_key,
                    "Symbol",
                    symbol["name"],
                    path,
                    symbol["signature"],
                    record["content_hash"],
                    {
                        "kind": symbol["kind"],
                        "line": symbol["line"],
                    },
                    refreshed_at,
                )
                self._insert_edge(
                    connection,
                    file_key,
                    symbol_key,
                    "DEFINES",
                    {"line": symbol["line"]},
                    refreshed_at,
                )
            for imported in record["imports"]:
                target = imported["target"]
                module_key = f"module:{target}"
                module_targets.add(target)
                resolved = _resolve_import(path, target, aliases)
                self._insert_edge(
                    connection,
                    file_key,
                    module_key,
                    "IMPORTS",
                    {
                        "line": imported["line"],
                        "kind": imported["kind"],
                    },
                    refreshed_at,
                )
                if resolved:
                    self._insert_edge(
                        connection,
                        file_key,
                        f"file:{resolved}",
                        "DEPENDS_ON",
                        {
                            "line": imported["line"],
                            "target": target,
                        },
                        refreshed_at,
                    )
            for test_path in related_tests.get(path, []):
                self._insert_edge(
                    connection,
                    f"file:{test_path}",
                    file_key,
                    "TESTS",
                    {"inferred": True},
                    refreshed_at,
                )
        for target in sorted(module_targets):
            self._insert_node(
                connection,
                f"module:{target}",
                "Module",
                target,
                "",
                "",
                "",
                {"import_target": target},
                refreshed_at,
            )
        counts = self._counts(connection)
        if counts["nodes"] > MAX_GRAPH_NODES:
            raise ValueError("project graph exceeds node safety limit")
        if counts["edges"] > MAX_GRAPH_EDGES:
            raise ValueError("project graph exceeds edge safety limit")

    @staticmethod
    def _insert_node(
        connection: sqlite3.Connection,
        node_key: str,
        node_type: str,
        name: str,
        path: str,
        purpose: str,
        content_hash: str,
        properties: Dict[str, Any],
        updated_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO graph_nodes (
                node_key, node_type, name, path, purpose, content_hash,
                properties_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_key,
                node_type,
                name,
                path,
                purpose,
                content_hash,
                json.dumps(properties, ensure_ascii=False),
                updated_at,
            ),
        )

    @staticmethod
    def _insert_edge(
        connection: sqlite3.Connection,
        source_key: str,
        target_key: str,
        edge_type: str,
        evidence: Dict[str, Any],
        updated_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO graph_edges (
                source_key, target_key, edge_type, evidence_json, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                source_key,
                target_key,
                edge_type,
                json.dumps(evidence, ensure_ascii=False),
                updated_at,
            ),
        )

    def _edges_for_keys(
        self,
        connection: sqlite3.Connection,
        node_keys: Sequence[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        if not node_keys:
            return []
        placeholders = ",".join("?" for _ in node_keys)
        rows = connection.execute(
            f"""
            SELECT source_key, target_key, edge_type, evidence_json
            FROM graph_edges
            WHERE source_key IN ({placeholders})
              AND target_key IN ({placeholders})
            ORDER BY edge_type, source_key, target_key
            LIMIT ?
            """,
            (*node_keys, *node_keys, limit),
        ).fetchall()
        return [
            {
                "source_key": row["source_key"],
                "target_key": row["target_key"],
                "edge_type": row["edge_type"],
                "evidence": json.loads(row["evidence_json"]),
            }
            for row in rows
        ]

    def _sync_neo4j(self) -> None:
        with self._connect() as connection:
            nodes = [
                {
                    **dict(row),
                    "properties_json": row["properties_json"],
                }
                for row in connection.execute(
                    """
                    SELECT node_key, node_type, name, path, purpose,
                           content_hash, properties_json
                    FROM graph_nodes
                    ORDER BY node_key
                    """
                ).fetchall()
            ]
            edges = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT source_key, target_key, edge_type, evidence_json
                    FROM graph_edges
                    ORDER BY source_key, target_key, edge_type
                    """
                ).fetchall()
            ]
        mirror = Neo4jGraphMirror(self.config)
        try:
            mirror.sync_snapshot(
                self.workspace_id,
                str(self.workspace.root),
                nodes,
                edges,
            )
        finally:
            mirror.close()

    def _record_mirror_status(self, error: str, refreshed_at: str) -> None:
        with self._connect() as connection:
            self._set_metadata(connection, "neo4j_last_error", error)
            if not error:
                self._set_metadata(
                    connection,
                    "neo4j_last_sync",
                    refreshed_at,
                )

    def _safe_mirror_error(self, error: Exception) -> str:
        message = f"{type(error).__name__}: {error}"
        if self.config.neo4j_password:
            message = message.replace(
                self.config.neo4j_password,
                "[redacted]",
            )
        return message[:2_000]

    @staticmethod
    def _set_metadata(
        connection: sqlite3.Connection,
        key: str,
        value: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    @staticmethod
    def _counts(connection: sqlite3.Connection) -> Dict[str, int]:
        return {
            "profiles": connection.execute(
                "SELECT COUNT(*) FROM file_profiles"
            ).fetchone()[0],
            "stale_profiles": connection.execute(
                "SELECT COUNT(*) FROM file_profiles WHERE stale = 1"
            ).fetchone()[0],
            "nodes": connection.execute(
                "SELECT COUNT(*) FROM graph_nodes"
            ).fetchone()[0],
            "edges": connection.execute(
                "SELECT COUNT(*) FROM graph_edges"
            ).fetchone()[0],
        }

    def _relative_path(self, path: str) -> str:
        resolved = self.workspace.resolve(path)
        return resolved.relative_to(self.workspace.root).as_posix()

    def _connect(self) -> sqlite3.Connection:
        self._ensure_storage_path()
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_profiles (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                language TEXT NOT NULL,
                line_count INTEGER NOT NULL,
                purpose TEXT NOT NULL,
                responsibilities_json TEXT NOT NULL,
                public_symbols_json TEXT NOT NULL,
                imports_json TEXT NOT NULL,
                related_tests_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                evidence_json TEXT NOT NULL,
                stale INTEGER NOT NULL,
                profile_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS profile_fts USING fts5(
                path UNINDEXED,
                purpose,
                responsibilities,
                symbols,
                terms,
                tokenize = 'unicode61'
            );
            CREATE TABLE IF NOT EXISTS graph_nodes (
                node_key TEXT PRIMARY KEY,
                node_type TEXT NOT NULL,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                purpose TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS graph_nodes_path_idx
            ON graph_nodes(path);
            CREATE INDEX IF NOT EXISTS graph_nodes_type_idx
            ON graph_nodes(node_type);
            CREATE TABLE IF NOT EXISTS graph_edges (
                source_key TEXT NOT NULL,
                target_key TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_key, target_key, edge_type)
            );
            CREATE INDEX IF NOT EXISTS graph_edges_source_idx
            ON graph_edges(source_key);
            CREATE INDEX IF NOT EXISTS graph_edges_target_idx
            ON graph_edges(target_key);
            CREATE INDEX IF NOT EXISTS graph_edges_type_idx
            ON graph_edges(edge_type);
            """
        )
        return connection

    def _ensure_storage_path(self) -> None:
        paths = [
            self.workspace.root / ".simple-agent",
            self.root,
            self.database_path,
        ]
        if any(path.is_symlink() for path in paths if path.exists()):
            raise ValueError("project graph paths cannot contain symbolic links")
        try:
            self.database_path.resolve(strict=False).relative_to(
                self.workspace.root
            )
        except ValueError as exc:
            raise ValueError(
                "project graph path is outside the workspace"
            ) from exc


def profile_to_dict(profile: Optional[FileProfile]) -> Optional[Dict[str, Any]]:
    if profile is None:
        return None
    return {"citation": profile.citation, **asdict(profile)}


def _profile_from_row(row: sqlite3.Row) -> FileProfile:
    return FileProfile(
        path=row["path"],
        content_hash=row["content_hash"],
        language=row["language"],
        line_count=row["line_count"],
        purpose=row["purpose"],
        responsibilities=json.loads(row["responsibilities_json"]),
        public_symbols=json.loads(row["public_symbols_json"]),
        imports=json.loads(row["imports_json"]),
        related_tests=json.loads(row["related_tests_json"]),
        confidence=float(row["confidence"]),
        evidence=json.loads(row["evidence_json"]),
        stale=bool(row["stale"]),
        profile_version=row["profile_version"],
        updated_at=row["updated_at"],
    )


def _node_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "node_key": row["node_key"],
        "node_type": row["node_type"],
        "name": row["name"],
        "path": row["path"],
        "purpose": row["purpose"],
        "properties": json.loads(row["properties_json"]),
    }


def _build_profile(
    record: Dict[str, Any],
    related_tests: List[str],
    refreshed_at: str,
) -> FileProfile:
    path = record["path"]
    symbols = record["symbols"][:100]
    imports = list(
        dict.fromkeys(item["target"] for item in record["imports"])
    )[:100]
    leading = record["leading_content"]
    purpose, confidence = _infer_purpose(path, leading, symbols)
    responsibilities = _responsibilities(path, symbols, imports)
    evidence = [
        f"{path}#L{symbol['line']}"
        for symbol in symbols[:20]
    ]
    if not evidence:
        evidence = [f"{path}#L1"]
    return FileProfile(
        path=path,
        content_hash=record["content_hash"],
        language=record["language"],
        line_count=record["line_count"],
        purpose=purpose,
        responsibilities=responsibilities,
        public_symbols=symbols[:50],
        imports=imports,
        related_tests=related_tests,
        confidence=confidence,
        evidence=evidence,
        stale=False,
        profile_version=PROFILE_VERSION,
        updated_at=refreshed_at,
    )


def _infer_purpose(
    path: str,
    leading: str,
    symbols: List[Dict[str, Any]],
) -> Tuple[str, float]:
    file_path = Path(path)
    if _is_test_path(path):
        target = file_path.stem.removeprefix("test_").replace("_", " ")
        return f"验证 {target or file_path.name} 相关行为和边界条件。", 0.9
    doc = _leading_description(leading, file_path.suffix.lower())
    if doc:
        return doc[:500], 0.92
    names = [symbol["name"] for symbol in symbols[:8]]
    if file_path.name == "__init__.py":
        return "定义包入口并组织或导出该包的公共能力。", 0.75
    if file_path.name in {"cli.py", "main.py", "__main__.py"}:
        return "提供程序入口、参数处理和顶层运行流程。", 0.85
    if file_path.suffix.lower() in {".md", ".rst"}:
        return f"记录 {file_path.stem} 相关项目文档。", 0.72
    if names:
        return (
            f"实现 {file_path.stem} 模块，主要定义 "
            + "、".join(names)
            + "。"
        ), 0.78
    if file_path.suffix.lower() in {
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
    }:
        return f"保存 {file_path.stem} 相关配置或结构化数据。", 0.7
    return f"实现或描述 {file_path.stem} 相关项目能力。", 0.55


def _leading_description(text: str, suffix: str) -> str:
    if suffix == ".py":
        match = re.match(
            r'\s*(?:[rubfRUBF]{0,2})?("""|\'\'\')(.+?)\1',
            text,
            re.DOTALL,
        )
        if match:
            return " ".join(match.group(2).strip().split())
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs"}:
        match = re.match(r"\s*/\*+\s*(.+?)\*/", text, re.DOTALL)
        if match:
            return " ".join(match.group(1).replace("*", " ").split())
        match = re.match(r"\s*//\s*(.+)", text)
        if match:
            return match.group(1).strip()
    if suffix in {".md", ".mdx", ".rst"}:
        for line in text.splitlines():
            clean = line.strip().lstrip("#").strip()
            if clean:
                return f"记录 {clean} 相关项目说明。"
    return ""


def _responsibilities(
    path: str,
    symbols: List[Dict[str, Any]],
    imports: List[str],
) -> List[str]:
    responsibilities = []
    classes = [
        symbol["name"]
        for symbol in symbols
        if symbol["kind"] in {"class", "interface", "struct", "trait"}
    ]
    functions = [
        symbol["name"]
        for symbol in symbols
        if symbol["kind"] == "function"
    ]
    if classes:
        responsibilities.append(
            "定义核心类型：" + "、".join(classes[:12])
        )
    if functions:
        responsibilities.append(
            "提供主要函数：" + "、".join(functions[:15])
        )
    if imports:
        responsibilities.append(
            "集成依赖：" + "、".join(imports[:12])
        )
    if _is_test_path(path):
        responsibilities.append("提供自动化验证和回归保护")
    if not responsibilities:
        responsibilities.append("承载该文件路径所对应的项目实现或配置")
    return responsibilities[:10]


def _related_test_map(
    records: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    source_by_stem: Dict[str, List[str]] = {}
    tests = []
    for record in records:
        path = record["path"]
        stem = Path(path).stem
        if _is_test_path(path):
            tests.append(path)
        else:
            source_by_stem.setdefault(stem, []).append(path)
    result: Dict[str, List[str]] = {}
    for test in tests:
        target_stem = Path(test).stem.removeprefix("test_")
        for source in source_by_stem.get(target_stem, []):
            result.setdefault(source, []).append(test)
    return {
        path: sorted(set(paths))
        for path, paths in result.items()
    }


def _module_aliases(
    records: List[Dict[str, Any]],
) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for record in records:
        path = record["path"]
        file_path = Path(path)
        if file_path.suffix.lower() not in {
            ".py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
        }:
            continue
        no_suffix = file_path.with_suffix("").as_posix()
        candidates = {
            no_suffix,
            no_suffix.replace("/", "."),
            file_path.stem,
        }
        if no_suffix.startswith("src/"):
            stripped = no_suffix[4:]
            candidates.update({stripped, stripped.replace("/", ".")})
        if file_path.stem == "__init__":
            parent = file_path.parent.as_posix()
            candidates.update({parent, parent.replace("/", ".")})
            if parent.startswith("src/"):
                stripped = parent[4:]
                candidates.update({stripped, stripped.replace("/", ".")})
        for candidate in candidates:
            aliases.setdefault(candidate, path)
    return aliases


def _resolve_import(
    importer: str,
    target: str,
    aliases: Dict[str, str],
) -> str:
    normalized = target.strip().strip("\"'")
    if not normalized:
        return ""
    if normalized.startswith("."):
        importer_path = Path(importer)
        candidate = (importer_path.parent / normalized).as_posix()
        candidate = str(Path(candidate))
        for suffix in ("", ".py", ".js", ".ts", "/__init__.py"):
            resolved = aliases.get(candidate + suffix)
            if resolved:
                return resolved
        python_target = normalized.lstrip(".")
        parent_parts = list(importer_path.parent.parts)
        levels = len(normalized) - len(normalized.lstrip("."))
        base = parent_parts[: max(0, len(parent_parts) - levels + 1)]
        dotted = ".".join([*base, python_target]).strip(".")
        return aliases.get(dotted, "")
    return aliases.get(normalized, "")


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name.lower()
    return (
        "tests" in parts
        or "test" in parts
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.ts"))
    )


def _index_terms(text: str) -> Set[str]:
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
    return sorted(_index_terms(query), key=lambda item: (-len(item), item))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
