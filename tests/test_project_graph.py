import json
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simple_agent.project_graph import (
    Neo4jGraphMirror,
    ProjectGraph,
    ProjectGraphConfig,
)
from simple_agent.cli import _handle_graph_actions
from simple_agent.project_index import ProjectIndex
from simple_agent.tools import (
    FileProfileTool,
    ImpactAnalysisTool,
    ProjectGraphOverviewTool,
    QueryFileProfilesTool,
    QueryProjectGraphTool,
    RefreshProjectGraphTool,
)
from simple_agent.workspace import Workspace


class ProjectGraphTests(unittest.TestCase):
    def _project(self, root: Path) -> ProjectGraph:
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "src" / "auth.py").write_text(
            '"""Authenticate users and enforce login policy."""\n'
            "from .service import check_password\n\n"
            "class AuthService:\n"
            "    pass\n",
            encoding="utf-8",
        )
        (root / "src" / "service.py").write_text(
            '"""Provide password verification primitives."""\n\n'
            "def check_password():\n"
            "    return True\n",
            encoding="utf-8",
        )
        (root / "tests" / "test_auth.py").write_text(
            "from src.auth import AuthService\n\n"
            "def test_auth_service():\n"
            "    assert AuthService\n",
            encoding="utf-8",
        )
        workspace = Workspace(root)
        return ProjectGraph(
            workspace,
            ProjectIndex(workspace),
            ProjectGraphConfig(),
        )

    def test_builds_profiles_and_relationship_graph(self):
        with TemporaryDirectory() as directory:
            graph = self._project(Path(directory))

            result = graph.refresh()
            profile = graph.get_profile("src/auth.py")
            matches = graph.search_profiles("authenticate login")
            neighbors = graph.neighbors("src/auth.py", depth=1)
            impact = graph.impact_analysis("src/auth.py")

            self.assertEqual(result.updated_profiles, 3)
            self.assertGreaterEqual(result.nodes, 7)
            self.assertGreaterEqual(result.edges, 8)
            self.assertIn("Authenticate users", profile.purpose)
            self.assertEqual(
                profile.related_tests,
                ["tests/test_auth.py"],
            )
            self.assertEqual(matches[0].path, "src/auth.py")
            edge_types = {edge["edge_type"] for edge in neighbors["edges"]}
            self.assertIn("DEPENDS_ON", edge_types)
            self.assertIn("TESTS", edge_types)
            self.assertIn("tests/test_auth.py", impact["related_tests"])

    def test_unchanged_refresh_does_not_read_source_content(self):
        with TemporaryDirectory() as directory:
            graph = self._project(Path(directory))
            graph.refresh()
            original = Path.read_bytes

            def guarded(path):
                if str(path).endswith((".py", ".js", ".ts")):
                    raise AssertionError(f"unexpected reread: {path}")
                return original(path)

            with patch.object(Path, "read_bytes", guarded):
                result = graph.refresh()

            self.assertEqual(result.updated_profiles, 0)
            self.assertEqual(result.unchanged_profiles, 3)

    def test_changed_and_deleted_files_update_profiles(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph = self._project(root)
            graph.refresh()
            service = root / "src" / "service.py"
            service.write_text(
                '"""Verify passwords and lock compromised accounts."""\n\n'
                "def check_password():\n"
                "    return False\n",
                encoding="utf-8",
            )

            changed = graph.refresh(["src/service.py"])
            service_profile = graph.get_profile("src/service.py")
            (root / "src" / "auth.py").unlink()
            deleted = graph.refresh(["src/auth.py"])

            self.assertEqual(changed.updated_profiles, 1)
            self.assertIn("lock compromised", service_profile.purpose)
            self.assertEqual(deleted.deleted_profiles, 1)
            self.assertIsNone(graph.get_profile("src/auth.py"))

    def test_graph_tools_return_bounded_json_results(self):
        with TemporaryDirectory() as directory:
            graph = self._project(Path(directory))
            refresh = json.loads(
                RefreshProjectGraphTool(graph).execute({})
            )
            overview = json.loads(
                ProjectGraphOverviewTool(graph).execute(
                    {"max_profiles": 2}
                )
            )
            profiles = json.loads(
                QueryFileProfilesTool(graph).execute(
                    {"query": "login", "limit": 2}
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
            self.assertLessEqual(len(relations["nodes"]), 20)
            self.assertIn("tests/test_auth.py", impact["related_tests"])

    def test_cli_refreshes_and_reports_graph_status(self):
        with TemporaryDirectory() as directory:
            graph = self._project(Path(directory))

            output = _handle_graph_actions(
                Namespace(refresh_graph=True, graph_status=True),
                graph,
            )

            self.assertIn("项目图谱已增量刷新", output)
            self.assertIn("项目图谱状态", output)
            self.assertIn('"profiles": 3', output)

    def test_graph_storage_rejects_symbolic_links(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            state = root / ".simple-agent"
            state.mkdir()
            (state / "graph").symlink_to(outside, target_is_directory=True)
            workspace = Workspace(root)
            graph = ProjectGraph(
                workspace,
                ProjectIndex(workspace),
                ProjectGraphConfig(),
            )

            with self.assertRaisesRegex(ValueError, "symbolic links"):
                graph.status()


class Neo4jMirrorTests(unittest.TestCase):
    def test_default_backend_falls_back_when_neo4j_is_not_configured(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            graph = ProjectGraph(
                workspace,
                ProjectIndex(workspace),
                ProjectGraphConfig(),
            )

            status = graph.status()

            self.assertEqual(status["requested_backend"], "neo4j")
            self.assertEqual(status["backend"], "sqlite")
            self.assertTrue(status["fallback_active"])
            self.assertIn("NEO4J_URI", status["fallback_reason"])

    def test_explicit_sqlite_does_not_report_fallback(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            graph = ProjectGraph(
                workspace,
                ProjectIndex(workspace),
                ProjectGraphConfig(backend="sqlite"),
            )

            status = graph.status()

            self.assertEqual(status["backend"], "sqlite")
            self.assertFalse(status["fallback_active"])

    def test_mirror_uses_constraint_and_parameterized_snapshot(self):
        class FakeDriver:
            def __init__(self):
                self.calls = []
                self.verified = False
                self.closed = False

            def verify_connectivity(self):
                self.verified = True

            def execute_query(self, query, **kwargs):
                self.calls.append((query, kwargs))
                return [], None, []

            def close(self):
                self.closed = True

        driver = FakeDriver()
        created = {}

        def factory(uri, auth):
            created["uri"] = uri
            created["auth"] = auth
            return driver

        config = ProjectGraphConfig(
            backend="neo4j",
            neo4j_uri="neo4j://localhost:7687",
            neo4j_username="neo4j",
            neo4j_password="secret",
        )
        mirror = Neo4jGraphMirror(config, driver_factory=factory)
        mirror.sync_snapshot(
            "workspace-id",
            "/workspace",
            [
                {
                    "node_key": "file:app.py",
                    "node_type": "File",
                    "name": "app.py",
                    "path": "app.py",
                    "purpose": "entrypoint",
                    "content_hash": "hash",
                    "properties_json": json.dumps({}),
                }
            ],
            [],
        )
        mirror.close()

        self.assertEqual(created["auth"], ("neo4j", "secret"))
        self.assertTrue(driver.verified)
        self.assertEqual(len(driver.calls), 2)
        parameters = driver.calls[1][1]["parameters_"]
        self.assertEqual(parameters["workspace_id"], "workspace-id")
        self.assertEqual(parameters["nodes"][0]["path"], "app.py")
        self.assertNotIn("secret", str(driver.calls))
        self.assertTrue(driver.closed)

    def test_mirror_error_redacts_password_before_persistence(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            graph = ProjectGraph(
                workspace,
                ProjectIndex(workspace),
                ProjectGraphConfig(
                    backend="neo4j",
                    neo4j_uri="neo4j://localhost:7687",
                    neo4j_username="neo4j",
                    neo4j_password="top-secret",
                ),
            )

            message = graph._safe_mirror_error(
                RuntimeError("authentication failed for top-secret")
            )

            self.assertNotIn("top-secret", message)
            self.assertIn("[redacted]", message)

    def test_sync_failure_falls_back_and_next_refresh_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text(
                '"""Application entry point."""\n',
                encoding="utf-8",
            )
            workspace = Workspace(root)
            graph = ProjectGraph(
                workspace,
                ProjectIndex(workspace),
                ProjectGraphConfig(
                    backend="neo4j",
                    neo4j_uri="neo4j://localhost:7687",
                    neo4j_username="neo4j",
                    neo4j_password="secret",
                ),
            )

            with patch.object(
                graph,
                "_sync_neo4j",
                side_effect=RuntimeError("connection unavailable"),
            ):
                failed = graph.refresh()

            self.assertEqual(failed.backend, "sqlite")
            self.assertTrue(graph.status()["fallback_active"])
            self.assertIn(
                "connection unavailable",
                graph.status()["fallback_reason"],
            )

            with patch.object(graph, "_sync_neo4j") as sync:
                recovered = graph.refresh()

            sync.assert_called_once()
            self.assertEqual(recovered.backend, "neo4j")
            self.assertFalse(graph.status()["fallback_active"])
            self.assertTrue(recovered.neo4j_synced)


if __name__ == "__main__":
    unittest.main()
