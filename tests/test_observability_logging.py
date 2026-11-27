import io
import json
import logging

import pytest

from app.core import logging_config as lc

CANARIES = ("hunter2-password", "sk-ant-abcdefghij1234", "jane.doe@corp.com", "123-45-6789", "PROMPT-CANARY-TEXT")


def _logger(fmt="json"):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(lc.JsonFormatter() if fmt == "json" else lc.RedactingTextFormatter())
    log = logging.getLogger(f"nexus.test.{fmt}.{id(stream)}")
    log.handlers = [handler]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log, stream


def lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_lines_have_fixed_fields_and_the_request_id():
    log, stream = _logger()
    token = lc.request_id_var.set("req123")
    try:
        log.info("http_request", extra={"event": "http_request", "method": "GET", "route": "/ping", "status": 200, "duration_ms": 1.5})
    finally:
        lc.request_id_var.reset(token)
    (entry,) = lines(stream)
    assert entry["event"] == "http_request" and entry["request_id"] == "req123"
    assert entry["route"] == "/ping" and entry["status"] == 200 and entry["level"] == "INFO"


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_credentials_and_personal_data_are_redacted_from_messages(fmt):
    log, stream = _logger(fmt)
    log.warning("login failed password=hunter2-password key sk-ant-abcdefghij1234 mail jane.doe@corp.com ssn 123-45-6789")
    log.warning("calling Authorization: Bearer abc.def.ghi and x-api-key: secretvalue1234567")
    out = stream.getvalue()
    for canary in CANARIES[:4] + ("abc.def.ghi", "secretvalue1234567"):
        assert canary not in out
    assert "[REDACTED" in out


def test_extra_fields_outside_the_allowlist_are_never_emitted():
    log, stream = _logger()
    log.info("something_happened", extra={"prompt": "PROMPT-CANARY-TEXT", "authorization": "Bearer x", "count": 3})
    (entry,) = lines(stream)
    assert "PROMPT-CANARY-TEXT" not in stream.getvalue() and "prompt" not in entry and entry["count"] == 3


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_exception_messages_are_dropped_but_type_and_frames_kept(fmt):
    log, stream = _logger(fmt)
    try:
        raise ValueError("PROMPT-CANARY-TEXT and hunter2-password")
    except ValueError:
        log.exception("request_failed")
    out = stream.getvalue()
    assert "PROMPT-CANARY-TEXT" not in out and "hunter2-password" not in out
    assert "ValueError" in out and "test_observability_logging.py" in out


def test_long_messages_are_bounded():
    log, stream = _logger()
    log.info("x" * 50000)
    assert len(lines(stream)[0]["msg"]) <= lc.MAX_MESSAGE_CHARS


def test_configure_logging_is_idempotent_and_leaves_the_root_logger_alone(monkeypatch):
    root_before = list(logging.getLogger().handlers)
    lc.configure_logging()
    lc.configure_logging()
    nexus = logging.getLogger("nexus")
    assert sum(getattr(h, "_nexus_handler", False) for h in nexus.handlers) == 1
    assert logging.getLogger().handlers == root_before


@pytest.mark.parametrize("name,value", [("NEXUS_LOG_LEVEL", "LOUD"), ("NEXUS_LOG_FORMAT", "xml")])
def test_invalid_logging_settings_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        lc.configure_logging()
