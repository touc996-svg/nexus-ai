"""Structured JSON logging with centralised redaction.

Policy: logs carry events, identifiers and numbers. They never carry prompts, user messages, documents, model output,
credentials or exception *messages* (exceptions are logged as a type name and ``file:line function`` frames).
``redact_log_text`` is the single choke point for any free text that reaches a log line; it is defence in depth on top
of not logging such text in the first place.
"""

import json
import logging
import os
import re
import sys
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime

from app.security.content_safety import redact_pii, scrub_model_output

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

ALLOWED_EXTRA = ("event", "method", "route", "status", "duration_ms", "error_type", "agent", "count", "model", "op",
                "rule", "value", "threshold", "window_seconds", "severity")
MAX_MESSAGE_CHARS = 1000
_SENSITIVE_PAIR = re.compile(
    r"(?i)\b(authorization|x-api-key|api[_-]?key|password|passwd|secret|token|credential|prompt|message|content)\b"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_EVENT = re.compile(r"^[a-z][a-z0-9_]{2,60}$")


def redact_log_text(text) -> str:
    value = str(text)[: MAX_MESSAGE_CHARS * 4]
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _SENSITIVE_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)
    value = redact_pii(scrub_model_output(value, max_chars=MAX_MESSAGE_CHARS * 4))
    return value[:MAX_MESSAGE_CHARS]


def _exception_summary(exc_info):
    """Type name and bounded ``file:line function`` frames; the exception message is deliberately dropped."""
    exc_type, _, tb = exc_info
    frames = [f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in traceback.extract_tb(tb)[-8:]]
    return {"error_type": exc_type.__name__ if exc_type else "unknown", "stack": frames}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = redact_log_text(record.getMessage())
        first = message.split(" ", 1)[0]
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or (first if _EVENT.match(first) else "log"),
            "msg": message,
            "request_id": request_id_var.get(),
        }
        for name in ALLOWED_EXTRA:
            value = getattr(record, name, None)
            if name == "event" or value is None:
                continue
            payload[name] = redact_log_text(value) if isinstance(value, str) else value if isinstance(value, (int, float, bool)) else None
        if record.exc_info and record.exc_info[0] is not None:
            payload.update(_exception_summary(record.exc_info))
        return json.dumps(payload, ensure_ascii=True, default=str)


class RedactingTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = f"{record.levelname} {record.name} {redact_log_text(record.getMessage())}"
        if record.exc_info and record.exc_info[0] is not None:
            summary = _exception_summary(record.exc_info)
            line += f" error_type={summary['error_type']} stack={'; '.join(summary['stack'])}"
        return line


def configure_logging() -> None:
    """Attach one handler to the ``nexus`` logger tree. Idempotent; leaves uvicorn's and the root logger alone."""
    level_name = os.getenv("NEXUS_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = logging.getLevelNamesMapping().get(level_name)
    if level is None:
        raise ValueError("NEXUS_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    fmt = os.getenv("NEXUS_LOG_FORMAT", "json").strip().lower() or "json"
    if fmt not in {"json", "text"}:
        raise ValueError("NEXUS_LOG_FORMAT must be json or text")
    logger = logging.getLogger("nexus")
    logger.setLevel(level)
    for handler in list(logger.handlers):
        if getattr(handler, "_nexus_handler", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._nexus_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter() if fmt == "json" else RedactingTextFormatter())
    logger.addHandler(handler)
