"""Qdrant connection settings, client construction and failure handling.

Local development: ``QDRANT_URL`` unset or pointing at localhost works exactly as before (plain HTTP, no key).
Remote host (for example Qdrant Cloud): the URL must be ``https://`` and ``QDRANT_API_KEY`` is required; anything else is
refused at start-up so a key is never sent in clear text and an unauthenticated remote store is never used by mistake.
The API key lives only in the environment (secret manager), is never logged, and is hidden from ``repr``.

``QDRANT_TIMEOUT_SECONDS`` (default 30, 1-120) bounds every request. ``QDRANT_MAX_RETRIES`` (default 1, 0-3) retries only
transport failures and 5xx answers, with a short backoff; all operations NEXUS issues are idempotent.
"""

import logging
import os
import time
import warnings
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from app.core.metrics import metrics

logger = logging.getLogger("nexus.qdrant")
DEFAULT_URL = "http://localhost:6333"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TENANT_FIELD = "user_id"


class QdrantUnavailable(RuntimeError):
    """Qdrant could not be reached or kept failing. The message never contains the URL, key or request data."""


@dataclass(frozen=True)
class QdrantSettings:
    url: str = DEFAULT_URL
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: int = 30
    max_retries: int = 1

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    @property
    def is_local(self) -> bool:
        return self.host in LOCAL_HOSTS

    def describe(self) -> str:
        """Safe to log: scheme and host only, never the key or the path."""
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.hostname}"


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def load_qdrant_settings() -> QdrantSettings:
    url = (os.getenv("QDRANT_URL") or DEFAULT_URL).strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("QDRANT_URL must be an http(s) URL with a host")
    api_key = (os.getenv("QDRANT_API_KEY") or "").strip() or None
    settings = QdrantSettings(
        url=url,
        api_key=api_key,
        timeout_seconds=_env_int("QDRANT_TIMEOUT_SECONDS", 30, 1, 120),
        max_retries=_env_int("QDRANT_MAX_RETRIES", 1, 0, 3),
    )
    if not settings.is_local:
        if parts.scheme != "https":
            raise ValueError("A remote QDRANT_URL must use https://")
        if not api_key:
            raise ValueError("QDRANT_API_KEY is required for a remote Qdrant")
    elif api_key and parts.scheme != "https":
        logger.warning("qdrant_key_over_plain_http host=local")  # allowed for a local container, never for a remote host
    return settings


_RETRYABLE_STATUS = {502, 503, 504}
_WRAPPED = {
    "collection_exists", "get_collection", "create_collection", "create_payload_index", "upsert", "query_points",
    "search", "scroll", "delete", "count", "retrieve", "set_payload",
}


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, UnexpectedResponse):
        return exc.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (ResponseHandlingException, TimeoutError, ConnectionError, OSError))


class ResilientQdrant:
    """Delegates to a QdrantClient; retries transport failures and turns persistent ones into QdrantUnavailable."""

    def __init__(self, client, max_retries: int, backoff_seconds: float = 0.2):
        self._client, self._max_retries, self._backoff = client, max_retries, backoff_seconds

    def _call(self, name, *args, **kwargs):
        method = getattr(self._client, name)
        for attempt in range(self._max_retries + 1):
            try:
                return method(*args, **kwargs)
            except Exception as exc:  # classified below
                if not _retryable(exc):
                    raise
                if attempt == self._max_retries:
                    logger.warning("qdrant_unavailable op=%s error_type=%s", name, type(exc).__name__)
                    metrics.inc("qdrant_unavailable_total")
                    raise QdrantUnavailable("Qdrant is unavailable") from None  # `from None`: no URL or key in the chain
                time.sleep(self._backoff * (2 ** attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    def __getattr__(self, name):
        if name in _WRAPPED:
            return lambda *args, **kwargs: self._call(name, *args, **kwargs)
        return getattr(self._client, name)


def build_qdrant_client(settings: QdrantSettings | None = None):
    settings = settings or load_qdrant_settings()
    client = QdrantClient(
        url=settings.url,
        api_key=settings.api_key,
        timeout=settings.timeout_seconds,
        prefer_grpc=False,
        check_compatibility=False,  # do not make an extra version request (and warning) on every start
    )
    return ResilientQdrant(client, settings.max_retries)


def ensure_tenant_index(client, collection: str) -> None:
    """Index ``user_id`` as the tenant key (idempotent). Tenant isolation is still enforced by the query filter; the index
    makes that filter fast and groups a tenant's vectors together. A local in-memory client ignores payload indexes."""
    info = client.get_collection(collection)
    if TENANT_FIELD in (getattr(info, "payload_schema", None) or {}):
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client.create_payload_index(
            collection_name=collection,
            field_name=TENANT_FIELD,
            field_schema=models.KeywordIndexParams(type="keyword", is_tenant=True),
        )
