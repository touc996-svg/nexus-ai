import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from docx import Document
from pypdf import PdfReader
from qdrant_client import QdrantClient, models


class NexusRAG:
    COLLECTION = "nexus_knowledge"
    VECTOR_SIZE = 768
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
    OLLAMA_URL = os.getenv(
        "OLLAMA_BASE_URL",
        "http://localhost:11434"
    ).replace("/v1", "").rstrip("/")

    def __init__(self):
        self.client = QdrantClient(
            url=os.getenv("QDRANT_URL", "http://localhost:6333"),
            timeout=30
        )
        self._ensure_collection()

    def _ensure_collection(self):
        if not self.client.collection_exists(self.COLLECTION):
            self.client.create_collection(
                collection_name=self.COLLECTION,
                vectors_config=models.VectorParams(
                    size=self.VECTOR_SIZE,
                    distance=models.Distance.COSINE
                )
            )

    def embed(self, text: str) -> list[float]:
        response = httpx.post(
            f"{self.OLLAMA_URL}/api/embed",
            json={
                "model": self.EMBEDDING_MODEL,
                "input": text
            },
            timeout=60
        )
        response.raise_for_status()
        vector = response.json()["embeddings"][0]

        if len(vector) != self.VECTOR_SIZE:
            raise ValueError(
                f"Expected {self.VECTOR_SIZE} dimensions, got {len(vector)}"
            )

        return vector

    @staticmethod
    def extract_text(file_path: str) -> str:
        path = Path(file_path)
        suffix = path.suffix.lower()

        if suffix == ".pdf":
            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)

        if suffix == ".docx":
            document = Document(str(path))
            return "\n".join(p.text for p in document.paragraphs)

        if suffix in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="replace")

        raise ValueError(f"Unsupported document format: {suffix}")

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

    def ingest_text(
        self,
        text: str,
        source: str,
        user_id: str = "system",
        metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("Document contains no extractable text")

        chunks = self.chunk_text(text)
        points = []

        for index, chunk in enumerate(chunks):
            vector = self.embed(chunk)
            fingerprint = hashlib.sha256(
                f"{user_id}:{source}:{index}:{chunk}".encode()
            ).hexdigest()
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint))

            payload = {
                "text": chunk,
                "source": source,
                "chunk_index": index,
                "user_id": user_id,
                "metadata": metadata or {}
            }

            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload
                )
            )

        self.client.upsert(
            collection_name=self.COLLECTION,
            points=points,
            wait=True
        )

        return {
            "source": source,
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

        text = self.extract_text(str(path))

        return self.ingest_text(
            text=text,
            source=path.name,
            user_id=user_id,
            metadata=metadata
        )

    def search(
        self,
        query: str,
        user_id: str = "system",
        limit: int = 5
    ) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("Search query cannot be empty")

        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")

        vector = self.embed(query)

        results = self.client.query_points(
            collection_name=self.COLLECTION,
            query=vector,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="user_id",
                        match=models.MatchValue(value=user_id)
                    )
                ]
            ),
            limit=limit,
            with_payload=True
        ).points

        return [
            {
                "text": point.payload.get("text", ""),
                "source": point.payload.get("source", ""),
                "chunk_index": point.payload.get("chunk_index", 0),
                "score": point.score,
                "metadata": point.payload.get("metadata", {})
            }
            for point in results
        ]


rag = NexusRAG()
