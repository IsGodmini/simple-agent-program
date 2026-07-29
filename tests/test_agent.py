import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from simple_agent.agent import Agent, IterationBudget
from simple_agent.tools import ListFilesTool, ReadFileTool, ToolRegistry
from simple_agent.workspace import Workspace


def assistant_message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


class FakeLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools=None):
        self.requests.append((list(messages), tools))
        return next(self.responses)


class AgentTests(unittest.TestCase):
    def test_agent_executes_tool_and_returns_final_answer(self):
        with TemporaryDirectory() as directory:
            Path(directory, "README.md").write_text("hello", encoding="utf-8")
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            llm = FakeLLM(
                [
                    assistant_message(
                        tool_calls=[tool_call("call-1", "list_files", {})]
                    ),
                    assistant_message(content="I found README.md."),
                ]
            )

            result = Agent(llm, registry).run("What files are here?")

            self.assertEqual(result.content, "I found README.md.")
            self.assertEqual(result.iterations, 2)
            self.assertEqual(result.tool_executions[0].name, "list_files")
            second_request = llm.requests[1][0]
            self.assertEqual(second_request[-1]["role"], "tool")
            self.assertIn("README.md", second_request[-1]["content"])

    def test_agent_reports_model_and_tool_progress(self):
        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            llm = FakeLLM(
                [
                    assistant_message(
                        tool_calls=[tool_call("call-1", "list_files", {})]
                    ),
                    assistant_message(content="完成"),
                ]
            )
            events = []

            Agent(
                llm,
                registry,
                progress_callback=events.append,
                progress_role="executor",
            ).run("查看项目")

            self.assertEqual(
                [event["event"] for event in events],
                [
                    "model_started",
                    "tool_started",
                    "tool_completed",
                    "model_started",
                    "agent_completed",
                ],
            )
            self.assertEqual(events[1]["tool"], "list_files")
            self.assertNotIn("arguments", events[1])

    def test_empty_request_is_rejected(self):
        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            with self.assertRaisesRegex(ValueError, "cannot be empty"):
                Agent(FakeLLM([]), registry).run(" ")

    def test_repeated_tool_results_stop_as_stagnation(self):
        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            repeated_call = assistant_message(
                tool_calls=[tool_call("call-1", "list_files", {})]
            )
            events = []
            with self.assertRaisesRegex(RuntimeError, "without new evidence"):
                Agent(
                    FakeLLM([repeated_call, repeated_call, repeated_call]),
                    registry,
                    max_iterations=1,
                    progress_callback=events.append,
                    iteration_extension=1,
                    stagnation_limit=2,
                ).run("Keep looking")
            self.assertEqual(events[-1]["event"], "stagnation_detected")
            self.assertEqual(events[-1]["stagnant_iterations"], 2)

    def test_new_evidence_extends_initial_iteration_allowance(self):
        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            llm = FakeLLM(
                [
                    assistant_message(
                        tool_calls=[
                            tool_call("call-1", "list_files", {"offset": 0})
                        ]
                    ),
                    assistant_message(
                        tool_calls=[
                            tool_call("call-2", "list_files", {"offset": 1})
                        ]
                    ),
                    assistant_message(content="调查完成"),
                ]
            )
            events = []

            result = Agent(
                llm,
                registry,
                max_iterations=1,
                progress_callback=events.append,
                iteration_extension=1,
                stagnation_limit=2,
            ).run("继续调查")

            self.assertEqual(result.iterations, 3)
            extensions = [
                event
                for event in events
                if event["event"] == "iteration_budget_extended"
            ]
            self.assertEqual([event["allowance"] for event in extensions], [2, 3])

    def test_requirement_wide_budget_is_last_resort_cap(self):
        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            calls = [
                assistant_message(
                    tool_calls=[
                        tool_call(f"call-{index}", "list_files", {"offset": index})
                    ]
                )
                for index in range(3)
            ]
            events = []

            with self.assertRaisesRegex(RuntimeError, "last-resort"):
                Agent(
                    FakeLLM(calls),
                    registry,
                    iteration_budget=IterationBudget(2),
                    progress_callback=events.append,
                ).run("持续调查")

            self.assertEqual(events[-1]["event"], "requirement_budget_reached")
            self.assertEqual(events[-1]["used"], 2)


class FileToolTests(unittest.TestCase):
    def test_list_and_read_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text(
                "first\nsecond\n", encoding="utf-8"
            )
            workspace = Workspace(root)

            listing = ListFilesTool(workspace).execute({"path": "."})
            content = ReadFileTool(workspace).execute({"path": "src/app.py"})

            self.assertIn("src/app.py", listing)
            self.assertIn("1 | first", content)
            self.assertIn("2 | second", content)

    def test_workspace_escape_is_rejected(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            with self.assertRaisesRegex(ValueError, "outside the workspace"):
                workspace.resolve("../secret.txt")

    def test_sensitive_files_are_hidden_and_cannot_be_read(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("API_KEY=secret", encoding="utf-8")
            (root / ".env.example").write_text(
                "API_KEY=placeholder", encoding="utf-8"
            )
            workspace = Workspace(root)
            registry = ToolRegistry(
                [ListFilesTool(workspace), ReadFileTool(workspace)]
            )

            listing = registry.execute("list_files", "{}")
            denied = registry.execute("read_file", '{"path": ".env"}')
            example = registry.execute(
                "read_file", '{"path": ".env.example"}'
            )

            self.assertNotIn("\n.env\n", f"\n{listing}\n")
            self.assertIn(".env.example", listing)
            self.assertIn("sensitive path is denied", denied)
            self.assertIn("placeholder", example)

    def test_registry_returns_errors_to_the_model(self):
        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ReadFileTool(Workspace(Path(directory)))])

            invalid_json = registry.execute("read_file", "{")
            unknown_tool = registry.execute("delete_file", "{}")

            self.assertIn("Tool error", invalid_json)
            self.assertIn("unknown tool", unknown_tool)


if __name__ == "__main__":
    unittest.main()
