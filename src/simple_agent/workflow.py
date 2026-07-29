"""Plan-and-Act orchestration with evidence-based reflection."""

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .agent import Agent, AgentResult, IterationBudget, ToolExecution
from .context import ContextManager
from .llm import ChatModel, Message
from .prompts import (
    EXECUTOR_PROMPT,
    PLANNER_PROMPT,
    REVIEWER_PROMPT,
    SYNTHESIZER_PROMPT,
)
from .tools import ToolRegistry

VALID_STEP_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
VALID_VERDICTS = {"pass", "revise", "blocked"}


class WorkflowError(RuntimeError):
    """Base class for orchestration failures."""


class WorkflowBlockedError(WorkflowError):
    """Raised when review determines that safe progress cannot continue."""

    def __init__(
        self,
        message: str,
        workflow: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.workflow = workflow


@dataclass(frozen=True)
class ComplexityAssessment:
    """Deterministic routing decision for one user requirement."""

    mode: str
    score: int
    reasons: List[str]


@dataclass
class PlanStep:
    """One executable unit in a structured development plan."""

    step_id: str
    objective: str
    description: str = ""
    dependencies: List[str] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)
    expected_outputs: List[str] = field(default_factory=list)
    allowed_tools: List[str] = field(default_factory=list)
    relevant_paths: List[str] = field(default_factory=list)
    status: str = "pending"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanStep":
        step_id = data.get("id", data.get("step_id"))
        objective = data.get("objective")
        if not isinstance(step_id, str) or not VALID_STEP_ID.fullmatch(step_id):
            raise WorkflowError(f"invalid plan step id: {step_id}")
        if not isinstance(objective, str) or not objective.strip():
            raise WorkflowError(f"plan step {step_id} has no objective")
        return cls(
            step_id=step_id,
            objective=objective.strip(),
            description=_string(data.get("description")),
            dependencies=_string_list(data.get("dependencies")),
            acceptance_criteria=_string_list(
                data.get("acceptance_criteria")
            ),
            expected_outputs=_string_list(data.get("expected_outputs")),
            allowed_tools=_string_list(data.get("allowed_tools")),
            relevant_paths=_string_list(data.get("relevant_paths")),
        )


@dataclass
class TaskPlan:
    """Validated DAG of development steps."""

    goal: str
    acceptance_criteria: List[str]
    assumptions: List[str]
    steps: List[PlanStep]
    version: int = 1

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        max_steps: int,
    ) -> "TaskPlan":
        goal = data.get("goal")
        raw_steps = data.get("steps")
        if not isinstance(goal, str) or not goal.strip():
            raise WorkflowError("plan has no goal")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise WorkflowError("plan must contain at least one step")
        if len(raw_steps) > max_steps:
            raise WorkflowError(
                f"plan contains {len(raw_steps)} steps; maximum is {max_steps}"
            )
        steps = [
            PlanStep.from_dict(item)
            for item in raw_steps
            if isinstance(item, dict)
        ]
        if len(steps) != len(raw_steps):
            raise WorkflowError("every plan step must be an object")
        plan = cls(
            goal=goal.strip(),
            acceptance_criteria=_string_list(
                data.get("acceptance_criteria")
            ),
            assumptions=_string_list(data.get("assumptions")),
            steps=steps,
            version=_positive_int(data.get("version"), default=1),
        )
        plan._validate_graph()
        return plan

    def ordered_steps(self) -> List[PlanStep]:
        by_id = {step.step_id: step for step in self.steps}
        pending = set(by_id)
        ordered: List[PlanStep] = []
        while pending:
            ready = [
                step
                for step in self.steps
                if step.step_id in pending
                and all(
                    dependency not in pending
                    for dependency in step.dependencies
                )
            ]
            if not ready:
                raise WorkflowError("plan dependency graph contains a cycle")
            for step in ready:
                ordered.append(step)
                pending.remove(step.step_id)
        return ordered

    def _validate_graph(self) -> None:
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise WorkflowError("plan step ids must be unique")
        known = set(ids)
        for step in self.steps:
            unknown = set(step.dependencies) - known
            if unknown:
                raise WorkflowError(
                    f"step {step.step_id} has unknown dependencies: "
                    f"{sorted(unknown)}"
                )
            if step.step_id in step.dependencies:
                raise WorkflowError(
                    f"step {step.step_id} cannot depend on itself"
                )
        self.ordered_steps()


@dataclass(frozen=True)
class ReviewFinding:
    """One actionable issue found by the reflection reviewer."""

    severity: str
    category: str
    evidence: str
    recommended_action: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewFinding":
        return cls(
            severity=_string(data.get("severity"), "medium"),
            category=_string(data.get("category"), "quality"),
            evidence=_string(data.get("evidence"), "未提供证据"),
            recommended_action=_string(
                data.get("recommended_action"),
                "检查并修正该问题",
            ),
        )


@dataclass(frozen=True)
class ReviewResult:
    """Structured evidence-based verdict."""

    verdict: str
    summary: str
    findings: List[ReviewFinding]
    criteria: List[Dict[str, str]]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewResult":
        verdict = str(data.get("verdict", "")).lower()
        if verdict not in VALID_VERDICTS:
            raise WorkflowError(f"invalid review verdict: {verdict}")
        raw_findings = data.get("findings", [])
        if not isinstance(raw_findings, list):
            raise WorkflowError("review findings must be a list")
        raw_criteria = data.get("criteria", [])
        if not isinstance(raw_criteria, list):
            raise WorkflowError("review criteria must be a list")
        criteria = []
        for item in raw_criteria:
            if isinstance(item, dict):
                criteria.append(
                    {
                        "criterion": _string(item.get("criterion")),
                        "status": _string(item.get("status"), "unknown"),
                    }
                )
        return cls(
            verdict=verdict,
            summary=_string(data.get("summary")),
            findings=[
                ReviewFinding.from_dict(item)
                for item in raw_findings
                if isinstance(item, dict)
            ],
            criteria=criteria,
        )


@dataclass(frozen=True)
class WorkflowConfig:
    """Resource and recursion limits for orchestration."""

    mode: str = "auto"
    complexity_threshold: int = 3
    max_plan_steps: int = 12
    max_step_revisions: int = 2
    planner_iterations: int = 24
    executor_iterations: int = 64
    reviewer_iterations: int = 24
    total_iteration_budget: int = 512
    iteration_extension: int = 16
    stagnation_limit: int = 6

    def __post_init__(self) -> None:
        if self.mode not in {"auto", "react", "plan"}:
            raise ValueError("agent mode must be auto, react, or plan")
        values = (
            self.complexity_threshold,
            self.max_plan_steps,
            self.planner_iterations,
            self.executor_iterations,
            self.reviewer_iterations,
            self.total_iteration_budget,
            self.iteration_extension,
            self.stagnation_limit,
        )
        if any(value < 1 for value in values):
            raise ValueError("workflow limits must be positive")
        if self.max_step_revisions < 0 or self.max_step_revisions > 3:
            raise ValueError("max_step_revisions must be from 0 to 3")


class ComplexityRouter:
    """Route obvious small work to ReAct and larger work to Plan-and-Act."""

    CATEGORY_KEYWORDS = {
        "architecture": {
            "architecture",
            "架构",
            "重构",
            "refactor",
            "模块化",
            "framework",
        },
        "data": {
            "数据库",
            "迁移",
            "schema",
            "migration",
            "数据模型",
        },
        "security": {
            "安全",
            "权限",
            "认证",
            "security",
            "permission",
            "authentication",
        },
        "cross_cutting": {
            "完整",
            "端到端",
            "多个模块",
            "跨模块",
            "全项目",
            "end-to-end",
            "multi-module",
        },
    }
    DELIVERY_SURFACES = {
        "api": {"api", "接口", "路由", "endpoint"},
        "ui": {"前端", "页面", "组件", "ui", "frontend"},
        "tests": {"测试", "test", "验证", "构建"},
        "configuration": {"配置", "config", "部署", "docker"},
        "documentation": {"文档", "readme", "说明"},
    }

    def __init__(self, threshold: int = 3) -> None:
        self.threshold = threshold

    def assess(self, request: str, forced_mode: str = "auto") -> ComplexityAssessment:
        if forced_mode == "react":
            return ComplexityAssessment("react", 0, ["用户配置强制 ReAct"])
        if forced_mode == "plan":
            return ComplexityAssessment(
                "plan_and_act",
                self.threshold,
                ["用户配置强制 Plan-and-Act"],
            )

        lowered = request.lower()
        score = 0
        reasons = []
        for category, keywords in self.CATEGORY_KEYWORDS.items():
            matched = sorted(word for word in keywords if word in lowered)
            if matched:
                score += 2 if category in {"architecture", "data"} else 1
                reasons.append(f"{category}: {', '.join(matched[:3])}")
        surfaces = [
            name
            for name, keywords in self.DELIVERY_SURFACES.items()
            if any(word in lowered for word in keywords)
        ]
        if len(surfaces) >= 2:
            score += 2
            reasons.append(f"跨交付面：{', '.join(surfaces)}")
        if len(request) >= 180:
            score += 1
            reasons.append("需求描述较长")
        enumerated = len(re.findall(r"(?:^|\n)\s*(?:\d+[.)、]|[-*])\s+", request))
        if enumerated >= 3:
            score += 2
            reasons.append("包含多个显式子目标")
        conjunctions = sum(
            lowered.count(word)
            for word in ("并且", "同时", "以及", "然后", " and ", " then ")
        )
        if conjunctions >= 1:
            score += 1
            reasons.append("存在多个相互关联的动作")
        mode = "plan_and_act" if score >= self.threshold else "react"
        if not reasons:
            reasons.append("未发现明显复杂度信号")
        return ComplexityAssessment(mode, score, reasons)


class PlannerAgent:
    """Use read-only ReAct reconnaissance to produce a structured plan."""

    def __init__(
        self,
        llm: ChatModel,
        tools: ToolRegistry,
        config: WorkflowConfig,
        context_manager: Optional[ContextManager],
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.agent = Agent(
            llm,
            tools,
            max_iterations=config.planner_iterations,
            system_prompt=PLANNER_PROMPT,
            context_manager=context_manager,
            progress_callback=progress_callback,
            progress_role="planner",
            iteration_extension=config.iteration_extension,
            stagnation_limit=config.stagnation_limit,
        )
        self.max_steps = config.max_plan_steps

    def create(
        self,
        request: str,
        context_messages: Sequence[Message],
    ) -> Tuple[TaskPlan, AgentResult]:
        result = self.agent.run(request, context_messages=context_messages)
        plan = TaskPlan.from_dict(
            _extract_json_object(result.content),
            self.max_steps,
        )
        return plan, result


class ReflectionAgent:
    """Review a step or complete task using only read-only tools."""

    def __init__(
        self,
        llm: ChatModel,
        tools: ToolRegistry,
        config: WorkflowConfig,
        context_manager: Optional[ContextManager],
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.agent = Agent(
            llm,
            tools,
            max_iterations=config.reviewer_iterations,
            system_prompt=REVIEWER_PROMPT,
            context_manager=context_manager,
            progress_callback=progress_callback,
            progress_role="reviewer",
            iteration_extension=config.iteration_extension,
            stagnation_limit=config.stagnation_limit,
        )

    def review(
        self,
        review_request: Dict[str, Any],
        context_messages: Sequence[Message],
    ) -> Tuple[ReviewResult, AgentResult]:
        result = self.agent.run(
            json.dumps(review_request, ensure_ascii=False, indent=2),
            context_messages=context_messages,
        )
        review = ReviewResult.from_dict(_extract_json_object(result.content))
        return review, result


class WorkflowOrchestrator:
    """Select ReAct or bounded Plan-and-Act with reflection."""

    def __init__(
        self,
        llm: ChatModel,
        executor_tools: ToolRegistry,
        planning_tools: ToolRegistry,
        review_tools: ToolRegistry,
        config: Optional[WorkflowConfig] = None,
        context_manager: Optional[ContextManager] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.llm = llm
        self.executor_tools = executor_tools
        self.tools = executor_tools
        self.config = config or WorkflowConfig()
        self.context_manager = context_manager
        self.progress_callback = progress_callback
        self.router = ComplexityRouter(self.config.complexity_threshold)
        self.planner = PlannerAgent(
            llm,
            planning_tools,
            self.config,
            context_manager,
            progress_callback,
        )
        self.reviewer = ReflectionAgent(
            llm,
            review_tools,
            self.config,
            context_manager,
            progress_callback,
        )

    def run(
        self,
        user_request: str,
        context_messages: Optional[Sequence[Message]] = None,
    ) -> AgentResult:
        if not user_request.strip():
            raise ValueError("user request cannot be empty")
        self._iteration_budget = IterationBudget(
            self.config.total_iteration_budget
        )
        self.planner.agent.iteration_budget = self._iteration_budget
        self.reviewer.agent.iteration_budget = self._iteration_budget
        context = list(context_messages or [])
        assessment = self.router.assess(user_request, self.config.mode)
        self._emit(
            "workflow_routed",
            mode=assessment.mode,
            score=assessment.score,
            reasons=assessment.reasons,
            message=(
                "复杂需求进入 Plan-and-Act"
                if assessment.mode == "plan_and_act"
                else "需求进入 ReAct 执行"
            ),
        )
        self._workflow_state: Dict[str, Any] = {
            "mode": assessment.mode,
            "assessment": asdict(assessment),
            "iteration_budget": self._budget_dict(),
        }
        try:
            if assessment.mode == "react":
                return self._run_react(user_request, context, assessment)
            return self._run_plan(user_request, context, assessment)
        except WorkflowBlockedError as exc:
            if exc.workflow is None:
                exc.workflow = dict(self._workflow_state)
            raise

    def _run_react(
        self,
        request: str,
        context: Sequence[Message],
        assessment: ComplexityAssessment,
    ) -> AgentResult:
        self._emit(
            "execution_started",
            message="Executor 正在理解项目并执行需求",
        )
        execution = self._execute(request, context)
        escalation = _plan_escalation(execution.content)
        runtime_reasons = _runtime_complexity_reasons(execution)
        if self.config.mode == "auto" and (escalation or runtime_reasons):
            reasons = list(assessment.reasons)
            if escalation:
                reasons.append(
                    "Executor 请求规划复杂子任务："
                    + _string(escalation.get("reason"), "未说明原因")
                )
            reasons.extend(runtime_reasons)
            upgraded = ComplexityAssessment(
                mode="plan_and_act",
                score=max(
                    self.config.complexity_threshold,
                    assessment.score,
                ),
                reasons=reasons,
            )
            planning_request = request
            if escalation:
                planning_request = json.dumps(
                    {
                        "original_request": request,
                        "complex_subtask": escalation,
                        "instruction": (
                            "根据当前项目状态规划复杂子任务和剩余验收工作。"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            return self._run_plan(
                request,
                context,
                upgraded,
                prior_results=[execution],
                planning_request=planning_request,
            )
        all_results = [execution]
        reviews: List[ReviewResult] = []
        should_review = any(
            item.name in {"apply_patch", "run_command"}
            for item in execution.tool_executions
        )
        if should_review:
            execution, review_results, extra_results = self._review_and_revise(
                original_request=request,
                step=None,
                execution=execution,
                context=context,
            )
            reviews.extend(review_results)
            all_results.extend(extra_results)
        result = _merge_results(all_results, execution.content)
        result.workflow = {
            "mode": "react",
            "assessment": asdict(assessment),
            "reviews": [asdict(review) for review in reviews],
            "iteration_budget": self._budget_dict(),
        }
        self._emit("workflow_completed", message="需求执行与评审已完成")
        return result

    def _run_plan(
        self,
        request: str,
        context: Sequence[Message],
        assessment: ComplexityAssessment,
        prior_results: Optional[List[AgentResult]] = None,
        planning_request: Optional[str] = None,
    ) -> AgentResult:
        self._emit("planning_started", message="Planner 正在调查项目并制定计划")
        plan, planning_result = self.planner.create(
            planning_request or request,
            context,
        )
        self._emit(
            "plan_created",
            goal=plan.goal,
            steps=[
                {
                    "step_id": step.step_id,
                    "objective": step.objective,
                }
                for step in plan.ordered_steps()
            ],
            message=f"计划已生成，共 {len(plan.steps)} 个步骤",
        )
        self._workflow_state.update(
            {
                "mode": "plan_and_act",
                "assessment": asdict(assessment),
                "plan": _plan_dict(plan),
                "steps": [],
                "reviews": [],
            }
        )
        all_results = [*(prior_results or []), planning_result]
        reviews: List[Dict[str, Any]] = []
        step_records = []
        completed_summaries: List[Dict[str, str]] = []

        ordered_steps = plan.ordered_steps()
        for step_index, step in enumerate(ordered_steps, start=1):
            step.status = "running"
            self._emit(
                "step_started",
                step_id=step.step_id,
                objective=step.objective,
                step_index=step_index,
                step_count=len(ordered_steps),
                message=(
                    f"正在执行步骤 {step_index}/{len(ordered_steps)}："
                    f"{step.objective}"
                ),
            )
            self._workflow_state["plan"] = _plan_dict(plan)
            step_request = self._step_request(
                request,
                plan,
                step,
                completed_summaries,
            )
            execution = self._execute(step_request, context)
            all_results.append(execution)
            execution, step_reviews, extra_results = self._review_and_revise(
                original_request=request,
                step=step,
                execution=execution,
                context=context,
            )
            all_results.extend(extra_results)
            reviews.extend(
                {
                    "scope": step.step_id,
                    **asdict(review),
                }
                for review in step_reviews
            )
            step.status = "completed"
            self._emit(
                "step_completed",
                step_id=step.step_id,
                objective=step.objective,
                step_index=step_index,
                step_count=len(ordered_steps),
                message=f"步骤已完成：{step.objective}",
            )
            completed_summaries.append(
                {
                    "step_id": step.step_id,
                    "objective": step.objective,
                    "result": execution.content[:4_000],
                }
            )
            step_records.append(
                {
                    "step_id": step.step_id,
                    "status": step.status,
                    "result": execution.content[:4_000],
                    "revisions": max(0, len(step_reviews) - 1),
                }
            )
            self._workflow_state.update(
                {
                    "plan": _plan_dict(plan),
                    "steps": list(step_records),
                    "reviews": list(reviews),
                }
            )

        final_review_request = {
            "scope": "final",
            "original_request": request,
            "acceptance_criteria": plan.acceptance_criteria,
            "plan": _plan_dict(plan),
            "completed_steps": completed_summaries,
            "evidence": _execution_evidence(
                [
                    execution
                    for execution in all_results
                    if execution is not planning_result
                ]
            ),
        }
        self._emit(
            "review_started",
            scope="final",
            message="Reflection 正在进行最终验收",
        )
        final_review, final_review_result = self.reviewer.review(
            final_review_request,
            context,
        )
        self._emit(
            "review_completed",
            scope="final",
            verdict=final_review.verdict,
            message=f"最终评审结论：{final_review.verdict}",
        )
        all_results.append(final_review_result)
        reviews.append({"scope": "final", **asdict(final_review)})
        self._workflow_state["reviews"] = list(reviews)
        final_revisions = 0
        while final_review.verdict == "revise":
            if final_revisions >= self.config.max_step_revisions:
                raise WorkflowBlockedError(
                    final_review.summary
                    or "final review exceeded the revision limit"
                )
            repair_request = self._repair_request(
                request,
                None,
                final_review,
            )
            repair = self._execute(repair_request, context)
            all_results.append(repair)
            repaired_review_request = {
                **final_review_request,
                "repair_result": repair.content[:4_000],
                "repair_evidence": _execution_evidence([repair]),
            }
            final_review, reviewed_repair = self.reviewer.review(
                repaired_review_request,
                context,
            )
            all_results.append(reviewed_repair)
            reviews.append(
                {"scope": "final-repair", **asdict(final_review)}
            )
            self._workflow_state["reviews"] = list(reviews)
            final_revisions += 1
        if final_review.verdict == "blocked":
            raise WorkflowBlockedError(
                final_review.summary or "final review blocked completion"
            )

        self._emit(
            "synthesis_started",
            message="正在整理执行结果并生成最终回复",
        )
        final_content = self._synthesize(
            request,
            plan,
            completed_summaries,
            reviews,
        )
        result = _merge_results(all_results, final_content)
        result.workflow = {
            "mode": "plan_and_act",
            "assessment": asdict(assessment),
            "plan": _plan_dict(plan),
            "steps": step_records,
            "reviews": reviews,
            "iteration_budget": self._budget_dict(),
        }
        self._emit("workflow_completed", message="计划、执行与评审已全部完成")
        return result

    def _execute(
        self,
        request: str,
        context: Sequence[Message],
    ) -> AgentResult:
        return Agent(
            self.llm,
            self.executor_tools,
            max_iterations=self.config.executor_iterations,
            system_prompt=EXECUTOR_PROMPT,
            context_manager=self.context_manager,
            progress_callback=self.progress_callback,
            progress_role="executor",
            iteration_budget=self._iteration_budget,
            iteration_extension=self.config.iteration_extension,
            stagnation_limit=self.config.stagnation_limit,
        ).run(request, context_messages=context)

    def _review_and_revise(
        self,
        original_request: str,
        step: Optional[PlanStep],
        execution: AgentResult,
        context: Sequence[Message],
    ) -> Tuple[AgentResult, List[ReviewResult], List[AgentResult]]:
        reviews = []
        extra_results = []
        current = execution
        for revision in range(self.config.max_step_revisions + 1):
            scope = step.step_id if step else "complete-react-task"
            self._emit(
                "review_started",
                scope=scope,
                revision=revision,
                message="Reflection 正在根据代码与验证证据进行评审",
            )
            review_request = {
                "scope": scope,
                "original_request": original_request,
                "step": asdict(step) if step else None,
                "execution_result": current.content[:4_000],
                "evidence": _execution_evidence([current]),
            }
            review, review_agent_result = self.reviewer.review(
                review_request,
                context,
            )
            reviews.append(review)
            extra_results.append(review_agent_result)
            self._emit(
                "review_completed",
                scope=scope,
                revision=revision,
                verdict=review.verdict,
                message=f"Reflection 评审结论：{review.verdict}",
            )
            if review.verdict == "pass":
                return current, reviews, extra_results
            if review.verdict == "blocked":
                raise WorkflowBlockedError(
                    review.summary or "review blocked execution"
                )
            if revision >= self.config.max_step_revisions:
                raise WorkflowBlockedError(
                    review.summary
                    or "review still requires changes after revision limit"
                )
            self._emit(
                "repair_started",
                scope=scope,
                revision=revision + 1,
                message="评审要求修订，Executor 正在修复问题",
            )
            current = self._execute(
                self._repair_request(original_request, step, review),
                context,
            )
            extra_results.append(current)
        raise WorkflowBlockedError("unreachable review state")

    def _emit(self, event: str, **details: Any) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback({"event": event, **details})
        except Exception:
            return

    def _budget_dict(self) -> Dict[str, int]:
        return {
            "used": self._iteration_budget.used,
            "maximum": self._iteration_budget.maximum,
            "remaining": (
                self._iteration_budget.maximum
                - self._iteration_budget.used
            ),
        }

    def _synthesize(
        self,
        request: str,
        plan: TaskPlan,
        completed_steps: List[Dict[str, str]],
        reviews: List[Dict[str, Any]],
    ) -> str:
        if not self._iteration_budget.consume():
            self._emit(
                "requirement_budget_reached",
                used=self._iteration_budget.used,
                maximum=self._iteration_budget.maximum,
                message="整个需求已达到灾难保护调用上限",
            )
            raise WorkflowError(
                "requirement exceeded its last-resort model-call budget"
            )
        message = {
            "role": "user",
            "content": json.dumps(
                {
                    "request": request,
                    "plan_goal": plan.goal,
                    "completed_steps": completed_steps,
                    "reviews": reviews,
                },
                ensure_ascii=False,
                indent=2,
            )[:30_000],
        }
        assistant = self.llm.complete(
            [
                {"role": "system", "content": SYNTHESIZER_PROMPT},
                message,
            ]
        )
        content = getattr(assistant, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise WorkflowError("synthesizer returned no final content")
        return content

    @staticmethod
    def _step_request(
        original_request: str,
        plan: TaskPlan,
        step: PlanStep,
        completed: List[Dict[str, str]],
    ) -> str:
        return json.dumps(
            {
                "mode": "execute_plan_step",
                "original_request": original_request,
                "plan_goal": plan.goal,
                "global_acceptance_criteria": plan.acceptance_criteria,
                "current_step": asdict(step),
                "completed_dependencies": completed,
                "instruction": (
                    "只完成 current_step；使用工具核对项目并验证结果。"
                    "不要提前执行无关后续步骤。"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _repair_request(
        original_request: str,
        step: Optional[PlanStep],
        review: ReviewResult,
    ) -> str:
        return json.dumps(
            {
                "mode": "repair_after_review",
                "original_request": original_request,
                "step": asdict(step) if step else None,
                "review": asdict(review),
                "instruction": (
                    "仅修复评审指出且有证据支持的问题，完成必要验证。"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )


def _extract_json_object(content: str) -> Dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise WorkflowError("model returned no structured JSON")
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise WorkflowError("model response does not contain a JSON object")
        try:
            data = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"invalid structured JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError("structured response must be a JSON object")
    return data


def _merge_results(
    results: Sequence[AgentResult],
    final_content: str,
) -> AgentResult:
    messages: List[Message] = []
    executions: List[ToolExecution] = []
    iterations = 0
    compactions = 0
    for result in results:
        messages.extend(result.messages)
        executions.extend(result.tool_executions)
        iterations += result.iterations
        compactions += result.compactions
    return AgentResult(
        content=final_content,
        iterations=iterations,
        messages=messages,
        tool_executions=executions,
        compactions=compactions,
    )


def _execution_evidence(results: Sequence[AgentResult]) -> List[Dict[str, Any]]:
    evidence = []
    for result in results:
        for execution in result.tool_executions:
            evidence.append(
                {
                    "tool": execution.name,
                    "arguments": execution.arguments[:2_000],
                    "result": execution.result[:4_000],
                }
            )
    return evidence[-30:]


def _plan_dict(plan: TaskPlan) -> Dict[str, Any]:
    return asdict(plan)


def _string(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    return default


def _plan_escalation(content: str) -> Optional[Dict[str, Any]]:
    try:
        data = _extract_json_object(content)
    except WorkflowError:
        return None
    if data.get("workflow_request") != "plan_and_act":
        return None
    objective = data.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        return None
    return data


def _runtime_complexity_reasons(result: AgentResult) -> List[str]:
    changed_paths = set()
    failed_validations = 0
    for execution in result.tool_executions:
        if execution.name == "apply_patch":
            try:
                arguments = json.loads(execution.arguments)
            except json.JSONDecodeError:
                arguments = {}
            path = arguments.get("path") if isinstance(arguments, dict) else None
            if isinstance(path, str):
                changed_paths.add(path)
        elif execution.name == "run_command" and "Exit code: 0" not in execution.result:
            failed_validations += 1
    reasons = []
    if len(changed_paths) >= 3:
        reasons.append("ReAct 已涉及至少三个修改文件")
    if failed_validations >= 2:
        reasons.append("ReAct 中出现多次验证失败")
    if len(result.tool_executions) >= 8:
        reasons.append("ReAct 工具调用数量达到复杂任务阈值")
    if result.compactions >= 2:
        reasons.append("ReAct 上下文多次压缩")
    return reasons
