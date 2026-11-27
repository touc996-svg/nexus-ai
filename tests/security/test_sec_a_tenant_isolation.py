"""A. Tenant isolation over HTTP: /chat, /knowledge/*, /profile, /memory, sessions and history.

Alice and Bob hold per-user keys. Every cross-tenant attempt below must fail (403) or reveal/modify nothing.
"""

import pytest

from tests.security.helpers import ALICE_KEY, BOB_KEY, OPERATOR_KEY, auth


def _ingest(client, key, text, *, name="note.txt", user_id=None, params=None):
    data = {"user_id": user_id} if user_id else {}
    return client.post("/knowledge/ingest", files={"file": (name, text.encode(), "text/plain")}, data=data,
                       headers=auth(key), params=params)


# ---- /profile -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method,kwargs", [("get", {}), ("put", {"json": {"goals": ["x"]}}), ("delete", {})])
def test_profile_of_another_user_is_forbidden_by_path(app_ctx, method, kwargs):
    c = app_ctx.client
    assert c.put("/profile/alice", json={"goals": ["alice goal"]}, headers=auth(ALICE_KEY)).status_code == 200
    assert getattr(c, method)("/profile/alice", headers=auth(BOB_KEY), **kwargs).status_code == 403
    assert c.get("/profile/alice", headers=auth(ALICE_KEY)).json()["profile"]["goals"] == ["alice goal"]


def test_profile_path_cannot_be_smuggled_with_encodings_or_traversal(app_ctx):
    c = app_ctx.client
    c.put("/profile/alice", json={"goals": ["alice goal"]}, headers=auth(ALICE_KEY))
    for path in ("/profile/Alice", "/profile/alice%20", "/profile/%61lice/", "/profile/../profile/alice",
                 "/profile/alice%00", "/profile/alice;bob", "/profile/%2e%2e%2falice"):
        response = c.get(path, headers=auth(BOB_KEY))
        assert response.status_code in {307, 308, 400, 403, 404, 422}, path
        assert "alice goal" not in response.text, path


def test_profile_update_cannot_reassign_ownership_through_the_body(app_ctx):
    c = app_ctx.client
    response = c.put("/profile/bob", json={"goals": ["mine"], "user_id": "alice"}, headers=auth(BOB_KEY))
    assert response.status_code == 200
    assert c.get("/profile/alice", headers=auth(OPERATOR_KEY)).status_code == 404  # nothing was written for alice
    assert c.get("/profile/bob", headers=auth(BOB_KEY)).json()["profile"]["goals"] == ["mine"]


# ---- /memory and sessions -----------------------------------------------------------------------------------------

def test_same_session_id_is_private_per_user_for_read_write_delete(app_ctx):
    c = app_ctx.client
    c.post("/memory/shared", json={"role": "user", "content": "alice secret"}, headers=auth(ALICE_KEY))
    assert c.get("/memory/shared", headers=auth(BOB_KEY)).json()["history"] == []
    assert c.delete("/memory/shared", headers=auth(BOB_KEY)).json()["deleted"] is False
    assert [m["content"] for m in c.get("/memory/shared", headers=auth(ALICE_KEY)).json()["history"]] == ["alice secret"]


def test_guessing_the_internal_namespace_does_not_cross_tenants(app_ctx):
    from app.security.identity import Principal, scope_session_id

    c = app_ctx.client
    c.post("/memory/diary", json={"role": "user", "content": "alice secret"}, headers=auth(ALICE_KEY))
    internal = scope_session_id(Principal(user_id="alice", is_admin=False), "diary")  # what the store actually keys on
    assert c.get(f"/memory/{internal}", headers=auth(BOB_KEY)).json()["history"] == []
    assert c.get(f"/memory/{internal}", headers=auth(ALICE_KEY)).json()["history"] == []  # even the owner cannot address it raw


def test_memory_body_cannot_select_another_users_session(app_ctx):
    c = app_ctx.client
    c.post("/memory/s", json={"role": "user", "content": "bob note"}, headers=auth(BOB_KEY))
    response = c.post("/memory/s", json={"role": "user", "content": "x", "user_id": "bob", "session_id": "s"},
                      headers=auth(ALICE_KEY))
    assert response.status_code == 200
    assert [m["content"] for m in c.get("/memory/s", headers=auth(BOB_KEY)).json()["history"]] == ["bob note"]


def test_users_cannot_reach_operator_or_legacy_unscoped_sessions(app_ctx):
    app_ctx.memory.add_message("legacy", "user", "legacy secret")
    assert app_ctx.client.get("/memory/legacy", headers=auth(ALICE_KEY)).json()["history"] == []
    assert app_ctx.client.delete("/memory/legacy", headers=auth(ALICE_KEY)).json()["deleted"] is False
    assert app_ctx.memory.get_history("legacy")


# ---- /chat ------------------------------------------------------------------------------------------------------

def test_chat_refuses_to_act_as_another_user(app_ctx):
    response = app_ctx.client.post("/chat", json={"message": "hello", "user_id": "alice"}, headers=auth(BOB_KEY))
    assert response.status_code == 403
    assert app_ctx.llm.calls == []  # refused before any model call


def test_chat_history_and_profile_of_one_user_never_reach_another_users_prompt(app_ctx):
    c = app_ctx.client
    app_ctx.memory.update_profile("alice", {"goals": ["ALICE-PROFILE-CANARY"]})
    c.post("/memory/talk", json={"role": "user", "content": "ALICE-HISTORY-CANARY"}, headers=auth(ALICE_KEY))
    response = c.post("/chat", json={"message": "plan my week", "session_id": "talk"}, headers=auth(BOB_KEY))
    assert response.status_code == 200
    assert "ALICE-PROFILE-CANARY" not in app_ctx.llm.prompts_text()
    assert "ALICE-HISTORY-CANARY" not in app_ctx.llm.prompts_text()
    assert "ALICE-HISTORY-CANARY" not in response.text


def test_chat_stores_conversation_only_in_the_callers_namespace(app_ctx):
    c = app_ctx.client
    c.post("/chat", json={"message": "BOB-CHAT-CANARY", "session_id": "room"}, headers=auth(BOB_KEY))
    assert "BOB-CHAT-CANARY" in c.get("/memory/room", headers=auth(BOB_KEY)).text
    assert "BOB-CHAT-CANARY" not in c.get("/memory/room", headers=auth(ALICE_KEY)).text


def test_chat_retrieval_is_confined_to_the_callers_documents(app_ctx):
    c = app_ctx.client
    assert _ingest(c, ALICE_KEY, "quarterly roadmap ALICE-DOC-CANARY confidential").status_code == 200
    assert _ingest(c, BOB_KEY, "quarterly roadmap BOB-DOC-CANARY public").status_code == 200
    response = c.post("/chat", json={"message": "quarterly roadmap"}, headers=auth(BOB_KEY))
    assert response.status_code == 200
    prompts = app_ctx.llm.prompts_text()
    assert "BOB-DOC-CANARY" in prompts and "ALICE-DOC-CANARY" not in prompts


# ---- /knowledge -----------------------------------------------------------------------------------------------------

def test_knowledge_search_and_listing_are_confined_even_when_user_id_is_forged(app_ctx):
    c = app_ctx.client
    _ingest(c, ALICE_KEY, "alice payroll ALICE-DOC-CANARY")
    _ingest(c, BOB_KEY, "bob payroll BOB-DOC-CANARY")
    forged_search = c.post("/knowledge/search", json={"query": "payroll", "user_id": "alice"}, headers=auth(BOB_KEY))
    assert forged_search.status_code == 403
    assert c.get("/knowledge/sources", params={"user_id": "alice"}, headers=auth(BOB_KEY)).status_code == 403
    own = c.post("/knowledge/search", json={"query": "payroll"}, headers=auth(BOB_KEY)).json()
    assert own["results_count"] >= 1 and "ALICE-DOC-CANARY" not in str(own)
    assert [s["source"] for s in c.get("/knowledge/sources", headers=auth(BOB_KEY)).json()["sources"]] != []


def test_knowledge_delete_cannot_remove_another_tenants_source(app_ctx):
    c = app_ctx.client
    _ingest(c, ALICE_KEY, "alice payroll ALICE-DOC-CANARY")
    source_id = c.get("/knowledge/sources", headers=auth(ALICE_KEY)).json()["sources"][0]["source_id"]
    assert c.delete(f"/knowledge/sources/{source_id}", params={"user_id": "alice"}, headers=auth(BOB_KEY)).status_code == 403
    c.delete(f"/knowledge/sources/{source_id}", headers=auth(BOB_KEY))  # Bob's own scope: nothing of Alice's may go
    assert c.post("/knowledge/search", json={"query": "payroll"}, headers=auth(ALICE_KEY)).json()["results_count"] >= 1


def test_upload_form_user_id_and_filename_metadata_cannot_redirect_ownership(app_ctx):
    c = app_ctx.client
    forged = _ingest(c, BOB_KEY, "planted text BOB-PLANT", user_id="alice")
    assert forged.status_code == 403
    traversal = _ingest(c, BOB_KEY, "mine MINE-DOC", name="../../alice/secret.txt")
    assert traversal.status_code == 200
    assert c.get("/knowledge/sources", headers=auth(ALICE_KEY)).json()["sources"] == []
    assert c.post("/knowledge/search", json={"query": "planted"}, headers=auth(ALICE_KEY)).json()["results_count"] == 0


def test_user_id_in_query_string_of_ingest_is_ignored(app_ctx):
    c = app_ctx.client
    response = _ingest(c, BOB_KEY, "bob only BOB-ONLY", params={"user_id": "alice"})
    assert response.status_code == 200
    assert c.post("/knowledge/search", json={"query": "bob only"}, headers=auth(ALICE_KEY)).json()["results_count"] == 0


def test_same_filename_uploaded_by_two_tenants_does_not_overwrite(app_ctx):
    c = app_ctx.client
    _ingest(c, ALICE_KEY, "alice version ALICE-V", name="plan.txt")
    _ingest(c, BOB_KEY, "bob version BOB-V", name="plan.txt")
    assert "ALICE-V" in str(c.post("/knowledge/search", json={"query": "version"}, headers=auth(ALICE_KEY)).json())
    assert "BOB-V" not in str(c.post("/knowledge/search", json={"query": "version"}, headers=auth(ALICE_KEY)).json())


def test_the_operator_can_read_any_tenant_by_design(app_ctx):
    c = app_ctx.client
    _ingest(c, ALICE_KEY, "alice payroll ALICE-DOC-CANARY")
    found = c.post("/knowledge/search", json={"query": "payroll", "user_id": "alice"}, headers=auth(OPERATOR_KEY))
    assert found.status_code == 200 and "ALICE-DOC-CANARY" in found.text
