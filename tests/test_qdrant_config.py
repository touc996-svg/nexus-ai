"""Qdrant settings, TLS/key policy, retry and failure handling, tenant index (no server needed)."""

import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from app.services import qdrant_config as qc

ENV = ("QDRANT_URL", "QDRANT_API_KEY", "QDRANT_TIMEOUT_SECONDS", "QDRANT_MAX_RETRIES")
KEY = "cloud-key-0123456789-abcdef"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_keep_local_development_working():
    settings = qc.load_qdrant_settings()
    assert settings.url == "http://localhost:6333" and settings.api_key is None and settings.is_local
    assert (settings.timeout_seconds, settings.max_retries) == (30, 1)


def test_remote_host_requires_https_and_a_key(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://xyz.cloud.qdrant.io:6333")
    monkeypatch.setenv("QDRANT_API_KEY", KEY)
    with pytest.raises(ValueError, match="https"):
        qc.load_qdrant_settings()
    monkeypatch.setenv("QDRANT_URL", "https://xyz.cloud.qdrant.io:6333")
    monkeypatch.delenv("QDRANT_API_KEY")
    with pytest.raises(ValueError, match="QDRANT_API_KEY"):
        qc.load_qdrant_settings()
    monkeypatch.setenv("QDRANT_API_KEY", KEY)
    assert not qc.load_qdrant_settings().is_local


@pytest.mark.parametrize("url", ["ftp://x.example.com", "localhost:6333", "https://", "not a url"])
def test_malformed_urls_are_refused(monkeypatch, url):
    monkeypatch.setenv("QDRANT_URL", url)
    with pytest.raises(ValueError):
        qc.load_qdrant_settings()


def test_local_container_with_a_key_over_http_is_allowed(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("QDRANT_API_KEY", KEY)
    assert qc.load_qdrant_settings().api_key == KEY


@pytest.mark.parametrize("name,value", [("QDRANT_TIMEOUT_SECONDS", "0"), ("QDRANT_TIMEOUT_SECONDS", "121"),
                                        ("QDRANT_TIMEOUT_SECONDS", "x"), ("QDRANT_MAX_RETRIES", "-1"),
                                        ("QDRANT_MAX_RETRIES", "4")])
def test_timeout_and_retry_bounds(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        qc.load_qdrant_settings()


def test_the_key_never_appears_in_repr_or_description(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "https://xyz.cloud.qdrant.io:6333/secret-path")
    monkeypatch.setenv("QDRANT_API_KEY", KEY)
    settings = qc.load_qdrant_settings()
    assert KEY not in repr(settings) and KEY not in settings.describe()
    assert settings.describe() == "https://xyz.cloud.qdrant.io" and "secret-path" not in settings.describe()


def test_client_is_built_with_the_key_timeout_and_no_grpc(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "https://xyz.cloud.qdrant.io:6333")
    monkeypatch.setenv("QDRANT_API_KEY", KEY)
    monkeypatch.setenv("QDRANT_TIMEOUT_SECONDS", "7")
    with patch.object(qc, "QdrantClient") as factory:
        qc.build_qdrant_client()
    kwargs = factory.call_args.kwargs
    assert kwargs["url"].startswith("https://") and kwargs["api_key"] == KEY
    assert kwargs["timeout"] == 7 and kwargs["prefer_grpc"] is False


# ---- retry and failure handling ------------------------------------------------------------------------------------

def _resilient(inner, retries=2):
    return qc.ResilientQdrant(inner, retries, backoff_seconds=0)


def test_transport_failures_are_retried_then_succeed():
    inner = MagicMock()
    inner.get_collection.side_effect = [ResponseHandlingException(ConnectionError("down")), "ok"]
    assert _resilient(inner).get_collection("c") == "ok"
    assert inner.get_collection.call_count == 2


def test_persistent_failure_becomes_a_safe_error_without_url_or_key():
    inner = MagicMock()
    inner.query_points.side_effect = ResponseHandlingException(ConnectionError(f"https://secret-host/{KEY}"))
    with pytest.raises(qc.QdrantUnavailable) as caught:
        _resilient(inner, retries=1).query_points("c")
    assert inner.query_points.call_count == 2
    assert KEY not in str(caught.value) and "secret-host" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
def test_authentication_and_client_errors_are_not_retried(status):
    inner = MagicMock()
    inner.upsert.side_effect = UnexpectedResponse(status, "x", b"", {})
    with pytest.raises(UnexpectedResponse):
        _resilient(inner).upsert("c", points=[])
    assert inner.upsert.call_count == 1


def test_server_errors_are_retried():
    inner = MagicMock()
    inner.scroll.side_effect = [UnexpectedResponse(503, "x", b"", {}), UnexpectedResponse(502, "x", b"", {}), "ok"]
    assert _resilient(inner).scroll("c") == "ok"


def test_zero_retries_means_one_attempt():
    inner = MagicMock()
    inner.delete.side_effect = ResponseHandlingException(TimeoutError("slow"))
    with pytest.raises(qc.QdrantUnavailable):
        _resilient(inner, retries=0).delete("c")
    assert inner.delete.call_count == 1


def test_other_attributes_pass_through():
    inner = MagicMock()
    inner.close.return_value = 5
    assert _resilient(inner).close() == 5


# ---- tenant index --------------------------------------------------------------------------------------------------

def test_tenant_index_is_created_once_and_marked_as_the_tenant_key():
    client = MagicMock()
    client.get_collection.return_value = MagicMock(payload_schema={})
    qc.ensure_tenant_index(client, "nexus_knowledge")
    kwargs = client.create_payload_index.call_args.kwargs
    assert kwargs["field_name"] == "user_id"
    assert kwargs["field_schema"].is_tenant is True and kwargs["field_schema"].type == "keyword"
    client.reset_mock()
    client.get_collection.return_value = MagicMock(payload_schema={"user_id": object()})
    qc.ensure_tenant_index(client, "nexus_knowledge")
    client.create_payload_index.assert_not_called()


def test_tenant_index_is_harmless_on_the_local_in_memory_client():
    client = QdrantClient(location=":memory:")
    client.create_collection("t", vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # the local-mode warning must be contained
        qc.ensure_tenant_index(client, "t")


def test_rag_keeps_working_when_the_tenant_index_cannot_be_created():
    from app.services.rag import NexusRAG

    rag = NexusRAG(client=QdrantClient(location=":memory:"), demo_mode=False)
    with patch("app.services.rag.ensure_tenant_index", side_effect=RuntimeError("forbidden")):
        assert rag.client is not None
    assert rag.health_check()["qdrant"] == "online"


def test_evaluation_adapter_never_reads_qdrant_settings(monkeypatch):
    from evals import nexus_adapter

    source = Path(nexus_adapter.__file__).read_text()
    assert "QDRANT_API_KEY" not in source and "QDRANT_URL" not in source.replace("never reads ``QDRANT_URL``", "")
