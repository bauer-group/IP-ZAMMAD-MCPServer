"""
ZammadClient tests.

Exercises:
  * Auth-header building (Bearer vs Token=) - the most common config error.
  * Retry policy for transient failures.
  * Error-mapping for the well-known 4xx / 5xx status codes.
  * Pass-through of per-call bearer tokens overriding the instance API token.
"""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from zammad.client import ZammadClient
from zammad.errors import (
    ZammadAuthError,
    ZammadConflict,
    ZammadForbidden,
    ZammadNotFound,
    ZammadServerError,
    ZammadValidationError,
)

BASE = "https://zammad.example.com/api/v1"


@pytest.fixture
async def static_client():
    """ZammadClient with a static API token configured."""
    client = ZammadClient(base_url=BASE, api_token="static-token")
    yield client
    await client.aclose()


@pytest.fixture
async def bearerless_client():
    """ZammadClient with NO configured token - relies on per-call bearer."""
    client = ZammadClient(base_url=BASE)
    yield client
    await client.aclose()


# ── Auth-header building ───────────────────────────────────────────────────


@respx.mock
async def test_static_token_uses_token_token_header(static_client):
    route = respx.get(f"{BASE}/tickets/1").respond(200, json={"id": 1})
    await static_client.get("/tickets/1")
    assert route.called
    sent = route.calls.last.request
    # Zammad-specific Personal Access Token format.
    assert sent.headers["authorization"] == "Token token=static-token"


@respx.mock
async def test_per_call_bearer_overrides_static_token(static_client):
    """Per-call bearer token wins over instance-level API token."""
    route = respx.get(f"{BASE}/tickets/1").respond(200, json={"id": 1})
    await static_client.get("/tickets/1", bearer_token="user-oauth-token")
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer user-oauth-token"


@respx.mock
async def test_no_token_raises(bearerless_client):
    """No bearer + no static -> ZammadAuthError before the wire."""
    # Don't register a respx route - the request shouldn't hit the wire.
    with pytest.raises(ZammadAuthError, match="No Zammad authentication"):
        await bearerless_client.get("/tickets/1")


# ── Error mapping ──────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.parametrize(
    "status,exc_cls",
    [
        (401, ZammadAuthError),
        (403, ZammadForbidden),
        (404, ZammadNotFound),
        (409, ZammadConflict),
        (400, ZammadValidationError),
        (422, ZammadValidationError),
        (500, ZammadServerError),
    ],
)
async def test_error_status_maps_to_typed_exception(static_client, status, exc_cls):
    respx.get(f"{BASE}/tickets/99").respond(
        status,
        json={"error": "synthetic", "error_human": "human-readable"},
    )
    with pytest.raises(exc_cls) as caught:
        # Use max_retries=0 to skip the retry loop on 5xx.
        client = ZammadClient(base_url=BASE, api_token="x", max_retries=0)
        try:
            await client.get("/tickets/99")
        finally:
            await client.aclose()
    assert caught.value.status_code == status
    # error_human is preferred for the message.
    assert "human-readable" in str(caught.value)


@respx.mock
async def test_2xx_returns_response(static_client):
    payload = {"id": 1, "title": "Hello"}
    respx.get(f"{BASE}/tickets/1").respond(200, json=payload)
    response = await static_client.get("/tickets/1")
    assert response.status_code == 200
    assert response.json() == payload


# ── Retry behaviour ────────────────────────────────────────────────────────


@respx.mock
async def test_retries_on_503_then_succeeds():
    """Transient 503 -> retry -> 200."""
    route = respx.get(f"{BASE}/tickets/1").mock(
        side_effect=[
            Response(503, json={"error": "temp"}),
            Response(200, json={"id": 1}),
        ]
    )
    # Small backoff to keep the test fast.
    client = ZammadClient(
        base_url=BASE, api_token="x", max_retries=3, backoff_base=0.0, backoff_max=0.0
    )
    try:
        response = await client.get("/tickets/1")
        assert response.status_code == 200
        assert route.call_count == 2
    finally:
        await client.aclose()


@respx.mock
async def test_does_not_retry_404():
    """404 is permanent - no retries."""
    route = respx.get(f"{BASE}/tickets/1").respond(404, json={"error": "not found"})
    client = ZammadClient(base_url=BASE, api_token="x", max_retries=3)
    try:
        with pytest.raises(ZammadNotFound):
            await client.get("/tickets/1")
    finally:
        await client.aclose()
    assert route.call_count == 1


@respx.mock
async def test_gives_up_after_max_retries():
    """All retries exhausted -> permanent error of last response."""
    route = respx.get(f"{BASE}/tickets/1").respond(503, json={"error": "down"})
    client = ZammadClient(
        base_url=BASE, api_token="x", max_retries=2, backoff_base=0.0, backoff_max=0.0
    )
    try:
        with pytest.raises(ZammadServerError):
            await client.get("/tickets/1")
    finally:
        await client.aclose()
    # 1 initial + 2 retries = 3 attempts.
    assert route.call_count == 3
