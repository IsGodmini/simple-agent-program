import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simple_agent.agent import AgentResult, ToolExecution
from simple_agent.memory import ContextBuilder, ProjectMemoryStore, TaskSummary
from simple_agent.project_graph import FileProfile, GraphRefreshResult
from simple_agent.project_index import ProjectIndex
from simple_agent.session import SessionManager
from simple_agent.tools import ListFilesTool, ReadEpisodeTool, ReadFileTool
from simple_agent.workspace import Workspace


class MemoryLifecycleTests(unittest.TestCase):
    def test_current_requirement_is_always_anchored_in_context(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))

            built = ContextBuilder(store).build("实现支付接口并通过测试")

            self.assertTrue(built.messages)
            self.assertEqual(built.messages[0]["role"], "system")
            self.assertIn(
                "实现支付接口并通过测试",
                built.messages[0]["content"],
            )
            self.assertIn(
                "current_requirement_json",
                built.messages[0]["content"],
            )

    def test_session_places_neo4j_context_before_source_chunks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "auth.py").write_text(
                '"""Authenticate users with login tokens."""\n'
                "def login():\n"
                "    return True\n",
                encoding="utf-8",
            )
            manager = SessionManager(
                ProjectMemoryStore(Workspace(root))
            )

            requirement = manager.start_task("change login authentication")
            contents = [
                message["content"]
                for message in requirement.context_messages
            ]

            graph_position = next(
                index
                for index, content in enumerate(contents)
                if "<project_graph_json>" in content
            )
            index_position = next(
                index
                for index, content in enumerate(contents)
                if "<project_index_json>" in content
            )
            self.assertLess(graph_position, index_position)
            self.assertEqual(requirement.project_graph_citations, [])
            self.assertIn(
                "Neo4j graph requires",
                contents[graph_position],
            )

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
                workflow={
                    "mode": "plan_and_act",
                    "reviews": [{"verdict": "pass"}],
                },
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
            self.assertEqual(episode["workflow"]["mode"], "plan_and_act")
            self.assertTrue(
                all(
                    message.get("role") != "system"
                    for message in episode["messages"]
                )
            )
            self.assertIn(first.task_id, second.memory_summary_ids)

    def test_task_completion_batches_changed_file_graph_refresh(self):
        class RecordingGraph:
            def __init__(self):
                self.calls = []

            def refresh(self, paths=None):
                self.calls.append(list(paths or []))
                return GraphRefreshResult(
                    scanned_files=len(paths or []),
                    updated_profiles=len(paths or []),
                    unchanged_profiles=0,
                    deleted_profiles=0,
                    nodes=0,
                    edges=0,
                    duration_ms=0,
                    backend="neo4j",
                    neo4j_synced=True,
                    refreshed_at="now",
                )

        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            graph = RecordingGraph()
            manager = SessionManager(
                store,
                context_builder=ContextBuilder(store),
                project_graph=graph,
            )
            task = manager.start_task("修改认证和服务模块")
            result = AgentResult(
                content="完成",
                iterations=1,
                messages=[],
                tool_executions=[
                    ToolExecution(
                        tool_call_id="one",
                        name="apply_patch",
                        arguments='{"path":"auth.py"}',
                        result="Updated auth.py",
                    ),
                    ToolExecution(
                        tool_call_id="two",
                        name="apply_patch",
                        arguments='{"path":"service.py"}',
                        result="Updated service.py",
                    ),
                    ToolExecution(
                        tool_call_id="three",
                        name="apply_patch",
                        arguments='{"path":"auth.py"}',
                        result="Updated auth.py",
                    ),
                ],
            )

            manager.complete_task(task, result)

            self.assertEqual(graph.calls, [["auth.py", "service.py"]])
            episode = store.read_episode(task.task_id)
            self.assertEqual(
                episode["project_graph_refresh"]["updated_profiles"],
                2,
            )

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

    def test_graph_matches_suppress_automatic_source_chunk_injection(self):
        class GraphWithMatch:
            def refresh(self):
                return None

            def search_profiles(self, query, limit):
                return [
                    FileProfile(
                        path="auth.py",
                        content_hash="hash",
                        language="Python",
                        line_count=2,
                        purpose="处理用户认证。",
                        responsibilities=["验证登录令牌"],
                        public_symbols=[],
                        imports=[],
                        related_tests=[],
                        confidence=0.9,
                        evidence=["auth.py#L1"],
                        stale=False,
                        profile_version=2,
                        updated_at="now",
                    )
                ]

            def neighbors(self, path, depth, limit):
                return {"nodes": [], "edges": []}

            def overview(self, max_profiles):
                return {"ready": True, "backend": "neo4j"}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "auth.py").write_text(
                "def login(token):\n    return bool(token)\n",
                encoding="utf-8",
            )
            workspace = Workspace(root)
            store = ProjectMemoryStore(workspace)
            index = ProjectIndex(workspace)
            index.refresh()
            with patch.object(
                index,
                "search_hybrid",
                wraps=index.search_hybrid,
            ) as source_search:
                built = ContextBuilder(
                    store,
                    project_index=index,
                    project_graph=GraphWithMatch(),
                ).build("认证模块负责什么")

            source_search.assert_not_called()
            self.assertEqual(built.project_index_citations, [])
            self.assertEqual(
                built.project_graph_citations,
                ["graph:auth.py"],
            )

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
