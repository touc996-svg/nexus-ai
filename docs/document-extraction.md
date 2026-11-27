# Bounded document extraction

Uploads to `/knowledge/ingest` (PDF, DOCX, TXT, MD) are untrusted. Text is extracted by
`app/services/document_extraction.py`, which runs `app/services/_extract_worker.py` in a child process.

## Controls
- **Signature vs extension:** PDF must contain `%PDF-` near the start; DOCX must be a ZIP with `[Content_Types].xml` and
  `word/document.xml`; TXT/MD must not contain NUL bytes or start like a PDF, ZIP, ELF or PE file.
- **DOCX (zip bombs):** max entries, max total uncompressed size, and a per-entry compression-ratio limit, read from the
  archive directory before any parsing.
- **PDF:** page-count limit, encrypted PDFs refused, text collected page by page and stopped at the character limit.
- **All formats:** extraction stops at `NEXUS_RAG_MAX_DOCUMENT_CHARS` while reading, not afterwards.
- **Isolation:** child process with a wall-clock timeout (killed on expiry), `RLIMIT_CPU`, `RLIMIT_AS` (memory), no file
  writes (`RLIMIT_FSIZE=0`), no core dumps, 64 open files, `python -I`, stdin closed, and an environment of only
  `PATH`, `LANG`, `PYTHONDONTWRITEBYTECODE`. API keys, `DATABASE_URL` and JWT secrets are not passed on.
- **Failures:** timeouts, crashes, resource kills and parser errors become a `ValueError` with a safe message (HTTP 400);
  nothing from the parser's error text is returned.
- **Event loop:** the endpoint runs extraction and embedding with `run_in_threadpool`.

## Settings (defaults)
`NEXUS_EXTRACT_TIMEOUT_SECONDS=20`, `NEXUS_EXTRACT_CPU_SECONDS=15`, `NEXUS_EXTRACT_MEMORY_MB=1024`,
`NEXUS_EXTRACT_MAX_PDF_PAGES=500`, `NEXUS_EXTRACT_MAX_DOCX_ENTRIES=2000`, `NEXUS_EXTRACT_MAX_DOCX_BYTES=104857600`,
`NEXUS_EXTRACT_MAX_DOCX_RATIO=100`, `NEXUS_RAG_MAX_DOCUMENT_CHARS=2000000`.

## Limitations
- `NexusRAG.extract_text` and `NexusRAG.ingest_file` in `app/services/rag.py` are **unchanged and still parse in-process**. The API no
  longer uses them (it calls the isolated extractor, then `ingest_text`). They were left alone because the evaluation replay
  cache is keyed on a hash of `rag.py` and cannot be regenerated without a live Ollama; remove them in a follow-up together
  with a cache refresh. Do not call them with untrusted files.
- **macOS:** `RLIMIT_AS` is ignored or rejected, so the memory limit is **not enforced** there (timeout, CPU limit and the
  structural checks still are). The memory test is skipped off Linux. Production runs on Linux (Docker).
- No network isolation, seccomp or namespace sandbox: a parser exploit that gains code execution in the child still has the
  server user's read access and network. A separate container or gVisor/Firecracker-style sandbox would be the next step.
- Concurrent uploads each spawn a child: bound them with the existing rate limits (not changed here) or a semaphore.
- `RLIMIT_AS` limits virtual memory; the 1024 MB default was sized for pypdf/python-docx and may need raising for very
  large valid files.
- Heuristic checks cannot prove a file is benign.
