"""Regression tests for the two load-test findings: multi-worker keep-alive latency (TCP_NODELAY) and liveness under pool saturation."""

import asyncio
import http.client
import inspect
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from app import serve
from app.api import health

ROOT = Path(__file__).resolve().parents[1]


def test_the_listening_socket_gets_tcp_nodelay(monkeypatch):
    monkeypatch.setattr(uvicorn.Config, "bind_socket", serve._original_bind_socket)
    serve.install_nodelay()
    try:
        sock = uvicorn.Config("app.main:app", host="127.0.0.1", port=0).bind_socket()
        try:
            assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0  # macOS reports the flag as a non-zero value other than 1
        finally:
            sock.close()
    finally:
        uvicorn.Config.bind_socket = serve._original_bind_socket  # type: ignore[method-assign]


def test_a_plain_listener_does_not_have_nodelay():
    """Documents the premise of the fix: without it the option is off (so the test above is meaningful)."""
    sock = serve._original_bind_socket(uvicorn.Config("app.main:app", host="127.0.0.1", port=0))
    try:
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 0
    finally:
        sock.close()


def test_serve_keeps_the_previous_uvicorn_settings(monkeypatch):
    captured = {}
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
    monkeypatch.setenv("UVICORN_WORKERS", "3")
    monkeypatch.setenv("PORT", "9123")
    serve.main()
    assert captured == {"app": "app.main:app", "host": "0.0.0.0", "port": 9123, "workers": 3, "proxy_headers": False,
                        "timeout_graceful_shutdown": 25}
    uvicorn.Config.bind_socket = serve._original_bind_socket  # type: ignore[method-assign]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_two_workers_keep_alive_requests_are_not_delayed_by_nagle():
    """Real ``python -m app.serve`` with two workers. On Linux, plain multi-worker uvicorn answers every keep-alive request after
    the first in about 42 ms (measured); with the fix it is a few milliseconds. On macOS the loopback behaves differently, so
    this test only discriminates on Linux (CI)."""
    port = _free_port()
    env = {**os.environ, "NEXUS_DEMO_MODE": "true", "UVICORN_WORKERS": "2", "PORT": str(port), "HOST": "127.0.0.1",
           "NEXUS_LOG_LEVEL": "ERROR", "NEXUS_METRICS_PERSISTENCE": "false"}
    proc = subprocess.Popen([sys.executable, "-m", "app.serve"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        for _ in range(150):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise AssertionError("server did not start")
        samples = []
        for _ in range(3):  # a few connections, so both workers are likely sampled
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/health/live")
            conn.getresponse().read()
            for _ in range(15):
                started = time.perf_counter()
                conn.request("GET", "/health/live")
                conn.getresponse().read()
                samples.append((time.perf_counter() - started) * 1000)
            conn.close()
        assert statistics.median(samples) < 15, f"median keep-alive latency {statistics.median(samples):.1f} ms"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)


def test_liveness_is_a_coroutine_and_answers_while_the_thread_pool_is_saturated():
    assert inspect.iscoroutinefunction(health.liveness)
    api = FastAPI()
    api.include_router(health.router)

    @api.get("/slow")
    def slow():  # a synchronous handler: runs in the shared thread pool, like /chat waiting on a model
        time.sleep(1.5)
        return {"ok": True}

    async def scenario():
        from anyio import to_thread

        tokens = to_thread.current_default_thread_limiter().total_tokens
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as client:
            busy = [asyncio.create_task(client.get("/slow")) for _ in range(int(tokens) + 5)]
            await asyncio.sleep(0.4)  # every pool thread is now blocked
            started = time.perf_counter()
            response = await client.get("/health/live")
            elapsed = time.perf_counter() - started
            await asyncio.gather(*busy)
        return response.status_code, elapsed

    status, elapsed = asyncio.run(scenario())
    assert status == 200 and elapsed < 0.5, f"liveness took {elapsed:.2f} s behind a saturated pool"
