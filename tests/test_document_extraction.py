"""Adversarial and regression tests for bounded, isolated document extraction."""

import asyncio
import io
import sys
import textwrap
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from docx import Document
from fastapi.concurrency import run_in_threadpool
from pypdf import PdfWriter

from app.services import document_extraction as de
from app.services.document_extraction import (
    ExtractionError,
    ExtractionLimits,
    extract_document_text,
)

LIMITS = ExtractionLimits(max_chars=10_000, max_pdf_pages=10, max_docx_entries=100,
                          max_docx_uncompressed=5_000_000, max_docx_ratio=100, timeout_seconds=10,
                          memory_mb=1024, cpu_seconds=10)
LINUX = sys.platform.startswith("linux")


def _pdf_with_text(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def _docx(paragraphs) -> bytes:
    document, buffer = Document(), io.BytesIO()
    for text in paragraphs:
        document.add_paragraph(text)
    document.save(buffer)
    return buffer.getvalue()


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# ---- valid documents keep working -------------------------------------------------------------------------------

def test_valid_documents_still_extract(tmp_path):
    assert "Hello NEXUS" in extract_document_text(_write(tmp_path, "a.pdf", _pdf_with_text("Hello NEXUS")), LIMITS)
    assert "quarterly plan" in extract_document_text(_write(tmp_path, "a.docx", _docx(["quarterly plan", "x"])), LIMITS)
    assert extract_document_text(_write(tmp_path, "a.txt", "héllo".encode()), LIMITS) == "héllo"
    assert extract_document_text(_write(tmp_path, "a.md", b"# title"), LIMITS) == "# title"


# ---- signatures vs extensions -----------------------------------------------------------------------------------

@pytest.mark.parametrize("name,data", [
    ("fake.pdf", b"just some text, not a pdf"),
    ("fake.docx", b"plain text"),
    ("fake.docx", _pdf_with_text("x")),
    ("fake.txt", _pdf_with_text("x")),
    ("fake.md", _docx(["x"])),
    ("fake.txt", b"MZ\x90\x00 binary"),
    ("fake.txt", b"abc\x00def"),
])
def test_mislabelled_files_are_refused(tmp_path, name, data):
    with pytest.raises(ExtractionError):
        extract_document_text(_write(tmp_path, name, data), LIMITS)


def test_zip_that_is_not_a_word_document_is_refused(tmp_path):
    path = _write(tmp_path, "x.docx", _zip({"hello.txt": b"hi"}))
    with pytest.raises(ExtractionError, match="rejected"):
        extract_document_text(path, LIMITS)


def test_unsupported_extension_is_refused(tmp_path):
    with pytest.raises(ExtractionError):
        extract_document_text(_write(tmp_path, "x.exe", b"MZ"), LIMITS)


# ---- decompression bombs and size limits -------------------------------------------------------------------------

def _bomb(size, extra=None):
    entries = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"\0" * size}
    entries.update(extra or {})
    return _zip(entries)


def test_docx_that_expands_too_far_is_refused(tmp_path):
    data = _bomb(20_000_000)
    assert len(data) < 100_000  # tiny on disk, 20 MB expanded
    with pytest.raises(ExtractionError, match="expands"):
        extract_document_text(_write(tmp_path, "bomb.docx", data), LIMITS)


def test_docx_with_implausible_compression_ratio_is_refused(tmp_path):
    with pytest.raises(ExtractionError, match="ratio"):
        extract_document_text(_write(tmp_path, "ratio.docx", _bomb(3_000_000)), LIMITS)


def test_docx_with_too_many_entries_is_refused(tmp_path):
    many = {f"f{i}.xml": b"x" for i in range(300)}
    with pytest.raises(ExtractionError, match="too many"):
        extract_document_text(_write(tmp_path, "many.docx", _bomb(10, many)), LIMITS)


def test_pdf_with_too_many_pages_is_refused(tmp_path):
    writer, buffer = PdfWriter(), io.BytesIO()
    for _ in range(30):
        writer.add_blank_page(width=100, height=100)
    writer.write(buffer)
    with pytest.raises(ExtractionError, match="too many pages"):
        extract_document_text(_write(tmp_path, "pages.pdf", buffer.getvalue()), LIMITS)


def test_text_beyond_the_character_limit_is_refused_while_extracting(tmp_path):
    with pytest.raises(ExtractionError, match="character limit"):
        extract_document_text(_write(tmp_path, "big.txt", b"a" * 50_000), LIMITS)
    with pytest.raises(ExtractionError, match="character limit"):
        extract_document_text(_write(tmp_path, "big.docx", _docx(["b" * 6000, "c" * 6000])), LIMITS)


def test_corrupt_files_do_not_crash_the_caller(tmp_path):
    with pytest.raises(ExtractionError):
        extract_document_text(_write(tmp_path, "bad.pdf", b"%PDF-1.4\n garbage \xff\xfe"), LIMITS)
    with pytest.raises(ExtractionError):
        extract_document_text(_write(tmp_path, "bad.docx", b"PK\x03\x04" + b"\xff" * 50), LIMITS)


# ---- isolation: hostile worker scripts (they run the real apply_limits, then misbehave) --------------------------

def _hostile(tmp_path, monkeypatch, body: str):
    worker_dir = Path(de.__file__).parent
    script = tmp_path / "hostile.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(worker_dir)!r})\n"
        "import _extract_worker as w\n"
        "w.apply_limits(int(sys.argv[8]), int(sys.argv[9]))\n" + textwrap.dedent(body)
    )
    monkeypatch.setattr(de, "_WORKER", script)


def test_a_hung_worker_is_killed_at_the_timeout(tmp_path, monkeypatch):
    _hostile(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(ExtractionError, match="timed out"):
        extract_document_text(_write(tmp_path, "a.txt", b"x"), replace(LIMITS, timeout_seconds=1))
    assert time.monotonic() - started < 8


def test_a_cpu_spinning_worker_is_stopped_by_the_cpu_limit(tmp_path, monkeypatch):
    _hostile(tmp_path, monkeypatch, "while True:\n    pass\n")
    started = time.monotonic()
    with pytest.raises(ExtractionError, match="safely"):
        extract_document_text(_write(tmp_path, "a.txt", b"x"), replace(LIMITS, timeout_seconds=30, cpu_seconds=1))
    assert time.monotonic() - started < 15


@pytest.mark.skipif(not LINUX, reason="RLIMIT_AS is enforced on Linux only (macOS ignores or rejects it)")
def test_a_memory_hungry_worker_is_stopped_by_the_address_space_limit(tmp_path, monkeypatch):
    _hostile(tmp_path, monkeypatch, "blob = bytearray(2_000_000_000)\nprint('allocated')\n")
    with pytest.raises(ExtractionError, match="safely"):
        extract_document_text(_write(tmp_path, "a.txt", b"x"), replace(LIMITS, memory_mb=512))


def test_a_crashing_worker_is_handled(tmp_path, monkeypatch):
    _hostile(tmp_path, monkeypatch, "import os\nos.abort()\n")
    with pytest.raises(ExtractionError, match="safely"):
        extract_document_text(_write(tmp_path, "a.txt", b"x"), LIMITS)


def test_the_child_gets_no_application_secrets(tmp_path, monkeypatch):
    for name in ("NEXUS_API_KEY", "DATABASE_URL", "OPENAI_API_KEY", "NEXUS_JWT_SECRETS"):
        monkeypatch.setenv(name, "super-secret-value")
    _hostile(tmp_path, monkeypatch, "import os\nprint(sorted(os.environ))\nprint(os.getcwd())\n")
    out = extract_document_text(_write(tmp_path, "a.txt", b"x"), LIMITS)
    assert "secret" not in out.lower()
    for name in ("NEXUS_API_KEY", "DATABASE_URL", "OPENAI_API_KEY", "NEXUS_JWT_SECRETS"):
        assert name not in out


def test_the_child_cannot_write_files(tmp_path, monkeypatch):
    if sys.platform == "darwin":
        pytest.skip("macOS applies RLIMIT_FSIZE inconsistently")
    target = tmp_path / "pwned.txt"
    _hostile(tmp_path, monkeypatch, f"with open({str(target)!r}, 'w') as handle:\n    handle.write('x' * 10)\n")
    with pytest.raises(ExtractionError):
        extract_document_text(_write(tmp_path, "a.txt", b"x"), LIMITS)
    assert not target.exists() or target.stat().st_size == 0


def test_the_event_loop_stays_responsive_during_extraction(tmp_path, monkeypatch):
    _hostile(tmp_path, monkeypatch, "import time\ntime.sleep(1.5)\nprint('done')\n")
    path = _write(tmp_path, "a.txt", b"x")

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        task = asyncio.create_task(ticker())
        result = await run_in_threadpool(extract_document_text, path, LIMITS)
        task.cancel()
        return result, ticks

    result, ticks = asyncio.run(scenario())
    assert "done" in result
    assert ticks >= 10  # the loop kept running for the ~1.5 s the child took


def test_limit_settings_are_validated(monkeypatch):
    monkeypatch.setenv("NEXUS_EXTRACT_MEMORY_MB", "1")
    with pytest.raises(ValueError, match="NEXUS_EXTRACT_MEMORY_MB"):
        ExtractionLimits.from_env()
