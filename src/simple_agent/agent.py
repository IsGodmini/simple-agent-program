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


@dataclass
class IterationBudget:
    """A requirement-wide last-resort cap shared by all sub-agents."""

    maximum: int
    used: int = 0

    def __post_init__(self) -> None:
        if self.maximum < 1:
            raise ValueError("iteration budget maximum must be positive")

    def consume(self) -> bool:
        if self.used >= self.maximum:
            return False
        self.used += 1
        return True


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
    ) -> AgentResult:
        if not user_request.strip():
            raise ValueError("user request cannot be empty")

        messages: List[Message] = [
            {"role": "system", "content": self.system_prompt},
            *(dict(message) for message in (context_messages or [])),
            {"role": "user", "content": user_request},
        ]
        tool_executions: List[ToolExecution] = []
        compactions = 0
        current_allowance = self.max_iterations
        seen_evidence: Set[str] = set()
        stagnant_iterations = 0
        iteration = 0

        while True:
            iteration += 1
            if self.iteration_budget and not self.iteration_budget.consume():
                self._emit(
                    "requirement_budget_reached",
                    iteration=iteration - 1,
                    used=self.iteration_budget.used,
                    maximum=self.iteration_budget.maximum,
                    message=(
                        "整个需求已达到灾难保护调用上限；"
                        "这是防止异常无限循环的最后保护"
                    ),
                )
                raise RuntimeError(
                    "Requirement exceeded the last-resort model-call budget "
                    f"of {self.iteration_budget.maximum}"
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
            if self.context_manager:
                prepared = self.context_manager.prepare(
                    messages,
                    self.tools.definitions,
                )
                messages = prepared.messages
                compactions += prepared.removed_blocks
                if prepared.removed_blocks:
                    self._emit(
                        "context_compacted",
                        iteration=iteration,
                        removed_blocks=prepared.removed_blocks,
                        message="已压缩较早的工具交互以控制上下文长度",
                    )
            assistant = self.llm.complete(messages, self.tools.definitions)
            messages.append(self._assistant_message(assistant))

            tool_calls = getattr(assistant, "tool_calls", None) or []
            if tool_calls:
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
                    raise RuntimeError(
                        "Agent stopped after "
                        f"{stagnant_iterations} consecutive tool rounds "
                        "without new evidence"
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
            raise RuntimeError("Model returned neither content nor tool calls")

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
