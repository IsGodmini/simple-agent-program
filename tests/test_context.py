import unittest
from types import SimpleNamespace

from simple_agent.config import Settings
from simple_agent.context import (
    ContextBudget,
    ContextLimitError,
    ContextManager,
)
from simple_agent.llm import OpenAICompatibleLLM


def tool_block(call_id, result):
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "file.py"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": result,
        },
    ]


class ContextManagerTests(unittest.TestCase):
    def test_old_complete_tool_block_is_compacted(self):
        manager = ContextManager(
            ContextBudget(
                context_window=2_000,
                max_input_tokens=1_500,
                max_output_tokens=500,
                compact_at_tokens=500,
            )
        )
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            *tool_block("old-call", "old content " * 200),
            *tool_block("latest-call", "latest result"),
        ]

        prepared = manager.prepare(messages, [])

        serialized = str(prepared.messages)
        self.assertEqual(prepared.removed_blocks, 1)
        self.assertNotIn("old-call", serialized)
        self.assertIn("latest-call", serialized)
        self.assertIn("earlier tool-interaction", serialized)

    def test_oversized_unshrinkable_request_is_rejected(self):
        manager = ContextManager(
            ContextBudget(
                context_window=200,
                max_input_tokens=150,
                max_output_tokens=50,
                compact_at_tokens=100,
            )
        )
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "very large " * 200},
        ]

        with self.assertRaisesRegex(ContextLimitError, "too large"):
            manager.prepare(messages, [])

    def test_invalid_budget_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot exceed context_window"):
            ContextBudget(
                context_window=100,
                max_input_tokens=90,
                max_output_tokens=20,
                compact_at_tokens=80,
            )


class FakeCompletions:
    def __init__(self):
        self.request = None

    def create(self, **request):
        self.request = request
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )


class LLMOutputLimitTests(unittest.TestCase):
    def test_max_output_tokens_are_sent_to_provider(self):
        settings = Settings(
            model="test-model",
            base_url="https://example.com/v1",
            api_key="test-key",
            context_window=1_000,
            max_input_tokens=700,
            max_output_tokens=200,
            compact_at_tokens=600,
        )
        llm = OpenAICompatibleLLM(settings)
        completions = FakeCompletions()
        llm.client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        llm.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(completions.request["max_tokens"], 200)


if __name__ == "__main__":
    unittest.main()
