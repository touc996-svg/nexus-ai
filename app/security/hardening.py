"""Production security middleware for the NEXUS API.

The rate limiter is process-local. Use an API gateway/shared store when scaling
across multiple workers or instances. The middleware deliberately never logs
request bodies, API keys, authorization headers, or user prompts.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque

from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request

from app.security.identity import OPERATOR, SYSTEM_USER, Principal

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
    if (api_key or user_keys) and not demo_mode:
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
    max_request_bytes = int(os.getenv("NEXUS_MAX_REQUEST_BYTES", str(25 * 1024 * 1024)))
    if rate_limit < 1:
        raise ValueError("NEXUS_RATE_LIMIT_PER_MINUTE must be at least 1")
    if max_request_bytes < 1024:
        raise ValueError("NEXUS_MAX_REQUEST_BYTES must be at least 1024")

    return SecuritySettings(
        demo_mode=demo_mode,
        app_env=app_env,
        api_key=api_key,
        user_keys=user_keys,
        require_api_key=require_api_key,
        cors_origins=origins,
        rate_limit_per_minute=rate_limit,
        max_request_bytes=max_request_bytes,
    )


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


def install_security(app) -> None:
    """Install authentication, request limits, response headers and CORS."""
    settings = load_security_settings()
    limiter = SlidingWindowRateLimiter(settings.rate_limit_per_minute)
    app.state.security_settings = settings

    @app.middleware("http")
    async def nexus_security_middleware(request: Request, call_next):
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        path = request.url.path
        client_host = request.client.host if request.client else "unknown"

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
            if principal is None:
                return secure_headers(JSONResponse(
                    status_code=401,
                    content={"detail": "Valid API key required."},
                    headers={"WWW-Authenticate": "Bearer"},
                ))
            request.state.principal = principal

        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_failed request_id=%s path=%s", request_id, path)
            raise

        response.headers["X-RateLimit-Limit"] = str(settings.rate_limit_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response = secure_headers(response)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "request_completed request_id=%s method=%s path=%s status=%s duration_ms=%s",
            request_id, request.method, path, response.status_code, duration_ms,
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
