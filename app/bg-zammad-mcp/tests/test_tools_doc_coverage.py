"""`docs/tools.md` must cover the surface the PROFILE publishes, not just this
repository's half of it.

The generated reference opens with "Every tool the Zammad MCP server exposes",
and it derives its content from `server.register()` — which is only the first of
the two tool sources `src/profiles/zammad.json` declares. The second pulls
`bg.ping` and `bg.health` out of the bg-mcpcore registry, so `ping` and
`upstream_health` reach every client and appeared in no documentation. Two
counts then circulated for one surface, 75 and 77, with nothing saying that they
measured different things — which is the drift the generator was written to end
(its own docstring lists the four counts that preceded it).

Comparing against the rendered file rather than against a number keeps this
honest when a tool is added: a stale count fails here, and so does a tool that
never reached the page.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP

APP = Path(__file__).resolve().parents[1]
PROFILE = APP / "src" / "profiles" / "zammad.json"


def _find_tools_doc() -> Path | None:
    """Locate docs/tools.md by walking up, or None when it is not in the tree.

    Searched rather than computed from a fixed depth: the image's test stage
    copies tests/ to /app/tests, where the repository's `parents[1]` does not
    exist. Indexing it raised at import time — before the skip below could run,
    which is exactly the case that skip was written for.
    """
    for base in (APP, *APP.parents):
        candidate = base / "docs" / "tools.md"
        if candidate.is_file():
            return candidate
    return None


TOOLS_DOC = _find_tools_doc()


class _NullCtx:
    """Registration only, exactly as the doc generator does it."""

    settings = None
    client = None

    async def request(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise RuntimeError("registration must not perform requests")


async def _published_tool_names() -> set[str]:
    """Every tool name the profile puts on the wire, from both of its sources."""
    from bg_mcpcore.tools.registry import get_tool

    import server

    mcp: FastMCP = FastMCP("doc-coverage")
    ctx = _NullCtx()
    sources = json.loads(PROFILE.read_text(encoding="utf-8"))["tools"]

    for source in sources:
        if source["source"] == "python":
            server.register(mcp, ctx)
        elif source["source"] == "registry":
            for name in source["include"]:
                get_tool(name)(mcp, ctx)
        else:  # pragma: no cover - a new source kind should fail loudly
            raise AssertionError(f"unhandled profile tool source: {source['source']}")

    return {t.name for t in await mcp.list_tools(run_middleware=False)}


@pytest.fixture(scope="module")
def tools_doc() -> str:
    if TOOLS_DOC is None:
        # The image's test stage copies src/, tests/ and extensions/ but not
        # docs/, so this cannot run from inside the build. It still gates every
        # push: CI and any local run work from a full checkout.
        pytest.skip("docs/tools.md not present (running inside the image build)")
    return TOOLS_DOC.read_text(encoding="utf-8")


async def test_the_reference_documents_every_published_tool(tools_doc: str) -> None:
    names = await _published_tool_names()
    missing = sorted(name for name in names if f"`{name}`" not in tools_doc)
    assert not missing, (
        f"docs/tools.md does not mention {missing}. Regenerate it with "
        "`python scripts/generate-tools-doc.py`."
    )


async def test_the_headline_count_matches_the_published_surface(tools_doc: str) -> None:
    """The count is the first thing a reader trusts and the easiest to leave
    behind — it used to come from `server.register()`'s return value while the
    breakdown beside it was computed from the registered tools."""
    names = await _published_tool_names()
    assert f"**{len(names)} tools**" in tools_doc


async def test_the_registry_tools_are_part_of_the_surface() -> None:
    """Guards the premise of the two tests above: if the profile ever stops
    pulling them in, the reference is complete without them and these tests
    would keep passing while documenting a different server."""
    names = await _published_tool_names()
    assert {"ping", "upstream_health"} <= names


async def test_a_tool_that_declares_no_annotations_is_not_reported_as_a_write(
    tools_doc: str,
) -> None:
    """`_kind()` treated a missing annotation block as "additive write", which
    was safe while every documented tool came from this repository and carried
    one. The registry tools carry none, so the guess started publishing a claim
    they never made — and inflated the write count in the headline with it."""
    import re

    from bg_mcpcore.tools.registry import get_tool

    import server  # noqa: F401  (import parity with _published_tool_names)

    mcp: FastMCP = FastMCP("annotation-check")
    ctx = _NullCtx()
    for name in json.loads(PROFILE.read_text(encoding="utf-8"))["tools"][1]["include"]:
        get_tool(name)(mcp, ctx)
    unannotated = {
        t.name for t in await mcp.list_tools(run_middleware=False) if t.annotations is None
    }
    assert unannotated, "premise gone: the registry tools now declare annotations"

    for name in sorted(unannotated):
        row = re.search(rf"^\| `{re.escape(name)}` \| ([^|]*)\|", tools_doc, re.M)
        assert row, f"{name} has no row in docs/tools.md"
        assert row.group(1).strip() != "write", (
            f"docs/tools.md calls `{name}` a write; it declares no annotations at all"
        )
