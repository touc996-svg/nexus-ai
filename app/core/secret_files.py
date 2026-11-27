"""Docker/Kubernetes-style secret files: ``NAME_FILE=/run/secrets/name`` fills ``NAME`` from the file's contents.

Only the variables listed in ``SECRET_VARIABLES`` are eligible. A value already present in the environment wins, so
local development with plain variables is unchanged. The file is read once, at import of the ``app`` package (before any
settings are read), stripped of surrounding whitespace, and must be a regular, non-empty, reasonably small file. Errors
name the variable and the problem, never the content. Secrets therefore stay out of ``docker inspect`` output, process
environments in images, and the repository.
"""

import os
from pathlib import Path

SECRET_VARIABLES = (
    "DATABASE_URL",
    "NEXUS_API_KEY",
    "NEXUS_API_KEYS",
    "NEXUS_JWT_SECRETS",
    "QDRANT_API_KEY",
)
MAX_SECRET_BYTES = 8192


class SecretFileError(RuntimeError):
    """A ``*_FILE`` variable points at something unusable. The message never includes file contents."""


def load_secret_files(environ=None) -> list[str]:
    """Populate ``environ`` from ``*_FILE`` variables. Returns the names that were filled."""
    env = os.environ if environ is None else environ
    filled = []
    for name in SECRET_VARIABLES:
        pointer = (env.get(f"{name}_FILE") or "").strip()
        if not pointer:
            continue
        if (env.get(name) or "").strip():
            continue  # an explicit variable wins
        path = Path(pointer)
        try:
            if not path.is_file():
                raise SecretFileError(f"{name}_FILE does not point at a regular file")
            if path.stat().st_size > MAX_SECRET_BYTES:
                raise SecretFileError(f"{name}_FILE is larger than {MAX_SECRET_BYTES} bytes")
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SecretFileError(f"{name}_FILE cannot be read ({type(exc).__name__})") from None
        except UnicodeDecodeError:
            raise SecretFileError(f"{name}_FILE is not valid UTF-8 text") from None
        if not value:
            raise SecretFileError(f"{name}_FILE is empty")
        env[name] = value
        filled.append(name)
    return filled
