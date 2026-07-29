import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from simple_agent.agent import AgentResult, ToolExecution
from simple_agent.memory import ContextBuilder, ProjectMemoryStore, TaskSummary
from simple_agent.session import SessionManager
from simple_agent.tools import ListFilesTool, ReadEpisodeTool, ReadFileTool
from simple_agent.workspace import Workspace


class MemoryLifecycleTests(unittest.TestCase):
    def test_new_task_gets_summary_but_not_old_raw_tool_transcript(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            store = ProjectMemoryStore(workspace)
            manager = SessionManager(store)
            first = manager.start_task("创建登录接口")
            result = AgentResult(
                content="登录接口已经完成并通过测试。",
                iterations=3,
                messages=[
                    {"role": "user", "content": "创建登录接口"},
                    {
                        "role": "tool",
                        "tool_call_id": "call-read",
                        "content": "RAW_INTERNAL_TOOL_RESULT",
                    },
                ],
                tool_executions=[
                    ToolExecution(
                        tool_call_id="call-edit",
                        name="apply_patch",
                        arguments=json.dumps(
                            {
                                "path": "src/auth.py",
                                "mode": "create",
                                "new_text": "def login(): pass\n",
                            }
                        ),
                        result="Created src/auth.py",
                    ),
                    ToolExecution(
                        tool_call_id="call-test",
                        name="run_command",
                        arguments=json.dumps(
                            {
                                "command": [
                                    "python3",
                                    "-m",
                                    "unittest",
                                ]
                            }
                        ),
                        result="Exit code: 0\nSTDOUT:\nOK",
                    ),
                ],
            )

            summary = manager.complete_task(first, result)
            second = manager.start_task("为登录接口增加失败次数限制")
            context = json.dumps(
                second.context_messages,
                ensure_ascii=False,
            )
            episode = store.read_episode(first.task_id)

            self.assertEqual(summary.files_changed, ["src/auth.py"])
            self.assertEqual(summary.validations[0]["exit_code"], 0)
            self.assertIn("登录接口已经完成", context)
            self.assertNotIn("RAW_INTERNAL_TOOL_RESULT", context)
            self.assertIn("RAW_INTERNAL_TOOL_RESULT", json.dumps(episode))
            self.assertTrue(
                all(
                    message.get("role") != "system"
                    for message in episode["messages"]
                )
            )
            self.assertIn(first.task_id, second.memory_summary_ids)

    def test_failed_task_is_persisted(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            manager = SessionManager(store)
            task = manager.start_task("执行失败任务")

            summary = manager.fail_task(task, RuntimeError("boom"))

            self.assertEqual(summary.status, "failed")
            self.assertIn("boom", store.read_episode(task.task_id)["error"])


class MemoryRetrievalTests(unittest.TestCase):
    def test_chinese_keyword_search_finds_relevant_old_task(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            store.append_summary(
                TaskSummary(
                    task_id="task-auth",
                    request="实现用户登录接口",
                    status="completed",
                    summary="使用 JWT 完成登录认证",
                    files_changed=["src/auth.py"],
                )
            )
            store.append_summary(
                TaskSummary(
                    task_id="task-report",
                    request="生成销售报表",
                    status="completed",
                    summary="增加 CSV 报表导出",
                    files_changed=["src/report.py"],
                )
            )

            matches = store.search_summaries("登录认证失败", limit=1)

            self.assertEqual(matches[0].task_id, "task-auth")

    def test_read_episode_tool_requires_valid_id(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            store.write_episode(
                "task-safe",
                {"task_id": "task-safe", "detail": "past decision"},
            )
            tool = ReadEpisodeTool(store)

            result = tool.execute({"task_id": "task-safe"})

            self.assertIn("past decision", result)
            with self.assertRaisesRegex(ValueError, "invalid memory id"):
                tool.execute({"task_id": "../escape"})

    def test_context_builder_limits_recent_summaries(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            for index in range(4):
                store.append_summary(
                    TaskSummary(
                        task_id=f"task-{index}",
                        request=f"需求 {index}",
                        status="completed",
                        summary=f"结果 {index}",
                    )
                )

            built = ContextBuilder(
                store,
                recent_limit=2,
                relevant_limit=0,
            ).build("无关的新需求")
            context = json.dumps(built.messages, ensure_ascii=False)

            self.assertNotIn("task-0", context)
            self.assertNotIn("task-1", context)
            self.assertIn("task-2", context)
            self.assertIn("task-3", context)


class MemoryFilesystemIsolationTests(unittest.TestCase):
    def test_memory_directory_is_hidden_from_general_file_tools(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            memory_dir = root / ".simple-agent"
            memory_dir.mkdir()
            (memory_dir / "secret.json").write_text(
                '{"detail": "private memory"}',
                encoding="utf-8",
            )
            workspace = Workspace(root)

            listing = ListFilesTool(workspace).execute({"path": "."})

            self.assertNotIn(".simple-agent", listing)
            with self.assertRaisesRegex(ValueError, "sensitive path"):
                ReadFileTool(workspace).execute(
                    {"path": ".simple-agent/secret.json"}
                )

    def test_memory_store_rejects_symlinked_memory_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / ".simple-agent").symlink_to(outside, target_is_directory=True)
            store = ProjectMemoryStore(Workspace(root))

            with self.assertRaisesRegex(ValueError, "symbolic links"):
                store.append_summary(
                    TaskSummary(
                        task_id="task-safe",
                        request="request",
                        status="completed",
                        summary="summary",
                    )
                )


if __name__ == "__main__":
    unittest.main()
