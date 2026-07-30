"""Neo4j project graph with LLM-generated file profiles."""

import hashlib
import json
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple

from dotenv import load_dotenv

from .config import Settings
from .llm import ChatModel, OpenAICompatibleLLM
from .project_index import ProjectIndex
from .vector_store import (
    ChromaVectorStore,
    VectorRecord,
    content_fingerprint,
    reciprocal_rank_fusion,
)
from .workspace import Workspace

GRAPH_VERSION = 2
PROFILE_VERSION = 2
DEFAULT_PROFILE_LIMIT = 6
MAX_PROFILE_RESULTS = 30
PROFILE_BATCH_SIZE = 2
PROFILE_MAX_CONCURRENCY = 3
PROFILE_MAX_OUTPUT_TOKENS = 6_000
PROFILE_SOURCE_EXCERPT_CHARS = 4_000


@dataclass(frozen=True)
class ProjectGraphConfig:
    """Required Neo4j configuration; no local graph fallback is provided."""

    neo4j_uri: str = ""
    neo4j_username: str = ""
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"

    @classmethod
    def from_env(cls) -> "ProjectGraphConfig":
        load_dotenv()
        return cls(
            neo4j_uri=os.getenv("NEO4J_URI", ""),
            neo4j_username=os.getenv("NEO4J_USERNAME", ""),
            neo4j_password=os.getenv("NEO4J_PASSWORD", ""),
            neo4j_database=os.getenv("NEO4J_DATABASE", "neo4j"),
        )

    @property
    def missing_settings(self) -> List[str]:
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
    def configured(self) -> bool:
        return not self.missing_settings


@dataclass(frozen=True)
class FileProfile:
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
    scanned_files: int
    updated_profiles: int
    unchanged_profiles: int
    deleted_profiles: int
    nodes: int
    edges: int
    duration_ms: int
    backend: str
    neo4j_synced: bool
    refreshed_at: str
    error: str = ""


class FileProfileGenerator(Protocol):
    def generate(
        self,
        records: Sequence[Dict[str, Any]],
        related_tests: Dict[str, List[str]],
        generated_at: str,
    ) -> List[FileProfile]:
        """Generate profiles for changed files."""


class LLMFileProfileGenerator:
    """Generate bounded, evidence-oriented file descriptions in batches."""

    def __init__(
        self,
        model: Optional[ChatModel] = None,
        batch_size: int = PROFILE_BATCH_SIZE,
    ) -> None:
        self.model = model
        self.batch_size = batch_size

    def generate(
        self,
        records: Sequence[Dict[str, Any]],
        related_tests: Dict[str, List[str]],
        generated_at: str,
    ) -> List[FileProfile]:
        if not records:
            return []
        self._ensure_model()
        batches = [
            list(records[offset : offset + self.batch_size])
            for offset in range(0, len(records), self.batch_size)
        ]
        if len(batches) == 1:
            generated_batches = [self._complete(batches[0])]
        else:
            with ThreadPoolExecutor(
                max_workers=min(PROFILE_MAX_CONCURRENCY, len(batches)),
                thread_name_prefix="file-profile",
            ) as executor:
                generated_batches = list(executor.map(self._complete, batches))
        profiles: List[FileProfile] = []
        for batch, generated in zip(batches, generated_batches):
            by_path = {
                item.get("path"): item
                for item in generated
                if isinstance(item, dict)
            }
            missing = [
                record["path"]
                for record in batch
                if record["path"] not in by_path
            ]
            if missing:
                raise RuntimeError(
                    "LLM file profile response omitted: "
                    + ", ".join(missing)
                )
            for record in batch:
                item = by_path[record["path"]]
                purpose = str(item.get("purpose", "")).strip()
                responsibilities = _string_list(
                    item.get("responsibilities"),
                    limit=12,
                )
                evidence = _string_list(item.get("evidence"), limit=20)
                if not purpose or not responsibilities:
                    raise RuntimeError(
                        "LLM returned an incomplete profile for "
                        + record["path"]
                    )
                if not evidence:
                    evidence = [
                        f"{record['path']}#L{symbol['line']}"
                        for symbol in record["symbols"][:10]
                    ] or [f"{record['path']}#L1"]
                profiles.append(
                    FileProfile(
                        path=record["path"],
                        content_hash=record["content_hash"],
                        language=record["language"],
                        line_count=record["line_count"],
                        purpose=purpose[:1_000],
                        responsibilities=responsibilities,
                        public_symbols=record["symbols"][:100],
                        imports=list(
                            dict.fromkeys(
                                imported["target"]
                                for imported in record["imports"]
                            )
                        )[:100],
                        related_tests=related_tests.get(
                            record["path"],
                            [],
                        ),
                        confidence=_confidence(item.get("confidence")),
                        evidence=evidence,
                        stale=False,
                        profile_version=PROFILE_VERSION,
                        updated_at=generated_at,
                    )
                )
        return profiles

    def _ensure_model(self) -> None:
        if self.model is None:
            settings = replace(
                Settings.from_env(),
                max_output_tokens=PROFILE_MAX_OUTPUT_TOKENS,
            )
            self.model = OpenAICompatibleLLM(
                settings,
                timeout=180.0,
                max_retries=0,
            )

    def _complete(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        assert self.model is not None
        payload = [
            {
                "path": record["path"],
                "language": record["language"],
                "line_count": record["line_count"],
                "symbols": record["symbols"][:60],
                "imports": record["imports"][:60],
                "source_excerpt": record["leading_content"][
                    :PROFILE_SOURCE_EXCERPT_CHARS
                ],
            }
            for record in records
        ]
        response = self.model.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "你是代码库文件职责分析器。根据提供的源码证据，为每个"
                        "文件生成准确、简洁的中文功能档案。不得猜测未出现的"
                        "实现。只输出 JSON 数组；每项包含 path、purpose、"
                        "responsibilities、confidence、evidence。evidence 使用"
                        " path#L<行号>。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ]
        )
        content = (
            response.get("content", "")
            if isinstance(response, dict)
            else getattr(response, "content", "")
        )
        parsed = json.loads(_strip_json_fence(content))
        if not isinstance(parsed, list):
            raise RuntimeError("LLM file profile response must be a JSON array")
        return parsed


class Neo4jProjectStore:
    """All graph nodes, relationships, and file profiles live in Neo4j."""

    def __init__(
        self,
        config: ProjectGraphConfig,
        driver_factory: Optional[Any] = None,
    ) -> None:
        if not config.configured:
            raise RuntimeError(
                "Neo4j graph requires " + ", ".join(config.missing_settings)
            )
        self.config = config
        if driver_factory is None:
            try:
                from neo4j import GraphDatabase
            except ImportError as exc:
                raise RuntimeError(
                    "Neo4j graph requires the neo4j package"
                ) from exc
            driver_factory = GraphDatabase.driver
        self.driver = driver_factory(
            config.neo4j_uri,
            auth=(config.neo4j_username, config.neo4j_password),
        )

    def close(self) -> None:
        self.driver.close()

    def ensure_schema(self) -> None:
        self.driver.verify_connectivity()
        statements = [
            """
            CREATE CONSTRAINT sa_node_key IF NOT EXISTS
            FOR (n:ProjectNode)
            REQUIRE (n.workspace_id, n.node_key) IS UNIQUE
            """,
            """
            CREATE FULLTEXT INDEX sa_file_profile_search IF NOT EXISTS
            FOR (n:ProjectFile)
            ON EACH [n.path, n.purpose, n.responsibilities_text]
            """,
        ]
        for statement in statements:
            self._query(statement)

    def status(self, workspace_id: str) -> Dict[str, Any]:
        records = self._query(
            """
            OPTIONAL MATCH (n:ProjectNode {workspace_id: $workspace_id})
            WITH count(n) AS nodes
            OPTIONAL MATCH ()-[r]->()
            WHERE r.workspace_id = $workspace_id
            WITH nodes, count(r) AS edges
            OPTIONAL MATCH (
                w:ProjectWorkspace {workspace_id: $workspace_id}
            )
            RETURN nodes, edges, w.updated_at AS last_refresh,
                   w.graph_version AS graph_version,
                   w.last_error AS last_error
            """,
            {"workspace_id": workspace_id},
        )
        row = records[0] if records else {}
        nodes = int(row.get("nodes") or 0)
        profiles = self._query(
            """
            MATCH (f:ProjectFile {workspace_id: $workspace_id})
            RETURN count(f) AS profiles
            """,
            {"workspace_id": workspace_id},
        )
        return {
            "ready": int(row.get("graph_version") or 0) == GRAPH_VERSION,
            "backend": "neo4j",
            "workspace_id": workspace_id,
            "profiles": int(profiles[0].get("profiles") or 0)
            if profiles
            else 0,
            "nodes": nodes,
            "edges": int(row.get("edges") or 0),
            "last_refresh": row.get("last_refresh") or "",
            "graph_version": int(row.get("graph_version") or 0),
            "last_error": row.get("last_error") or "",
        }

    def fetch_profiles(self, workspace_id: str) -> Dict[str, FileProfile]:
        rows = self._query(
            """
            MATCH (f:ProjectFile {workspace_id: $workspace_id})
            RETURN f
            """,
            {"workspace_id": workspace_id},
        )
        return {
            profile.path: profile
            for profile in (
                _profile_from_node(_node_dict(row["f"])) for row in rows
            )
        }

    def stage_profiles(
        self,
        workspace_id: str,
        profiles: Sequence[FileProfile],
    ) -> None:
        """Persist completed LLM batches before the full graph is ready."""
        if not profiles:
            return
        self._query(
            """
            UNWIND $profiles AS item
            MERGE (f:ProjectNode:ProjectFile {
                workspace_id: $workspace_id,
                node_key: item.node_key
            })
            SET f.node_type = 'File',
                f.name = item.name,
                f.path = item.path,
                f.purpose = item.purpose,
                f.content_hash = item.content_hash,
                f.properties_json = item.properties_json,
                f.language = item.language,
                f.line_count = item.line_count,
                f.responsibilities = item.responsibilities,
                f.responsibilities_text = item.responsibilities_text,
                f.imports = item.imports,
                f.related_tests = item.related_tests,
                f.confidence = item.confidence,
                f.evidence = item.evidence,
                f.public_symbols_json = item.public_symbols_json,
                f.profile_version = item.profile_version,
                f.updated_at = item.updated_at,
                f.stale = false,
                f.draft = true
            """,
            {
                "workspace_id": workspace_id,
                "profiles": [
                    _profile_node_payload(profile) for profile in profiles
                ],
            },
        )

    def sync_snapshot(
        self,
        workspace_id: str,
        workspace_path: str,
        records: Sequence[Dict[str, Any]],
        profiles: Sequence[FileProfile],
        updated_at: str,
    ) -> None:
        nodes, edges = _graph_payload(records, profiles, workspace_path)
        session = self.driver.session(database=self.config.neo4j_database)
        transaction = session.begin_transaction()

        def run(query: str, parameters: Dict[str, Any]) -> None:
            transaction.run(query, **parameters).consume()

        try:
            run(
                """
                MATCH (n:ProjectNode {workspace_id: $workspace_id})
                DETACH DELETE n
                """,
                {"workspace_id": workspace_id},
            )
            run(
                """
                UNWIND $nodes AS item
                CREATE (n:ProjectNode {
                    workspace_id: $workspace_id,
                    node_key: item.node_key,
                    node_type: item.node_type,
                    name: item.name,
                    path: item.path,
                    purpose: item.purpose,
                    content_hash: item.content_hash,
                    properties_json: item.properties_json
                })
                FOREACH (_ IN CASE WHEN item.node_type = 'Workspace'
                                   THEN [1] ELSE [] END |
                    SET n:ProjectWorkspace)
                FOREACH (_ IN CASE WHEN item.node_type = 'File'
                                   THEN [1] ELSE [] END |
                    SET n:ProjectFile,
                        n.language = item.language,
                        n.line_count = item.line_count,
                        n.responsibilities = item.responsibilities,
                        n.responsibilities_text = item.responsibilities_text,
                        n.imports = item.imports,
                        n.related_tests = item.related_tests,
                        n.confidence = item.confidence,
                        n.evidence = item.evidence,
                        n.public_symbols_json = item.public_symbols_json,
                        n.profile_version = item.profile_version,
                        n.updated_at = item.updated_at,
                        n.stale = false)
                FOREACH (_ IN CASE WHEN item.node_type = 'Symbol'
                                   THEN [1] ELSE [] END |
                    SET n:ProjectSymbol)
                FOREACH (_ IN CASE WHEN item.node_type = 'Module'
                                   THEN [1] ELSE [] END |
                    SET n:ProjectModule)
                """,
                {"workspace_id": workspace_id, "nodes": nodes},
            )
            for edge_type in (
                "CONTAINS",
                "DEFINES",
                "IMPORTS",
                "DEPENDS_ON",
                "TESTS",
            ):
                selected = [
                    edge for edge in edges if edge["edge_type"] == edge_type
                ]
                if not selected:
                    continue
                run(
                    f"""
                    UNWIND $edges AS edge
                    MATCH (source:ProjectNode {{
                        workspace_id: $workspace_id,
                        node_key: edge.source_key
                    }})
                    MATCH (target:ProjectNode {{
                        workspace_id: $workspace_id,
                        node_key: edge.target_key
                    }})
                    CREATE (source)-[r:{edge_type}]->(target)
                    SET r.workspace_id = $workspace_id,
                        r.evidence_json = edge.evidence_json
                    """,
                    {"workspace_id": workspace_id, "edges": selected},
                )
            run(
                """
                MATCH (w:ProjectWorkspace {workspace_id: $workspace_id})
                SET w.workspace_path = $workspace_path,
                    w.updated_at = $updated_at,
                    w.graph_version = $graph_version,
                    w.last_error = ''
                """,
                {
                    "workspace_id": workspace_id,
                    "workspace_path": workspace_path,
                    "updated_at": updated_at,
                    "graph_version": GRAPH_VERSION,
                },
            )
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
        finally:
            session.close()

    def overview(
        self,
        workspace_id: str,
        max_profiles: int,
    ) -> Dict[str, Any]:
        status = self.status(workspace_id)
        rows = self._query(
            """
            MATCH (f:ProjectFile {workspace_id: $workspace_id})
            RETURN f.path AS path, f.language AS language,
                   f.purpose AS purpose, f.confidence AS confidence,
                   f.stale AS stale
            ORDER BY CASE WHEN f.path STARTS WITH 'src/' THEN 0
                          WHEN f.path STARTS WITH 'tests/' THEN 2
                          ELSE 1 END, f.path
            LIMIT $limit
            """,
            {"workspace_id": workspace_id, "limit": max_profiles},
        )
        edge_types = self._query(
            """
            MATCH ()-[r]->()
            WHERE r.workspace_id = $workspace_id
            RETURN type(r) AS edge_type, count(r) AS count
            ORDER BY count DESC, edge_type
            """,
            {"workspace_id": workspace_id},
        )
        return {
            **status,
            "edge_types": edge_types,
            "representative_files": rows,
        }

    def get_profile(
        self,
        workspace_id: str,
        path: str,
    ) -> Optional[FileProfile]:
        rows = self._query(
            """
            MATCH (f:ProjectFile {
                workspace_id: $workspace_id,
                path: $path
            })
            RETURN f
            LIMIT 1
            """,
            {"workspace_id": workspace_id, "path": path},
        )
        return (
            _profile_from_node(_node_dict(rows[0]["f"])) if rows else None
        )

    def mark_profiles_stale(
        self,
        workspace_id: str,
        paths: Sequence[str],
    ) -> int:
        rows = self._query(
            """
            MATCH (f:ProjectFile {workspace_id: $workspace_id})
            WHERE f.path IN $paths
            SET f.stale = true
            RETURN count(f) AS updated
            """,
            {"workspace_id": workspace_id, "paths": list(paths)},
        )
        return int(rows[0].get("updated") or 0) if rows else 0

    def search_profiles(
        self,
        workspace_id: str,
        query: str,
        limit: int,
    ) -> List[FileProfile]:
        rows = self._query(
            """
            CALL db.index.fulltext.queryNodes(
                'sa_file_profile_search', $query, {limit: $candidate_limit}
            )
            YIELD node, score
            WHERE node.workspace_id = $workspace_id
            RETURN node AS f, score
            ORDER BY score DESC
            LIMIT $limit
            """,
            {
                "workspace_id": workspace_id,
                "query": query,
                "candidate_limit": max(limit * 4, limit),
                "limit": limit,
            },
        )
        return [_profile_from_node(_node_dict(row["f"])) for row in rows]

    def neighbors(
        self,
        workspace_id: str,
        path: str,
        depth: int,
        limit: int,
    ) -> Dict[str, Any]:
        rows = self._query(
            f"""
            MATCH (start:ProjectFile {{
                workspace_id: $workspace_id,
                path: $path
            }})
            MATCH p=(start)-[*0..{depth}]-(related:ProjectNode)
            WITH DISTINCT related
            LIMIT $limit
            RETURN related
            """,
            {
                "workspace_id": workspace_id,
                "path": path,
                "limit": limit,
            },
        )
        nodes = [_public_node(_node_dict(row["related"])) for row in rows]
        keys = [node["node_key"] for node in nodes]
        edge_rows = (
            self._query(
                """
                MATCH (source:ProjectNode)-[r]->(target:ProjectNode)
                WHERE source.workspace_id = $workspace_id
                  AND source.node_key IN $keys
                  AND target.node_key IN $keys
                RETURN source.node_key AS source_key,
                       target.node_key AS target_key,
                       type(r) AS edge_type,
                       r.evidence_json AS evidence_json
                LIMIT $edge_limit
                """,
                {
                    "workspace_id": workspace_id,
                    "keys": keys,
                    "edge_limit": limit * 3,
                },
            )
            if keys
            else []
        )
        return {
            "path": path,
            "depth": depth,
            "nodes": nodes,
            "edges": [_public_edge(row) for row in edge_rows],
        }

    def _query(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        result = self.driver.execute_query(
            query,
            parameters_=parameters or {},
            database_=self.config.neo4j_database,
        )
        records = result[0] if isinstance(result, tuple) else result.records
        return [
            record.data() if hasattr(record, "data") else dict(record)
            for record in records
        ]

class ProjectGraph:
    """Neo4j-only relationship graph with Chroma profile retrieval."""

    def __init__(
        self,
        workspace: Workspace,
        project_index: Optional[ProjectIndex] = None,
        config: Optional[ProjectGraphConfig] = None,
        profile_generator: Optional[FileProfileGenerator] = None,
        vector_store: Optional[ChromaVectorStore] = None,
        store: Optional[Neo4jProjectStore] = None,
    ) -> None:
        self.workspace = workspace
        self.vector_store = vector_store or ChromaVectorStore.from_env(workspace)
        self.project_index = project_index or ProjectIndex(
            workspace,
            vector_store=self.vector_store,
        )
        self.config = config or ProjectGraphConfig.from_env()
        self.profile_generator = profile_generator or LLMFileProfileGenerator()
        self.workspace_id = hashlib.sha256(
            str(workspace.root).encode("utf-8")
        ).hexdigest()[:24]
        self._store = store
        self.last_error = ""

    def refresh(
        self,
        paths: Optional[Sequence[str]] = None,
    ) -> GraphRefreshResult:
        started = time.monotonic()
        index_result = self.project_index.refresh(paths)
        if (
            paths
            and not index_result.indexed_files
            and not index_result.deleted_files
        ):
            # Edits update the lexical index immediately but defer embeddings
            # until this requirement-level graph refresh.
            self.project_index.sync_vector_index()
        refreshed_at = _now()
        records = self._index_records()
        if not self.config.configured and self._store is None:
            error = "Neo4j graph requires " + ", ".join(
                self.config.missing_settings
            )
            self.last_error = error
            return self._refresh_result(
                records,
                started,
                refreshed_at,
                error=error,
            )
        try:
            store = self._neo4j()
            store.ensure_schema()
            graph_status = store.status(self.workspace_id)
            existing = store.fetch_profiles(self.workspace_id)
            current_paths = {record["path"] for record in records}
            changed = [
                record
                for record in records
                if (
                    record["path"] not in existing
                    or existing[record["path"]].content_hash
                    != record["content_hash"]
                    or existing[record["path"]].profile_version
                    != PROFILE_VERSION
                )
            ]
            stale_unchanged = [
                record["path"]
                for record in records
                if (
                    record["path"] in existing
                    and existing[record["path"]].stale
                    and existing[record["path"]].content_hash
                    == record["content_hash"]
                    and existing[record["path"]].profile_version
                    == PROFILE_VERSION
                )
            ]
            deleted = sorted(set(existing) - current_paths)
            related_tests = _related_test_map(records)
            generated = self._generate_and_stage_profiles(
                store,
                changed,
                related_tests,
                refreshed_at,
            )
            profiles = {
                path: profile
                for path, profile in existing.items()
                if path in current_paths and path not in {
                    record["path"] for record in changed
                }
            }
            profiles.update({profile.path: profile for profile in generated})
            if (
                changed
                or deleted
                or stale_unchanged
                or not existing
                or graph_status.get("graph_version") != GRAPH_VERSION
            ):
                store.sync_snapshot(
                    self.workspace_id,
                    str(self.workspace.root),
                    records,
                    [profiles[path] for path in sorted(profiles)],
                    refreshed_at,
                )
            self._sync_profile_vectors(list(profiles.values()))
            status = store.status(self.workspace_id)
            self.last_error = ""
            return GraphRefreshResult(
                scanned_files=len(records),
                updated_profiles=len(changed),
                unchanged_profiles=len(records) - len(changed),
                deleted_profiles=len(deleted),
                nodes=status["nodes"],
                edges=status["edges"],
                duration_ms=round((time.monotonic() - started) * 1000),
                backend="neo4j",
                neo4j_synced=True,
                refreshed_at=refreshed_at,
            )
        except Exception as exc:
            error = self._safe_error(exc)
            self.last_error = error
            return self._refresh_result(
                records,
                started,
                refreshed_at,
                error=error,
            )

    def status(self) -> Dict[str, Any]:
        if not self.config.configured and self._store is None:
            return self._unavailable_status(
                "Neo4j graph requires "
                + ", ".join(self.config.missing_settings)
            )
        try:
            store = self._neo4j()
            store.ensure_schema()
            return {
                **store.status(self.workspace_id),
                "storage": "neo4j-only",
                "vector": self.vector_store.status(),
                "last_error": self.last_error,
            }
        except Exception as exc:
            return self._unavailable_status(self._safe_error(exc))

    def overview(self, max_profiles: int = 30) -> Dict[str, Any]:
        if not 1 <= max_profiles <= 200:
            raise ValueError("max_profiles must be from 1 to 200")
        try:
            return self._neo4j().overview(self.workspace_id, max_profiles)
        except Exception as exc:
            self.last_error = self._safe_error(exc)
            return self._unavailable_status(self.last_error)

    def get_profile(self, path: str) -> Optional[FileProfile]:
        try:
            return self._neo4j().get_profile(
                self.workspace_id,
                self._relative_path(path),
            )
        except Exception as exc:
            self.last_error = self._safe_error(exc)
            return None

    def search_profiles(
        self,
        query: str,
        limit: int = DEFAULT_PROFILE_LIMIT,
    ) -> List[FileProfile]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("file profile query must be non-empty")
        if not 1 <= limit <= MAX_PROFILE_RESULTS:
            raise ValueError("profile search limit must be from 1 to 30")
        try:
            lexical_query = _lucene_query(query)
            keyword = self._neo4j().search_profiles(
                self.workspace_id,
                lexical_query,
                min(MAX_PROFILE_RESULTS, limit * 3),
            ) if lexical_query else []
        except Exception as exc:
            self.last_error = self._safe_error(exc)
            keyword = []
        try:
            vectors = self.vector_store.query(
                "file_profiles",
                query,
                min(MAX_PROFILE_RESULTS, limit * 3),
            )
        except Exception:
            vectors = []
        keyword_ids = [profile.path for profile in keyword]
        vector_ids = [
            str(hit.metadata.get("path", "")) for hit in vectors
        ]
        scores = reciprocal_rank_fusion([keyword_ids, vector_ids])
        candidates = {profile.path: profile for profile in keyword}
        for path in vector_ids:
            if path and path not in candidates:
                profile = self.get_profile(path)
                if profile is not None:
                    candidates[path] = profile
        return [
            profile
            for _, profile in sorted(
                candidates.items(),
                key=lambda item: (-scores.get(item[0], 0.0), item[0]),
            )[:limit]
        ]

    def neighbors(
        self,
        path: str,
        depth: int = 1,
        limit: int = 100,
    ) -> Dict[str, Any]:
        if not 1 <= depth <= 4:
            raise ValueError("graph depth must be from 1 to 4")
        if not 1 <= limit <= 500:
            raise ValueError("graph result limit must be from 1 to 500")
        relative = self._relative_path(path)
        try:
            return self._neo4j().neighbors(
                self.workspace_id,
                relative,
                depth,
                limit,
            )
        except Exception as exc:
            self.last_error = self._safe_error(exc)
            return {
                "path": relative,
                "depth": depth,
                "nodes": [],
                "edges": [],
                "error": self.last_error,
            }

    def impact_analysis(
        self,
        path: str,
        depth: int = 2,
        limit: int = 100,
    ) -> Dict[str, Any]:
        graph = self.neighbors(path, depth, limit)
        relative = graph["path"]
        files = [
            node["path"]
            for node in graph["nodes"]
            if node["node_type"] == "File"
            and node.get("path")
            and node["path"] != relative
        ]
        return {
            "target": relative,
            "profile": profile_to_dict(self.get_profile(relative)),
            "affected_files": sorted(
                set(path for path in files if not _is_test_path(path))
            ),
            "related_tests": sorted(
                set(path for path in files if _is_test_path(path))
            ),
            "graph": graph,
        }

    def mark_stale(self, paths: Sequence[str]) -> int:
        relative_paths = [
            self._relative_path(path) for path in paths if path
        ]
        if not relative_paths:
            return 0
        if not self.config.configured and self._store is None:
            return 0
        try:
            return self._neo4j().mark_profiles_stale(
                self.workspace_id,
                relative_paths,
            )
        except Exception as exc:
            self.last_error = self._safe_error(exc)
            return 0

    def record_source_change(self, path: str) -> None:
        """Update deterministic evidence now and defer LLM profile refresh."""
        relative = self._relative_path(path)
        self.project_index.refresh([relative], sync_vectors=False)
        self.mark_stale([relative])

    def _generate_and_stage_profiles(
        self,
        store: Neo4jProjectStore,
        records: Sequence[Dict[str, Any]],
        related_tests: Dict[str, List[str]],
        generated_at: str,
    ) -> List[FileProfile]:
        if not records:
            return []
        if isinstance(self.profile_generator, LLMFileProfileGenerator):
            self.profile_generator._ensure_model()
        batches = [
            list(records[offset : offset + PROFILE_BATCH_SIZE])
            for offset in range(0, len(records), PROFILE_BATCH_SIZE)
        ]
        generated: List[FileProfile] = []
        failures: List[Exception] = []
        with ThreadPoolExecutor(
            max_workers=min(PROFILE_MAX_CONCURRENCY, len(batches)),
            thread_name_prefix="profile-stage",
        ) as executor:
            futures: Dict[Future[List[FileProfile]], List[Dict[str, Any]]] = {
                executor.submit(
                    self.profile_generator.generate,
                    batch,
                    related_tests,
                    generated_at,
                ): batch
                for batch in batches
            }
            for future in as_completed(futures):
                try:
                    profiles = future.result()
                    store.stage_profiles(self.workspace_id, profiles)
                    generated.extend(profiles)
                except Exception as exc:
                    failures.append(exc)
        if failures:
            raise failures[0]
        return sorted(generated, key=lambda profile: profile.path)

    def _neo4j(self) -> Neo4jProjectStore:
        if self._store is None:
            self._store = Neo4jProjectStore(self.config)
        return self._store

    def _index_records(self) -> List[Dict[str, Any]]:
        return self.project_index.file_evidence_records()

    def _sync_profile_vectors(self, profiles: List[FileProfile]) -> None:
        self.vector_store.sync_namespace(
            "file_profiles",
            [
                VectorRecord(
                    id=content_fingerprint("profile", profile.path),
                    text="\n".join(
                        [
                            profile.path,
                            profile.purpose,
                            *profile.responsibilities,
                            *[
                                symbol.get("name", "")
                                for symbol in profile.public_symbols
                            ],
                        ]
                    ),
                    content_hash=content_fingerprint(
                        profile.content_hash,
                        str(profile.profile_version),
                        profile.purpose,
                        *profile.responsibilities,
                    ),
                    metadata={"path": profile.path},
                )
                for profile in profiles
            ],
        )

    def _refresh_result(
        self,
        records: Sequence[Dict[str, Any]],
        started: float,
        refreshed_at: str,
        error: str,
    ) -> GraphRefreshResult:
        return GraphRefreshResult(
            scanned_files=len(records),
            updated_profiles=0,
            unchanged_profiles=0,
            deleted_profiles=0,
            nodes=0,
            edges=0,
            duration_ms=round((time.monotonic() - started) * 1000),
            backend="neo4j",
            neo4j_synced=False,
            refreshed_at=refreshed_at,
            error=error,
        )

    def _unavailable_status(self, error: str) -> Dict[str, Any]:
        return {
            "ready": False,
            "backend": "neo4j",
            "storage": "neo4j-only",
            "workspace_id": self.workspace_id,
            "profiles": 0,
            "nodes": 0,
            "edges": 0,
            "last_refresh": "",
            "graph_version": GRAPH_VERSION,
            "last_error": error,
            "vector": self.vector_store.status(),
        }

    def _safe_error(self, error: Exception) -> str:
        message = f"{type(error).__name__}: {error}"
        if self.config.neo4j_password:
            message = message.replace(
                self.config.neo4j_password,
                "[redacted]",
            )
        return message[:2_000]

    def _relative_path(self, path: str) -> str:
        resolved = self.workspace.resolve(path)
        return resolved.relative_to(self.workspace.root).as_posix()


def profile_to_dict(profile: Optional[FileProfile]) -> Optional[Dict[str, Any]]:
    return (
        {"citation": profile.citation, **asdict(profile)}
        if profile is not None
        else None
    )


def _profile_node_payload(profile: FileProfile) -> Dict[str, Any]:
    return {
        "node_key": f"file:{profile.path}",
        "name": Path(profile.path).name,
        "path": profile.path,
        "purpose": profile.purpose,
        "content_hash": profile.content_hash,
        "properties_json": json.dumps(
            {"is_test": _is_test_path(profile.path)},
            ensure_ascii=False,
        ),
        "language": profile.language,
        "line_count": profile.line_count,
        "responsibilities": profile.responsibilities,
        "responsibilities_text": "\n".join(profile.responsibilities),
        "imports": profile.imports,
        "related_tests": profile.related_tests,
        "confidence": profile.confidence,
        "evidence": profile.evidence,
        "public_symbols_json": json.dumps(
            profile.public_symbols,
            ensure_ascii=False,
        ),
        "profile_version": profile.profile_version,
        "updated_at": profile.updated_at,
    }


def _graph_payload(
    records: Sequence[Dict[str, Any]],
    profiles: Sequence[FileProfile],
    workspace_path: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_path = {profile.path: profile for profile in profiles}
    aliases = _module_aliases(records)
    nodes = [
        {
            "node_key": "workspace:.",
            "node_type": "Workspace",
            "name": Path(workspace_path).name,
            "path": "",
            "purpose": "",
            "content_hash": "",
            "properties_json": json.dumps(
                {"workspace_path": workspace_path},
                ensure_ascii=False,
            ),
            **_empty_profile_properties(),
        }
    ]
    edges: List[Dict[str, Any]] = []
    modules: Set[str] = set()
    related_tests = _related_test_map(records)
    for record in records:
        path = record["path"]
        profile = by_path[path]
        file_key = f"file:{path}"
        nodes.append(
            {
                "node_type": "File",
                **_profile_node_payload(profile),
            }
        )
        edges.append(_edge("workspace:.", file_key, "CONTAINS", {"path": path}))
        for symbol in record["symbols"]:
            symbol_key = f"symbol:{path}:{symbol['name']}:{symbol['line']}"
            nodes.append(
                {
                    "node_key": symbol_key,
                    "node_type": "Symbol",
                    "name": symbol["name"],
                    "path": path,
                    "purpose": symbol["signature"],
                    "content_hash": record["content_hash"],
                    "properties_json": json.dumps(
                        {
                            "kind": symbol["kind"],
                            "line": symbol["line"],
                        },
                        ensure_ascii=False,
                    ),
                    **_empty_profile_properties(),
                }
            )
            edges.append(
                _edge(
                    file_key,
                    symbol_key,
                    "DEFINES",
                    {"line": symbol["line"]},
                )
            )
        for imported in record["imports"]:
            target = imported["target"]
            modules.add(target)
            edges.append(
                _edge(
                    file_key,
                    f"module:{target}",
                    "IMPORTS",
                    {"line": imported["line"]},
                )
            )
            resolved = _resolve_import(path, target, aliases)
            if resolved:
                edges.append(
                    _edge(
                        file_key,
                        f"file:{resolved}",
                        "DEPENDS_ON",
                        {"target": target, "line": imported["line"]},
                    )
                )
        for test_path in related_tests.get(path, []):
            edges.append(
                _edge(
                    f"file:{test_path}",
                    file_key,
                    "TESTS",
                    {"inferred": True},
                )
            )
    for target in sorted(modules):
        nodes.append(
            {
                "node_key": f"module:{target}",
                "node_type": "Module",
                "name": target,
                "path": "",
                "purpose": "",
                "content_hash": "",
                "properties_json": json.dumps(
                    {"import_target": target},
                    ensure_ascii=False,
                ),
                **_empty_profile_properties(),
            }
        )
    return nodes, edges


def _empty_profile_properties() -> Dict[str, Any]:
    return {
        "language": "",
        "line_count": 0,
        "responsibilities": [],
        "responsibilities_text": "",
        "imports": [],
        "related_tests": [],
        "confidence": 0.0,
        "evidence": [],
        "public_symbols_json": "[]",
        "profile_version": 0,
        "updated_at": "",
    }


def _edge(
    source: str,
    target: str,
    kind: str,
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "source_key": source,
        "target_key": target,
        "edge_type": kind,
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
    }


def _profile_from_node(node: Dict[str, Any]) -> FileProfile:
    return FileProfile(
        path=str(node.get("path", "")),
        content_hash=str(node.get("content_hash", "")),
        language=str(node.get("language", "")),
        line_count=int(node.get("line_count", 0)),
        purpose=str(node.get("purpose", "")),
        responsibilities=list(node.get("responsibilities") or []),
        public_symbols=json.loads(node.get("public_symbols_json") or "[]"),
        imports=list(node.get("imports") or []),
        related_tests=list(node.get("related_tests") or []),
        confidence=float(node.get("confidence", 0.0)),
        evidence=list(node.get("evidence") or []),
        stale=bool(node.get("stale", False)),
        profile_version=int(node.get("profile_version", 0)),
        updated_at=str(node.get("updated_at", "")),
    )


def _node_dict(node: Any) -> Dict[str, Any]:
    return dict(node.items()) if hasattr(node, "items") else dict(node)


def _public_node(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "node_key": node.get("node_key", ""),
        "node_type": node.get("node_type", ""),
        "name": node.get("name", ""),
        "path": node.get("path", ""),
        "purpose": node.get("purpose", ""),
        "properties": json.loads(node.get("properties_json") or "{}"),
    }


def _public_edge(edge: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source_key": edge.get("source_key", ""),
        "target_key": edge.get("target_key", ""),
        "edge_type": edge.get("edge_type", ""),
        "evidence": json.loads(edge.get("evidence_json") or "{}"),
    }


def _string_list(value: Any, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()[:500]
        for item in value
        if str(item).strip()
    ][:limit]


def _confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.7


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def _lucene_query(text: str) -> str:
    terms = re.findall(
        r"[A-Za-z_][A-Za-z0-9_.$:/-]{1,127}|[\u3400-\u9fff]+",
        text,
    )
    return " OR ".join(
        '"' + term.replace("\\", "\\\\").replace('"', '\\"') + '"'
        for term in terms[:64]
    )


def _related_test_map(
    records: Sequence[Dict[str, Any]],
) -> Dict[str, List[str]]:
    sources: Dict[str, List[str]] = {}
    tests = []
    for record in records:
        path = record["path"]
        if _is_test_path(path):
            tests.append(path)
        else:
            sources.setdefault(Path(path).stem, []).append(path)
    result: Dict[str, List[str]] = {}
    for test in tests:
        stem = Path(test).stem.removeprefix("test_")
        for source in sources.get(stem, []):
            result.setdefault(source, []).append(test)
    return {
        path: sorted(set(paths)) for path, paths in result.items()
    }


def _module_aliases(
    records: Sequence[Dict[str, Any]],
) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for record in records:
        path = record["path"]
        file_path = Path(path)
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
        for candidate in candidates:
            aliases.setdefault(candidate, path)
    return aliases


def _resolve_import(
    importer: str,
    target: str,
    aliases: Dict[str, str],
) -> str:
    normalized = target.strip().strip("\"'")
    if normalized.startswith("."):
        importer_path = Path(importer)
        dotted = ".".join(
            [
                *importer_path.parent.parts,
                normalized.lstrip("."),
            ]
        ).strip(".")
        return aliases.get(dotted, aliases.get(normalized.lstrip("."), ""))
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
