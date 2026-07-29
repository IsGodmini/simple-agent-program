import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from simple_agent.agent import AgentResult, ToolExecution
from simple_agent.session import write_trace
from simple_agent.tools import ApplyPatchTool, ReadOnlyCommandTool, RunCommandTool
from simple_agent.workspace import Workspace


class ApplyPatchToolTests(unittest.TestCase):
    def test_create_and_replace_text(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tool = ApplyPatchTool(Workspace(root))

            created = tool.execute(
                {
                    "path": "src/app.py",
                    "mode": "create",
                    "new_text": "answer = 1\n",
                }
            )
            updated = tool.execute(
                {
                    "path": "src/app.py",
                    "mode": "replace",
                    "old_text": "answer = 1",
                    "new_text": "answer = 42",
                }
            )

            self.assertIn("Created", created)
            self.assertIn("Updated", updated)
            self.assertEqual(
                (root / "src" / "app.py").read_text(encoding="utf-8"),
                "answer = 42\n",
            )

    def test_replace_requires_unique_old_text(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "values.txt").write_text("same\nsame\n", encoding="utf-8")
            tool = ApplyPatchTool(Workspace(root))

            with self.assertRaisesRegex(ValueError, "exactly once"):
                tool.execute(
                    {
                        "path": "values.txt",
                        "mode": "replace",
                        "old_text": "same",
                        "new_text": "changed",
                    }
                )

    def test_sensitive_file_cannot_be_created(self):
        with TemporaryDirectory() as directory:
            tool = ApplyPatchTool(Workspace(Path(directory)))
            with self.assertRaisesRegex(ValueError, "sensitive path"):
                tool.execute(
                    {
                        "path": ".env",
                        "mode": "create",
                        "new_text": "SECRET=value",
                    }
                )


class RunCommandToolTests(unittest.TestCase):
    def test_allowlisted_python_module_runs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "valid.py").write_text("answer = 42\n", encoding="utf-8")
            tool = RunCommandTool(Workspace(root))

            result = tool.execute(
                {"command": ["python3", "-m", "compileall", "-q", "valid.py"]}
            )

            self.assertIn("Exit code: 0", result)

    def test_shell_and_arbitrary_python_are_rejected(self):
        with TemporaryDirectory() as directory:
            tool = RunCommandTool(Workspace(Path(directory)))

            with self.assertRaisesRegex(ValueError, "not allowlisted"):
                tool.execute({"command": ["sh", "-c", "echo unsafe"]})
            with self.assertRaisesRegex(ValueError, "must use"):
                tool.execute({"command": ["python3", "-c", "print('unsafe')"]})

    def test_destructive_git_subcommand_is_rejected(self):
        with TemporaryDirectory() as directory:
            tool = RunCommandTool(Workspace(Path(directory)))
            with self.assertRaisesRegex(ValueError, "subcommand"):
                tool.execute({"command": ["git", "reset", "--hard"]})

    def test_reviewer_command_tool_rejects_mutating_commands(self):
        with TemporaryDirectory() as directory:
            tool = ReadOnlyCommandTool(Workspace(Path(directory)))

            with self.assertRaisesRegex(ValueError, "read-only reviewer"):
                tool.execute({"command": ["cargo", "fmt"]})
            with self.assertRaisesRegex(ValueError, "source-modifying"):
                tool.execute({"command": ["ruff", "check", "--fix", "."]})
            with self.assertRaisesRegex(ValueError, "read-only reviewer"):
                tool.execute({"command": ["npm", "test"]})


class TraceTests(unittest.TestCase):
    def test_trace_is_written_only_to_requested_path(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "trace.json"
            result = AgentResult(
                content="done",
                iterations=2,
                messages=[{"role": "user", "content": "task"}],
                tool_executions=[
                    ToolExecution(
                        tool_call_id="call-1",
                        name="list_files",
                        arguments="{}",
                        result="README.md",
                    )
                ],
                workflow={"mode": "react", "reviews": []},
            )

            write_trace(path, "task", result)
            trace = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(trace["final_content"], "done")
            self.assertEqual(trace["tool_executions"][0]["name"], "list_files")
            self.assertEqual(trace["workflow"]["mode"], "react")


if __name__ == "__main__":
    unittest.main()
