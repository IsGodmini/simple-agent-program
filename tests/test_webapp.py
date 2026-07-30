from tests import _TEST_STORAGE_HOME  # noqa: F401

import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from simple_agent.agent import AgentResult
from simple_agent.memory import ProjectMemoryStore
from simple_agent.webapp import create_app
from simple_agent.workspace import Workspace


class _FakeAgent:
    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback

    def run(self, request, context_messages=None):
        if self.progress_callback:
            self.progress_callback(
                {
                    "event": "workflow_routed",
                    "mode": "plan_and_act",
                    "message": "复杂需求进入 Plan-and-Act",
                }
            )
            self.progress_callback(
                {
                    "event": "tool_started",
                    "role": "executor",
                    "tool": "read_file",
                    "message": "正在调用工具：read_file",
                }
            )
            self.progress_callback(
                {
                    "event": "model_intent",
                    "role": "executor",
                    "intent": "读取健康检查相关代码并确认接口结构",
                    "message": "读取健康检查相关代码并确认接口结构",
                }
            )
        return AgentResult(
            content=f"已完成：{request}",
            iterations=2,
            messages=[
                {"role": "user", "content": request},
                {"role": "assistant", "content": f"已完成：{request}"},
            ],
            tool_executions=[],
            compactions=0,
            workflow={
                "mode": "plan_and_act",
                "assessment": {"reason": "测试复杂需求"},
                "plan": {
                    "steps": [
                        {"title": "检查项目", "status": "completed"},
                        {"title": "完成实现", "status": "completed"},
                    ]
                },
                "reviews": [
                    {"verdict": "pass", "summary": "实现符合需求"}
                ],
            },
        )


def _fake_agent_factory(*args, **kwargs):
    return _FakeAgent(kwargs.get("progress_callback"))


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.client_context = TestClient(
            create_app(
                default_workspace=self.workspace,
                agent_factory=_fake_agent_factory,
            )
        )
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temporary.cleanup()

    def test_serves_client_and_bootstrap(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Simple Agent", page.text)
        self.assertIn("项目会话", page.text)

        bootstrap = self.client.get("/api/bootstrap")
        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(
            bootstrap.json()["default_workspace"],
            str(self.workspace.resolve()),
        )
        self.assertEqual(
            bootstrap.json()["agent_modes"],
            ["auto", "react", "plan"],
        )

    def test_creates_and_lists_sessions(self):
        overview = self.client.get(
            "/api/workspace",
            params={"path": str(self.workspace)},
        )
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(
            overview.json()["storage"]["project_root"],
            str(ProjectMemoryStore(Workspace(self.workspace)).root),
        )
        self.assertEqual(overview.json()["sessions"][0]["session_id"], "default")

        created = self.client.post(
            "/api/sessions",
            json={"workspace": str(self.workspace), "title": "认证模块"},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["title"], "认证模块")

        sessions = self.client.get(
            "/api/sessions",
            params={"workspace": str(self.workspace)},
        )
        self.assertEqual(len(sessions.json()), 2)

    def test_upload_replaces_same_named_knowledge_and_removes_it(self):
        first = self.client.post(
            "/api/knowledge/upload",
            params={"workspace": str(self.workspace)},
            files={"files": ("规范.md", "第一版规范", "text/markdown")},
        )
        self.assertEqual(first.status_code, 201)
        document_id = first.json()[0]["document_id"]
        self.assertEqual(first.json()[0]["source_name"], "规范.md")

        second = self.client.post(
            "/api/knowledge/upload",
            params={"workspace": str(self.workspace)},
            files={"files": ("规范.md", "第二版规范", "text/markdown")},
        )
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()[0]["document_id"], document_id)

        documents = self.client.get(
            "/api/knowledge",
            params={"workspace": str(self.workspace)},
        ).json()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["source_name"], "规范.md")

        removed = self.client.delete(
            f"/api/knowledge/{document_id}",
            params={"workspace": str(self.workspace)},
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(
            self.client.get(
                "/api/knowledge",
                params={"workspace": str(self.workspace)},
            ).json(),
            [],
        )

    def test_runs_requirement_and_persists_summary_and_episode(self):
        (self.workspace / "app.py").write_text(
            "def health_check():\n    return True\n",
            encoding="utf-8",
        )
        self.client.get(
            "/api/workspace",
            params={"path": str(self.workspace)},
        )
        submitted = self.client.post(
            "/api/requirements",
            json={
                "workspace": str(self.workspace),
                "session_id": "default",
                "request": "实现健康检查接口",
                "agent_mode": "plan",
            },
        )
        self.assertEqual(submitted.status_code, 202)
        job = submitted.json()

        for _ in range(100):
            job = self.client.get(f"/api/jobs/{job['job_id']}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["content"], "已完成：实现健康检查接口")
        self.assertEqual(
            job["result"]["workflow"]["reviews"][0]["verdict"],
            "pass",
        )
        progress_events = [event["event"] for event in job["progress"]]
        self.assertIn("context_building", progress_events)
        self.assertIn("workflow_routed", progress_events)
        self.assertIn("tool_started", progress_events)
        self.assertIn("model_intent", progress_events)
        self.assertEqual(progress_events[-1], "completed")

        requirements = self.client.get(
            "/api/sessions/default/requirements",
            params={"workspace": str(self.workspace)},
        ).json()
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]["request"], "实现健康检查接口")
        self.assertEqual(requirements[0]["verification"], "verified")

        episode = self.client.get(
            f"/api/episodes/{job['result']['requirement_id']}",
            params={"workspace": str(self.workspace)},
        )
        self.assertEqual(episode.status_code, 200)
        self.assertEqual(episode.json()["status"], "completed")

        result = self.client.get(
            f"/api/requirements/{job['result']['requirement_id']}",
            params={"workspace": str(self.workspace)},
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["content"], "已完成：实现健康检查接口")
        self.assertNotIn("messages", result.json())

        index = self.client.get(
            "/api/project-index",
            params={"workspace": str(self.workspace)},
        )
        self.assertEqual(index.status_code, 200)
        self.assertTrue(index.json()["ready"])
        self.assertEqual(index.json()["indexed_files"], 1)

        graph = self.client.get(
            "/api/project-graph",
            params={"workspace": str(self.workspace)},
        )
        self.assertEqual(graph.status_code, 200)
        self.assertFalse(graph.json()["ready"])
        self.assertEqual(graph.json()["storage"], "neo4j-only")

        refreshed_graph = self.client.post(
            "/api/project-graph/refresh",
            params={"workspace": str(self.workspace)},
        )
        self.assertEqual(refreshed_graph.status_code, 200)
        self.assertEqual(refreshed_graph.json()["updated_profiles"], 0)
        self.assertIn("Neo4j graph requires", refreshed_graph.json()["error"])

    def test_rejects_invalid_mode_and_unknown_session(self):
        invalid_mode = self.client.post(
            "/api/requirements",
            json={
                "workspace": str(self.workspace),
                "session_id": "default",
                "request": "测试",
                "agent_mode": "invalid",
            },
        )
        self.assertEqual(invalid_mode.status_code, 400)

        unknown = self.client.get(
            "/api/sessions/not-found/requirements",
            params={"workspace": str(self.workspace)},
        )
        self.assertEqual(unknown.status_code, 400)


if __name__ == "__main__":
    unittest.main()
