"""Bearer-token (JWT) verification, HS256 only. This is not OIDC.

It verifies tokens signed with a shared secret by an issuer you trust, for example an API gateway or an internal
token service. Tenant identity comes from the verified ``sub`` claim; nothing in a token can make the caller an
administrator (that stays with the operator key and stored admin keys).

Not implemented, because it needs ``cryptography`` and a network path to an identity provider: asymmetric
signatures (RS256, ES256), JWKS and OIDC discovery, token introspection and revocation. There is no ``jti``
replay tracking either: a token is valid until it expires, so keep lifetimes short. The shared secret is a master
credential for every non-administrator identity: anyone holding it can mint a token for any user.

A token is accepted only if all of these hold; anything else is refused without saying why:
  * three base64url segments, at most 4096 characters, ASCII only;
  * a JSON object header whose ``alg`` is exactly ``HS256`` (so ``none`` and every other algorithm are refused),
    with none of the headers that point at a key (``jku``, ``jwk``, ``x5u``, ``x5c``, ``x5t``) or are
    ``crit``ical;
  * an HMAC-SHA256 signature that verifies, in constant time, under a configured secret (up to two, so the secret
    can be rotated with an overlap);
  * a JSON object payload, with no duplicate keys and no NaN or infinity;
  * ``iss`` equal to the configured issuer and ``aud`` equal to (or a list containing) the configured audience;
  * a numeric ``exp`` (required) that is not past and not further away than the configured maximum lifetime, and
    ``nbf`` and ``iat``, when present, that are not in the future (with a small clock-skew leeway);
  * a ``sub`` that is a valid user id (the same rule as ``NEXUS_API_KEYS`` and stored keys).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import time
from dataclasses import dataclass

from app.security.identity import Principal, is_valid_user_id

MAX_TOKEN_LENGTH = 4096
MIN_SECRET_LENGTH = 32
MAX_SECRETS = 2
DEFAULT_LEEWAY_SECONDS = 30
MAX_LEEWAY_SECONDS = 300
DEFAULT_MAX_LIFETIME_SECONDS = 86_400
MIN_MAX_LIFETIME_SECONDS = 60
MAX_MAX_LIFETIME_SECONDS = 7 * 86_400
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_HEADERS_THAT_POINT_AT_A_KEY = frozenset({"jku", "jwk", "x5u", "x5c", "x5t", "x5t#S256", "crit"})


@dataclass(frozen=True)
class JwtSettings:
    secrets: tuple[bytes, ...]
    issuer: str
    audience: str
    leeway_seconds: int = DEFAULT_LEEWAY_SECONDS
    max_lifetime_seconds: int = DEFAULT_MAX_LIFETIME_SECONDS

    def __repr__(self) -> str:  # never let a signing secret reach a log line through repr()
        return (
            f"JwtSettings(issuer={self.issuer!r}, audience={self.audience!r}, secrets=<{len(self.secrets)} hidden>, "
            f"leeway_seconds={self.leeway_seconds}, max_lifetime_seconds={self.max_lifetime_seconds})"
        )


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number") from exc
    if not low <= value <= high:
        raise ValueError(f"{name} must be from {low} to {high}")
    return value


def load_jwt_settings() -> JwtSettings | None:
    """Read the JWT configuration. None when ``NEXUS_JWT_SECRETS`` is unset; a partial or invalid one raises."""
    raw = os.getenv("NEXUS_JWT_SECRETS", "").strip()
    if not raw:
        return None
    secrets_ = tuple(s.strip() for s in raw.split(",") if s.strip())
    if not 1 <= len(secrets_) <= MAX_SECRETS:
        raise ValueError(f"NEXUS_JWT_SECRETS takes one secret, or two while rotating (at most {MAX_SECRETS})")
    if any(len(s.encode("utf-8")) < MIN_SECRET_LENGTH for s in secrets_):
        raise ValueError(f"NEXUS_JWT_SECRETS secrets must be at least {MIN_SECRET_LENGTH} bytes")
    if len(set(secrets_)) != len(secrets_):
        raise ValueError("NEXUS_JWT_SECRETS contains a duplicated secret")
    issuer = os.getenv("NEXUS_JWT_ISSUER", "").strip()
    audience = os.getenv("NEXUS_JWT_AUDIENCE", "").strip()
    if not issuer or not audience:
        raise RuntimeError("NEXUS_JWT_ISSUER and NEXUS_JWT_AUDIENCE are required when NEXUS_JWT_SECRETS is set")
    return JwtSettings(
        secrets=tuple(s.encode("utf-8") for s in secrets_),
        issuer=issuer,
        audience=audience,
        leeway_seconds=_env_int("NEXUS_JWT_LEEWAY_SECONDS", DEFAULT_LEEWAY_SECONDS, 0, MAX_LEEWAY_SECONDS),
        max_lifetime_seconds=_env_int("NEXUS_JWT_MAX_LIFETIME_SECONDS", DEFAULT_MAX_LIFETIME_SECONDS,
                                      MIN_MAX_LIFETIME_SECONDS, MAX_MAX_LIFETIME_SECONDS),
    )


def _b64url_decode(segment: str) -> bytes | None:
    if not _SEGMENT.fullmatch(segment):
        return None
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError):
        return None


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _refuse_constant(name):
    raise ValueError("non-finite number")


def _load_object(raw: bytes) -> dict | None:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        return None
    return number if math.isfinite(number) else None


class JwtVerifier:
    def __init__(self, settings: JwtSettings, clock=time.time):
        self.settings = settings
        self.clock = clock

    def verify(self, token) -> Principal | None:
        """The scoped principal for a valid token, or None. It never raises and never says why it refused."""
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH or not token.isascii():
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_raw, payload_raw, signature = (_b64url_decode(part) for part in parts)
        if header_raw is None or payload_raw is None or signature is None:
            return None

        header = _load_object(header_raw)
        if header is None or header.get("alg") != "HS256" or _HEADERS_THAT_POINT_AT_A_KEY & header.keys():
            return None
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        if not any(
            hmac.compare_digest(hmac.new(secret, signing_input, hashlib.sha256).digest(), signature)
            for secret in self.settings.secrets
        ):
            return None

        claims = _load_object(payload_raw)
        if claims is None or not self._claims_are_acceptable(claims):
            return None
        return Principal(user_id=claims["sub"], is_admin=False)

    def _claims_are_acceptable(self, claims: dict) -> bool:
        now, leeway = self.clock(), self.settings.leeway_seconds
        audience = claims.get("aud")
        audience_ok = (
            audience == self.settings.audience if isinstance(audience, str)
            else isinstance(audience, list) and all(isinstance(a, str) for a in audience)
            and self.settings.audience in audience
        )
        if claims.get("iss") != self.settings.issuer or not audience_ok:
            return False
        if not is_valid_user_id(claims.get("sub")):
            return False
        expires = _number(claims.get("exp"))
        if expires is None or expires + leeway <= now or expires > now + self.settings.max_lifetime_seconds:
            return False
        for name in ("nbf", "iat"):
            if name in claims:
                moment = _number(claims[name])
                if moment is None or moment - leeway > now:
                    return False
        return True
