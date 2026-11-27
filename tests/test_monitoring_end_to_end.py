"""Two real uvicorn workers share one database; a graceful SIGTERM flushes; a fresh process still sees the totals."""

import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.metrics_store import MetricsStore

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")
OPERATOR = "operator-key-0123456789-abcdef"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(port: int) -> None:
    for _ in range(150):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("server did not start")


def test_two_workers_aggregate_and_a_graceful_shutdown_flushes(tmp_path):
    db = tmp_path / "e2e.db"
    port = _free_port()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "NEXUS_API_KEY": OPERATOR, "NEXUS_DEMO_MODE": "true",
           "NEXUS_METRICS_FLUSH_SECONDS": "300", "NEXUS_LOG_LEVEL": "ERROR", "NEXUS_RATE_LIMIT_PER_MINUTE": "100000"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
                             "--workers", "2", "--log-level", "error"], env=env, cwd=".")
    try:
        _wait_ready(port)
        # Requests are spread over both workers (separate connections); every request is /system/alerts so it is counted.
        for _ in range(40):
            response = httpx.get(f"http://127.0.0.1:{port}/system/alerts", headers={"X-API-Key": OPERATOR}, timeout=5)
            assert response.status_code == 200
    finally:
        proc.send_signal(signal.SIGTERM)  # graceful: each worker's lifespan shutdown makes the final flush
        proc.wait(timeout=30)
    engine = create_engine(f"sqlite:///{db}")
    snapshot = MetricsStore(sessionmaker(bind=engine)).snapshot()
    total = sum(c["value"] for c in snapshot["counters"]
                if c["name"] == "http_requests_total" and c["labels"].get("route") == "/system/alerts")
    with engine.connect() as connection:
        flushes = connection.exec_driver_sql("SELECT count(*) FROM metric_flushes").scalar()
    assert flushes >= 2  # with a 300 s interval only the shutdown flush ran: one per worker that served traffic
    assert total == 40  # nothing lost at shutdown, nothing counted twice, across both workers
    # "restart": a brand new process reads the same totals back through the real endpoint
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
                             "--log-level", "error"], env=env, cwd=".")
    try:
        _wait_ready(port)
        body = httpx.get(f"http://127.0.0.1:{port}/system/metrics", headers={"X-API-Key": OPERATOR}, timeout=5).json()
        assert body["persistence"]["status"] == "ok"
        seen = sum(c["value"] for c in body["counters"]
                   if c["name"] == "http_requests_total" and c["labels"].get("route") == "/system/alerts")
        assert seen == 40
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    engine.dispose()
