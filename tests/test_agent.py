from tests import _TEST_STORAGE_HOME  # noqa: F401

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
                        content="行动说明：先查看项目文件列表，确认需要读取的范围",
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
            latest_tool = next(
                message
                for message in reversed(second_request)
                if message["role"] == "tool"
            )
            self.assertIn("README.md", latest_tool["content"])
            for request_messages, _ in llm.requests:
                reminder = request_messages[-1]
                self.assertEqual(reminder["role"], "user")
                self.assertIn(
                    "What files are here?",
                    reminder["content"],
                )
                self.assertIn("如果现有证据已经足够", reminder["content"])
            self.assertFalse(
                any(
                    "任务锚点（第" in str(message.get("content", ""))
                    for message in result.messages
                )
            )

    def test_every_model_call_keeps_root_requirement_for_subtask(self):
        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            llm = FakeLLM([assistant_message(content="子任务完成")])

            Agent(llm, registry).run(
                "只检查配置文件",
                root_requirement="实现用户登录并通过测试",
            )

            reminder = llm.requests[0][0][-1]["content"]
            self.assertIn("实现用户登录并通过测试", reminder)
            self.assertIn("只检查配置文件", reminder)

    def test_duplicate_unchanged_file_read_is_skipped(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            registry = ToolRegistry([ReadFileTool(Workspace(root))])
            repeated = {"path": "app.py"}
            llm = FakeLLM(
                [
                    assistant_message(
                        tool_calls=[tool_call("read-1", "read_file", repeated)]
                    ),
                    assistant_message(
                        tool_calls=[tool_call("read-2", "read_file", repeated)]
                    ),
                    assistant_message(content="完成"),
                ]
            )

            result = Agent(llm, registry).run("检查 app.py")

            self.assertIn("value = 1", result.tool_executions[0].result)
            self.assertIn(
                "重复读取已跳过",
                result.tool_executions[1].result,
            )

    def test_agent_reports_model_and_tool_progress(self):
        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            llm = FakeLLM(
                [
                    assistant_message(
                        content="行动说明：先查看项目文件列表，确认需要读取的范围",
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
                    "model_intent",
                    "tool_started",
                    "tool_completed",
                    "model_started",
                    "agent_completed",
                ],
            )
            self.assertEqual(
                events[1]["intent"],
                "先查看项目文件列表，确认需要读取的范围",
            )
            self.assertEqual(events[2]["tool"], "list_files")
            self.assertNotIn("arguments", events[1])
            self.assertNotIn("arguments", events[2])

    def test_non_public_tool_content_is_not_exposed_as_intent(self):
        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            events = []
            llm = FakeLLM(
                [
                    assistant_message(
                        content="internal reasoning that must stay hidden",
                        tool_calls=[tool_call("call-1", "list_files", {})],
                    ),
                    assistant_message(content="完成"),
                ]
            )

            Agent(
                llm,
                registry,
                progress_callback=events.append,
            ).run("查看项目")

            intent = next(
                event for event in events if event["event"] == "model_intent"
            )
            self.assertNotIn("internal reasoning", intent["intent"])
            self.assertIn("list_files", intent["intent"])

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
            result = Agent(
                FakeLLM([repeated_call, repeated_call, repeated_call]),
                registry,
                max_iterations=1,
                progress_callback=events.append,
                iteration_extension=1,
                stagnation_limit=2,
            ).run("Keep looking")

            self.assertEqual(result.stop_reason, "stagnation")
            self.assertTrue(result.content)
            detected = [
                event
                for event in events
                if event["event"] == "stagnation_detected"
            ]
            self.assertEqual(detected[0]["stagnant_iterations"], 2)
            self.assertEqual(events[-1]["event"], "final_answer_completed")

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

            result = Agent(
                FakeLLM(calls),
                registry,
                iteration_budget=IterationBudget(2),
                progress_callback=events.append,
            ).run("持续调查")

            self.assertEqual(result.stop_reason, "budget_exhausted")
            self.assertTrue(result.content)
            reached = [
                event
                for event in events
                if event["event"] == "requirement_budget_reached"
            ]
            self.assertEqual(reached[0]["used"], 1)
            self.assertEqual(events[-1]["event"], "final_answer_completed")

    def test_model_failure_still_returns_local_fallback_answer(self):
        class FailingLLM:
            def complete(self, messages, tools=None):
                raise ConnectionError("offline")

        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            events = []

            result = Agent(
                FailingLLM(),
                registry,
                iteration_budget=IterationBudget(3),
                progress_callback=events.append,
            ).run("完成需求")

            self.assertEqual(result.stop_reason, "model_error")
            self.assertIn("停止原因", result.content)
            completed = [
                event
                for event in events
                if event["event"] == "final_answer_completed"
            ]
            self.assertFalse(completed[-1]["generated_by_model"])

    def test_requirement_budget_warns_model_to_converge(self):
        with TemporaryDirectory() as directory:
            registry = ToolRegistry([ListFilesTool(Workspace(Path(directory)))])
            llm = FakeLLM(
                [
                    assistant_message(
                        tool_calls=[
                            tool_call("call-1", "list_files", {"offset": 0})
                        ]
                    ),
                    assistant_message(content="已根据现有证据完成"),
                ]
            )
            events = []

            result = Agent(
                llm,
                registry,
                iteration_budget=IterationBudget(3),
                progress_callback=events.append,
            ).run("完成调查")

            self.assertEqual(result.iterations, 2)
            warnings = [
                event
                for event in events
                if event["event"] == "requirement_budget_low"
            ]
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0]["remaining"], 2)
            second_request_messages = llm.requests[1][0]
            self.assertTrue(
                any(
                    "立即收敛" in str(message.get("content", ""))
                    for message in second_request_messages
                )
            )


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
