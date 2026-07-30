"""Chroma-backed semantic retrieval and rank fusion."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence
from urllib.parse import urlparse

from openai import OpenAI
from dotenv import load_dotenv

from .workspace import Workspace
from .storage import ProjectStorage


class EmbeddingProvider(Protocol):
    model: str

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Encode text into dense vectors."""


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LocalOllamaEmbeddingProvider:
    """Local-only Ollama adapter using its OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
    ) -> None:
        if not model.strip():
            raise ValueError("local embedding model must be non-empty")
        self.base_url = _validate_local_base_url(base_url)
        self.model = model
        self.client = OpenAI(api_key="ollama", base_url=self.base_url)

    @classmethod
    def from_env(cls) -> Optional["LocalOllamaEmbeddingProvider"]:
        load_dotenv()
        model = os.getenv("EMBEDDING_MODEL", "").strip()
        if not model:
            return None
        base_url = (
            os.getenv("EMBEDDING_BASE_URL", "").strip()
            or DEFAULT_OLLAMA_BASE_URL
        )
        return cls(model, base_url)

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        response = self.client.embeddings.create(
            model=self.model,
            input=list(texts),
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in ordered]


@dataclass(frozen=True)
class VectorRecord:
    id: str
    text: str
    content_hash: str
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class VectorHit:
    id: str
    text: str
    metadata: Dict[str, Any]
    distance: float


class ChromaVectorStore:
    """One persistent Chroma database per workspace."""

    def __init__(
        self,
        workspace: Workspace,
        provider: Optional[EmbeddingProvider] = None,
        client: Optional[Any] = None,
    ) -> None:
        self.workspace = workspace
        self.storage = ProjectStorage(workspace)
        self.provider = provider
        self.root = self.storage.root / "vector"
        self.workspace_id = self.storage.project_id[:16]
        self._client = client

    @classmethod
    def from_env(cls, workspace: Workspace) -> "ChromaVectorStore":
        return cls(workspace, LocalOllamaEmbeddingProvider.from_env())

    @property
    def available(self) -> bool:
        return self.provider is not None

    def status(self) -> Dict[str, Any]:
        return {
            "backend": "chroma",
            "enabled": self.available,
            "embedding_model": (
                self.provider.model if self.provider is not None else ""
            ),
            "provider": "local-ollama",
            "local_only": True,
            "base_url": (
                getattr(self.provider, "base_url", "")
                if self.provider is not None
                else ""
            ),
            "path": str(self.root),
        }

    def sync_namespace(
        self,
        namespace: str,
        records: Sequence[VectorRecord],
    ) -> Dict[str, int]:
        """Make one collection exactly match the supplied records."""
        if not self.available:
            return {"embedded": 0, "unchanged": 0, "deleted": 0}
        collection = self._collection(namespace)
        current = collection.get(include=["metadatas"])
        current_meta = {
            item_id: metadata or {}
            for item_id, metadata in zip(
                current.get("ids") or [],
                current.get("metadatas") or [],
            )
        }
        incoming = {record.id: record for record in records}
        stale = sorted(set(current_meta) - set(incoming))
        if stale:
            collection.delete(ids=stale)
        changed = [
            record
            for record in records
            if (
                current_meta.get(record.id, {}).get("content_hash")
                != record.content_hash
                or current_meta.get(record.id, {}).get("embedding_model")
                != self.provider.model
            )
        ]
        self._upsert(collection, changed)
        return {
            "embedded": len(changed),
            "unchanged": len(records) - len(changed),
            "deleted": len(stale),
        }

    def replace_group(
        self,
        namespace: str,
        group: str,
        records: Sequence[VectorRecord],
    ) -> None:
        if not self.available:
            return
        collection = self._collection(namespace)
        existing = collection.get(
            where={"group": group},
            include=["metadatas"],
        )
        current_meta = {
            item_id: metadata or {}
            for item_id, metadata in zip(
                existing.get("ids") or [],
                existing.get("metadatas") or [],
            )
        }
        incoming = {record.id: record for record in records}
        stale = sorted(set(current_meta) - set(incoming))
        if stale:
            collection.delete(ids=stale)
        changed = [
            record
            for record in records
            if (
                current_meta.get(record.id, {}).get("content_hash")
                != record.content_hash
                or current_meta.get(record.id, {}).get("embedding_model")
                != self.provider.model
            )
        ]
        self._upsert(collection, changed)

    def remove_group(self, namespace: str, group: str) -> None:
        if not self.available:
            return
        collection = self._collection(namespace)
        existing = collection.get(where={"group": group})
        ids = existing.get("ids") or []
        if ids:
            collection.delete(ids=ids)

    def query(
        self,
        namespace: str,
        text: str,
        limit: int,
    ) -> List[VectorHit]:
        if not self.available or limit < 1:
            return []
        vector = self.provider.embed([text])[0]
        result = self._collection(namespace).query(
            query_embeddings=[vector],
            n_results=limit,
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            VectorHit(
                id=item_id,
                text=document or "",
                metadata=metadata or {},
                distance=float(distance),
            )
            for item_id, document, metadata, distance in zip(
                ids,
                documents,
                metadatas,
                distances,
            )
        ]

    def _collection(self, namespace: str) -> Any:
        self._ensure_storage_path()
        if self._client is None:
            try:
                import chromadb
            except ImportError as exc:
                raise RuntimeError(
                    "Chroma vector search requires the chromadb package"
                ) from exc
            self.root.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.root))
        safe = "".join(
            char if char.isalnum() or char in "._-" else "_"
            for char in namespace.lower()
        )
        name = f"sa_{self.workspace_id}_{safe}"[:63]
        return self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=None,
        )

    def _upsert(
        self,
        collection: Any,
        records: Sequence[VectorRecord],
    ) -> None:
        for offset in range(0, len(records), 64):
            batch = list(records[offset : offset + 64])
            embeddings = self.provider.embed(
                [record.text for record in batch]
            )
            metadatas = [
                {
                    **_scalar_metadata(record.metadata),
                    "content_hash": record.content_hash,
                    "embedding_model": self.provider.model,
                }
                for record in batch
            ]
            collection.upsert(
                ids=[record.id for record in batch],
                embeddings=embeddings,
                documents=[record.text for record in batch],
                metadatas=metadatas,
            )

    def _ensure_storage_path(self) -> None:
        self.storage.ensure_path(self.root, "vector store")


def reciprocal_rank_fusion(
    ranked_ids: Iterable[Sequence[str]],
    *,
    constant: int = 60,
) -> Dict[str, float]:
    """Fuse independently ranked result sets without comparing raw scores."""
    scores: Dict[str, float] = {}
    for ranking in ranked_ids:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (
                constant + rank
            )
    return scores


def content_fingerprint(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def _scalar_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in metadata.items()
        if isinstance(value, (str, int, float, bool)) and value is not None
    }


def _validate_local_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOCAL_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "EMBEDDING_BASE_URL must be a local Ollama HTTP endpoint "
            "on localhost, 127.0.0.1, or ::1"
        )
    path = parsed.path.rstrip("/")
    if path != "/v1":
        raise ValueError(
            "EMBEDDING_BASE_URL must end with /v1 for Ollama's "
            "OpenAI-compatible embeddings endpoint"
        )
    return base_url.rstrip("/")
