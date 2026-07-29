"""Application configuration."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    """Configuration required by the LLM client and agent loop."""

    model: str
    base_url: str
    api_key: str
    max_iterations: int = 12
    context_window: int = 128_000
    max_input_tokens: int = 96_000
    max_output_tokens: int = 16_000
    compact_at_tokens: int = 80_000

    def __post_init__(self) -> None:
        budget_values = (
            self.context_window,
            self.max_input_tokens,
            self.max_output_tokens,
            self.compact_at_tokens,
        )
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if any(value < 1 for value in budget_values):
            raise ValueError("all context budget values must be positive")
        if self.compact_at_tokens > self.max_input_tokens:
            raise ValueError(
                "AGENT_COMPACT_AT_TOKENS cannot exceed LLM_MAX_INPUT_TOKENS"
            )
        if self.max_input_tokens + self.max_output_tokens > self.context_window:
            raise ValueError(
                "LLM_MAX_INPUT_TOKENS + LLM_MAX_OUTPUT_TOKENS "
                "cannot exceed LLM_CONTEXT_WINDOW"
            )

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            model=os.getenv("LLM_MODEL", "ark-code-latest"),
            base_url=_required_env("LLM_BASE_URL"),
            api_key=_required_env("LLM_API_KEY"),
            max_iterations=int(os.getenv("AGENT_MAX_ITERATIONS", "12")),
            context_window=int(os.getenv("LLM_CONTEXT_WINDOW", "128000")),
            max_input_tokens=int(os.getenv("LLM_MAX_INPUT_TOKENS", "96000")),
            max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "16000")),
            compact_at_tokens=int(
                os.getenv("AGENT_COMPACT_AT_TOKENS", "80000")
            ),
        )
