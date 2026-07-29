"""Tool protocol and dispatch."""

import json
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List

ToolDefinition = Dict[str, Any]


class Tool(ABC):
    """A callable capability exposed to the model."""

    name: str
    description: str
    parameters: Dict[str, Any]

    @property
    def definition(self) -> ToolDefinition:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    def execute(self, arguments: Dict[str, Any]) -> str:
        """Execute the tool and return text for the model."""


class ToolRegistry:
    """Expose tool definitions and safely dispatch model requests."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}
        if not self._tools:
            raise ValueError("At least one tool is required")

    @property
    def definitions(self) -> List[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def execute(self, name: str, raw_arguments: str) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Tool error: unknown tool '{name}'"

        try:
            arguments = json.loads(raw_arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
            return tool.execute(arguments)
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as exc:
            return f"Tool error: {exc}"
