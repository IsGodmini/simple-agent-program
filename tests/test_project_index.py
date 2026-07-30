import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simple_agent.cli import _handle_index_actions
from simple_agent.memory import ContextBuilder, ProjectMemoryStore
from simple_agent.project_index import ProjectIndex
from simple_agent.tools import (
    ApplyPatchTool,
    DependencyGraphTool,
    FindReferencesTool,
    ProjectOverviewTool,
    QueryProjectIndexTool,
    SearchSymbolsTool,
)
from simple_agent.workspace import Workspace


class ProjectIndexTests(unittest.TestCase):
    def test_builds_map_symbols_imports_and_code_search(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "node_modules").mkdir()
            (root / "example.egg-info").mkdir()
            (root / "README.md").write_text(
                "# Example service\n", encoding="utf-8"
            )
            (root / "src" / "auth.py").write_text(
                "from .tokens import verify_token\n\n"
                "class AuthService:\n"
                "    def authenticate(self, token):\n"
                "        return verify_token(token)\n",
                encoding="utf-8",
            )
            (root / "src" / "tokens.py").write_text(
                "def verify_token(token):\n"
                "    return token == 'valid'\n",
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "SECRET=must-not-index\n", encoding="utf-8"
            )
            (root / "node_modules" / "secret.js").write_text(
                "const dependencySecret = true;\n", encoding="utf-8"
            )
            (root / "example.egg-info" / "dependency_links.txt").write_text(
                "\n", encoding="utf-8"
            )
            index = ProjectIndex(Workspace(root))

            refreshed = index.refresh()

            self.assertEqual(refreshed.indexed_files, 3)
            self.assertEqual(index.status()["files"], 3)
            overview = index.overview()
            self.assertIn("src/auth.py", overview["tree"])
            self.assertEqual(overview["modules"][0]["path"], "src")
            self.assertEqual(overview["modules"][0]["symbols"], 3)
            self.assertNotIn(".env", json.dumps(overview))
            self.assertNotIn("node_modules", json.dumps(overview))
            self.assertNotIn("egg-info", json.dumps(overview))

            hits = index.search("authenticate token")
            self.assertTrue(any(hit.path == "src/auth.py" for hit in hits))
            symbols = index.search_symbols("AuthService")
            self.assertEqual(symbols[0].kind, "class")
            self.assertEqual(symbols[0].line, 3)
            imports = index.list_imports("src/auth.py")
            self.assertEqual(imports[0]["target"], ".tokens")
            references = index.find_references("verify_token")
            self.assertTrue(
                any(
                    item["path"] == "src/auth.py" and item["line"] == 5
                    for item in references
                )
            )

    def test_incremental_refresh_does_not_reread_unchanged_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "service.py"
            unchanged = root / "stable.py"
            source.write_text("def old_name():\n    return 1\n", encoding="utf-8")
            unchanged.write_text(
                "def stable_name():\n    return 2\n", encoding="utf-8"
            )
            index = ProjectIndex(Workspace(root))
            index.refresh()
            map_mtime = index.map_path.stat().st_mtime_ns

            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("unchanged files were reread"),
            ):
                second = index.refresh()
            self.assertEqual(second.indexed_files, 0)
            self.assertEqual(second.unchanged_files, 2)
            self.assertEqual(index.map_path.stat().st_mtime_ns, map_mtime)

            source.write_text(
                "def new_name():\n    return 3\n",
                encoding="utf-8",
            )
            changed = index.refresh()
            self.assertEqual(changed.indexed_files, 1)
            self.assertEqual(changed.unchanged_files, 1)
            self.assertTrue(index.search_symbols("new_name"))
            self.assertFalse(index.search_symbols("old_name"))

            unchanged.unlink()
            deleted = index.refresh()
            self.assertEqual(deleted.deleted_files, 1)
            self.assertFalse(index.search_symbols("stable_name"))

    def test_large_project_only_reindexes_changed_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "src"
            source_root.mkdir()
            for number in range(300):
                (source_root / f"module_{number}.py").write_text(
                    f"def function_{number}():\n    return {number}\n",
                    encoding="utf-8",
                )
            index = ProjectIndex(Workspace(root))

            first = index.refresh()
            second = index.refresh()
            (source_root / "module_177.py").write_text(
                "def function_177():\n    return 'changed-only'\n",
                encoding="utf-8",
            )
            third = index.refresh()

            self.assertEqual(first.indexed_files, 300)
            self.assertEqual(second.indexed_files, 0)
            self.assertEqual(second.unchanged_files, 300)
            self.assertEqual(third.indexed_files, 1)
            self.assertEqual(third.unchanged_files, 299)
            self.assertEqual(index.search("changed-only")[0].path, "src/module_177.py")

    def test_rejects_symlinked_index_storage(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            root = Path(directory)
            internal = root / ".simple-agent"
            internal.mkdir()
            (internal / "index").symlink_to(Path(outside), target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symbolic links"):
                ProjectIndex(Workspace(root)).refresh()

    def test_apply_patch_immediately_refreshes_changed_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "service.py"
            source.write_text(
                "def version():\n    return 'old'\n",
                encoding="utf-8",
            )
            index = ProjectIndex(Workspace(root))
            index.refresh()
            tool = ApplyPatchTool(
                Workspace(root),
                on_change=lambda path: index.refresh([path]),
            )

            tool.execute(
                {
                    "path": "service.py",
                    "mode": "replace",
                    "old_text": "return 'old'",
                    "new_text": "return 'incremental-index'",
                }
            )

            hits = index.search("incremental-index")
            self.assertEqual(hits[0].path, "service.py")

    def test_context_uses_project_map_and_relevant_chunks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "billing.py").write_text(
                "def calculate_invoice_total(items):\n"
                "    return sum(items)\n",
                encoding="utf-8",
            )
            workspace = Workspace(root)
            store = ProjectMemoryStore(workspace)
            store.ensure_session()
            index = ProjectIndex(workspace)
            context = ContextBuilder(store, project_index=index).build(
                "修改 invoice total 计算"
            )

            rendered = "\n".join(
                str(message.get("content")) for message in context.messages
            )
            self.assertIn("<project_index_json>", rendered)
            self.assertIn("billing.py", rendered)
            self.assertTrue(context.project_index_citations)


class ProjectIndexToolTests(unittest.TestCase):
    def test_tools_query_cached_index_without_refreshing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text(
                "def health_check():\n    return {'ok': True}\n",
                encoding="utf-8",
            )
            index = ProjectIndex(Workspace(root))
            index.refresh()

            overview = json.loads(ProjectOverviewTool(index).execute({}))
            query = json.loads(
                QueryProjectIndexTool(index).execute({"query": "health check"})
            )
            symbols = json.loads(
                SearchSymbolsTool(index).execute({"query": "health_check"})
            )
            references = json.loads(
                FindReferencesTool(index).execute({"symbol": "health_check"})
            )
            dependencies = DependencyGraphTool(index).execute({})

            self.assertEqual(overview["indexed_files"], 1)
            self.assertEqual(query[0]["path"], "app.py")
            self.assertEqual(symbols[0]["name"], "health_check")
            self.assertEqual(references[0]["line"], 1)
            self.assertEqual(dependencies, "索引中没有找到匹配的依赖关系。")

    def test_cli_can_refresh_and_report_index_status(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text(
                "def main():\n    return 0\n",
                encoding="utf-8",
            )
            index = ProjectIndex(Workspace(root))
            args = type(
                "Args",
                (),
                {"refresh_index": True, "index_status": True},
            )()

            output = _handle_index_actions(args, index)

            self.assertIn("项目索引已增量刷新", output)
            self.assertIn("项目索引状态", output)
            self.assertIn('"indexed_files": 1', output)


if __name__ == "__main__":
    unittest.main()
