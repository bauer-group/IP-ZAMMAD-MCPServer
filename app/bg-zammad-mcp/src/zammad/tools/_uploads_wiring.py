"""Publishing a write tool with or without its ``attachments`` argument.

``ZAMMAD_ATTACHMENT_UPLOAD_ENABLED=false`` must not merely reject the argument
at call time: that publishes a capability the server does not have, so the
model reads the schema, sends a file and learns only from the error. The
argument is removed from the schema instead, and the tool body needs no branch
because FastMCP passes ``None`` through to the original function.

Why the decision is made BEFORE registration rather than by editing the
registry afterwards: every lookup on FastMCP's local provider
(``get_tool``, ``_get_tool``, ``get_tool_by_hash``) is a coroutine, and
``register()`` is synchronous. ``Tool.from_tool`` accepts a plain callable —
its signature says ``Tool | Callable[..., Any]`` — so the transformed tool can
be built straight from the function and handed to the synchronous ``add_tool``.

``exclude_args`` would also work and is one line shorter. It has been
deprecated since FastMCP 2.14 and warns on every boot, so it is not used here.
"""

from __future__ import annotations

from typing import Any

from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform

ATTACHMENTS_ARG = "attachments"


def uploads_enabled(ctx: Any) -> bool:
    """Whether this deployment permits attaching files. Defaults to yes."""
    return bool(
        getattr(getattr(ctx, "settings", None), "zammad_attachment_upload_enabled", True)
    )


def register_write_tool(mcp: Any, fn: Any, *, enabled: bool, **spec: Any) -> None:
    """Register ``fn``, dropping its ``attachments`` argument when disabled."""
    if enabled:
        mcp.tool(**spec)(fn)
        return
    mcp.add_tool(
        Tool.from_tool(
            fn,
            transform_args={ATTACHMENTS_ARG: ArgTransform(hide=True)},
            **spec,
        )
    )


__all__ = ["ATTACHMENTS_ARG", "register_write_tool", "uploads_enabled"]
