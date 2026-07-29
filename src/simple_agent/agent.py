"""Tool-using agent loop."""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

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


class Agent:
    """Run the model until it returns a final answer or reaches a safety limit."""

    def __init__(
        self,
        llm: ChatModel,
        tools: ToolRegistry,
        max_iterations: int = 12,
        system_prompt: str = SYSTEM_PROMPT,
        context_manager: Optional[ContextManager] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        progress_role: str = "executor",
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self.llm = llm
        self.tools = tools
        self.max_iterations = max_iterations
        self.system_prompt = system_prompt
        self.context_manager = context_manager
        self.progress_callback = progress_callback
        self.progress_role = progress_role

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

        for iteration in range(1, self.max_iterations + 1):
            self._emit(
                "model_started",
                iteration=iteration,
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

        raise RuntimeError(
            f"Agent exceeded the maximum of {self.max_iterations} iterations"
        )

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
