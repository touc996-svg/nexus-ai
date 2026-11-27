"""Production entry point: ``python -m app.serve`` (the Docker image's CMD). Equivalent to the previous ``uvicorn app.main:app``
command line, with one fix for multi-worker deployments.

The problem (measured in docs/LOAD_TEST_REPORT.md): with ``--workers N`` uvicorn passes each worker a duplicate of the listening
socket whose ``proto`` is 0, and asyncio only sets TCP_NODELAY on accepted connections when ``sock.proto == IPPROTO_TCP``. Nagle's
algorithm then holds each response body until the client's delayed ACK, adding about 42 ms to every keep-alive request after the
first on Linux. Accepted sockets inherit TCP_NODELAY from the listening socket, so setting it once on the listener (before the
workers are spawned) restores the normal behaviour. Nothing else about serving changes.

Settings: ``UVICORN_WORKERS`` (default 1), ``PORT`` (default 8010), ``HOST`` (default 0.0.0.0). The application reads
``X-Forwarded-For`` itself (``NEXUS_TRUSTED_PROXY_HOPS``), so uvicorn's own proxy-header rewriting stays off; SIGTERM drains
in-flight requests for up to 25 s.
"""

import os
import socket

import uvicorn

_original_bind_socket = uvicorn.Config.bind_socket


def _bind_socket_with_nodelay(self):
    sock = _original_bind_socket(self)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def install_nodelay() -> None:
    uvicorn.Config.bind_socket = _bind_socket_with_nodelay  # type: ignore[method-assign]


def main() -> None:
    install_nodelay()
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),  # nosec B104  (the container's own interface; Compose decides what is published)
        port=int(os.getenv("PORT", "8010")),
        workers=int(os.getenv("UVICORN_WORKERS", "1")),
        proxy_headers=False,
        timeout_graceful_shutdown=25,
    )


if __name__ == "__main__":
    main()
