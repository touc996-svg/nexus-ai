"""Production security middleware for the NEXUS API.

The rate limiter is process-local. Use an API gateway/shared store when scaling
across multiple workers or instances. The middleware deliberately never logs
request bodies, API keys, authorization headers, or user prompts.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request

from app.security.identity import OPERATOR, SYSTEM_USER, Principal
from app.security.jwt_auth import JwtSettings, JwtVerifier, load_jwt_settings

logger = logging.getLogger("nexus.security")
PUBLIC_PATHS = {"/", "/health/live", "/health/ready"}
DEMO_ACTIVITY_PATHS = {"/activity", "/activity/summary", "/dashboard/activity"}


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SecuritySettings:
    demo_mode: bool
    app_env: str
    api_key: str
    user_keys: tuple[tuple[str, str], ...]
    require_api_key: bool
    cors_origins: tuple[str, ...]
    rate_limit_per_minute: int
    max_request_bytes: int
    keystore_enabled: bool = False
    identity_rate_limit_per_minute: int = 120
    jwt: JwtSettings | None = None
    trusted_proxy_hops: int = 0


MIN_USER_KEY_LENGTH = 24


def parse_user_keys(raw: str, operator_key: str = "") -> tuple[tuple[str, str], ...]:
    """Parse ``key=user_id`` pairs separated by commas into (key, user_id) tuples.

    Rejects malformed entries, short keys, reused keys and a key equal to the operator key,
    because any of those would silently weaken isolation.
    """
    pairs: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        key, sep, user_id = entry.partition("=")
        key, user_id = key.strip(), user_id.strip()
        if not sep or not key or not user_id:
            raise ValueError("NEXUS_API_KEYS entries must look like key=user_id")
        if len(key) < MIN_USER_KEY_LENGTH:
            raise ValueError(f"NEXUS_API_KEYS keys must be at least {MIN_USER_KEY_LENGTH} characters")
        if len(user_id) > 128 or any(ord(c) < 33 for c in user_id):
            raise ValueError("NEXUS_API_KEYS user ids must be 1 to 128 printable characters without spaces")
        if user_id == SYSTEM_USER:
            raise ValueError("NEXUS_API_KEYS must not use the reserved user id 'system' (the shared pool)")
        if key in seen_keys:
            raise ValueError("NEXUS_API_KEYS contains a duplicated key")
        if operator_key and key == operator_key:
            raise ValueError("NEXUS_API_KEYS must not reuse NEXUS_API_KEY")
        seen_keys.add(key)
        pairs.append((key, user_id))
    return tuple(pairs)


def authenticate(supplied_key: str, settings: "SecuritySettings") -> Principal | None:
    """Match a supplied key to a principal. Compares against every configured key (no early exit)."""
    # Compare bytes: hmac.compare_digest raises TypeError on non-ASCII str, which would turn a
    # malformed credential into a 500 instead of a clean 401.
    supplied = supplied_key.encode("utf-8")
    match: Principal | None = None
    if settings.api_key and hmac.compare_digest(supplied, settings.api_key.encode("utf-8")):
        match = OPERATOR
    for key, user_id in settings.user_keys:
        if hmac.compare_digest(supplied, key.encode("utf-8")):
            match = Principal(user_id=user_id, is_admin=False)
    return match


def load_security_settings() -> SecuritySettings:
    demo_mode = _as_bool(os.getenv("NEXUS_DEMO_MODE"), False)
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    api_key = os.getenv("NEXUS_API_KEY", "").strip()
    default_required = app_env == "production" and not demo_mode
    require_api_key = _as_bool(
        os.getenv("NEXUS_REQUIRE_API_KEY"), default_required
    )
    user_keys = parse_user_keys(os.getenv("NEXUS_API_KEYS", ""), api_key)
    keystore_enabled = _as_bool(os.getenv("NEXUS_KEYSTORE_ENABLED"), False)
    jwt_settings = load_jwt_settings()  # None unless NEXUS_JWT_SECRETS is set; a partial configuration raises
    # Turning the key store or token verification on means the operator wants authentication: it must not be left
    # off by the development default. An environment key is still required (the break-glass credential), below.
    if (api_key or user_keys or keystore_enabled or jwt_settings is not None) and not demo_mode:
        require_api_key = True
    if require_api_key and not api_key and not user_keys and not demo_mode:
        raise RuntimeError(
            "NEXUS_API_KEY or NEXUS_API_KEYS is required when API-key authentication is enabled. "
            "Set a strong secret or explicitly disable NEXUS_REQUIRE_API_KEY "
            "for a trusted local development environment."
        )

    origins = tuple(
        origin.strip().rstrip("/")
        for origin in os.getenv("NEXUS_CORS_ORIGINS", "").split(",")
        if origin.strip()
    )
    rate_limit = int(os.getenv("NEXUS_RATE_LIMIT_PER_MINUTE", "120"))
    identity_rate_limit = int(os.getenv("NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE", str(rate_limit)))
    max_request_bytes = int(os.getenv("NEXUS_MAX_REQUEST_BYTES", str(25 * 1024 * 1024)))
    if rate_limit < 1:
        raise ValueError("NEXUS_RATE_LIMIT_PER_MINUTE must be at least 1")
    if identity_rate_limit < 1:
        raise ValueError("NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE must be at least 1")
    if max_request_bytes < 1024:
        raise ValueError("NEXUS_MAX_REQUEST_BYTES must be at least 1024")
    try:
        trusted_proxy_hops = int(os.getenv("NEXUS_TRUSTED_PROXY_HOPS", "0").strip() or "0")
    except ValueError as exc:
        raise ValueError("NEXUS_TRUSTED_PROXY_HOPS must be an integer") from exc
    if not 0 <= trusted_proxy_hops <= 10:
        raise ValueError("NEXUS_TRUSTED_PROXY_HOPS must be between 0 and 10")

    return SecuritySettings(
        demo_mode=demo_mode,
        app_env=app_env,
        api_key=api_key,
        user_keys=user_keys,
        require_api_key=require_api_key,
        cors_origins=origins,
        rate_limit_per_minute=rate_limit,
        max_request_bytes=max_request_bytes,
        keystore_enabled=keystore_enabled,
        identity_rate_limit_per_minute=identity_rate_limit,
        jwt=jwt_settings,
        trusted_proxy_hops=trusted_proxy_hops,
    )


class BodySizeLimitMiddleware:
    """Enforce the request size limit on the bytes actually received, not only on a declared Content-Length.

    A streamed (chunked) body has no Content-Length, so the header check alone lets it through and the framework would
    buffer all of it in memory. This wrapper counts the bytes as the application reads them. When the limit is crossed it
    answers 413 itself, tells the application the client went away (so it stops reading), and drops anything the
    application then tries to send. (Raising from ``receive`` is not enough: the framework turns that into a 400/422.)
    """

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = {"received": 0, "rejected": False, "started": False}

        async def counting_receive():
            if state["rejected"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                state["received"] += len(message.get("body", b""))
                if state["received"] > self.max_bytes:
                    state["rejected"] = True
                    if not state["started"]:
                        await JSONResponse(
                            status_code=413, content={"detail": "Request body exceeds the configured size limit."},
                        )(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            if state["rejected"]:
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        await self.app(scope, counting_receive, guarded_send)


def _parse_forwarded_entry(raw: str) -> str | None:
    """Return the normalised IP in one X-Forwarded-For entry (optionally with a port), or None if it is not an IP."""
    value = raw.strip()
    if not value or len(value) > 64:
        return None
    candidates = [value]
    if value.startswith("[") and "]" in value:  # [v6] or [v6]:port
        candidates.append(value[1:value.index("]")])
    elif value.count(":") == 1:  # v4:port
        candidates.append(value.split(":", 1)[0])
    for candidate in candidates:
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


def resolve_client_address(request: Request, trusted_hops: int) -> str:
    """The address the per-address rate limiter should key on.

    ``trusted_hops`` is how many reverse proxies, all under your control, sit in front of the application and each
    append the address they received the request from to ``X-Forwarded-For``. With 0 (the default) the header is never
    read and the socket peer is used. With N, the entry N places from the **right** is the client as seen by the first
    trusted proxy; entries further left are client-supplied and ignored, so a spoofed prefix cannot change the result.
    Any problem (header missing, fewer entries than hops, an unparsable entry on the trusted side) falls back to the
    socket peer rather than to a value an attacker chose.
    """
    peer = request.client.host if request.client else "unknown"
    if trusted_hops <= 0:
        return peer
    entries: list[str] = []
    for header in request.headers.getlist("x-forwarded-for"):
        entries.extend(part for part in header.split(","))
    if len(entries) < trusted_hops:
        return peer
    selected = [_parse_forwarded_entry(entry) for entry in entries[-trusted_hops:]]
    client = selected[0]
    if client is None or any(item is None for item in selected):
        return peer
    return client


class SlidingWindowRateLimiter:
    """Simple per-process sliding-window limiter keyed by client address."""

    def __init__(self, limit: int, window_seconds: int = 60, max_keys: int = 10_000):
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max(100, max_keys)
        self._events: dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, int]:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        with self._lock:
            # Remove idle client keys periodically at capacity to bound memory use.
            if len(self._events) >= self.max_keys and key not in self._events:
                stale = [
                    client for client, events in self._events.items()
                    if not events or events[-1] <= cutoff
                ]
                for client in stale:
                    self._events.pop(client, None)
                if len(self._events) >= self.max_keys:
                    oldest = min(
                        self._events,
                        key=lambda client: self._events[client][-1] if self._events[client] else float("-inf"),
                    )
                    self._events.pop(oldest, None)

            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (current - events[0])))
                return False, retry_after, 0
            events.append(current)
            return True, 0, max(0, self.limit - len(events))


def _identity_bucket(principal: Principal) -> str:
    """One budget per user; every administrator credential shares one (the operator is not a tenant)."""
    return f"user:{principal.user_id}" if principal.scoped else "admin"


def install_security(app, key_store=None, jwt_verifier=None) -> None:
    """Install authentication, request limits, response headers and CORS.

    ``key_store`` is a persistent key store (app/security/keystore.py). One is created when
    ``NEXUS_KEYSTORE_ENABLED`` is true; an explicitly supplied store is always honoured. It is consulted only after
    the environment keys, and only for ``nxk_`` keys. ``jwt_verifier`` (app/security/jwt_auth.py) is built from
    ``NEXUS_JWT_*`` when those are set; it is consulted last, and only for ``Authorization: Bearer`` tokens.
    """
    settings = load_security_settings()
    limiter = SlidingWindowRateLimiter(settings.rate_limit_per_minute)
    identity_limiter = SlidingWindowRateLimiter(settings.identity_rate_limit_per_minute)
    app.state.security_settings = settings

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_request: Request, exc: RequestValidationError):
        # The framework default echoes the rejected input back. That reflects attacker text and PII into responses and
        # fails to serialise values such as NaN or very deeply nested JSON (a 500). Report only where and why.
        errors = []
        for error in exc.errors()[:20]:
            item = {"loc": [str(part) for part in error.get("loc", ())][:6], "msg": str(error.get("msg", ""))[:200],
                    "type": str(error.get("type", ""))[:60]}
            context = error.get("ctx")  # the violated constraint (for example max_length), never the input itself
            if isinstance(context, dict):
                safe = {str(k): v for k, v in context.items()
                        if isinstance(v, (int, str, bool)) and not isinstance(v, float) or v is None}
                if safe:
                    item["ctx"] = {k: (v[:200] if isinstance(v, str) else v) for k, v in safe.items()}
            errors.append(item)
        return JSONResponse(status_code=422, content={"detail": errors})
    if key_store is None and settings.keystore_enabled:
        # imported on demand: constructing the store opens the application database
        from app.security.keystore import KeyStore

        key_store = KeyStore()
    if jwt_verifier is None and settings.jwt is not None:
        jwt_verifier = JwtVerifier(settings.jwt)

    @app.middleware("http")
    async def nexus_security_middleware(request: Request, call_next):
        started = time.perf_counter()
        request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
        path = request.url.path
        client_host = resolve_client_address(request, settings.trusted_proxy_hops)

        def secure_headers(response):
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            if request.url.scheme == "https":
                response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            return response

        if settings.demo_mode and path in DEMO_ACTIVITY_PATHS:
            return secure_headers(JSONResponse(
                status_code=403,
                content={"detail": "Activity history is not exposed in the public demo."},
            ))

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                request_size = int(content_length)
            except ValueError:
                return secure_headers(JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header."},
                ))
            if request_size > settings.max_request_bytes:
                return secure_headers(JSONResponse(
                    status_code=413,
                    content={"detail": "Request body exceeds the configured size limit."},
                ))

        if path not in PUBLIC_PATHS:
            allowed, retry_after, remaining = limiter.check(client_host)
            if not allowed:
                response = JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Retry shortly."},
                    headers={"Retry-After": str(retry_after)},
                )
                response.headers["X-RateLimit-Limit"] = str(settings.rate_limit_per_minute)
                response.headers["X-RateLimit-Remaining"] = "0"
                return secure_headers(response)
        else:
            remaining = settings.rate_limit_per_minute

        if settings.require_api_key and not settings.demo_mode and path not in PUBLIC_PATHS:
            authorization = request.headers.get("authorization", "")
            bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
            supplied_key = bearer or request.headers.get("x-api-key", "").strip()
            principal = authenticate(supplied_key, settings) if supplied_key else None
            if principal is None and supplied_key and key_store is not None:
                try:
                    principal = await run_in_threadpool(key_store.authenticate, supplied_key)
                except Exception as exc:  # noqa: BLE001 - fail closed: no store failure may authenticate a request
                    logger.error("keystore_unavailable request_id=%s error_type=%s", request_id, type(exc).__name__)
                    return secure_headers(JSONResponse(
                        status_code=503,
                        content={"detail": "Authentication is temporarily unavailable."},
                        headers={"Retry-After": "5"},
                    ))
            if principal is None and jwt_verifier is not None and bearer:
                principal = jwt_verifier.verify(bearer)
            if principal is None:
                return secure_headers(JSONResponse(
                    status_code=401,
                    content={"detail": "Valid API key required."},
                    headers={"WWW-Authenticate": "Bearer"},
                ))
            request.state.principal = principal
            # The per-address limiter above runs before authentication, to protect it. This one is per credential
            # owner: a key used from many addresses shares one budget. It does not separate users who sit behind one
            # proxy (they still share the per-address budget); that needs trusted-proxy handling, not implemented.
            identity_allowed, identity_retry_after, _ = identity_limiter.check(_identity_bucket(principal))
            if not identity_allowed:
                return secure_headers(JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded for this credential. Retry shortly."},
                    headers={"Retry-After": str(identity_retry_after)},
                ))

        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_failed request_id=%s", request_id)
            raise

        response.headers["X-RateLimit-Limit"] = str(settings.rate_limit_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response = secure_headers(response)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "request_completed request_id=%s method=%s status=%s duration_ms=%s",
            request_id, request.method, response.status_code, duration_ms,
        )
        return response

    # CORS is only enabled for explicitly trusted origins. Same-origin dashboard
    # traffic does not require a CORS entry.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
        max_age=600,
    )
    # Host-header allow-list (NEXUS_ALLOWED_HOSTS, comma separated). Unset keeps the previous behaviour (any host);
    # production start-up requires it (app/core/production_check.py). A request for another host gets 400.
    allowed_hosts = [h.strip() for h in os.getenv("NEXUS_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    # Added after CORS so it wraps everything installed above it; installed outermost relative to the security middleware.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
