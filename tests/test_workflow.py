from tests import _TEST_STORAGE_HOME  # noqa: F401

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from simple_agent.tools import ApplyPatchTool, ListFilesTool, ToolRegistry
from simple_agent.workflow import (
    ComplexityRouter,
    TaskPlan,
    WorkflowBlockedError,
    WorkflowConfig,
    WorkflowError,
    WorkflowOrchestrator,
)
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


def review(verdict="pass", findings=None):
    return json.dumps(
        {
            "verdict": verdict,
            "summary": f"review {verdict}",
            "findings": findings or [],
            "criteria": [
                {
                    "criterion": "requested behavior",
                    "status": "verified" if verdict == "pass" else "failed",
                }
            ],
        }
    )


def one_step_plan():
    return json.dumps(
        {
            "goal": "完成复杂改造",
            "version": 1,
            "assumptions": [],
            "acceptance_criteria": ["实现功能并通过测试"],
            "steps": [
                {
                    "id": "step-1",
                    "objective": "实现核心功能",
                    "dependencies": [],
                    "acceptance_criteria": ["核心功能完成"],
                    "expected_outputs": ["代码和测试证据"],
                    "allowed_tools": ["read_file", "apply_patch"],
                    "relevant_paths": ["src"],
                }
            ],
        }
    )


class FakeLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools=None):
        self.requests.append((list(messages), tools))
        return next(self.responses)


def orchestrator(root, llm, mode="auto", revisions=1, progress_callback=None):
    workspace = Workspace(root)
    read_tools = ToolRegistry([ListFilesTool(workspace)])
    return WorkflowOrchestrator(
        llm=llm,
        executor_tools=ToolRegistry(
            [ListFilesTool(workspace), ApplyPatchTool(workspace)]
        ),
        planning_tools=read_tools,
        review_tools=ToolRegistry([ListFilesTool(workspace)]),
        config=WorkflowConfig(
            mode=mode,
            max_step_revisions=revisions,
        ),
        progress_callback=progress_callback,
    )


class ComplexityRouterTests(unittest.TestCase):
    def test_routes_simple_and_complex_requests(self):
        router = ComplexityRouter(threshold=3)

        simple = router.assess("修正 README 中的一个错别字")
        complex_task = router.assess(
            "重构系统架构，同时迁移数据库，并且更新多个模块的安全权限"
        )
        multi_surface = router.assess("实现登录接口，同时添加完整测试")
        expansion = router.assess("你可以拓展一下项目功能吗")

        self.assertEqual(simple.mode, "react")
        self.assertEqual(complex_task.mode, "plan_and_act")
        self.assertEqual(multi_surface.mode, "plan_and_act")
        self.assertEqual(expansion.mode, "plan_and_act")
        self.assertGreaterEqual(complex_task.score, 3)

    def test_forced_mode_overrides_complexity(self):
        router = ComplexityRouter()

        self.assertEqual(
            router.assess("重构整个架构", forced_mode="react").mode,
            "react",
        )
        self.assertEqual(
            router.assess("改一个字", forced_mode="plan").mode,
            "plan_and_act",
        )


class TaskPlanTests(unittest.TestCase):
    def test_validates_and_orders_dependency_graph(self):
        plan = TaskPlan.from_dict(
            {
                "goal": "goal",
                "steps": [
                    {"id": "verify", "objective": "verify", "dependencies": ["code"]},
                    {"id": "code", "objective": "code", "dependencies": []},
                ],
            },
            max_steps=5,
        )

        self.assertEqual(
            [step.step_id for step in plan.ordered_steps()],
            ["code", "verify"],
        )

    def test_rejects_cycles_and_unknown_dependencies(self):
        with self.assertRaisesRegex(WorkflowError, "cycle"):
            TaskPlan.from_dict(
                {
                    "goal": "goal",
                    "steps": [
                        {"id": "a", "objective": "a", "dependencies": ["b"]},
                        {"id": "b", "objective": "b", "dependencies": ["a"]},
                    ],
                },
                max_steps=5,
            )
        with self.assertRaisesRegex(WorkflowError, "unknown dependencies"):
            TaskPlan.from_dict(
                {
                    "goal": "goal",
                    "steps": [
                        {
                            "id": "a",
                            "objective": "a",
                            "dependencies": ["missing"],
                        }
                    ],
                },
                max_steps=5,
            )


class WorkflowOrchestratorTests(unittest.TestCase):
    def test_budget_exhaustion_returns_reserved_final_answer(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            tools = ToolRegistry([ListFilesTool(workspace)])
            llm = FakeLLM(
                [
                    assistant_message(
                        tool_calls=[
                            tool_call("scan-1", "list_files", {"offset": 0})
                        ]
                    ),
                    assistant_message(
                        content="调用预算已用完；这是基于现有证据的最终答复。"
                    ),
                ]
            )
            events = []
            workflow = WorkflowOrchestrator(
                llm=llm,
                executor_tools=tools,
                planning_tools=tools,
                review_tools=tools,
                config=WorkflowConfig(
                    mode="react",
                    total_iteration_budget=2,
                ),
                progress_callback=events.append,
            )

            result = workflow.run("持续检查项目")

            self.assertEqual(result.stop_reason, "budget_exhausted")
            self.assertIn("最终答复", result.content)
            self.assertEqual(
                result.workflow["status"],
                "stopped_with_answer",
            )
            self.assertEqual(
                result.workflow["iteration_budget"],
                {"used": 2, "maximum": 2, "remaining": 0},
            )
            self.assertEqual(llm.requests[-1][1], [])
            self.assertIn(
                "workflow_stopped_with_answer",
                [event["event"] for event in events],
            )

    def test_reports_routing_execution_and_completion_progress(self):
        with TemporaryDirectory() as directory:
            events = []
            llm = FakeLLM(
                [
                    assistant_message(content="项目包含 README。"),
                    assistant_message(content=review("pass")),
                ]
            )

            orchestrator(
                Path(directory),
                llm,
                mode="react",
                progress_callback=events.append,
            ).run("项目里有什么？")

            names = [event["event"] for event in events]
            self.assertEqual(names[0], "workflow_routed")
            self.assertIn("execution_started", names)
            self.assertIn("model_started", names)
            self.assertEqual(names[-1], "workflow_completed")

    def test_simple_read_only_task_stays_in_react_with_one_reflection(self):
        with TemporaryDirectory() as directory:
            llm = FakeLLM(
                [
                    assistant_message(content="项目包含 README。"),
                    assistant_message(content=review("pass")),
                ]
            )

            result = orchestrator(Path(directory), llm, mode="react").run(
                "项目里有什么？"
            )

            self.assertEqual(result.content, "项目包含 README。")
            self.assertEqual(result.workflow["mode"], "react")
            self.assertEqual(len(result.workflow["reviews"]), 1)
            self.assertEqual(
                result.workflow["iteration_budget"],
                {"used": 2, "maximum": 24, "remaining": 22},
            )
            self.assertEqual(len(llm.requests), 2)

    def test_react_mutation_is_reviewed_before_completion(self):
        with TemporaryDirectory() as directory:
            llm = FakeLLM(
                [
                    assistant_message(
                        tool_calls=[
                            tool_call(
                                "edit-1",
                                "apply_patch",
                                {
                                    "path": "note.txt",
                                    "mode": "create",
                                    "new_text": "done\n",
                                },
                            )
                        ]
                    ),
                    assistant_message(content="已创建文件。"),
                    assistant_message(content=review("pass")),
                ]
            )

            result = orchestrator(Path(directory), llm, mode="react").run(
                "创建 note.txt"
            )

            self.assertEqual(result.workflow["reviews"][0]["verdict"], "pass")
            self.assertTrue(Path(directory, "note.txt").exists())
            self.assertEqual(
                [item.name for item in result.tool_executions],
                ["apply_patch"],
            )

    def test_auto_mode_never_changes_after_initial_routing(self):
        with TemporaryDirectory() as directory:
            attempted_escalation = json.dumps(
                {
                    "workflow_request": "plan_and_act",
                    "objective": "迁移认证数据并保持兼容",
                    "reason": "涉及数据库、接口和回滚步骤",
                    "evidence": ["已经确认存在两个存储版本"],
                }
            )
            llm = FakeLLM(
                [
                    assistant_message(content=attempted_escalation),
                    assistant_message(content=review("pass")),
                ]
            )

            result = orchestrator(Path(directory), llm, mode="auto").run(
                "调整登录行为"
            )

            self.assertEqual(result.workflow["mode"], "react")
            self.assertEqual(len(llm.requests), 2)
            self.assertEqual(result.content, attempted_escalation)

    def test_complex_task_runs_one_final_review_and_synthesis(self):
        with TemporaryDirectory() as directory:
            llm = FakeLLM(
                [
                    assistant_message(content=one_step_plan()),
                    assistant_message(content="核心功能已完成。"),
                    assistant_message(content=review("pass")),
                    assistant_message(content="复杂改造已经完成并通过评审。"),
                ]
            )

            result = orchestrator(Path(directory), llm, mode="plan").run(
                "完成复杂改造"
            )

            self.assertEqual(result.workflow["mode"], "plan_and_act")
            self.assertEqual(
                result.workflow["plan"]["steps"][0]["status"],
                "completed",
            )
            self.assertEqual(len(result.workflow["reviews"]), 1)
            self.assertEqual(
                result.content,
                "复杂改造已经完成并通过评审。",
            )
            for request_messages, _ in llm.requests:
                serialized = json.dumps(
                    request_messages,
                    ensure_ascii=False,
                )
                self.assertIn("完成复杂改造", serialized)
                self.assertIn("只做能直接推进需求的动作", serialized)

    def test_revise_verdict_creates_one_bounded_repair(self):
        with TemporaryDirectory() as directory:
            finding = {
                "severity": "high",
                "category": "test_gap",
                "evidence": "缺少边界测试",
                "recommended_action": "补充边界测试",
            }
            llm = FakeLLM(
                [
                    assistant_message(content=one_step_plan()),
                    assistant_message(content="初次实现。"),
                    assistant_message(content=review("revise", [finding])),
                    assistant_message(content="已补充边界测试。"),
                    assistant_message(content=review("pass")),
                    assistant_message(content="修正后完成。"),
                ]
            )

            result = orchestrator(
                Path(directory),
                llm,
                mode="plan",
                revisions=1,
            ).run("完成复杂改造")

            self.assertEqual(result.workflow["final_revisions"], 1)
            self.assertEqual(
                [item["verdict"] for item in result.workflow["reviews"]],
                ["revise", "pass"],
            )

    def test_revision_limit_blocks_unverified_completion(self):
        with TemporaryDirectory() as directory:
            llm = FakeLLM(
                [
                    assistant_message(content=one_step_plan()),
                    assistant_message(content="实现结果。"),
                    assistant_message(content=review("revise")),
                ]
            )

            with self.assertRaises(WorkflowBlockedError) as raised:
                orchestrator(
                    Path(directory),
                    llm,
                    mode="plan",
                    revisions=0,
                ).run("完成复杂改造")

            self.assertEqual(
                raised.exception.workflow["mode"],
                "plan_and_act",
            )
            self.assertEqual(
                raised.exception.workflow["plan"]["steps"][0]["status"],
                "completed",
            )

    def test_planner_and_reviewer_do_not_receive_write_tools(self):
        with TemporaryDirectory() as directory:
            runner = orchestrator(
                Path(directory),
                FakeLLM([]),
                mode="plan",
            )

            planner_names = {
                item["function"]["name"]
                for item in runner.planner.agent.tools.definitions
            }
            reviewer_names = {
                item["function"]["name"]
                for item in runner.reviewer.agent.tools.definitions
            }

            self.assertNotIn("apply_patch", planner_names)
            self.assertNotIn("apply_patch", reviewer_names)


if __name__ == "__main__":
    unittest.main()
