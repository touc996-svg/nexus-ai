import hashlib
import json
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from qdrant_client import QdrantClient, models

from app.services.document_extraction import ExtractionLimits, extract_document_text


class NexusRAG:
    COLLECTION = "nexus_knowledge"
    DEFAULT_VECTOR_SIZE = 768

    def __init__(
        self,
        client=None,
        demo_mode: bool | None = None,
        score_threshold: float | None = None
    ):
        env_demo = os.getenv("NEXUS_DEMO_MODE", "false").lower() in {
            "1", "true", "yes", "on"
        }
        self.demo_mode = env_demo if demo_mode is None else bool(demo_mode)
        self.vector_size = int(os.getenv(
            "NEXUS_EMBEDDING_DIMENSION",
            str(self.DEFAULT_VECTOR_SIZE)
        ))
        if self.vector_size <= 0:
            raise ValueError("NEXUS_EMBEDDING_DIMENSION must be greater than zero")

        self.embedding_model = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")
        self.ollama_url = base_url[:-3] if base_url.endswith("/v1") else base_url
        self.embedding_timeout = float(os.getenv("NEXUS_EMBEDDING_TIMEOUT", "45"))
        self.embedding_retries = max(0, min(
            int(os.getenv("NEXUS_EMBEDDING_RETRIES", "1")), 3
        ))
        self.score_threshold = (
            score_threshold
            if score_threshold is not None
            else float(os.getenv("NEXUS_RAG_MIN_SCORE", "0.15"))
        )
        if not math.isfinite(self.score_threshold) or not -1.0 <= self.score_threshold <= 1.0:
            raise ValueError("NEXUS_RAG_MIN_SCORE must be between -1 and 1")
        self.max_document_chars = max(1_000, int(os.getenv("NEXUS_RAG_MAX_DOCUMENT_CHARS", "2000000")))

        # Qdrant initialization is deferred until the first client access.
        self._client = client
        self._client_ready = False
        self._client_lock = threading.Lock()

    @property
    def client(self):
        if not self._client_ready:
            with self._client_lock:
                if not self._client_ready:
                    if self._client is None:
                        if self.demo_mode:
                            self._client = QdrantClient(location=":memory:")
                        else:
                            self._client = QdrantClient(
                                url=os.getenv(
                                    "QDRANT_URL",
                                    "http://localhost:6333"
                                ),
                                timeout=30
                            )

                    self._ensure_collection()
                    self._client_ready = True

        return self._client

    def _ensure_collection(self):
        client = self._client
        if client is None:
            raise RuntimeError("Qdrant client has not been initialized.")

        if not client.collection_exists(self.COLLECTION):
            client.create_collection(
                collection_name=self.COLLECTION,
                vectors_config=models.VectorParams(
                    size=self.vector_size,
                    distance=models.Distance.COSINE
                )
            )
            return

        collection = client.get_collection(self.COLLECTION)
        vectors = collection.config.params.vectors
        existing_size = getattr(vectors, "size", None)
        if existing_size and existing_size != self.vector_size:
            raise RuntimeError(
                f"Qdrant collection '{self.COLLECTION}' uses {existing_size}-dimension "
                f"vectors, but NEXUS is configured for {self.vector_size}. "
                "Use a new collection and re-index documents; do not silently mix vector sizes."
            )

    def embed(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding input cannot be empty")

        last_error = None
        for attempt in range(self.embedding_retries + 1):
            try:
                response = httpx.post(
                    f"{self.ollama_url}/api/embed",
                    json={"model": self.embedding_model, "input": text},
                    timeout=self.embedding_timeout
                )
                response.raise_for_status()
                payload = response.json()
                vectors = payload.get("embeddings")
                if vectors is None and payload.get("embedding") is not None:
                    vectors = [payload["embedding"]]
                if not isinstance(vectors, list) or not vectors:
                    raise ValueError("Ollama returned no embedding vector")

                vector = [float(value) for value in vectors[0]]
                if len(vector) != self.vector_size:
                    raise ValueError(
                        f"Expected {self.vector_size} dimensions from "
                        f"{self.embedding_model}, received {len(vector)}. "
                        "Check NEXUS_EMBEDDING_DIMENSION."
                    )
                if not all(math.isfinite(value) for value in vector):
                    raise ValueError("Embedding contains non-finite values")
                return vector
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                last_error = exc
                if attempt >= self.embedding_retries:
                    break

        raise RuntimeError(
            f"Ollama embedding request failed ({type(last_error).__name__}). "
            "Verify Ollama is running and the embedding model is installed."
        ) from last_error

    @staticmethod
    def extract_text(file_path: str, max_chars: int | None = None) -> str:
        """Extract text in an isolated, resource-limited child process (see document_extraction.py)."""
        limits = ExtractionLimits.from_env()
        if max_chars is not None:
            limits = ExtractionLimits(**{**limits.__dict__, "max_chars": max_chars})
        return extract_document_text(file_path, limits)

    @staticmethod
    def chunk_text(
        text: str,
        chunk_size: int = 1000,
        overlap: int = 150
    ) -> list[str]:
        if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
            raise ValueError("Invalid chunk_size or overlap")
        words = text.split()
        chunks = []
        step = chunk_size - overlap
        for start in range(0, len(words), step):
            chunk = " ".join(words[start:start + chunk_size])
            if chunk.strip():
                chunks.append(chunk)
        return chunks

    @staticmethod
    def _source_id(user_id: str, source: str) -> str:
        return hashlib.sha256(f"{user_id}:{source}".encode("utf-8")).hexdigest()

    def _user_filter(self, user_id: str):
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="user_id",
                    match=models.MatchValue(value=user_id)
                )
            ]
        )

    def ingest_text(
        self,
        text: str,
        source: str,
        user_id: str = "system",
        metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Document contains no extractable text")
        if len(text) > self.max_document_chars:
            raise ValueError(f"Document exceeds the {self.max_document_chars}-character limit")
        if not isinstance(source, str) or not source.strip() or len(source) > 255:
            raise ValueError("source must contain 1 to 255 characters")
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 128:
            raise ValueError("user_id must contain 1 to 128 characters")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be a dictionary")
        source = source.strip()
        user_id = user_id.strip()
        try:
            metadata_json = json.dumps(metadata or {}, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata must contain JSON-compatible values") from exc
        if len(metadata_json) > 8_000:
            raise ValueError("metadata exceeds the 8000-character limit")

        if self.demo_mode:
            return {
                "status": "disabled",
                "reason": "Knowledge ingestion is disabled in public demo mode.",
                "source": source,
                "chunks_ingested": 0,
                "collection": self.COLLECTION
            }

        chunks = self.chunk_text(text)
        if not chunks:
            raise ValueError("Document contains no extractable text")

        source_id = self._source_id(user_id, source)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        ingested_at = datetime.now(timezone.utc).isoformat()
        points = []

        # Build all embeddings first; never delete the previous source on a partial embedding failure.
        for index, chunk in enumerate(chunks):
            vector = self.embed(chunk)
            fingerprint = hashlib.sha256(
                f"{user_id}:{source_id}:{content_hash}:{index}:{chunk}".encode("utf-8")
            ).hexdigest()
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint))
            payload = {
                "text": chunk,
                "source": source,
                "source_id": source_id,
                "content_hash": content_hash,
                "ingested_at": ingested_at,
                "chunk_index": index,
                "user_id": user_id,
                "metadata": metadata or {}
            }
            points.append(models.PointStruct(
                id=point_id,
                vector=vector,
                payload=payload
            ))

        source_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="user_id",
                    match=models.MatchValue(value=user_id)
                ),
                models.FieldCondition(
                    key="source_id",
                    match=models.MatchValue(value=source_id)
                )
            ]
        )
        # Publish the new version first, then remove stale chunks. If the upsert
        # fails, the previous indexed document remains searchable.
        self.client.upsert(
            collection_name=self.COLLECTION,
            points=points,
            wait=True
        )
        stale_source_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="user_id",
                    match=models.MatchValue(value=user_id)
                ),
                models.FieldCondition(
                    key="source_id",
                    match=models.MatchValue(value=source_id)
                )
            ],
            must_not=[
                models.FieldCondition(
                    key="content_hash",
                    match=models.MatchValue(value=content_hash)
                )
            ]
        )
        self.client.delete(
            collection_name=self.COLLECTION,
            points_selector=models.FilterSelector(filter=stale_source_filter),
            wait=True
        )
        return {
            "status": "indexed",
            "source": source,
            "source_id": source_id,
            "content_hash": content_hash,
            "chunks_ingested": len(points),
            "collection": self.COLLECTION
        }

    def ingest_file(
        self,
        file_path: str,
        user_id: str = "system",
        metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(file_path)
        return self.ingest_text(
            text=self.extract_text(str(path), max_chars=self.max_document_chars),
            source=path.name,
            user_id=user_id,
            metadata=metadata
        )

    def search(
        self,
        query: str,
        user_id: str = "system",
        limit: int = 5,
        score_threshold: float | None = None
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Search query cannot be empty")
        if len(query) > 5_000:
            raise ValueError("Search query exceeds the 5000-character limit")
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 128:
            raise ValueError("user_id must contain 1 to 128 characters")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if self.demo_mode:
            return []

        vector = self.embed(query)
        threshold = self.score_threshold if score_threshold is None else score_threshold
        if not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not -1.0 <= threshold <= 1.0:
            raise ValueError("score_threshold must be between -1 and 1")
        results = self.client.query_points(
            collection_name=self.COLLECTION,
            query=vector,
            query_filter=self._user_filter(user_id),
            limit=limit,
            score_threshold=threshold,
            with_payload=True
        ).points

        return [
            {
                "text": point.payload.get("text", ""),
                "source": point.payload.get("source", ""),
                "source_id": point.payload.get("source_id", ""),
                "chunk_index": point.payload.get("chunk_index", 0),
                "content_hash": point.payload.get("content_hash", ""),
                "score": float(point.score),
                "metadata": point.payload.get("metadata", {})
            }
            for point in results if point.payload
        ]

    def list_sources(self, user_id: str = "system") -> list[dict[str, Any]]:
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 128:
            raise ValueError("user_id must contain 1 to 128 characters")
        if self.demo_mode:
            return []

        offset = None
        sources: dict[str, dict[str, Any]] = {}
        while True:
            points, next_offset = self.client.scroll(
                collection_name=self.COLLECTION,
                scroll_filter=self._user_filter(user_id),
                limit=500,
                offset=offset,
                with_payload=True,
                with_vectors=False
            )
            for point in points:
                payload = point.payload or {}
                source_id = payload.get("source_id")
                if not source_id:
                    continue
                item = sources.setdefault(source_id, {
                    "source": payload.get("source", ""),
                    "source_id": source_id,
                    "content_hash": payload.get("content_hash", ""),
                    "ingested_at": payload.get("ingested_at", ""),
                    "chunks": 0
                })
                item["chunks"] += 1
            if next_offset is None:
                break
            offset = next_offset

        return sorted(sources.values(), key=lambda item: item["source"].lower())

    def delete_source(self, source_id: str, user_id: str = "system") -> bool:
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 128:
            raise ValueError("source_id must contain 1 to 128 characters")
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 128:
            raise ValueError("user_id must contain 1 to 128 characters")
        if self.demo_mode:
            return False
        source_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="user_id",
                    match=models.MatchValue(value=user_id)
                ),
                models.FieldCondition(
                    key="source_id",
                    match=models.MatchValue(value=source_id)
                )
            ]
        )
        self.client.delete(
            collection_name=self.COLLECTION,
            points_selector=models.FilterSelector(filter=source_filter),
            wait=True
        )
        return True

    def health_check(self) -> dict[str, Any]:
        if self.demo_mode:
            return {
                "status": "demo",
                "collection": self.COLLECTION,
                "qdrant": "disabled",
                "embedding": "disabled"
            }
        try:
            self.client.get_collection(self.COLLECTION)
            qdrant_status = "online"
        except Exception as exc:
            qdrant_status = f"offline:{type(exc).__name__}"
        return {
            "status": "healthy" if qdrant_status == "online" else "degraded",
            "collection": self.COLLECTION,
            "vector_size": self.vector_size,
            "embedding_model": self.embedding_model,
            "qdrant": qdrant_status
        }


rag = NexusRAG()
