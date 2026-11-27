"""Concurrency cap on isolated document extraction. Tests that kill or time out workers run on Linux only (CI)."""

import io
import subprocess
import sys
import textwrap
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from app.services import document_extraction as de
from app.services.document_extraction import (
    ExtractionBusy,
    ExtractionError,
    ExtractionLimits,
    extract_document_text,
)

LINUX = sys.platform.startswith("linux")
linux_only = pytest.mark.skipif(not LINUX, reason="worker kill/timeout tests run on Linux CI only")
LIMITS = ExtractionLimits(max_chars=10_000, timeout_seconds=10, max_concurrent=2, queue_timeout_seconds=1)


@pytest.fixture(autouse=True)
def fresh_semaphores():
    de._SEMAPHORES.clear()
    yield
    de._SEMAPHORES.clear()


def _hostile(tmp_path, monkeypatch, body):
    script = tmp_path / "worker.py"
    script.write_text("import sys\n" + textwrap.dedent(body))
    monkeypatch.setattr(de, "_WORKER", script)


def _file(tmp_path, name="a.txt", data=b"x"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def free_slots(size):
    sem, taken = de._semaphore(size), 0
    while sem.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        sem.release()
    return taken


def test_n_plus_one_concurrent_extractions_one_is_turned_away(tmp_path, monkeypatch):
    _hostile(tmp_path, monkeypatch, "import time\ntime.sleep(2)\nprint('ok')\n")
    path = _file(tmp_path)
    live, peak, guard = 0, 0, threading.Lock()
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        try:
            return real_run(*args, **kwargs)
        finally:
            with guard:
                live -= 1

    monkeypatch.setattr(de.subprocess, "run", counting_run)
    started = time.monotonic()

    def attempt(_):
        try:
            return extract_document_text(path, LIMITS).strip()
        except ExtractionBusy:
            return "busy"

    with ThreadPoolExecutor(3) as pool:
        results = sorted(pool.map(attempt, range(3)))
    assert results == ["busy", "ok", "ok"]
    assert peak == 2  # never more than N children at once
    assert time.monotonic() - started < 6
    assert free_slots(2) == 2


def test_slot_is_released_after_normal_completion(tmp_path):
    path = _file(tmp_path, data=b"fine")
    for _ in range(5):  # more runs than slots: a leak would exhaust them
        assert extract_document_text(path, LIMITS) == "fine"
    assert free_slots(2) == 2


def test_slot_is_released_after_a_rejected_file(tmp_path):
    bad = _file(tmp_path, "x.docx", _zip_without_word_document())
    for _ in range(4):
        with pytest.raises(ExtractionError, match="rejected"):
            extract_document_text(bad, LIMITS)
    assert free_slots(2) == 2


def _zip_without_word_document():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hi")
    return buffer.getvalue()


@linux_only
def test_slot_is_released_after_a_timeout(tmp_path, monkeypatch):
    _hostile(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    for _ in range(3):
        with pytest.raises(ExtractionError, match="timed out"):
            extract_document_text(_file(tmp_path), replace(LIMITS, timeout_seconds=1))
    assert free_slots(2) == 2


@linux_only
def test_slot_is_released_after_sigkill(tmp_path, monkeypatch):
    _hostile(tmp_path, monkeypatch, "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    for _ in range(3):
        with pytest.raises(ExtractionError, match="safely"):
            extract_document_text(_file(tmp_path), LIMITS)
    assert free_slots(2) == 2


def test_busy_is_not_reported_as_a_bad_document():
    assert not issubclass(ExtractionBusy, ValueError)


def _upload_error(exception):
    import asyncio
    from unittest.mock import patch

    from app.api import knowledge
    from app.security.identity import UNAUTHENTICATED

    upload = UploadFile(file=io.BytesIO(b"hello"), filename="note.txt")
    with patch.object(knowledge, "extract_document_text", side_effect=exception):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(knowledge.ingest_document(file=upload, user_id=None, principal=UNAUTHENTICATED))
    return caught.value


def test_endpoint_returns_503_with_retry_after_when_busy():
    error = _upload_error(ExtractionBusy("full"))
    assert error.status_code == 503
    assert error.headers["Retry-After"] == "5"


def test_endpoint_still_returns_400_for_a_bad_document():
    assert _upload_error(ExtractionError("nope")).status_code == 400


def test_concurrency_settings_have_defaults_and_bounds(monkeypatch):
    limits = ExtractionLimits.from_env()
    assert (limits.max_concurrent, limits.queue_timeout_seconds) == (2, 5)
    for value in ("0", "17", "x"):
        monkeypatch.setenv("NEXUS_EXTRACT_MAX_CONCURRENT", value)
        with pytest.raises(ValueError, match="NEXUS_EXTRACT_MAX_CONCURRENT"):
            ExtractionLimits.from_env()
    monkeypatch.setenv("NEXUS_EXTRACT_MAX_CONCURRENT", "16")
    assert ExtractionLimits.from_env().max_concurrent == 16
    monkeypatch.delenv("NEXUS_EXTRACT_MAX_CONCURRENT")
    for value in ("0", "61"):
        monkeypatch.setenv("NEXUS_EXTRACT_QUEUE_TIMEOUT_SECONDS", value)
        with pytest.raises(ValueError, match="NEXUS_EXTRACT_QUEUE_TIMEOUT_SECONDS"):
            ExtractionLimits.from_env()
