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


# ── tool tags ────────────────────────────────────────────────────────────────


async def test_every_tool_carries_a_tag() -> None:
    """With 75 tools the published list is itself a slice of a client's context.

    Tags are how an operator or client talks about a subset ("the ticket half",
    "read-only only"), so an untagged tool is invisible to that filtering — and
    a new module is exactly the thing that would arrive untagged.
    """
    import server

    mcp: FastMCP = FastMCP("tag-test")
    server.register(mcp, RecordingCtx())
    untagged = [t.name for t in await mcp.list_tools(run_middleware=False) if not t.tags]
    assert not untagged, f"tools registered without a tag: {sorted(untagged)}"


async def test_tag_vocabulary_stays_closed() -> None:
    """A typo'd tag is worse than no tag: it silently creates a group of one."""
    import server

    allowed = {
        "tickets",
        "worklist",
        "communication",
        "bulk",
        "audit",
        "knowledge",
        "ai",
        "people",
        "reference",
        "reporting",
    }
    mcp: FastMCP = FastMCP("tag-vocab-test")
    server.register(mcp, RecordingCtx())
    used = {tag for t in await mcp.list_tools(run_middleware=False) for tag in t.tags}
    assert used <= allowed, f"unexpected tags: {sorted(used - allowed)}"


# ── extensions catalogue ─────────────────────────────────────────────────────


def test_shipped_extensions_catalogue_is_valid() -> None:
    """The catalogue is loaded at boot and the profile marks it optional.

    That combination is exactly how a typo ships unnoticed: a malformed
    catalogue means the server starts fine and simply has no prompts, with the
    failure buried in a log line nobody reads. Validate it here instead.
    """
    import json
    from pathlib import Path

    from bg_mcpcore.extensions.config import ExtensionsConfig

    path = Path(__file__).resolve().parent.parent / "extensions" / "extensions.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("$schema", None)
    catalogue = ExtensionsConfig.model_validate(data)

    assert catalogue.prompts, "the catalogue ships no prompts"
    assert catalogue.resources, "the catalogue ships no resources"


async def test_prompt_templates_only_reference_real_tools() -> None:
    """A prompt naming a tool that does not exist sends the model hunting.

    Prompts are where the house's way of working is written down, so they name
    tools constantly — and unlike tool descriptions nothing else checks them.
    """
    import json
    import re
    from pathlib import Path

    import server

    mcp: FastMCP = FastMCP("prompt-check")
    server.register(mcp, RecordingCtx())
    real = {t.name for t in await mcp.list_tools(run_middleware=False)}

    path = Path(__file__).resolve().parent.parent / "extensions" / "extensions.json"
    catalogue = json.loads(path.read_text(encoding="utf-8"))

    unknown: list[str] = []
    for prompt in catalogue.get("prompts", []):
        for token in re.findall(r"`([a-z][a-z0-9_]{3,})`", prompt.get("template", "")):
            # Prompt arguments are interpolated as ${name}; a bare backticked
            # word is either a tool or a parameter we mention on purpose.
            if token in real:
                continue
            if token in {a["name"] for a in prompt.get("arguments", [])}:
                continue
            if token in {"newest_first", "pending_time", "state_id", "ticket_id"}:
                continue
            unknown.append(f"{prompt['name']}: `{token}`")
    assert not unknown, "prompts reference unknown tools: " + ", ".join(unknown)
