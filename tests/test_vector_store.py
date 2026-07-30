from tests import _TEST_STORAGE_HOME  # noqa: F401

import math
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simple_agent.knowledge import KnowledgeBase
from simple_agent.memory import ProjectMemoryStore, TaskSummary
from simple_agent.project_index import ProjectIndex
from simple_agent.vector_store import (
    ChromaVectorStore,
    LocalOllamaEmbeddingProvider,
    VectorRecord,
    reciprocal_rank_fusion,
)
from simple_agent.workspace import Workspace


class FakeEmbeddingProvider:
    model = "fake-semantic-v1"

    def __init__(self):
        self.encoded = 0

    def embed(self, texts):
        self.encoded += len(texts)
        return [
            [1.0, 0.0]
            if any(
                term in text.lower()
                for term in ("authenticate", "login", "登录", "认证")
            )
            else [0.0, 1.0]
            for text in texts
        ]


class FakeCollection:
    def __init__(self):
        self.items = {}

    def get(self, ids=None, where=None, include=None):
        selected = self.items.items()
        if ids is not None:
            selected = [
                (item_id, item)
                for item_id, item in selected
                if item_id in ids
            ]
        if where:
            selected = [
                (item_id, item)
                for item_id, item in selected
                if all(
                    item["metadata"].get(key) == value
                    for key, value in where.items()
                )
            ]
        selected = list(selected)
        return {
            "ids": [item_id for item_id, _ in selected],
            "metadatas": [item["metadata"] for _, item in selected],
        }

    def delete(self, ids):
        for item_id in ids:
            self.items.pop(item_id, None)

    def upsert(self, ids, embeddings, documents, metadatas):
        for item_id, embedding, document, metadata in zip(
            ids,
            embeddings,
            documents,
            metadatas,
        ):
            self.items[item_id] = {
                "embedding": embedding,
                "document": document,
                "metadata": metadata,
            }

    def query(
        self,
        query_embeddings,
        n_results,
        include,
    ):
        query = query_embeddings[0]
        ranked = sorted(
            self.items.items(),
            key=lambda item: _cosine_distance(
                query,
                item[1]["embedding"],
            ),
        )[:n_results]
        return {
            "ids": [[item_id for item_id, _ in ranked]],
            "documents": [[item["document"] for _, item in ranked]],
            "metadatas": [[item["metadata"] for _, item in ranked]],
            "distances": [[
                _cosine_distance(query, item["embedding"])
                for _, item in ranked
            ]],
        }


class FakeChromaClient:
    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, name, metadata, embedding_function):
        return self.collections.setdefault(name, FakeCollection())


def _cosine_distance(left, right):
    numerator = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(
        sum(b * b for b in right)
    )
    return 1.0 - numerator / denominator


class ChromaVectorStoreTests(unittest.TestCase):
    def test_local_ollama_provider_uses_loopback_default(self):
        with patch.dict(
            os.environ,
            {
                "EMBEDDING_MODEL": "qwen3-embedding:0.6b",
                "EMBEDDING_BASE_URL": "",
            },
        ):
            provider = LocalOllamaEmbeddingProvider.from_env()

        self.assertEqual(provider.model, "qwen3-embedding:0.6b")
        self.assertEqual(
            provider.base_url,
            "http://127.0.0.1:11434/v1",
        )

    def test_local_ollama_provider_rejects_remote_endpoint(self):
        with self.assertRaisesRegex(ValueError, "local Ollama"):
            LocalOllamaEmbeddingProvider(
                "qwen3-embedding:0.6b",
                "https://embedding.example.com/v1",
            )

    def test_local_ollama_provider_requires_openai_v1_path(self):
        with self.assertRaisesRegex(ValueError, "end with /v1"):
            LocalOllamaEmbeddingProvider(
                "qwen3-embedding:0.6b",
                "http://127.0.0.1:11434/api/embed",
            )

    def test_content_hash_avoids_reencoding_unchanged_records(self):
        with TemporaryDirectory() as directory:
            provider = FakeEmbeddingProvider()
            store = ChromaVectorStore(
                Workspace(Path(directory)),
                provider,
                FakeChromaClient(),
            )
            records = [
                VectorRecord(
                    id="one",
                    text="authenticate a user",
                    content_hash="hash-one",
                    metadata={"path": "auth.py"},
                )
            ]

            first = store.sync_namespace("code_chunks", records)
            second = store.sync_namespace("code_chunks", records)
            hits = store.query("code_chunks", "用户登录", 1)

            self.assertEqual(first["embedded"], 1)
            self.assertEqual(second["unchanged"], 1)
            self.assertEqual(provider.encoded, 2)
            self.assertEqual(hits[0].metadata["path"], "auth.py")

    def test_project_index_fuses_keyword_and_semantic_results(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "auth.py").write_text(
                "def authenticate_user(token):\n    return bool(token)\n",
                encoding="utf-8",
            )
            (root / "report.py").write_text(
                "def export_csv():\n    return 'csv'\n",
                encoding="utf-8",
            )
            store = ChromaVectorStore(
                Workspace(root),
                FakeEmbeddingProvider(),
                FakeChromaClient(),
            )
            index = ProjectIndex(Workspace(root), vector_store=store)
            index.refresh()

            hits = index.search_hybrid("用户登录", limit=1)

            self.assertEqual(hits[0].path, "auth.py")

    def test_knowledge_search_fuses_semantic_results(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            store = ChromaVectorStore(
                workspace,
                FakeEmbeddingProvider(),
                FakeChromaClient(),
            )
            knowledge = KnowledgeBase(workspace, vector_store=store)
            source = workspace.root / "auth-guide.txt"
            source.write_text(
                "Authenticate users with short-lived access tokens.",
                encoding="utf-8",
            )
            knowledge.ingest(source)

            hits = knowledge.search("用户登录", limit=1)

            self.assertEqual(hits[0].source_name, "auth-guide.txt")

    def test_memory_search_fuses_semantic_results(self):
        with TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            store = ChromaVectorStore(
                workspace,
                FakeEmbeddingProvider(),
                FakeChromaClient(),
            )
            memory = ProjectMemoryStore(workspace, vector_store=store)
            memory.append_summary(
                TaskSummary(
                    task_id="task-auth",
                    request="Implement login authentication",
                    status="completed",
                    summary="Added token validation.",
                )
            )

            hits = memory.search_summaries("用户认证", limit=1)

            self.assertEqual(hits[0].task_id, "task-auth")

    def test_rrf_combines_independent_rankings(self):
        scores = reciprocal_rank_fusion(
            [["keyword-first", "shared"], ["vector-first", "shared"]]
        )

        self.assertGreater(scores["shared"], scores["keyword-first"])
        self.assertGreater(scores["shared"], scores["vector-first"])


if __name__ == "__main__":
    unittest.main()
