"""OpenAI-compatible chat model adapter."""

from typing import Any, Dict, List, Optional, Protocol

from openai import OpenAI

from .config import Settings

Message = Dict[str, Any]
ToolDefinition = Dict[str, Any]


class ChatModel(Protocol):
    """The small model interface required by the agent."""

    def complete(
        self,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
    ) -> Any:
        """Return one assistant message."""


class OpenAICompatibleLLM:
    """Chat Completions client for OpenAI-compatible providers."""

    def __init__(self, settings: Settings) -> None:
        self.model = settings.model
        self.max_output_tokens = settings.max_output_tokens
        self.client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

    def complete(
        self,
        messages: List[Message],
        tools: Optional[List[ToolDefinition]] = None,
    ) -> Any:
        request: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        response = self.client.chat.completions.create(**request)
        return response.choices[0].message
