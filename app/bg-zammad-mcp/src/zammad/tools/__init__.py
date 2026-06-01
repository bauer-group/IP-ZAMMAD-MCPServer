"""MCP tool registration — the hand-written Zammad tool surface.

Each submodule registers a logically-coherent group of tools against a passed-in
FastMCP instance, sharing one context whose ``request(method, path, **kwargs)``
returns the decoded Zammad response body on success and raises a typed
``ZammadError`` on failure.

At runtime that context is supplied by ``server._DecodingCtx`` (which wraps
bg-mcpcore's ``ToolContext`` and the framework's outbound HTTP client + OBO
resolver). The ``ToolContext`` Protocol below exists only as the structural type
the submodules annotate against.

Why hand-written instead of OpenAPI-generated?
  Zammad doesn't ship a maintained machine-readable OpenAPI spec. Hand curation
  also lets us annotate destructive tools, default ``expand=true`` so the LLM
  gets human-friendly names instead of IDs, and phrase descriptions for intent
  mapping.
"""

from __future__ import annotations

from typing import Any, Protocol


class ToolContext(Protocol):
    """Structural type the tool modules call against.

    Implemented at runtime by ``server._DecodingCtx``. ``request`` returns the
    decoded body (dict/list/str) on a 2xx response and raises a typed
    ``zammad.errors.ZammadError`` on any non-2xx response.
    """

    settings: Any

    async def request(self, method: str, path: str, **kwargs: Any) -> Any: ...


__all__ = ["ToolContext"]
