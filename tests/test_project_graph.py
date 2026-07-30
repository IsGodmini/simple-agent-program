from tests import _TEST_STORAGE_HOME  # noqa: F401

import json
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from simple_agent.cli import _handle_graph_actions
from simple_agent.project_graph import (
    FileProfile,
    LLMFileProfileGenerator,
    Neo4jProjectStore,
    ProjectGraph,
    ProjectGraphConfig,
)
from simple_agent.project_index import ProjectIndex
from simple_agent.vector_store import ChromaVectorStore
from simple_agent.tools import (
    FileProfileTool,
    ImpactAnalysisTool,
    ProjectGraphOverviewTool,
    QueryFileProfilesTool,
    QueryProjectGraphTool,
    RefreshProjectGraphTool,
)
from simple_agent.workspace import Workspace


class FakeProfileGenerator:
    def __init__(self):
        self.calls = []

    def generate(self, records, related_tests, generated_at):
        self.calls.append([record["path"] for record in records])
        return [
            FileProfile(
                path=record["path"],
                content_hash=record["content_hash"],
                language=record["language"],
                line_count=record["line_count"],
                purpose=(
                    "LLM 分析：处理用户认证与登录策略。"
                    if record["path"] == "src/auth.py"
                    else f"LLM 分析：实现 {record['path']} 的项目职责。"
                ),
                responsibilities=["由 LLM 识别该文件的核心职责"],
                public_symbols=record["symbols"],
                imports=[
                    item["target"] for item in record["imports"]
                ],
                related_tests=related_tests.get(record["path"], []),
                confidence=0.91,
                evidence=[f"{record['path']}#L1"],
                stale=False,
                profile_version=2,
                updated_at=generated_at,
            )
            for record in records
        ]


class InMemoryNeo4jStore:
    def __init__(self):
        self.profiles = {}
        self.records = []
        self.synced = 0
        self.stale_paths = []

    def ensure_schema(self):
        return None

    def fetch_profiles(self, workspace_id):
        return dict(self.profiles)

    def stage_profiles(self, workspace_id, profiles):
        self.profiles.update(
            {
                profile.path: replace(profile, stale=False)
                for profile in profiles
            }
        )

    def sync_snapshot(
        self,
        workspace_id,
        workspace_path,
        records,
        profiles,
        updated_at,
    ):
        self.records = list(records)
        self.profiles = {
            profile.path: replace(profile, stale=False)
            for profile in profiles
        }
        self.synced += 1

    def status(self, workspace_id):
        symbols = sum(len(record["symbols"]) for record in self.records)
        imports = sum(len(record["imports"]) for record in self.records)
        tests = sum(
            len(profile.related_tests) for profile in self.profiles.values()
        )
        nodes = 1 + len(self.records) + symbols + imports
        return {
            "ready": bool(self.records),
            "backend": "neo4j",
            "workspace_id": workspace_id,
            "profiles": len(self.profiles),
            "nodes": nodes,
            "edges": len(self.records) + symbols + imports + tests,
            "last_refresh": "",
            "graph_version": 2,
            "last_error": "",
        }

    def overview(self, workspace_id, max_profiles):
        return {
            **self.status(workspace_id),
            "edge_types": [{"edge_type": "DEFINES", "count": 3}],
            "representative_files": [
                {
                    "path": profile.path,
                    "language": profile.language,
                    "purpose": profile.purpose,
                    "confidence": profile.confidence,
                    "stale": False,
                }
                for profile in list(self.profiles.values())[:max_profiles]
            ],
        }

    def get_profile(self, workspace_id, path):
        return self.profiles.get(path)

    def mark_profiles_stale(self, workspace_id, paths):
        self.stale_paths.extend(paths)
        for path in paths:
            if path in self.profiles:
                self.profiles[path] = replace(
                    self.profiles[path],
                    stale=True,
                )
        return sum(path in self.profiles for path in paths)

    def search_profiles(self, workspace_id, query, limit):
        terms = [
            term.strip('"')
            for term in query.lower().replace(" or ", " ").split()
        ]
        return [
            profile
            for profile in self.profiles.values()
            if any(
                term in (profile.path + " " + profile.purpose).lower()
                for term in terms
            )
        ][:limit]

    def neighbors(self, workspace_id, path, depth, limit):
        nodes = [
            {
                "node_key": f"file:{item.path}",
                "node_type": "File",
                "name": Path(item.path).name,
                "path": item.path,
                "purpose": item.purpose,
                "properties": {},
            }
            for item in self.profiles.values()
        ][:limit]
        edges = []
        if path == "src/auth.py":
            edges.extend(
                [
                    {
                        "source_key": "file:src/auth.py",
                        "target_key": "file:src/service.py",
                        "edge_type": "DEPENDS_ON",
                        "evidence": {"line": 2},
                    },
                    {
                        "source_key": "file:tests/test_auth.py",
                        "target_key": "file:src/auth.py",
                        "edge_type": "TESTS",
                        "evidence": {"inferred": True},
                    },
                ]
            )
        return {
            "path": path,
            "depth": depth,
            "nodes": nodes,
            "edges": edges,
        }


class ProjectGraphTests(unittest.TestCase):
    def _project(self, root: Path):
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "src" / "auth.py").write_text(
            '"""Authenticate users."""\n'
            "from .service import check_password\n"
            "class AuthService:\n    pass\n",
            encoding="utf-8",
        )
        (root / "src" / "service.py").write_text(
            "def check_password():\n    return True\n",
            encoding="utf-8",
        )
        (root / "tests" / "test_auth.py").write_text(
            "from src.auth import AuthService\n",
            encoding="utf-8",
        )
        workspace = Workspace(root)
        vector_store = ChromaVectorStore(workspace)
        store = InMemoryNeo4jStore()
        graph = ProjectGraph(
            workspace,
            ProjectIndex(workspace, vector_store=vector_store),
            ProjectGraphConfig(
                neo4j_uri="neo4j://test",
                neo4j_username="neo4j",
                neo4j_password="secret",
            ),
            profile_generator=FakeProfileGenerator(),
            vector_store=vector_store,
            store=store,
        )
        return graph, store

    def test_builds_llm_profiles_and_neo4j_relationship_graph(self):
        with TemporaryDirectory() as directory:
            graph, store = self._project(Path(directory))

            result = graph.refresh()
            profile = graph.get_profile("src/auth.py")
            impact = graph.impact_analysis("src/auth.py")

            self.assertEqual(result.updated_profiles, 3)
            self.assertEqual(result.backend, "neo4j")
            self.assertTrue(result.neo4j_synced)
            self.assertEqual(store.synced, 1)
            self.assertIn("LLM 分析", profile.purpose)
            self.assertIn("tests/test_auth.py", impact["related_tests"])

    def test_unchanged_files_do_not_regenerate_profiles(self):
        with TemporaryDirectory() as directory:
            graph, store = self._project(Path(directory))
            graph.refresh()

            result = graph.refresh()

            self.assertEqual(result.updated_profiles, 0)
            self.assertEqual(result.unchanged_profiles, 3)
            self.assertEqual(store.synced, 1)

    def test_changed_and_deleted_files_update_neo4j_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph, store = self._project(root)
            graph.refresh()
            (root / "src" / "service.py").write_text(
                "def check_password():\n    return False\n",
                encoding="utf-8",
            )

            changed = graph.refresh(["src/service.py"])
            (root / "src" / "auth.py").unlink()
            deleted = graph.refresh(["src/auth.py"])

            self.assertEqual(changed.updated_profiles, 1)
            self.assertEqual(deleted.deleted_profiles, 1)
            self.assertNotIn("src/auth.py", store.profiles)

    def test_source_change_defers_llm_profile_generation_until_batch_refresh(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph, store = self._project(root)
            graph.refresh()
            generator = graph.profile_generator
            initial_calls = len(generator.calls)
            (root / "src" / "auth.py").write_text(
                "def login(token):\n    return bool(token)\n",
                encoding="utf-8",
            )

            graph.record_source_change("src/auth.py")

            self.assertEqual(len(generator.calls), initial_calls)
            self.assertEqual(store.stale_paths, ["src/auth.py"])

            graph.refresh(["src/auth.py"])

            self.assertEqual(len(generator.calls), initial_calls + 1)
            self.assertEqual(generator.calls[-1], ["src/auth.py"])

    def test_stale_profile_with_same_hash_is_cleared_without_llm(self):
        with TemporaryDirectory() as directory:
            graph, store = self._project(Path(directory))
            graph.refresh()
            generator = graph.profile_generator
            initial_calls = len(generator.calls)

            graph.record_source_change("src/auth.py")
            result = graph.refresh(["src/auth.py"])

            self.assertEqual(len(generator.calls), initial_calls)
            self.assertEqual(result.updated_profiles, 0)
            self.assertFalse(store.profiles["src/auth.py"].stale)

    def test_graph_tools_use_neo4j_results(self):
        with TemporaryDirectory() as directory:
            graph, _ = self._project(Path(directory))
            refresh = json.loads(RefreshProjectGraphTool(graph).execute({}))
            overview = json.loads(
                ProjectGraphOverviewTool(graph).execute(
                    {"max_profiles": 2}
                )
            )
            profiles = json.loads(
                QueryFileProfilesTool(graph).execute(
                    {"query": "auth", "limit": 2}
                )
            )
            profile = json.loads(
                FileProfileTool(graph).execute({"path": "src/auth.py"})
            )
            relations = json.loads(
                QueryProjectGraphTool(graph).execute(
                    {"path": "src/auth.py", "depth": 1, "limit": 20}
                )
            )
            impact = json.loads(
                ImpactAnalysisTool(graph).execute(
                    {"path": "src/auth.py", "depth": 2, "limit": 20}
                )
            )

            self.assertEqual(refresh["updated_profiles"], 3)
            self.assertEqual(len(overview["representative_files"]), 2)
            self.assertEqual(profiles[0]["path"], "src/auth.py")
            self.assertEqual(profile["citation"], "graph:src/auth.py")
            self.assertTrue(relations["edges"])
            self.assertIn("tests/test_auth.py", impact["related_tests"])

    def test_cli_reports_neo4j_status(self):
        with TemporaryDirectory() as directory:
            graph, _ = self._project(Path(directory))

            output = _handle_graph_actions(
                Namespace(refresh_graph=True, graph_status=True),
                graph,
            )

            self.assertIn("项目图谱已增量刷新", output)
            self.assertIn('"backend": "neo4j"', output)
            self.assertIn('"profiles": 3', output)

    def test_missing_neo4j_configuration_has_no_sqlite_fallback(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            graph = ProjectGraph(
                workspace,
                ProjectIndex(workspace),
                ProjectGraphConfig(),
            )

            result = graph.refresh()
            status = graph.status()

            self.assertFalse(result.neo4j_synced)
            self.assertEqual(status["storage"], "neo4j-only")
            self.assertEqual(status["backend"], "neo4j")
            self.assertNotIn("fallback", json.dumps(status))
            self.assertFalse(
                (workspace.root / ".simple-agent" / "graph").exists()
            )


class LLMProfileGeneratorTests(unittest.TestCase):
    def test_generates_purpose_and_responsibilities_from_model_json(self):
        class FakeModel:
            def complete(self, messages, tools=None):
                return SimpleNamespace(
                    content=json.dumps(
                        [
                            {
                                "path": "app.py",
                                "purpose": "启动 HTTP 服务并装配路由。",
                                "responsibilities": ["创建应用", "注册路由"],
                                "confidence": 0.95,
                                "evidence": ["app.py#L1"],
                            }
                        ],
                        ensure_ascii=False,
                    )
                )

        profiles = LLMFileProfileGenerator(FakeModel()).generate(
            [
                {
                    "path": "app.py",
                    "content_hash": "hash",
                    "language": "Python",
                    "line_count": 10,
                    "symbols": [],
                    "imports": [],
                    "leading_content": "create_app()",
                }
            ],
            {},
            "now",
        )

        self.assertEqual(profiles[0].purpose, "启动 HTTP 服务并装配路由。")
        self.assertEqual(profiles[0].profile_version, 2)

    def test_retries_only_incomplete_batch_items_as_single_files(self):
        class FakeModel:
            def __init__(self):
                self.paths = []

            def complete(self, messages, tools=None):
                records = json.loads(messages[-1]["content"])
                self.paths.append([record["path"] for record in records])
                if len(records) == 2:
                    result = [
                        {
                            "path": "app.py",
                            "purpose": "启动应用。",
                            "responsibilities": ["创建应用"],
                        },
                        {
                            "path": "verify.py",
                            "purpose": "",
                            "responsibilities": [],
                        },
                    ]
                else:
                    result = [
                        {
                            "path": records[0]["path"],
                            "purpose": "验证服务启动状态。",
                            "responsibilities": ["执行启动检查"],
                        }
                    ]
                return SimpleNamespace(content=json.dumps(result))

        model = FakeModel()
        records = [
            {
                "path": path,
                "content_hash": path,
                "language": "Python",
                "line_count": 10,
                "symbols": [],
                "imports": [],
                "leading_content": "pass",
            }
            for path in ("app.py", "verify.py")
        ]

        profiles = LLMFileProfileGenerator(model).generate(records, {}, "now")

        self.assertEqual(model.paths, [["app.py", "verify.py"], ["verify.py"]])
        self.assertEqual(
            [profile.path for profile in profiles],
            ["app.py", "verify.py"],
        )


class Neo4jStoreTests(unittest.TestCase):
    def test_uses_constraints_fulltext_and_real_relationship_types(self):
        class FakeDriver:
            def __init__(self):
                self.calls = []

            def verify_connectivity(self):
                return None

            def execute_query(self, query, **kwargs):
                self.calls.append((query, kwargs))
                return ([], None, [])

            def session(self, database):
                driver = self

                class Result:
                    def consume(self):
                        return None

                class Transaction:
                    def run(self, query, **parameters):
                        driver.calls.append(
                            (query, {"parameters_": parameters})
                        )
                        return Result()

                    def commit(self):
                        return None

                    def rollback(self):
                        return None

                class Session:
                    def begin_transaction(self):
                        return Transaction()

                    def close(self):
                        return None

                return Session()

            def close(self):
                return None

        driver = FakeDriver()
        store = Neo4jProjectStore(
            ProjectGraphConfig(
                neo4j_uri="neo4j://test",
                neo4j_username="neo4j",
                neo4j_password="secret",
            ),
            driver_factory=lambda uri, auth: driver,
        )
        store.ensure_schema()
        profile = FileProfile(
            path="app.py",
            content_hash="hash",
            language="Python",
            line_count=1,
            purpose="入口",
            responsibilities=["启动"],
            public_symbols=[],
            imports=[],
            related_tests=[],
            confidence=0.9,
            evidence=["app.py#L1"],
            stale=False,
            profile_version=2,
            updated_at="now",
        )
        store.sync_snapshot(
            "workspace",
            "/workspace",
            [
                {
                    "path": "app.py",
                    "content_hash": "hash",
                    "language": "Python",
                    "line_count": 1,
                    "symbols": [
                        {
                            "name": "main",
                            "kind": "function",
                            "line": 1,
                            "signature": "def main():",
                        }
                    ],
                    "imports": [],
                }
            ],
            [profile],
            "now",
        )

        queries = "\n".join(call[0] for call in driver.calls)
        self.assertIn("CREATE CONSTRAINT", queries)
        self.assertIn("CREATE FULLTEXT INDEX", queries)
        self.assertIn("CREATE (source)-[r:CONTAINS]", queries)
        self.assertIn("CREATE (source)-[r:DEFINES]", queries)
        self.assertIn("SET n:ProjectSymbol", queries)
        self.assertIn("SET n:ProjectModule", queries)
        self.assertNotIn("secret", str(driver.calls))


if __name__ == "__main__":
    unittest.main()
