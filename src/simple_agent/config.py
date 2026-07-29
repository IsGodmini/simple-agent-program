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
    max_iterations: int = 64
    total_iteration_budget: int = 512
    iteration_extension: int = 16
    stagnation_limit: int = 6
    context_window: int = 128_000
    max_input_tokens: int = 96_000
    max_output_tokens: int = 16_000
    compact_at_tokens: int = 80_000
    agent_mode: str = "auto"
    plan_complexity_threshold: int = 3
    max_plan_steps: int = 12
    max_step_revisions: int = 2
    planner_max_iterations: int = 24
    reviewer_max_iterations: int = 24

    def __post_init__(self) -> None:
        budget_values = (
            self.context_window,
            self.max_input_tokens,
            self.max_output_tokens,
            self.compact_at_tokens,
        )
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.agent_mode not in {"auto", "react", "plan"}:
            raise ValueError("AGENT_MODE must be auto, react, or plan")
        workflow_values = (
            self.plan_complexity_threshold,
            self.max_plan_steps,
            self.planner_max_iterations,
            self.reviewer_max_iterations,
            self.total_iteration_budget,
            self.iteration_extension,
            self.stagnation_limit,
        )
        if any(value < 1 for value in workflow_values):
            raise ValueError("all workflow limits must be positive")
        if not 0 <= self.max_step_revisions <= 3:
            raise ValueError("AGENT_MAX_STEP_REVISIONS must be from 0 to 3")
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
            max_iterations=int(os.getenv("AGENT_MAX_ITERATIONS", "64")),
            total_iteration_budget=int(
                os.getenv("AGENT_TOTAL_ITERATION_BUDGET", "512")
            ),
            iteration_extension=int(
                os.getenv("AGENT_ITERATION_EXTENSION", "16")
            ),
            stagnation_limit=int(
                os.getenv("AGENT_STAGNATION_LIMIT", "6")
            ),
            context_window=int(os.getenv("LLM_CONTEXT_WINDOW", "128000")),
            max_input_tokens=int(os.getenv("LLM_MAX_INPUT_TOKENS", "96000")),
            max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "16000")),
            compact_at_tokens=int(
                os.getenv("AGENT_COMPACT_AT_TOKENS", "80000")
            ),
            agent_mode=os.getenv("AGENT_MODE", "auto").lower(),
            plan_complexity_threshold=int(
                os.getenv("AGENT_PLAN_COMPLEXITY_THRESHOLD", "3")
            ),
            max_plan_steps=int(os.getenv("AGENT_MAX_PLAN_STEPS", "12")),
            max_step_revisions=int(
                os.getenv("AGENT_MAX_STEP_REVISIONS", "2")
            ),
            planner_max_iterations=int(
                os.getenv("AGENT_PLANNER_MAX_ITERATIONS", "24")
            ),
            reviewer_max_iterations=int(
                os.getenv("AGENT_REVIEWER_MAX_ITERATIONS", "24")
            ),
        )
