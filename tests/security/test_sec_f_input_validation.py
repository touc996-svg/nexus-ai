"""F. Malicious files and input validation over HTTP. Fixtures are small and bounded (no test allocates more than a few MB)."""

import io
import zipfile

import pytest

from tests.security.helpers import ALICE_KEY, auth


def upload(client, name, data, key=ALICE_KEY, content_type="application/octet-stream"):
    return client.post("/knowledge/ingest", files={"file": (name, data, content_type)}, headers=auth(key))


def post_json_bytes(client, path, body: bytes, content_type="application/json", key=ALICE_KEY):
    return client.post(path, content=body, headers={**auth(key), "Content-Type": content_type})


# ---- oversized payloads ---------------------------------------------------------------------------------------------

def test_a_declared_body_over_the_limit_is_refused_before_the_handler(make_app):
    ctx = make_app(env={"NEXUS_MAX_REQUEST_BYTES": 2048})
    response = ctx.client.post("/chat", content=b"x" * 5000, headers={**auth(ALICE_KEY), "Content-Type": "application/json"})
    assert response.status_code == 413 and ctx.llm.calls == []


@pytest.mark.parametrize("value", ["abc", "-1", "1e9", "99999999999999999999999999", ""])
def test_a_malformed_content_length_never_causes_a_server_error(app_ctx, value):
    response = app_ctx.client.post("/chat", content=b"{}", headers={**auth(ALICE_KEY), "Content-Type": "application/json",
                                                                       "Content-Length": value})
    assert response.status_code < 500


def test_a_chunked_body_without_content_length_is_also_bounded(make_app):
    """A streamed body has no Content-Length header; the limit must still apply while it is read."""
    ctx = make_app(env={"NEXUS_MAX_REQUEST_BYTES": 2048})

    def body():
        for _ in range(100):  # 100 KB streamed against a 2 KiB limit
            yield b"x" * 1024

    response = ctx.client.post("/chat", content=body(), headers={**auth(ALICE_KEY), "Content-Type": "application/json"})
    assert response.status_code == 413
    assert ctx.llm.calls == []


def test_the_upload_endpoint_enforces_its_own_file_size_cap(app_ctx, monkeypatch):
    from app.api import knowledge

    monkeypatch.setattr(knowledge, "MAX_FILE_SIZE", 10_000)
    assert upload(app_ctx.client, "big.txt", b"a" * 10_001).status_code == 413
    assert upload(app_ctx.client, "ok.txt", b"a" * 9_000).status_code == 200


@pytest.mark.parametrize("path", ["/chat", "/auto", "/team", "/agents/enterprise"])
def test_overlong_messages_are_rejected_without_a_model_call(app_ctx, path):
    response = app_ctx.client.post(path, json={"message": "y" * 5001}, headers=auth(ALICE_KEY))
    assert response.status_code == 422 and app_ctx.llm.calls == []


def test_overlong_identifiers_are_rejected(app_ctx):
    c = app_ctx.client
    assert c.post("/chat", json={"message": "hi", "session_id": "s" * 129}, headers=auth(ALICE_KEY)).status_code == 422
    assert c.post("/knowledge/search", json={"query": "q", "user_id": "u" * 129}, headers=auth(ALICE_KEY)).status_code == 422
    assert c.post("/memory/" + "s" * 3000, json={"role": "user", "content": "x"}, headers=auth(ALICE_KEY)).status_code < 500
    assert c.get("/profile/" + "u" * 3000, headers=auth(ALICE_KEY)).status_code in {403, 404, 422}


# ---- file validation ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["evil.exe", "page.html", "image.svg", "script.py", "archive.zip", "noextension", "a.pdf.exe",
                                  ".pdf.", "x.PDFX", "x.txt.sh", "doc.docm", "file.js"])
def test_unsupported_file_types_are_refused(app_ctx, name):
    assert upload(app_ctx.client, name, b"payload").status_code == 415


@pytest.mark.parametrize("name,data", [
    ("report.pdf", b"plain text pretending to be a pdf"),
    ("report.PDF", b"MZ\x90\x00 executable bytes"),
    ("memo.docx", b"not a zip at all"),
    ("memo.docx", b"%PDF-1.4 a real pdf header in a docx"),
    ("notes.txt", b"%PDF-1.4 pdf in a txt"),
    ("notes.md", b"PK\x03\x04 zip in a markdown file"),
    ("notes.txt", b"binary\x00with nul"),
])
def test_files_whose_content_does_not_match_the_extension_are_refused(app_ctx, name, data):
    response = upload(app_ctx.client, name, data)
    assert response.status_code == 400
    assert app_ctx.rag.search("pretending", user_id="alice") == []


def test_malformed_pdfs_are_refused_without_a_server_error(app_ctx):
    for data in (b"%PDF-1.4\n" + b"\xff\xfe garbage" * 20, b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n",
                 b"%PDF-" + b"A" * 5000, b"%PDF-1.7\n%%EOF\n"):
        response = upload(app_ctx.client, "broken.pdf", data)
        assert response.status_code == 400, data[:20]
        assert "Traceback" not in response.text


def _docx_zip(size, extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", b"\0" * size)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_a_bounded_zip_bomb_is_refused(app_ctx):
    data = _docx_zip(8_000_000)  # 8 MB when expanded, a few KB on the wire: ratio far above the limit
    assert len(data) < 50_000
    response = upload(app_ctx.client, "bomb.docx", data)
    assert response.status_code == 400 and "ratio" in response.text.lower() or "expands" in response.text.lower()


def test_a_docx_with_thousands_of_entries_is_refused(app_ctx):
    data = _docx_zip(10, {f"f{i}.xml": b"x" for i in range(2500)})
    assert upload(app_ctx.client, "many.docx", data).status_code == 400


def test_nested_archives_inside_a_docx_are_not_unpacked(app_ctx):
    inner = _docx_zip(1000)
    data = _docx_zip(10, {"word/embeddings/inner.docx": inner})
    response = upload(app_ctx.client, "nested.docx", data)
    assert response.status_code in {200, 400}  # either is safe; it must not hang or blow up
    assert response.status_code < 500


def test_path_traversal_in_filenames_never_reaches_the_filesystem(app_ctx, tmp_path):
    for name in ("../../etc/passwd.txt", "..\\..\\windows\\system.txt", "/etc/shadow.txt", "a/b/../../../c.txt", "%2e%2e%2fsecret.txt",
                 "name\x00.exe.txt", "C:\\boot.txt"):
        response = upload(app_ctx.client, name, b"harmless text")
        assert response.status_code in {200, 400, 415, 422}, name
    sources = app_ctx.client.get("/knowledge/sources", headers=auth(ALICE_KEY)).json()["sources"]
    assert all("/" not in s.get("source", "") and ".." not in s.get("source", "") for s in sources)


def test_empty_and_whitespace_files_are_refused(app_ctx):
    assert upload(app_ctx.client, "empty.txt", b"").status_code == 400
    assert upload(app_ctx.client, "blank.txt", b"   \n\t ").status_code in {200, 400}


def test_a_multipart_request_without_a_file_is_a_clean_client_error(app_ctx):
    response = app_ctx.client.post("/knowledge/ingest", data={"user_id": "alice"}, headers=auth(ALICE_KEY))
    assert response.status_code == 422


def test_error_responses_do_not_echo_internal_details(app_ctx):
    response = upload(app_ctx.client, "broken.pdf", b"%PDF-1.4 broken \xff")
    body = response.text
    for leak in ("/Users/", "/tmp/", "site-packages", "Traceback", "pypdf", "qdrant"):
        assert leak not in body


# ---- text edge cases ------------------------------------------------------------------------------------------------------

UNICODE_CASES = ["héllo wörld", "日本語のテキスト", "emoji 😀🔥👩\u200d💻", "\u202eevil\u202c text", "zero\u200bwidth\u200djoiner",
                 "\ufeffbom start", "combining e\u0301e\u0301e\u0301", "𝕳𝖊𝖑𝖑𝖔", "tab\tnew\nline\rreturn", "\x00nul", "\x1b[31mansi\x1b[0m",
                 "lone surrogate-ish \ud7ff", "RTL \u05e9\u05dc\u05d5\u05dd \u0645\u0631\u062d\u0628\u0627", "\u2028\u2029 separators"]


@pytest.mark.parametrize("text", UNICODE_CASES)
def test_unicode_and_control_characters_never_cause_a_server_error(app_ctx, text):
    c = app_ctx.client
    assert c.post("/chat", json={"message": text + " hello"}, headers=auth(ALICE_KEY)).status_code < 500
    assert c.post("/memory/uni", json={"role": "user", "content": text + "x"}, headers=auth(ALICE_KEY)).status_code < 500
    assert upload(c, "u.txt", (text + " body text").encode("utf-8", "replace")).status_code < 500
    assert c.post("/knowledge/search", json={"query": text + " q"}, headers=auth(ALICE_KEY)).status_code < 500


def test_bidi_and_control_characters_are_stripped_from_text_sent_to_the_model(app_ctx):
    app_ctx.client.post("/knowledge/ingest", files={"file": ("b.txt", "roadmap \u202etxt.exe\u202c note\u200b\x07".encode(), "text/plain")},
                        headers=auth(ALICE_KEY))
    app_ctx.client.post("/chat", json={"message": "roadmap"}, headers=auth(ALICE_KEY))
    prompts = app_ctx.llm.prompts_text()
    assert "\u202e" not in prompts and "\u200b" not in prompts and "\x07" not in prompts


def test_invalid_utf8_bodies_are_a_client_error(app_ctx):
    assert post_json_bytes(app_ctx.client, "/chat", b'{"message": "\xff\xfe\xfa"}').status_code in {400, 422}


# ---- malformed JSON and wrong types ---------------------------------------------------------------------------------------

BAD_BODIES = [b"", b"{", b"[]", b"null", b"123", b'"text"', b"{'message': 'x'}", b'{"message": }', b'{"message": "x",}',
              b'{"message": "x"} trailing', b'{"message": NaN}', b'{"message": Infinity}',
              b'{"message": 1e999}', b"[" * 5000 + b"]" * 5000, b'{"a":' * 3000 + b"1" + b"}" * 3000]


@pytest.mark.parametrize("body", BAD_BODIES)
def test_malformed_json_is_a_client_error_never_a_server_error(app_ctx, body):
    response = post_json_bytes(app_ctx.client, "/chat", body)
    assert response.status_code in {400, 413, 422}
    assert app_ctx.llm.calls == []


@pytest.mark.parametrize("payload", [{"message": 123}, {"message": ["a"]}, {"message": {"a": 1}}, {"message": None}, {"message": True},
                                     {"message": ""}, {"message": "   "}, {"message": "hi", "session_id": 5}, {"message": "hi", "session_id": ""},
                                     {"message": "hi", "session_id": {"x": 1}}, {"message": "hi", "user_id": ["alice"]},
                                     {"message": "hi", "user_id": 0}, {}, {"msg": "hi"}])
def test_wrong_types_and_missing_fields_are_rejected_before_any_model_call(app_ctx, payload):
    response = app_ctx.client.post("/chat", json=payload, headers=auth(ALICE_KEY))
    assert response.status_code in {400, 422}
    assert app_ctx.llm.calls == []


@pytest.mark.parametrize("body", [{"role": "root", "content": "x"}, {"role": "", "content": "x"}, {"role": "user", "content": ""},
                                  {"role": "user", "content": "x" * 5001}, {"role": ["user"], "content": "x"}, {"content": "x"}])
def test_memory_writes_validate_role_and_content(app_ctx, body):
    assert app_ctx.client.post("/memory/s", json=body, headers=auth(ALICE_KEY)).status_code in {400, 422}


@pytest.mark.parametrize("body", [{"goals": "not a list"}, {"skills": ["not", "a", "dict"]}, {"goals": ["x"] * 5000},
                                  {"career_direction": "x" * 100_000}, {"preferences": {"k": "v" * 100_000}},
                                  {"professional_background": {"nested": ["deep"] * 10}}, {}])
def test_profile_updates_validate_shape_and_size(app_ctx, body):
    assert app_ctx.client.put("/profile/alice", json=body, headers=auth(ALICE_KEY)).status_code in {400, 422}


def test_a_json_body_with_the_wrong_content_type_is_refused(app_ctx):
    response = post_json_bytes(app_ctx.client, "/chat", b'{"message": "hi"}', content_type="text/plain")
    assert response.status_code in {415, 422}


def test_search_limit_bounds_are_enforced(app_ctx):
    for limit in (0, -1, 51, 10**9, "ten", None, 1.5):
        response = app_ctx.client.post("/knowledge/search", json={"query": "q", "limit": limit}, headers=auth(ALICE_KEY))
        assert response.status_code == 422, limit


def test_the_chunked_limit_holds_over_a_real_http_server(make_app):
    """The ASGI test client is synthetic; this repeats the streamed-body case against a real uvicorn socket."""
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    ctx = make_app(env={"NEXUS_MAX_REQUEST_BYTES": 2048})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(ctx.api, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started

        def stream():
            for _ in range(200):  # 200 KB, sent chunked (no Content-Length)
                yield b"x" * 1024

        response = httpx.post(f"http://127.0.0.1:{port}/chat", content=stream(),
                              headers={**auth(ALICE_KEY), "Content-Type": "application/json"}, timeout=10)
        assert response.status_code == 413
        small = httpx.post(f"http://127.0.0.1:{port}/chat", json={"message": "hello"}, headers=auth(ALICE_KEY), timeout=10)
        assert small.status_code == 200  # the server is healthy afterwards
        assert ctx.llm.calls  # and only the small request reached the model
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_validation_errors_do_not_echo_the_rejected_input(app_ctx):
    response = app_ctx.client.post("/chat", json={"message": 12345, "session_id": "ECHO-CANARY" * 20}, headers=auth(ALICE_KEY))
    assert response.status_code == 422
    assert "ECHO-CANARY" not in response.text and "12345" not in response.text
    assert all(set(item) <= {"loc", "msg", "type", "ctx"} and "input" not in item for item in response.json()["detail"])


@pytest.mark.parametrize("length", [100, 103, 104, 120, 128])
def test_the_longest_allowed_session_ids_work_for_scoped_users(app_ctx, length):
    """Regression: a scoped user's stored session id is prefixed, which used to push 104-128 character ids past the store's limit (HTTP 500)."""
    session = "s" * length
    assert app_ctx.client.post("/chat", json={"message": "hello", "session_id": session}, headers=auth(ALICE_KEY)).status_code == 200
    assert app_ctx.client.post(f"/memory/{session}", json={"role": "user", "content": "x"}, headers=auth(ALICE_KEY)).status_code == 200
    assert app_ctx.client.get(f"/memory/{session}", headers=auth(ALICE_KEY)).status_code == 200
