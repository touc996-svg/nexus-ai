"""The load-testing tools themselves: statistics, the fake model server and the runner (no Docker needed)."""

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from loadtest import fake_llm
from loadtest.runner import run_load
from loadtest.scenarios import SCENARIOS, Ctx, docx_document, text_document
from loadtest.stats import percentile, summarize, timeline


def test_percentiles_use_nearest_rank():
    values = list(range(1, 101))
    assert percentile(values, 50) == 50 and percentile(values, 95) == 95 and percentile(values, 99) == 99
    assert percentile(values, 100) == 100 and percentile([7], 99) == 7 and percentile([], 50) is None


def test_summary_classifies_statuses_and_rates():
    samples = [(0.1, 10.0, 200, ""), (0.2, 20.0, 200, ""), (0.3, 30.0, 429, ""), (0.4, 40.0, 503, ""),
               (0.5, 50.0, 500, ""), (0.6, 60.0, 0, "ReadTimeout")]
    s = summarize(samples, 2.0)
    assert s["requests"] == 6 and s["rps"] == 3.0 and s["success_rps"] == 1.0 and s["ok"] == 2
    assert s["http_429"] == 1 and s["http_503"] == 1 and s["http_5xx_other"] == 1 and s["transport_errors"] == 1
    assert s["status_counts"]["error:ReadTimeout"] == 1 and s["error_rate"] == round(4 / 6, 4)
    assert s["latency_ms"]["p50"] == 30.0 and s["ok_latency_ms"]["p95"] == 20.0
    assert summarize([], 1.0)["success_rate"] is None


def test_timeline_buckets_show_an_outage():
    samples = [(t, 10.0, 200 if not 10 <= t < 20 else 500, "") for t in [x / 2 for x in range(0, 60)]]
    line = timeline(samples, 5.0)
    assert [b["success_rate"] for b in line] == [1.0, 1.0, 0.0, 0.0, 1.0, 1.0]


def test_fake_llm_replies_match_what_the_application_parses():
    client = TestClient(fake_llm.app)
    client.post("/_control", json={"reset": True, "latency_ms": 0, "jitter_ms": 0, "embed_latency_ms": 0})
    intent = client.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "system", "content": "return intent_type json"}]})
    import json

    body = json.loads(intent.json()["choices"][0]["message"]["content"])
    assert body["intent_type"] == "conversation" and body["clarifying_question"] is None
    profile = client.post("/v1/chat/completions", json={"messages": [{"role": "system", "content": 'return {"updates": {}}'}]})
    assert json.loads(profile.json()["choices"][0]["message"]["content"]) == {"updates": {}}
    assert client.get("/api/tags").json()["models"]
    vector = client.post("/api/embed", json={"model": "x", "input": "hello world"}).json()["embeddings"][0]
    assert len(vector) == 768 and abs(sum(v * v for v in vector) - 1) < 1e-6


def test_fake_llm_failure_latency_and_determinism_controls():
    client = TestClient(fake_llm.app)
    client.post("/_control", json={"reset": True, "latency_ms": 0, "jitter_ms": 0, "failure_rate": 1.0, "failure_status": 503})
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 503
    client.post("/_control", json={"failure_rate": 0.0, "latency_ms": 120, "jitter_ms": 0})
    started = time.perf_counter()
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 200
    assert time.perf_counter() - started >= 0.12
    assert fake_llm.embedding("same text") == fake_llm.embedding("same text")
    assert fake_llm.embedding("alpha beta") != fake_llm.embedding("gamma delta")
    client.post("/_control", json={"reset": True})
    assert client.get("/_stats").json()["state"]["failure_rate"] == 0.0


def test_generated_documents_are_valid_and_unique():
    import io
    import zipfile

    ctx = Ctx(client=None, user="u", session="s")  # type: ignore[arg-type]
    a, b = text_document(ctx), text_document(ctx)
    assert a != b and len(a) > 1000
    with zipfile.ZipFile(io.BytesIO(docx_document(ctx, 20))) as archive:
        assert {"[Content_Types].xml", "word/document.xml"} <= set(archive.namelist())
    assert {"health", "chat", "team", "ingest", "ingest_docx", "search", "ratelimit", "mixed"} <= set(SCENARIOS)


@pytest.fixture
def target():
    api = FastAPI()
    seen = {"n": 0}

    @api.get("/health/live")
    async def live():
        return {"ok": True}

    @api.get("/system/alerts")
    async def limited():
        seen["n"] += 1
        from fastapi.responses import JSONResponse

        return JSONResponse({"x": 1}, status_code=200 if seen["n"] <= 10 else 429)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_runner_measures_throughput_and_counts_throttling(target):
    result = asyncio.run(run_load("health", target, "k", concurrency=4, duration=1.0, warmup=0.2))
    assert result["requests"] > 20 and result["success_rate"] == 1.0 and result["latency_ms"]["p95"] is not None
    assert result["concurrency"] == 4 and result["timeline"]
    limited = asyncio.run(run_load("ratelimit", target, "k", concurrency=2, duration=0.5))
    assert limited["http_429"] > 0 and limited["ok"] == 10  # exactly the first 10 pass


def test_runner_reports_transport_errors_instead_of_raising():
    result = asyncio.run(run_load("health", "http://127.0.0.1:9", "k", concurrency=2, duration=0.3, timeout=1.0))
    assert result["transport_errors"] > 0 and result["ok"] == 0 and result["success_rate"] == 0.0
