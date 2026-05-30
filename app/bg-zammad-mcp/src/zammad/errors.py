"""
Zammad errors - typed exceptions mapped from Zammad's JSON error bodies.

Zammad returns errors as JSON objects with `error`, `error_human`, and
`error_code` keys (the exact shape varies slightly between v6 and v7 and
between endpoints). We normalise them into a typed hierarchy so tools can
`except ZammadNotFound` without inspecting status codes, and the MCP
serializer turns each subclass into a deterministic error response.
"""

from __future__ import annotations

from typing import Any


class ZammadError(Exception):
    """Base class for every Zammad-originating failure."""

    status_code: int | None = None
    error_code: str | None = None

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if error_code is not None:
            self.error_code = error_code
        self.body: dict[str, Any] = body or {}

    def __str__(self) -> str:
        bits = [self.message]
        if self.status_code:
            bits.append(f"(HTTP {self.status_code})")
        if self.error_code:
            bits.append(f"[{self.error_code}]")
        return " ".join(bits)


class ZammadAuthError(ZammadError):
    """401 - the token was rejected (invalid, expired, or wrong format)."""


class ZammadForbidden(ZammadError):
    """403 - the authenticated user lacks the required Zammad permission."""


class ZammadNotFound(ZammadError):
    """404 - the resource (ticket, user, organization, ...) does not exist."""


class ZammadValidationError(ZammadError):
    """400/422 - the request payload failed Zammad's validation."""


class ZammadConflict(ZammadError):
    """409 - duplicate login, locked ticket, etc."""


class ZammadRateLimited(ZammadError):
    """429 - upstream Zammad rate-limit hit (rare on self-hosted)."""


class ZammadServerError(ZammadError):
    """5xx - upstream Zammad is unhealthy or returned an unexpected error."""


class ZammadTransportError(ZammadError):
    """Network failure - connection refused, DNS, timeout, TLS."""


def from_status(
    status: int,
    *,
    body: dict[str, Any] | None = None,
    message: str | None = None,
) -> ZammadError:
    """Pick the most specific exception class for an HTTP status."""
    body = body or {}
    # Zammad uses `error_human` for the user-facing message and `error` for
    # the more technical variant - prefer the friendlier one for end users.
    detail = (
        body.get("error_human")
        or body.get("error")
        or body.get("message")
        or message
        or f"Zammad request failed with HTTP {status}"
    )
    error_code = body.get("error_code")

    if status == 401:
        return ZammadAuthError(detail, status_code=status, error_code=error_code, body=body)
    if status == 403:
        return ZammadForbidden(detail, status_code=status, error_code=error_code, body=body)
    if status == 404:
        return ZammadNotFound(detail, status_code=status, error_code=error_code, body=body)
    if status == 409:
        return ZammadConflict(detail, status_code=status, error_code=error_code, body=body)
    if status in (400, 422):
        return ZammadValidationError(detail, status_code=status, error_code=error_code, body=body)
    if status == 429:
        return ZammadRateLimited(detail, status_code=status, error_code=error_code, body=body)
    if 500 <= status < 600:
        return ZammadServerError(detail, status_code=status, error_code=error_code, body=body)
    return ZammadError(detail, status_code=status, error_code=error_code, body=body)
