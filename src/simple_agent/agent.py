"""Tool-using agent loop."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from .context import ContextManager
from .llm import ChatModel, Message
from .prompts import SYSTEM_PROMPT
from .tools import ToolRegistry


@dataclass
class ToolExecution:
    """One local tool invocation performed for the model."""

    tool_call_id: str
    name: str
    arguments: str
    result: str


@dataclass
class AgentResult:
    """Final result and execution details for one user request."""

    content: str
    iterations: int
    messages: List[Message]
    tool_executions: List[ToolExecution]
    compactions: int = 0
    workflow: Optional[Dict[str, Any]] = None
    stop_reason: Optional[str] = None


@dataclass
class IterationBudget:
    """A hard requirement-wide model-call cap shared by all sub-agents."""

    maximum: int
    used: int = 0

    def __post_init__(self) -> None:
        if self.maximum < 1:
            raise ValueError("iteration budget maximum must be positive")

    def consume(self, reserve_final: bool = False) -> bool:
        limit = self.maximum - (1 if reserve_final else 0)
        if self.used >= limit:
            return False
        self.used += 1
        return True

    @property
    def remaining(self) -> int:
        return max(0, self.maximum - self.used)

    @property
    def warning_threshold(self) -> int:
        """Reserve a small final window in which agents must converge."""

        return min(12, max(2, self.maximum // 5))


class Agent:
    """Run the model until it returns a final answer or reaches a safety limit."""

    def __init__(
        self,
        llm: ChatModel,
        tools: ToolRegistry,
        max_iterations: int = 64,
        system_prompt: str = SYSTEM_PROMPT,
        context_manager: Optional[ContextManager] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        progress_role: str = "executor",
        iteration_budget: Optional[IterationBudget] = None,
        iteration_extension: int = 16,
        stagnation_limit: int = 6,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if iteration_extension < 1:
            raise ValueError("iteration_extension must be at least 1")
        if stagnation_limit < 1:
            raise ValueError("stagnation_limit must be at least 1")
        self.llm = llm
        self.tools = tools
        self.max_iterations = max_iterations
        self.system_prompt = system_prompt
        self.context_manager = context_manager
        self.progress_callback = progress_callback
        self.progress_role = progress_role
        self.iteration_budget = iteration_budget
        self.iteration_extension = iteration_extension
        self.stagnation_limit = stagnation_limit

    def run(
        self,
        user_request: str,
        context_messages: Optional[Sequence[Message]] = None,
        root_requirement: Optional[str] = None,
    ) -> AgentResult:
        if not user_request.strip():
            raise ValueError("user request cannot be empty")
        active_requirement = (
            root_requirement.strip()
            if isinstance(root_requirement, str) and root_requirement.strip()
            else user_request.strip()
        )

        messages: List[Message] = [
            {"role": "system", "content": self.system_prompt},
            *(
                [
                    {
                        "role": "system",
                        "content": self._budget_guidance(),
                    }
                ]
                if self.iteration_budget
                else []
            ),
            *(dict(message) for message in (context_messages or [])),
            {"role": "user", "content": user_request},
        ]
        tool_executions: List[ToolExecution] = []
        compactions = 0
        current_allowance = self.max_iterations
        seen_evidence: Set[str] = set()
        stagnant_iterations = 0
        iteration = 0
        budget_warning_sent = False

        while True:
            iteration += 1
            if (
                self.iteration_budget
                and not self.iteration_budget.consume(reserve_final=True)
            ):
                self._emit(
                    "requirement_budget_reached",
                    iteration=iteration,
                    used=self.iteration_budget.used,
                    maximum=self.iteration_budget.maximum,
                    message=(
                        "执行调用额度已耗尽，正在使用保留调用生成最终答复"
                    ),
                )
                return self._force_final_answer(
                    messages,
                    tool_executions,
                    compactions,
                    iteration,
                    "budget_exhausted",
                    "需求已达到模型调用硬上限",
                    active_requirement,
                )
            if (
                self.iteration_budget
                and not budget_warning_sent
                and self.iteration_budget.remaining
                <= self.iteration_budget.warning_threshold
            ):
                budget_warning_sent = True
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "需求共享模型调用预算即将耗尽：仅剩 "
                            f"{self.iteration_budget.remaining} 次调用。立即收敛："
                            "停止扩展调查范围，只执行完成当前需求所必需的修改和"
                            "验证；如果已经具备足够证据，请直接返回最终结果；"
                            "如果无法安全完成，请明确说明阻塞原因，不要继续尝试"
                            "相似工具调用。"
                        ),
                    }
                )
                self._emit(
                    "requirement_budget_low",
                    iteration=iteration,
                    requirement_used=self.iteration_budget.used,
                    requirement_maximum=self.iteration_budget.maximum,
                    remaining=self.iteration_budget.remaining,
                    message=(
                        "需求模型调用预算即将耗尽，Agent 已进入收敛阶段"
                    ),
                )
            self._emit(
                "model_started",
                iteration=iteration,
                allowance=current_allowance,
                requirement_used=(
                    self.iteration_budget.used
                    if self.iteration_budget
                    else iteration
                ),
                requirement_maximum=(
                    self.iteration_budget.maximum
                    if self.iteration_budget
                    else None
                ),
                message=f"{self.progress_role} 正在分析并决定下一步",
            )
            goal_reminder = {
                "role": "user",
                "content": self._goal_reminder(
                    active_requirement,
                    user_request,
                    iteration,
                ),
            }
            request_messages = [*messages, goal_reminder]
            if self.context_manager:
                prepared = self.context_manager.prepare(
                    request_messages,
                    self.tools.definitions,
                )
                request_messages = prepared.messages
                messages = [
                    message
                    for message in prepared.messages
                    if message != goal_reminder
                ]
                compactions += prepared.removed_blocks
                if prepared.removed_blocks:
                    self._emit(
                        "context_compacted",
                        iteration=iteration,
                        removed_blocks=prepared.removed_blocks,
                        message="已压缩较早的工具交互以控制上下文长度",
                    )
            try:
                assistant = self.llm.complete(
                    request_messages,
                    self.tools.definitions,
                )
            except Exception as exc:
                self._emit(
                    "model_failed",
                    iteration=iteration,
                    error=type(exc).__name__,
                    message="模型请求异常，正在生成可展示的最终答复",
                )
                return self._force_final_answer(
                    messages,
                    tool_executions,
                    compactions,
                    iteration + 1,
                    "model_error",
                    f"模型请求异常：{type(exc).__name__}",
                    active_requirement,
                )
            messages.append(self._assistant_message(assistant))

            tool_calls = getattr(assistant, "tool_calls", None) or []
            if tool_calls:
                intent = self._public_intent(
                    getattr(assistant, "content", None),
                    [call.function.name for call in tool_calls],
                )
                self._emit(
                    "model_intent",
                    iteration=iteration,
                    intent=intent,
                    tools=[call.function.name for call in tool_calls],
                    message=intent,
                )
                new_evidence = False
                for tool_call in tool_calls:
                    self._emit(
                        "tool_started",
                        iteration=iteration,
                        tool=tool_call.function.name,
                        message=f"正在调用工具：{tool_call.function.name}",
                    )
                    result = self.tools.execute(
                        tool_call.function.name,
                        tool_call.function.arguments,
                    )
                    evidence = self._evidence_fingerprint(
                        tool_call.function.name,
                        tool_call.function.arguments,
                        result,
                    )
                    if (
                        evidence is not None
                        and evidence not in seen_evidence
                    ):
                        seen_evidence.add(evidence)
                        new_evidence = True
                    self._emit(
                        "tool_completed",
                        iteration=iteration,
                        tool=tool_call.function.name,
                        message=f"工具执行完成：{tool_call.function.name}",
                    )
                    tool_executions.append(
                        ToolExecution(
                            tool_call_id=tool_call.id,
                            name=tool_call.function.name,
                            arguments=tool_call.function.arguments,
                            result=result,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        }
                    )
                if new_evidence:
                    stagnant_iterations = 0
                else:
                    stagnant_iterations += 1
                    self._emit(
                        "stagnation_observed",
                        iteration=iteration,
                        stagnant_iterations=stagnant_iterations,
                        stagnation_limit=self.stagnation_limit,
                        message=(
                            "本轮没有产生新的工具证据，"
                            f"连续无进展 {stagnant_iterations}/"
                            f"{self.stagnation_limit} 轮"
                        ),
                    )
                if stagnant_iterations >= self.stagnation_limit:
                    self._emit(
                        "stagnation_detected",
                        iteration=iteration,
                        stagnant_iterations=stagnant_iterations,
                        message=(
                            "检测到连续重复操作或结果，"
                            "已停止无进展循环"
                        ),
                    )
                    return self._force_final_answer(
                        messages,
                        tool_executions,
                        compactions,
                        iteration + 1,
                        "stagnation",
                        (
                            f"连续 {stagnant_iterations} 轮工具调用"
                            "没有产生新证据"
                        ),
                        active_requirement,
                    )
                if iteration >= current_allowance:
                    previous_allowance = current_allowance
                    current_allowance += self.iteration_extension
                    self._emit(
                        "iteration_budget_extended",
                        iteration=iteration,
                        previous_allowance=previous_allowance,
                        allowance=current_allowance,
                        message=(
                            (
                                "Agent 仍在产生新证据"
                                if new_evidence
                                else "尚未达到连续停滞阈值"
                            )
                            + "，执行额度已自动扩展至 "
                            + f"{current_allowance} 轮"
                        ),
                    )
                continue

            content = getattr(assistant, "content", None)
            if content:
                self._emit(
                    "agent_completed",
                    iteration=iteration,
                    message=f"{self.progress_role} 已完成当前任务",
                )
                return AgentResult(
                    content=content,
                    iterations=iteration,
                    messages=messages,
                    tool_executions=tool_executions,
                    compactions=compactions,
                )
            return self._force_final_answer(
                messages,
                tool_executions,
                compactions,
                iteration + 1,
                "empty_model_response",
                "模型返回了空响应",
                active_requirement,
            )

    def _force_final_answer(
        self,
        messages: List[Message],
        tool_executions: List[ToolExecution],
        compactions: int,
        iteration: int,
        stop_reason: str,
        reason_text: str,
        root_requirement: str,
    ) -> AgentResult:
        """Spend the reserved call on a tool-free answer, then fall back locally."""

        final_messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    "现在必须立即结束工具循环并给出面向用户的最终答复。禁止继续"
                    "调用任何工具。请仅根据已经获得的证据说明：已完成什么、"
                    "验证情况、尚未完成或无法确认的部分，以及建议的下一步。"
                    f"停止原因：{reason_text}。不要声称未经验证的工作已经完成。"
                    "\n必须继续以以下原始用户需求为唯一目标：\n"
                    f"<original_user_requirement>\n"
                    f"{root_requirement[:8_000]}\n"
                    "</original_user_requirement>"
                ),
            },
        ]
        self._emit(
            "final_answer_started",
            iteration=iteration,
            stop_reason=stop_reason,
            message="工具循环已停止，正在生成最终答复",
        )
        can_call_model = (
            self.iteration_budget is None
            or self.iteration_budget.consume()
        )
        if can_call_model:
            try:
                if self.context_manager:
                    prepared = self.context_manager.prepare(final_messages, [])
                    final_messages = prepared.messages
                    compactions += prepared.removed_blocks
                assistant = self.llm.complete(final_messages, [])
                content = getattr(assistant, "content", None)
                if isinstance(content, str) and content.strip():
                    final_messages.append(self._assistant_message(assistant))
                    self._emit(
                        "final_answer_completed",
                        iteration=iteration,
                        stop_reason=stop_reason,
                        generated_by_model=True,
                        message="模型已基于现有证据给出最终答复",
                    )
                    return AgentResult(
                        content=content.strip(),
                        iterations=iteration,
                        messages=final_messages,
                        tool_executions=tool_executions,
                        compactions=compactions,
                        stop_reason=stop_reason,
                    )
            except Exception as exc:
                self._emit(
                    "final_answer_model_failed",
                    iteration=iteration,
                    stop_reason=stop_reason,
                    error=type(exc).__name__,
                    message="最终模型调用失败，正在生成本地兜底答复",
                )

        content = self._fallback_answer(
            reason_text,
            tool_executions,
        )
        final_messages.append({"role": "assistant", "content": content})
        self._emit(
            "final_answer_completed",
            iteration=iteration,
            stop_reason=stop_reason,
            generated_by_model=False,
            message="已生成本地兜底答复",
        )
        return AgentResult(
            content=content,
            iterations=iteration,
            messages=final_messages,
            tool_executions=tool_executions,
            compactions=compactions,
            stop_reason=stop_reason,
        )

    @staticmethod
    def _fallback_answer(
        reason_text: str,
        tool_executions: Sequence[ToolExecution],
    ) -> str:
        tools = [execution.name for execution in tool_executions[-8:]]
        activity = (
            "、".join(tools)
            if tools
            else "尚未获得可确认的工具执行结果"
        )
        return (
            "本次需求已停止继续执行，但未能获得模型生成的最终回复。\n\n"
            f"- 停止原因：{reason_text}\n"
            f"- 已执行的最近工具：{activity}\n"
            "- 完成状态：无法确认需求已经完整实现\n"
            "- 建议：检查当前工作区改动和测试结果后，再从未完成部分继续。"
        )

    def _budget_guidance(self) -> str:
        assert self.iteration_budget is not None
        return (
            "本需求的 Planner、Executor、Reviewer 和结果整理共享一个不可突破"
            f"的模型调用预算，共 {self.iteration_budget.maximum} 次；当前剩余 "
            f"{self.iteration_budget.remaining} 次。每次调用都应推进需求：优先"
            "复用项目索引，避免重复读取和无目的探索，获得足够证据后及时修改、"
            "验证并结束。接近预算上限时必须收敛或明确报告阻塞。"
        )

    @staticmethod
    def _goal_reminder(
        root_requirement: str,
        current_scope: str,
        iteration: int,
    ) -> str:
        root = root_requirement[:8_000]
        scope = current_scope[:4_000]
        return (
            f"任务锚点（第 {iteration} 次调用，本消息不是新需求）：\n"
            f"<original_user_requirement>\n{root}\n"
            "</original_user_requirement>\n"
            f"<current_agent_scope>\n{scope}\n</current_agent_scope>\n"
            "只执行能直接推进原始需求或其当前验收条件的动作。调用工具前先"
            "确认该结果会改变实现决策、完成代码修改或验证结果；不要重复读取"
            "已有证据，不要为了继续调查而调查。如果现有证据已经足够，立即"
            "给出最终回复，不再调用工具。不要输出内部思维链。"
        )

    @staticmethod
    def _evidence_fingerprint(
        tool_name: str,
        raw_arguments: str,
        result: str,
    ) -> Optional[str]:
        if result.lstrip().lower().startswith("tool error:"):
            return None
        try:
            arguments = json.loads(raw_arguments or "{}")
            normalized_arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (json.JSONDecodeError, TypeError):
            normalized_arguments = raw_arguments
        payload = "\n".join(
            [tool_name, normalized_arguments, result.strip()]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _public_intent(
        self,
        content: Any,
        tool_names: List[str],
    ) -> str:
        if isinstance(content, str):
            compact = " ".join(content.strip().split())
            prefix = "行动说明："
            if compact.startswith(prefix):
                intent = compact[len(prefix) :].strip()
                if intent:
                    return intent[:300]
        tools = "、".join(dict.fromkeys(tool_names))
        role = {
            "planner": "Planner",
            "reviewer": "Reflection",
            "executor": "Executor",
        }.get(self.progress_role, self.progress_role)
        return f"{role} 准备使用 {tools} 获取下一步所需的项目证据"

    def _emit(self, event: str, **details: Any) -> None:
        if self.progress_callback is None:
            return
        payload = {
            "event": event,
            "role": self.progress_role,
            **details,
        }
        try:
            self.progress_callback(payload)
        except Exception:
            # Progress reporting must never interrupt the actual agent.
            return

    @staticmethod
    def _assistant_message(assistant: Any) -> Dict[str, Any]:
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": getattr(assistant, "content", None),
        }
        tool_calls = getattr(assistant, "tool_calls", None)
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in tool_calls
            ]
        return message
