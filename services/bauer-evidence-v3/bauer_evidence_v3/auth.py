"""Signed, fail-closed authorization contexts for Bauer Evidence V3.

The compact token format is::

    base64url(canonical-header).base64url(canonical-claims).base64url(hmac)

Tokens are deliberately not general-purpose JWTs. Only the claims understood by
this module are accepted, and authorization never falls back to a public or
anonymous user when a claim is missing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping


TOKEN_TYPE = "BAUER-AUTH"
TOKEN_VERSION = 1
DEFAULT_MAX_TTL_SECONDS = 300
DEFAULT_MAX_AUTHORIZED_SOURCES = 1_000
MAX_CLOCK_SKEW_SECONDS = 60
MAX_IDENTIFIER_LENGTH = 200
MAX_KEY_ID_LENGTH = 128
MAX_TOKEN_BYTES = 256 * 1024
MIN_HMAC_KEY_BYTES = 32

_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_REQUIRED_CLAIMS = frozenset(
    {
        "tenant_id",
        "knowledge_base_id",
        "user_id",
        "agent_id",
        "audience",
        "issued_at",
        "expires_at",
        "authorized_source_ids",
    }
)
_OPTIONAL_CLAIMS = frozenset({"not_before"})
_HEADER_FIELDS = frozenset({"alg", "kid", "typ", "v"})


class AuthorizationContextError(ValueError):
    """Base class for rejected authorization contexts."""


class InvalidAuthorizationContext(AuthorizationContextError):
    """The token or its claims are malformed, untrusted, or out of policy."""


class ExpiredAuthorizationContext(AuthorizationContextError):
    """The authorization context is no longer valid."""


class AuthorizationContextNotYetValid(AuthorizationContextError):
    """The authorization context was issued for a future time."""


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    """Verified identity and source scope supplied to the evidence service."""

    tenant_id: str
    knowledge_base_id: str
    user_id: str
    agent_id: str
    audience: str
    issued_at: int
    expires_at: int
    authorized_source_ids: tuple[str, ...]
    not_before: int | None = None

    def to_claims(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible claim representation."""

        claims = asdict(self)
        claims["authorized_source_ids"] = list(self.authorized_source_ids)
        if self.not_before is None:
            claims.pop("not_before")
        return claims


def sign_authorization_context(
    claims: AuthorizationContext | Mapping[str, Any],
    *,
    key_id: str,
    keyring: Mapping[str, bytes | str],
    max_ttl_seconds: int = DEFAULT_MAX_TTL_SECONDS,
    max_authorized_sources: int = DEFAULT_MAX_AUTHORIZED_SOURCES,
) -> str:
    """Sign validated claims with the selected key from a rotation keyring."""

    normalized_key_id = _validate_identifier(
        key_id,
        field="key_id",
        maximum=MAX_KEY_ID_LENGTH,
    )
    secret = _resolve_key(keyring, normalized_key_id)
    context = _normalize_claims(
        claims.to_claims() if isinstance(claims, AuthorizationContext) else claims,
        max_ttl_seconds=max_ttl_seconds,
        max_authorized_sources=max_authorized_sources,
    )
    header = {
        "alg": "HS256",
        "kid": normalized_key_id,
        "typ": TOKEN_TYPE,
        "v": TOKEN_VERSION,
    }
    header_segment = _encode_canonical_json(header)
    claims_segment = _encode_canonical_json(context.to_claims())
    signing_input = f"{header_segment}.{claims_segment}".encode("ascii")
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    token = f"{header_segment}.{claims_segment}.{_base64url_encode(signature)}"
    if len(token.encode("ascii")) > MAX_TOKEN_BYTES:
        raise InvalidAuthorizationContext("Authorization context exceeds the token size limit")
    return token


def verify_authorization_context(
    token: str,
    *,
    keyring: Mapping[str, bytes | str],
    audience: str,
    now: int | None = None,
    max_ttl_seconds: int = DEFAULT_MAX_TTL_SECONDS,
    max_authorized_sources: int = DEFAULT_MAX_AUTHORIZED_SOURCES,
    clock_skew_seconds: int = 0,
) -> AuthorizationContext:
    """Verify a compact token and return a trusted authorization context.

    Signature comparison uses :func:`hmac.compare_digest`. The claims are not
    parsed until the signature has been checked.
    """

    if not isinstance(token, str) or not token:
        raise InvalidAuthorizationContext("Authorization context token is required")
    try:
        token_size = len(token.encode("ascii"))
    except UnicodeEncodeError as error:
        raise InvalidAuthorizationContext(
            "Authorization context token must contain ASCII characters only"
        ) from error
    if token_size > MAX_TOKEN_BYTES:
        raise InvalidAuthorizationContext("Authorization context exceeds the token size limit")

    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise InvalidAuthorizationContext("Authorization context must have three compact segments")
    header_segment, claims_segment, signature_segment = parts

    header = _decode_json_object(header_segment, label="header")
    if set(header) != _HEADER_FIELDS:
        raise InvalidAuthorizationContext("Authorization context header fields are invalid")
    if (
        header.get("alg") != "HS256"
        or header.get("typ") != TOKEN_TYPE
        or header.get("v") != TOKEN_VERSION
    ):
        raise InvalidAuthorizationContext("Authorization context header is unsupported")
    if _encode_canonical_json(header) != header_segment:
        raise InvalidAuthorizationContext("Authorization context header is not canonical")

    key_id = _validate_identifier(
        header.get("kid"),
        field="key_id",
        maximum=MAX_KEY_ID_LENGTH,
    )
    secret = _resolve_key(keyring, key_id)
    provided_signature = _base64url_decode(signature_segment, label="signature")
    signing_input = f"{header_segment}.{claims_segment}".encode("ascii")
    expected_signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise InvalidAuthorizationContext("Authorization context signature is invalid")

    decoded_claims = _decode_json_object(claims_segment, label="claims")
    if _encode_canonical_json(decoded_claims) != claims_segment:
        raise InvalidAuthorizationContext("Authorization context claims are not canonical")
    context = _normalize_claims(
        decoded_claims,
        max_ttl_seconds=max_ttl_seconds,
        max_authorized_sources=max_authorized_sources,
    )

    expected_audience = _validate_identifier(audience, field="audience")
    if not hmac.compare_digest(context.audience, expected_audience):
        raise InvalidAuthorizationContext("Authorization context audience does not match")

    checked_now = int(time.time()) if now is None else _validate_timestamp(now, field="now")
    skew = _validate_clock_skew(clock_skew_seconds)
    effective_not_before = (
        context.not_before if context.not_before is not None else context.issued_at
    )
    if checked_now + skew < effective_not_before:
        raise AuthorizationContextNotYetValid(
            "Authorization context is not valid yet"
        )
    if checked_now - skew >= context.expires_at:
        raise ExpiredAuthorizationContext("Authorization context has expired")
    return context


def _normalize_claims(
    claims: Mapping[str, Any],
    *,
    max_ttl_seconds: int,
    max_authorized_sources: int,
) -> AuthorizationContext:
    if not isinstance(claims, Mapping):
        raise InvalidAuthorizationContext("Authorization context claims must be an object")
    claim_names = set(claims)
    missing = sorted(_REQUIRED_CLAIMS - claim_names)
    if missing:
        raise InvalidAuthorizationContext(
            f"Authorization context is missing required claims: {', '.join(missing)}"
        )
    unknown = sorted(claim_names - _REQUIRED_CLAIMS - _OPTIONAL_CLAIMS)
    if unknown:
        raise InvalidAuthorizationContext(
            f"Authorization context contains unsupported claims: {', '.join(unknown)}"
        )

    ttl_limit = _validate_positive_limit(max_ttl_seconds, field="max_ttl_seconds")
    source_limit = _validate_positive_limit(
        max_authorized_sources,
        field="max_authorized_sources",
        maximum=DEFAULT_MAX_AUTHORIZED_SOURCES,
    )
    issued_at = _validate_timestamp(claims["issued_at"], field="issued_at")
    expires_at = _validate_timestamp(claims["expires_at"], field="expires_at")
    if expires_at <= issued_at:
        raise InvalidAuthorizationContext("expires_at must be later than issued_at")
    if expires_at - issued_at > ttl_limit:
        raise InvalidAuthorizationContext("Authorization context exceeds the maximum TTL")

    not_before_value = claims.get("not_before")
    not_before = (
        _validate_timestamp(not_before_value, field="not_before")
        if not_before_value is not None
        else None
    )
    if not_before is not None and not issued_at <= not_before < expires_at:
        raise InvalidAuthorizationContext(
            "not_before must be between issued_at and expires_at"
        )

    source_values = claims["authorized_source_ids"]
    if not isinstance(source_values, (list, tuple)):
        raise InvalidAuthorizationContext("authorized_source_ids must be an array")
    if len(source_values) > source_limit:
        raise InvalidAuthorizationContext(
            "Authorization context contains too many authorized sources"
        )
    normalized_sources = tuple(
        sorted(
            {
                _validate_identifier(value, field="authorized_source_ids item")
                for value in source_values
            }
        )
    )

    return AuthorizationContext(
        tenant_id=_validate_identifier(claims["tenant_id"], field="tenant_id"),
        knowledge_base_id=_validate_identifier(
            claims["knowledge_base_id"],
            field="knowledge_base_id",
        ),
        user_id=_validate_identifier(claims["user_id"], field="user_id"),
        agent_id=_validate_identifier(claims["agent_id"], field="agent_id"),
        audience=_validate_identifier(claims["audience"], field="audience"),
        issued_at=issued_at,
        expires_at=expires_at,
        authorized_source_ids=normalized_sources,
        not_before=not_before,
    )


def _resolve_key(
    keyring: Mapping[str, bytes | str],
    key_id: str,
) -> bytes:
    if not isinstance(keyring, Mapping):
        raise InvalidAuthorizationContext("Authorization keyring must be a mapping")
    if key_id not in keyring:
        raise InvalidAuthorizationContext("Authorization context key ID is not trusted")
    value = keyring[key_id]
    if isinstance(value, str):
        secret = value.encode("utf-8")
    elif isinstance(value, bytes):
        secret = value
    else:
        raise InvalidAuthorizationContext("Authorization signing key has an invalid type")
    if len(secret) < MIN_HMAC_KEY_BYTES:
        raise InvalidAuthorizationContext(
            f"Authorization signing keys must be at least {MIN_HMAC_KEY_BYTES} bytes"
        )
    return secret


def _validate_identifier(
    value: Any,
    *,
    field: str,
    maximum: int = MAX_IDENTIFIER_LENGTH,
) -> str:
    if not isinstance(value, str):
        raise InvalidAuthorizationContext(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise InvalidAuthorizationContext(f"{field} must not be empty")
    if len(normalized) > maximum:
        raise InvalidAuthorizationContext(f"{field} exceeds its length limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise InvalidAuthorizationContext(f"{field} contains control characters")
    return normalized


def _validate_timestamp(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidAuthorizationContext(f"{field} must be an integer Unix timestamp")
    if value < 0:
        raise InvalidAuthorizationContext(f"{field} must not be negative")
    return value


def _validate_positive_limit(
    value: Any,
    *,
    field: str,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidAuthorizationContext(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise InvalidAuthorizationContext(f"{field} exceeds its supported maximum")
    return value


def _validate_clock_skew(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_CLOCK_SKEW_SECONDS
    ):
        raise InvalidAuthorizationContext(
            f"clock_skew_seconds must be between 0 and {MAX_CLOCK_SKEW_SECONDS}"
        )
    return value


def _encode_canonical_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return _base64url_encode(payload)


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str, *, label: str) -> bytes:
    if not isinstance(value, str) or not value or not _BASE64URL_RE.fullmatch(value):
        raise InvalidAuthorizationContext(
            f"Authorization context {label} is not valid base64url"
        )
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise InvalidAuthorizationContext(
            f"Authorization context {label} is not valid base64url"
        ) from error


def _decode_json_object(value: str, *, label: str) -> dict[str, Any]:
    raw = _base64url_decode(value, label=label)

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise InvalidAuthorizationContext(
                    f"Authorization context {label} contains duplicate fields"
                )
            result[key] = item
        return result

    def reject_constant(value: str) -> None:
        raise InvalidAuthorizationContext(
            f"Authorization context {label} contains an invalid number: {value}"
        )

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except AuthorizationContextError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidAuthorizationContext(
            f"Authorization context {label} is not valid JSON"
        ) from error
    if not isinstance(decoded, dict):
        raise InvalidAuthorizationContext(
            f"Authorization context {label} must be a JSON object"
        )
    return decoded
