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

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        max_iterations = int(os.getenv("AGENT_MAX_ITERATIONS", "12"))
        if max_iterations < 1:
            raise ValueError("AGENT_MAX_ITERATIONS must be at least 1")

        return cls(
            model=os.getenv("LLM_MODEL", "ark-code-latest"),
            base_url=_required_env("LLM_BASE_URL"),
            api_key=_required_env("LLM_API_KEY"),
            max_iterations=max_iterations,
        )
