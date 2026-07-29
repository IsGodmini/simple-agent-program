import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from simple_agent.tools import (
    FindFilesTool,
    ListFilesTool,
    ReadFileTool,
    RepositoryMapTool,
    SearchCodeTool,
)
from simple_agent.workspace import Workspace


class LargeRepositoryToolTests(unittest.TestCase):
    def test_list_files_supports_depth_filtering_and_stable_pagination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "deep").mkdir(parents=True)
            (root / "node_modules").mkdir()
            (root / "README.md").write_text("project\n", encoding="utf-8")
            (root / "src" / "app.py").write_text("app\n", encoding="utf-8")
            (root / "src" / "deep" / "model.py").write_text(
                "model\n", encoding="utf-8"
            )
            (root / "node_modules" / "dependency.js").write_text(
                "generated\n", encoding="utf-8"
            )
            tool = ListFilesTool(Workspace(root))

            first_page = tool.execute(
                {"path": ".", "max_depth": 2, "offset": 0, "limit": 2}
            )
            second_page = tool.execute(
                {"path": ".", "max_depth": 2, "offset": 2, "limit": 2}
            )

            self.assertIn("下一次使用 offset=2", first_page)
            self.assertNotEqual(first_page, second_page)
            combined = f"{first_page}\n{second_page}"
            self.assertIn("README.md", combined)
            self.assertIn("src/app.py", combined)
            self.assertIn("src/deep/", combined)
            self.assertNotIn("src/deep/model.py", combined)
            self.assertNotIn("node_modules", combined)

    def test_find_files_locates_paths_without_reading_contents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "service.py").write_text("", encoding="utf-8")
            (root / "tests" / "test_service.py").write_text(
                "secret test contents\n", encoding="utf-8"
            )

            result = FindFilesTool(Workspace(root)).execute(
                {"pattern": "test*.py"}
            )

            self.assertIn("tests/test_service.py", result)
            self.assertNotIn("src/service.py", result)
            self.assertNotIn("secret test contents", result)

    def test_read_file_supports_bounded_line_ranges(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lines = "".join(f"line {number}\n" for number in range(1, 1001))
            (root / "large.txt").write_text(lines, encoding="utf-8")

            result = ReadFileTool(Workspace(root)).execute(
                {"path": "large.txt", "start_line": 501, "max_lines": 3}
            )

            self.assertIn("501 | line 501", result)
            self.assertIn("503 | line 503", result)
            self.assertNotIn("500 | line 500", result)
            self.assertNotIn("504 | line 504", result)
            self.assertIn("start_line=504", result)

    def test_read_file_rejects_binary_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "asset.bin").write_bytes(b"text\x00binary")

            with self.assertRaisesRegex(ValueError, "binary files"):
                ReadFileTool(Workspace(root)).execute({"path": "asset.bin"})

    def test_search_code_supports_globs_regex_and_pagination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "one.py").write_text(
                "def target_one():\n    return 1\n", encoding="utf-8"
            )
            (root / "src" / "two.py").write_text(
                "def target_two():\n    return 2\n", encoding="utf-8"
            )
            (root / "src" / "ignored.js").write_text(
                "function target_three() {}\n", encoding="utf-8"
            )
            tool = SearchCodeTool(Workspace(root), use_ripgrep=False)

            first = tool.execute(
                {
                    "query": r"def target_\w+",
                    "regex": True,
                    "glob": "*.py",
                    "limit": 1,
                }
            )
            second = tool.execute(
                {
                    "query": r"def target_\w+",
                    "regex": True,
                    "glob": "*.py",
                    "offset": 1,
                    "limit": 1,
                }
            )

            self.assertIn("src/one.py:1", first)
            self.assertIn("下一次使用 offset=1", first)
            self.assertIn("src/two.py:1", second)
            self.assertNotIn("ignored.js", first + second)

    def test_search_code_reports_invalid_regular_expression(self):
        with TemporaryDirectory() as directory:
            tool = SearchCodeTool(
                Workspace(Path(directory)), use_ripgrep=False
            )

            with self.assertRaisesRegex(ValueError, "invalid regular expression"):
                tool.execute({"query": "[", "regex": True})

    def test_repository_map_summarizes_project_shape(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "pyproject.toml").write_text(
                "[project]\nname='example'\n", encoding="utf-8"
            )
            (root / "src" / "main.py").write_text(
                "def main(): pass\n", encoding="utf-8"
            )
            (root / "src" / "service.py").write_text("", encoding="utf-8")
            (root / "tests" / "test_service.py").write_text(
                "", encoding="utf-8"
            )

            result = json.loads(
                RepositoryMapTool(Workspace(root)).execute({"path": "."})
            )

            self.assertEqual(result["files_scanned"], 4)
            self.assertIn("pyproject.toml", result["manifests"])
            self.assertIn("src/main.py", result["possible_entrypoints"])
            self.assertIn([".py", 3], result["top_file_extensions"])
            self.assertIn(["src", 2], result["top_level_file_counts"])


if __name__ == "__main__":
    unittest.main()
