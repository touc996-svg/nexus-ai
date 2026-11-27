"""Bounded, isolated extraction of text from uploaded documents (PDF, DOCX, TXT, MD).

Uploaded files are untrusted. Parsing happens in a short-lived child process (``_extract_worker.py``) that has a
timeout, CPU and address-space limits (POSIX; address-space limiting is effective on Linux only), a minimal environment
(no API keys, database URL or other application secrets) and no write access to files. A parser crash, hang or memory
blow-up therefore ends the child, not the API. The file signature is checked against the extension first.

Not provided: network isolation, a seccomp/namespace sandbox or a separate container. A parser exploit that achieves code
execution in the child still runs with the server user's file-read permissions.
"""

import os
# Fixed argv, no shell: the only caller-influenced values are a file path and numeric limits.
import subprocess  # nosec B404
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}
_WORKER = Path(__file__).with_name("_extract_worker.py")
_SAFE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}


class ExtractionError(ValueError):
    """The document was refused or could not be processed safely. The message is safe to show to the uploader."""


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


@dataclass(frozen=True)
class ExtractionLimits:
    max_chars: int = 2_000_000
    max_pdf_pages: int = 500
    max_docx_entries: int = 2_000
    max_docx_uncompressed: int = 100 * 1024 * 1024
    max_docx_ratio: int = 100
    timeout_seconds: int = 20
    memory_mb: int = 1024
    cpu_seconds: int = 15

    @classmethod
    def from_env(cls) -> "ExtractionLimits":
        return cls(
            max_chars=_env_int("NEXUS_RAG_MAX_DOCUMENT_CHARS", cls.max_chars, 1_000, 50_000_000),
            max_pdf_pages=_env_int("NEXUS_EXTRACT_MAX_PDF_PAGES", cls.max_pdf_pages, 1, 20_000),
            max_docx_entries=_env_int("NEXUS_EXTRACT_MAX_DOCX_ENTRIES", cls.max_docx_entries, 10, 100_000),
            max_docx_uncompressed=_env_int("NEXUS_EXTRACT_MAX_DOCX_BYTES", cls.max_docx_uncompressed, 1_000_000, 2_000_000_000),
            max_docx_ratio=_env_int("NEXUS_EXTRACT_MAX_DOCX_RATIO", cls.max_docx_ratio, 2, 10_000),
            timeout_seconds=_env_int("NEXUS_EXTRACT_TIMEOUT_SECONDS", cls.timeout_seconds, 1, 300),
            memory_mb=_env_int("NEXUS_EXTRACT_MEMORY_MB", cls.memory_mb, 128, 16_384),
            cpu_seconds=_env_int("NEXUS_EXTRACT_CPU_SECONDS", cls.cpu_seconds, 1, 300),
        )


def validate_signature(path: Path, suffix: str) -> None:
    """Refuse a file whose leading bytes do not match its extension."""
    with open(path, "rb") as handle:
        head = handle.read(2048)
    if suffix == ".pdf":
        if b"%PDF-" not in head[:1024]:
            raise ExtractionError("File content is not a PDF")
    elif suffix == ".docx":
        if not head.startswith(b"PK\x03\x04") or not zipfile.is_zipfile(path):
            raise ExtractionError("File content is not a DOCX document")
    elif suffix in {".txt", ".md"}:
        if b"\x00" in head or head.startswith((b"%PDF-", b"PK\x03\x04", b"\x7fELF", b"MZ")):
            raise ExtractionError("File content is not plain text")
    else:
        raise ExtractionError(f"Unsupported document format: {suffix}")


def extract_document_text(file_path: str, limits: ExtractionLimits | None = None) -> str:
    """Return the text of a document, or raise ExtractionError (a ValueError). Blocks: call it from a worker thread."""
    limits = limits or ExtractionLimits.from_env()
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ExtractionError(f"Unsupported document format: {suffix}")
    validate_signature(path, suffix)

    argv = [
        sys.executable, "-I", str(_WORKER), str(path), suffix,
        str(limits.max_chars), str(limits.max_pdf_pages), str(limits.max_docx_entries),
        str(limits.max_docx_uncompressed), str(limits.max_docx_ratio), str(limits.memory_mb), str(limits.cpu_seconds),
    ]
    try:
        completed = subprocess.run(  # nosec B603
            argv, capture_output=True, timeout=limits.timeout_seconds, env=_SAFE_ENV,
            cwd=tempfile.gettempdir(), check=False, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:  # run() kills the child before raising
        raise ExtractionError("Document processing timed out") from exc
    except OSError as exc:
        raise ExtractionError("Document processing is unavailable") from exc

    if completed.returncode == 0:
        return completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode == 3:
        raise ExtractionError(f"Document exceeds the {limits.max_chars}-character limit")
    if completed.returncode == 2:
        reason = completed.stderr.decode("utf-8", errors="replace").strip()[:120] or "rejected"
        raise ExtractionError(f"Document rejected: {reason}")
    raise ExtractionError("Document could not be processed safely")
