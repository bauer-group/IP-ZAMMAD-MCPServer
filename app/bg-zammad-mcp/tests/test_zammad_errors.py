"""
Error mapping unit tests.
"""

from __future__ import annotations

import pytest

from zammad.errors import (
    ZammadAuthError,
    ZammadConflict,
    ZammadError,
    ZammadForbidden,
    ZammadNotFound,
    ZammadRateLimited,
    ZammadServerError,
    ZammadValidationError,
    from_status,
)


@pytest.mark.parametrize(
    "status,exc_cls",
    [
        (401, ZammadAuthError),
        (403, ZammadForbidden),
        (404, ZammadNotFound),
        (409, ZammadConflict),
        (400, ZammadValidationError),
        (422, ZammadValidationError),
        (429, ZammadRateLimited),
        (500, ZammadServerError),
        (502, ZammadServerError),
        (599, ZammadServerError),
        (418, ZammadError),  # I'm a teapot - falls back to base class
    ],
)
def test_status_maps_to_expected_class(status, exc_cls):
    exc = from_status(status, body={"error": "x"})
    assert isinstance(exc, exc_cls)
    assert exc.status_code == status


def test_uses_error_human_when_present():
    exc = from_status(404, body={"error": "tech", "error_human": "Ticket not found"})
    assert "Ticket not found" in str(exc)


def test_falls_back_to_error_when_no_human():
    exc = from_status(404, body={"error": "Ticket missing"})
    assert "Ticket missing" in str(exc)


def test_fallback_message_when_body_empty():
    exc = from_status(500, body={})
    assert "Zammad request failed" in str(exc)


def test_preserves_body_for_inspection():
    body = {"error": "x", "error_code": "E_TICKET_LOCKED", "ticket_id": 42}
    exc = from_status(409, body=body)
    assert exc.body == body
    assert exc.error_code == "E_TICKET_LOCKED"
