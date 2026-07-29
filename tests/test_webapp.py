import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from simple_agent.agent import AgentResult
from simple_agent.webapp import create_app


class _FakeAgent:
    def run(self, request, context_messages=None):
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
    return _FakeAgent()


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
