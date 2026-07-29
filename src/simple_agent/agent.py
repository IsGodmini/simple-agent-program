"""Tool-using agent loop."""

from dataclasses import dataclass
from typing import Any, Dict, List

from .llm import ChatModel, Message
from .prompts import SYSTEM_PROMPT
from .tools import ToolRegistry


@dataclass
class AgentResult:
    """Final result and execution details for one user request."""

    content: str
    iterations: int
    messages: List[Message]


class Agent:
    """Run the model until it returns a final answer or reaches a safety limit."""

    def __init__(
        self,
        llm: ChatModel,
        tools: ToolRegistry,
        max_iterations: int = 12,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self.llm = llm
        self.tools = tools
        self.max_iterations = max_iterations
        self.system_prompt = system_prompt

    def run(self, user_request: str) -> AgentResult:
        if not user_request.strip():
            raise ValueError("user request cannot be empty")

        messages: List[Message] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_request},
        ]

        for iteration in range(1, self.max_iterations + 1):
            assistant = self.llm.complete(messages, self.tools.definitions)
            messages.append(self._assistant_message(assistant))

            tool_calls = getattr(assistant, "tool_calls", None) or []
            if tool_calls:
                for tool_call in tool_calls:
                    result = self.tools.execute(
                        tool_call.function.name,
                        tool_call.function.arguments,
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
                return AgentResult(
                    content=content,
                    iterations=iteration,
                    messages=messages,
                )
            raise RuntimeError("Model returned neither content nor tool calls")

        raise RuntimeError(
            f"Agent exceeded the maximum of {self.max_iterations} iterations"
        )

    @staticmethod
    def _assistant_message(assistant: Any) -> Dict[str, Any]:
        if hasattr(assistant, "model_dump"):
            return assistant.model_dump(exclude_none=True)

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
