"""End-to-end wiring tests: the assembled server, its routes, and the audit log.

These are the only tests that build the real server the way ``main.py`` does —
profile + settings + middleware — rather than exercising a module in isolation.
They exist because the expensive failures in this project have all been wiring
failures rather than logic failures: a scope that never reached Zammad, a claim
name that drifted between two files, a role gate reading a claim nobody emitted.

The OAuth discovery documents get their own test because Claude and every other
remote MCP client bootstrap from them. If a FastMCP upgrade moves one of those
routes, nothing in the tool layer notices and every client silently loses the
ability to authenticate.
"""

from __future__ import annotations

from typing import Any

import pytest
from bg_mcpcore import build_app_from_profile, load_profile
from fastmcp import Client, FastMCP

from audit import WriteAuditMiddleware
from config import Settings
from tests.test_tools_inventory import MODULES, RecordingCtx

PROFILE_PATH = "src/profiles/zammad.json"


def _profile() -> Any:
    from pathlib import Path

    return load_profile(str(Path(__file__).resolve().parent.parent / PROFILE_PATH))


# ── OAuth discovery surface ──────────────────────────────────────────────────


@pytest.fixture
async def zammad_app(base_zammad_env) -> Any:  # type: ignore[no-untyped-def]
    """The real server, assembled from the profile exactly as main.py does."""
    base_zammad_env.setenv("AUTH_STORAGE_ENCRYPTION_KEY", "A" * 43 + "=")
    return await build_app_from_profile(
        _profile(),
        Settings(),
        version="test",
        extra_middleware=[WriteAuditMiddleware()],
    )


async def test_profile_assembles_into_a_server(zammad_app) -> None:  # type: ignore[no-untyped-def]
    assert zammad_app is not None


@pytest.mark.parametrize(
    "path",
    [
        # RFC 9728 — what a 401 points clients at via WWW-Authenticate.
        "/.well-known/oauth-protected-resource/mcp",
        # RFC 8414 — where clients find the authorize/token/register endpoints.
        "/.well-known/oauth-authorization-server",
    ],
)
async def test_oauth_discovery_documents_are_served(zammad_app, path: str) -> None:  # type: ignore[no-untyped-def]
    """Claude bootstraps from these. A moved route is a silent total outage."""
    from starlette.testclient import TestClient

    with TestClient(zammad_app.http_app()) as client:
        response = client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}"
    body = response.json()
    assert body, f"{path} returned an empty document"


async def test_healthz_needs_no_auth(zammad_app) -> None:  # type: ignore[no-untyped-def]
    """The container liveness probe must not depend on Zammad or on a token."""
    from starlette.testclient import TestClient

    with TestClient(zammad_app.http_app()) as client:
        assert client.get("/healthz").status_code == 200


# ── audit middleware ─────────────────────────────────────────────────────────


@pytest.fixture
def audited_server() -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("audit-test")
    ctx = RecordingCtx()
    for module in MODULES.values():
        module.register(mcp, ctx)
    mcp.add_middleware(WriteAuditMiddleware())
    return mcp, ctx


async def _audit_events(mcp: FastMCP, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Call a tool and return the structlog events the audit middleware emitted.

    Not caplog: whether structlog events reach stdlib logging depends on whether
    bg-mcpcore's logging has been configured yet, which makes a caplog-based
    assertion pass or fail on test ordering. capture_logs intercepts at the
    structlog layer and is deterministic.
    """
    from structlog.testing import capture_logs

    with capture_logs() as captured:
        async with Client(mcp) as client:
            await client.call_tool(tool, args)
    return [e for e in captured if str(e.get("event", "")).startswith("audit.")]


async def test_write_is_audited(audited_server) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = audited_server
    events = await _audit_events(mcp, "add_internal_note", {"ticket_id": 4711, "body": "checked"})
    assert len(events) == 1
    assert events[0]["event"] == "audit.write"
    assert events[0]["tool"] == "add_internal_note"
    assert events[0]["ticket_id"] == 4711


async def test_reads_are_not_audited(audited_server) -> None:  # type: ignore[no-untyped-def]
    """Reads are the overwhelming majority of traffic; logging them would bury
    the writes that the trail exists for."""
    mcp, _ = audited_server
    assert await _audit_events(mcp, "get_ticket", {"ticket_id": 4711}) == []


async def test_audit_records_identifiers_but_not_content(audited_server) -> None:  # type: ignore[no-untyped-def]
    """An audit trail answers "who touched ticket 4711", not "what did they say".

    Note bodies, customer addresses and search queries must never reach the log
    aggregator through this path.
    """
    mcp, _ = audited_server
    secret = "customer said their password is hunter2"
    events = await _audit_events(mcp, "add_internal_note", {"ticket_id": 4711, "body": secret})

    assert events and events[0]["ticket_id"] == 4711
    rendered = repr(events)
    assert secret not in rendered
    assert "hunter2" not in rendered
