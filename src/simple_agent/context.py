"""Conservative context-window budgeting and deterministic compaction."""

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from .llm import Message, ToolDefinition


class ContextLimitError(RuntimeError):
    """Raised when a request cannot fit safely within the configured budget."""


@dataclass(frozen=True)
class ContextBudget:
    """Token budgets applied before every model request."""

    context_window: int
    max_input_tokens: int
    max_output_tokens: int
    compact_at_tokens: int

    def __post_init__(self) -> None:
        values = {
            "context_window": self.context_window,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "compact_at_tokens": self.compact_at_tokens,
        }
        if any(value < 1 for value in values.values()):
            raise ValueError("all context budget values must be positive")
        if self.compact_at_tokens > self.max_input_tokens:
            raise ValueError("compact_at_tokens cannot exceed max_input_tokens")
        if self.max_input_tokens + self.max_output_tokens > self.context_window:
            raise ValueError(
                "max_input_tokens + max_output_tokens cannot exceed context_window"
            )


@dataclass
class PreparedContext:
    """Messages ready to send and compaction metadata."""

    messages: List[Message]
    estimated_tokens: int
    removed_blocks: int = 0


class ContextManager:
    """Estimate request size and remove only complete old interaction blocks."""

    def __init__(self, budget: ContextBudget) -> None:
        self.budget = budget

    def prepare(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> PreparedContext:
        active = [dict(message) for message in messages]
        estimated = self.estimate_request(active, tools)
        if estimated <= self.budget.compact_at_tokens:
            return PreparedContext(active, estimated)

        if len(active) < 2:
            self._raise_limit(estimated)

        fixed = active[:2]
        blocks = self._interaction_blocks(active[2:])
        removed = 0

        while len(blocks) > 1:
            blocks.pop(0)
            removed += 1
            candidate = self._with_notice(fixed, blocks, removed)
            estimated = self.estimate_request(candidate, tools)
            if estimated <= self.budget.compact_at_tokens:
                return PreparedContext(candidate, estimated, removed)

        candidate = self._with_notice(fixed, blocks, removed)
        estimated = self.estimate_request(candidate, tools)
        if estimated > self.budget.max_input_tokens:
            self._raise_limit(estimated)
        return PreparedContext(candidate, estimated, removed)

    def estimate_request(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> int:
        """Estimate tokens conservatively across Latin text, CJK, and JSON."""
        payload: Dict[str, Any] = {
            "messages": list(messages),
            "tools": list(tools),
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        content_tokens = math.ceil(len(serialized.encode("utf-8")) / 3)
        protocol_overhead = 8 * len(messages) + 16 * len(tools) + 32
        return content_tokens + protocol_overhead

    @staticmethod
    def _interaction_blocks(messages: Sequence[Message]) -> List[List[Message]]:
        blocks: List[List[Message]] = []
        current: List[Message] = []
        for message in messages:
            if message.get("role") == "assistant":
                if current:
                    blocks.append(current)
                current = [message]
            else:
                current.append(message)
        if current:
            blocks.append(current)
        return blocks

    @staticmethod
    def _with_notice(
        fixed: List[Message],
        blocks: Sequence[Sequence[Message]],
        removed: int,
    ) -> List[Message]:
        messages = list(fixed)
        if removed:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"{removed} earlier tool-interaction block(s) were "
                        "removed to stay within the context budget. Re-read "
                        "files if their exact contents are needed."
                    ),
                }
            )
        for block in blocks:
            messages.extend(block)
        return messages

    def _raise_limit(self, estimated: int) -> None:
        raise ContextLimitError(
            "Request is too large after safe compaction: "
            f"estimated {estimated} input tokens, "
            f"limit {self.budget.max_input_tokens}"
        )
