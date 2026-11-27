"""Outermost HTTP middleware: request id, latency and error metrics, and one structured log line per request.

Installed after ``install_security`` so it wraps it and sees every response, including 401 and 429. It never reads or
logs request or response bodies, headers, query strings or credentials. Routes are recorded by template (for example
``/profile/{user_id}``), never by the concrete path.
"""

import logging
import uuid
from time import perf_counter

from starlette.requests import Request

from app.core.logging_config import request_id_var
from app.core.metrics import metrics

logger = logging.getLogger("nexus.requests")
QUIET_PREFIXES = ("/health/",)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


def install_observability(app) -> None:
    @app.middleware("http")
    async def nexus_observability_middleware(request: Request, call_next):
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = perf_counter()
        quiet = request.url.path.startswith(QUIET_PREFIXES)
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                duration = round((perf_counter() - started) * 1000, 2)
                route = _route_template(request)
                metrics.record_request(request.method, route, 500, duration)
                logger.error("http_request_failed", extra={
                    "event": "http_request_failed", "method": request.method, "route": route,
                    "status": 500, "duration_ms": duration, "error_type": type(exc).__name__,
                })
                raise
            duration = round((perf_counter() - started) * 1000, 2)
            route = _route_template(request)
            metrics.record_request(request.method, route, response.status_code, duration)
            logger.log(logging.DEBUG if quiet else logging.INFO, "http_request", extra={
                "event": "http_request", "method": request.method, "route": route,
                "status": response.status_code, "duration_ms": duration,
            })
            return response
        finally:
            request_id_var.reset(token)
