"""Document text extraction worker. Runs in a child process started by app/services/document_extraction.py.

Standalone on purpose: it imports no application module, so the child never loads configuration, keys or the database.
Protocol: argv = path, suffix, max_chars, max_pdf_pages, max_docx_entries, max_docx_uncompressed, max_docx_ratio,
memory_mb, cpu_seconds. Extracted text goes to stdout (UTF-8). Exit codes: 0 ok, 2 rejected (reason on stderr),
3 character limit exceeded. Any other exit status or a signal means the parser crashed or hit a resource limit.
"""

import sys
import zipfile

EXIT_OK, EXIT_REJECTED, EXIT_TOO_LONG = 0, 2, 3


class Rejected(Exception):
    pass


class TooLong(Exception):
    pass


def apply_limits(memory_mb: int, cpu_seconds: int) -> None:
    try:
        import resource
    except ImportError:  # not POSIX
        return
    for name, value in (
        ("RLIMIT_CPU", cpu_seconds),
        ("RLIMIT_AS", memory_mb * 1024 * 1024),  # enforced on Linux; macOS ignores or rejects it
        ("RLIMIT_FSIZE", 0),                      # the worker never writes files
        ("RLIMIT_CORE", 0),
        ("RLIMIT_NOFILE", 64),
    ):
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            pass  # platform cannot apply this limit; the parent's timeout still applies


class Collector:
    def __init__(self, max_chars: int):
        self.max_chars = max_chars
        self.total = 0
        self.parts: list[str] = []

    def add(self, text: str) -> None:
        self.total += len(text) + 1
        if self.total > self.max_chars:
            raise TooLong
        self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


def check_docx_zip(path: str, max_entries: int, max_uncompressed: int, max_ratio: int) -> None:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise Rejected("not a valid DOCX archive") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise Rejected("DOCX contains too many files")
        names = {info.filename for info in infos}
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise Rejected("not a Word document")
        total = 0
        for info in infos:
            total += info.file_size
            if total > max_uncompressed:
                raise Rejected("DOCX expands beyond the allowed size")
            if info.file_size > 1_000_000 and info.compress_size > 0 and info.file_size / info.compress_size > max_ratio:
                raise Rejected("DOCX compression ratio is not plausible")


def extract_pdf(path: str, collector: Collector, max_pages: int) -> None:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise Rejected("encrypted PDF")
    if len(reader.pages) > max_pages:
        raise Rejected("PDF has too many pages")
    for page in reader.pages:
        collector.add(page.extract_text() or "")


def extract_docx(path: str, collector: Collector) -> None:
    from docx import Document

    for paragraph in Document(path).paragraphs:
        collector.add(paragraph.text)


def extract_plain(path: str, collector: Collector) -> None:
    with open(path, "rb") as handle:
        data = handle.read(collector.max_chars * 4 + 4)
    collector.add(data.decode("utf-8", errors="replace"))


def main(argv: list[str]) -> int:
    (path, suffix, max_chars, max_pages, max_entries, max_uncompressed, max_ratio, memory_mb, cpu_seconds) = argv[1:10]
    apply_limits(int(memory_mb), int(cpu_seconds))
    collector = Collector(int(max_chars))
    try:
        if suffix == ".pdf":
            extract_pdf(path, collector, int(max_pages))
        elif suffix == ".docx":
            check_docx_zip(path, int(max_entries), int(max_uncompressed), int(max_ratio))
            extract_docx(path, collector)
        elif suffix in {".txt", ".md"}:
            extract_plain(path, collector)
        else:
            raise Rejected("unsupported format")
    except TooLong:
        return EXIT_TOO_LONG
    except Rejected as exc:
        sys.stderr.write(str(exc)[:200])
        return EXIT_REJECTED
    sys.stdout.buffer.write(collector.text().encode("utf-8", errors="replace"))
    sys.stdout.buffer.flush()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
