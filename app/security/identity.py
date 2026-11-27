"""Request identity and tenant isolation.

Authentication produces a ``Principal`` that the middleware attaches to the request.
Endpoints that touch per-user data must resolve the user (and session) through
this module instead of trusting identifiers supplied by the client.

Principal kinds
  * scoped user: authenticated with a key from ``NEXUS_API_KEYS``. Bound to one user_id.
    Cross-user access is rejected with 403, and session ids are stored in a per-user namespace.
  * operator: authenticated with the legacy ``NEXUS_API_KEY``. Full access, as before.
    This is an explicit administrator credential, not an end-user credential.
  * unauthenticated: authentication is switched off (local development or demo mode).
    No enforcement, as before. Never use this mode with real data.

If authentication is required but no principal is present, access is refused (fail closed).

Roles
  * ``admin``: the operator credential, and the no-authentication development/demo mode.
  * ``user``: a scoped key. It reaches user-data routes (confined to its own data) and the agent routes,
    and is refused (403) on system-level routes: AEGIS-X, activity history and system status. Those routes
    declare ``Depends(require_admin)``; a new route that is not classified fails ``tests/test_authorization_roles.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Annotated, overload

from fastapi import Depends, HTTPException, Request

SYSTEM_USER = "system"
ROLE_ADMIN = "admin"
ROLE_USER = "user"


@dataclass(frozen=True)
class Principal:
    user_id: str | None
    is_admin: bool

    def __post_init__(self) -> None:
        # A non-administrator without a user id would be neither confined to a user nor an administrator.
        if not self.is_admin and self.user_id is None:
            raise ValueError("A non-administrator principal must have a user id")

    @property
    def scoped(self) -> bool:
        """True when the caller is bound to a single user and must be confined to it."""
        return not self.is_admin and self.user_id is not None

    @property
    def role(self) -> str:
        """``admin`` for the operator credential (and for no-authentication mode), ``user`` for a scoped key."""
        return ROLE_ADMIN if self.is_admin else ROLE_USER


UNAUTHENTICATED = Principal(user_id=None, is_admin=True)
OPERATOR = Principal(user_id=None, is_admin=True)


def get_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        return principal
    settings = getattr(request.app.state, "security_settings", None)
    if settings is not None and settings.require_api_key and not settings.demo_mode:
        raise HTTPException(status_code=401, detail="Valid API key required.")
    return UNAUTHENTICATED


def is_valid_user_id(value) -> bool:
    """The rule for a user id wherever one is issued or asserted (stored keys, token subjects).

    1 to 128 printable characters without spaces, and not the reserved shared-pool id. NEXUS_API_KEYS entries
    follow the same rule (hardening.parse_user_keys); tests keep the three in step.
    """
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(ord(c) >= 33 for c in value)
        and value != SYSTEM_USER
    )


def require_admin(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    """Dependency for system-level routes: administrator credentials only.

    A valid scoped key is still refused (403): being authenticated is not the same as being authorized. A request
    with no principal is refused by ``get_principal`` (401) whenever authentication is required, so this fails
    closed even if the authentication middleware is missing. With authentication switched off (local development,
    public demo) there is no identity and, as before, no enforcement.
    """
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Administrator credentials required.")
    return principal


@overload
def resolve_user_id(principal: Principal, requested: str, default: str | None = None) -> str: ...
@overload
def resolve_user_id(principal: Principal, requested: str | None, default: str) -> str: ...
@overload
def resolve_user_id(principal: Principal, requested: str | None, default: None = None) -> str | None: ...


def resolve_user_id(principal: Principal, requested: str | None, default: str | None = None) -> str | None:
    """Return the user id this request may act on.

    Scoped callers always act as themselves; naming anyone else is a 403. Operators and
    unauthenticated callers keep the requested id (or ``default`` when none was given).
    """
    if principal.scoped:
        own = principal.user_id
        if own is None:  # unreachable (scoped implies a user id); fail closed rather than fall through
            raise HTTPException(status_code=403, detail="Access to another user's data is not permitted.")
        if requested is not None and requested != own:
            raise HTTPException(status_code=403, detail="Access to another user's data is not permitted.")
        return own
    return requested if requested is not None else default


def scope_session_id(principal: Principal, session_id: str) -> str:
    """Namespace a session id per user so sessions cannot be read or overwritten across users.

    A fixed-length hash prefix keeps the mapping collision-free for any user_id/session_id pair.
    """
    if not principal.scoped or principal.user_id is None:
        return session_id
    prefix = hashlib.sha256(principal.user_id.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{session_id}"
