import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from simple_agent.agent import AgentResult
from simple_agent.cli import _handle_session_actions
from simple_agent.knowledge import KnowledgeBase
from simple_agent.memory import ContextBuilder, ProjectMemoryStore, TaskSummary
from simple_agent.session import SessionManager
from simple_agent.tools import SearchMemoryTool
from simple_agent.workspace import Workspace


def completed_result(content):
    return AgentResult(
        content=content,
        iterations=1,
        messages=[{"role": "assistant", "content": content}],
        tool_executions=[],
    )


class ConversationSessionStoreTests(unittest.TestCase):
    def test_workspace_can_create_and_list_multiple_sessions(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))

            first = store.create_session("认证开发")
            second = store.create_session("报表开发")

            self.assertNotEqual(first.session_id, second.session_id)
            self.assertEqual(
                [item.title for item in store.list_sessions()],
                ["认证开发", "报表开发"],
            )

    def test_one_session_can_contain_multiple_requirements(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            conversation = store.create_session("连续开发")
            manager = SessionManager(store, session_id=conversation.session_id)

            first = manager.start_requirement("实现登录接口")
            manager.complete_task(first, completed_result("登录接口已完成"))
            second = manager.start_requirement("增加登录失败限制")
            manager.complete_task(second, completed_result("失败限制已完成"))

            persisted = store.get_session(conversation.session_id)
            self.assertEqual(len(persisted.requirement_ids), 2)
            self.assertIn("登录接口已完成", persisted.summary)
            self.assertIn("失败限制已完成", persisted.summary)
            second_context = json.dumps(
                second.context_messages,
                ensure_ascii=False,
            )
            self.assertIn("登录接口已完成", second_context)
            self.assertNotIn("失败限制已完成", second_context)

    def test_legacy_summaries_belong_to_default_session(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            store.append_summary(
                TaskSummary(
                    task_id="legacy-task",
                    request="旧需求",
                    status="completed",
                    summary="旧结果",
                )
            )

            summary = store.list_summaries()[0]

            self.assertEqual(summary.session_id, "default")


class CrossSessionContextTests(unittest.TestCase):
    def test_other_session_memory_is_shared_only_when_relevant(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            session_a = store.create_session("接口开发")
            session_b = store.create_session("认证设计")
            session_c = store.create_session("报表开发")
            auth = TaskSummary(
                task_id="task-auth",
                session_id=session_b.session_id,
                request="确定认证令牌方案",
                status="completed",
                summary="使用 JWT 并设置十五分钟过期时间",
                verification="verified",
            )
            report = TaskSummary(
                task_id="task-report",
                session_id=session_c.session_id,
                request="实现销售报表",
                status="completed",
                summary="支持 CSV 导出",
                verification="verified",
            )
            for item in (auth, report):
                store.append_summary(item)
                store.append_requirement_to_session(item)

            built = ContextBuilder(store).build(
                "实现 JWT 认证刷新",
                session_id=session_a.session_id,
            )
            serialized = json.dumps(built.messages, ensure_ascii=False)

            self.assertIn("task-auth", serialized)
            self.assertIn(session_b.session_id, serialized)
            self.assertNotIn("task-report", serialized)
            self.assertIn("cross_session_relevant_episodes", serialized)

    def test_failed_cross_session_requirement_is_not_auto_injected(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            current = store.create_session("当前会话")
            failed_session = store.create_session("失败实验")
            failed = TaskSummary(
                task_id="task-failed-auth",
                session_id=failed_session.session_id,
                request="尝试 JWT 认证",
                status="failed",
                summary="JWT 实验失败",
                verification="failed",
            )
            store.append_summary(failed)
            store.append_requirement_to_session(failed)

            built = ContextBuilder(store).build(
                "实现 JWT 认证",
                session_id=current.session_id,
            )

            self.assertNotIn(
                "task-failed-auth",
                json.dumps(built.messages, ensure_ascii=False),
            )

    def test_knowledge_base_is_shared_by_all_workspace_sessions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rules.md"
            source.write_text(
                "所有认证接口必须记录安全审计日志。",
                encoding="utf-8",
            )
            workspace = Workspace(root)
            store = ProjectMemoryStore(workspace)
            first = store.create_session("会话一")
            second = store.create_session("会话二")
            knowledge = KnowledgeBase(workspace)
            knowledge.ingest(source)
            builder = ContextBuilder(store, knowledge_base=knowledge)

            first_context = builder.build(
                "增加认证审计",
                session_id=first.session_id,
            )
            second_context = builder.build(
                "增加认证审计",
                session_id=second.session_id,
            )

            self.assertTrue(first_context.knowledge_citations)
            self.assertEqual(
                first_context.knowledge_citations,
                second_context.knowledge_citations,
            )

    def test_memory_tool_searches_workspace_or_one_session(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            first = store.create_session("第一会话")
            second = store.create_session("第二会话")
            for task_id, session_id, summary in (
                ("task-one", first.session_id, "JWT 认证方案一"),
                ("task-two", second.session_id, "JWT 认证方案二"),
            ):
                item = TaskSummary(
                    task_id=task_id,
                    session_id=session_id,
                    request="认证设计",
                    status="completed",
                    summary=summary,
                )
                store.append_summary(item)
                store.append_requirement_to_session(item)
            tool = SearchMemoryTool(store)

            workspace_results = tool.execute({"query": "JWT 认证"})
            session_results = tool.execute(
                {
                    "query": "JWT 认证",
                    "session_id": first.session_id,
                }
            )

            self.assertIn("task-one", workspace_results)
            self.assertIn("task-two", workspace_results)
            self.assertIn("task-one", session_results)
            self.assertNotIn("task-two", session_results)


class ConversationSessionCliTests(unittest.TestCase):
    def test_cli_can_create_continue_and_list_sessions(self):
        with TemporaryDirectory() as directory:
            store = ProjectMemoryStore(Workspace(Path(directory)))
            create_args = SimpleNamespace(
                new_session=True,
                session=None,
                session_title="认证会话",
                list_sessions=True,
            )

            session_id, output = _handle_session_actions(create_args, store)
            continue_args = SimpleNamespace(
                new_session=False,
                session=session_id,
                session_title=None,
                list_sessions=False,
            )
            continued_id, _ = _handle_session_actions(continue_args, store)

            self.assertEqual(session_id, continued_id)
            self.assertIn("认证会话", output)
            self.assertIn(session_id, output)

    def test_cli_rejects_conflicting_session_options(self):
        with TemporaryDirectory() as directory:
            args = SimpleNamespace(
                new_session=True,
                session="existing",
                session_title=None,
                list_sessions=False,
            )

            with self.assertRaisesRegex(ValueError, "cannot be used together"):
                _handle_session_actions(
                    args,
                    ProjectMemoryStore(Workspace(Path(directory))),
                )


if __name__ == "__main__":
    unittest.main()
