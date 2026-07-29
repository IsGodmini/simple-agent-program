"""A minimal LLM client for an OpenAI-compatible API."""

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


LLM_MODEL = os.getenv("LLM_MODEL", "ark-code-latest")

llm = OpenAI(
    api_key=_required_env("LLM_API_KEY"),
    base_url=_required_env("LLM_BASE_URL"),
)


def chat(prompt: str, system_prompt: str = "You are a helpful assistant.") -> str:
    """Send one prompt to the configured model and return its text response."""
    response = llm.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content or ""


if __name__ == "__main__":
    print(chat("你好，请用一句话介绍你自己。"))
