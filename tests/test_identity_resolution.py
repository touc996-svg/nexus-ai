"""Regression tests for user-id resolution (tenant isolation) and the typing fixes made with it."""

import pytest
from fastapi import HTTPException

from app.core.memory import NexusMemory
from app.security.identity import (
    OPERATOR,
    SYSTEM_USER,
    UNAUTHENTICATED,
    Principal,
    resolve_user_id,
)

ALICE = Principal(user_id="alice", is_admin=False)


def test_a_non_admin_principal_must_have_a_user_id():
    with pytest.raises(ValueError):
        Principal(user_id=None, is_admin=False)
    assert Principal(user_id=None, is_admin=True).role == "admin"


def test_scoped_caller_always_acts_as_themselves():
    assert resolve_user_id(ALICE, None) == "alice"
    assert resolve_user_id(ALICE, "alice") == "alice"
    assert resolve_user_id(ALICE, None, default=SYSTEM_USER) == "alice"  # the default never replaces the caller


def test_scoped_caller_cannot_name_another_user():
    for requested in ("bob", SYSTEM_USER, ""):
        with pytest.raises(HTTPException) as caught:
            resolve_user_id(ALICE, requested, default=SYSTEM_USER)
        assert caught.value.status_code == 403


@pytest.mark.parametrize("admin", [OPERATOR, UNAUTHENTICATED])
def test_administrators_keep_the_requested_id_or_the_default(admin):
    assert resolve_user_id(admin, "bob") == "bob"
    assert resolve_user_id(admin, None, default=SYSTEM_USER) == SYSTEM_USER
    assert resolve_user_id(admin, None) is None


def test_history_limit_precedence_is_unchanged(monkeypatch):
    monkeypatch.setenv("NEXUS_MEMORY_HISTORY_LIMIT", "7")
    assert NexusMemory(initialize=False).history_limit == 7
    assert NexusMemory(history_limit=3, initialize=False).history_limit == 3
    monkeypatch.delenv("NEXUS_MEMORY_HISTORY_LIMIT")
    assert NexusMemory(initialize=False).history_limit == 16
