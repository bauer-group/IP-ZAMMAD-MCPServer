"""Write-path audit logging.

The MCP acts with the calling user's Zammad rights, which is the point — but it
also means Zammad's own history attributes every change to that user with no
record of whether a human or an agent made it. Zammad cannot distinguish the
two; this server can, so it logs it here.

What is recorded: who (the token subject), what tool, and the identifier of the
primary object it touched. What is deliberately NOT recorded: article bodies,
customer e-mail addresses, note text, search queries — an audit trail is for
answering "who changed ticket 4711 and when", not for building a second copy of
the helpdesk's contents in the log aggregator. Read-only calls are skipped
entirely: they are the overwhelming majority of traffic and logging them would
bury the writes.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

logger = structlog.stdlib.get_logger("bg-zammad-mcp.audit")

# Argument names that identify the object a write acted on. Anything not listed
# here is not logged, so adding a tool with a new identifier argument means
# adding it here deliberately rather than leaking whatever it happens to carry.
_IDENTIFIER_ARGS = (
    "ticket_id",
    "ticket_ids",
    "article_id",
    "user_id",
    "organization_id",
    "notification_id",
    "object_id",
    "object_type",
    "group_id",
    "macro_id",
    "checklist_id",
    "item_id",
    "customer",
    "group",
)


def _identifiers(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {}
    out: dict[str, Any] = {}
    for name in _IDENTIFIER_ARGS:
        if name not in arguments:
            continue
        value = arguments[name]
        if isinstance(value, list):
            # Bulk operations: record how many and which, not the payload.
            out[name] = value[:25]
            if len(value) > 25:
                out[f"{name}_truncated_from"] = len(value)
        elif isinstance(value, str | int | bool):
            out[name] = value
    return out


class WriteAuditMiddleware(Middleware):
    """Log every non-read-only tool call with the caller and the object touched."""

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        from fastmcp.server.dependencies import get_access_token

        message = context.message
        tool_name = getattr(message, "name", None) or "<unknown>"

        # Read-only tools are skipped. Resolving the annotation needs the tool
        # object; if that lookup fails for any reason, fall back to logging
        # (over-logging is the safe direction for an audit trail).
        if await _is_read_only(context, tool_name):
            return await call_next(context)

        token = get_access_token()
        claims = getattr(token, "claims", {}) or {} if token is not None else {}
        fields = {
            "tool": tool_name,
            "sub": claims.get("sub"),
            "login": claims.get("preferred_username"),
            **_identifiers(getattr(message, "arguments", None)),
        }

        try:
            result = await call_next(context)
        except Exception as exc:
            logger.warning("audit.write_failed", outcome="error",
                           error_type=type(exc).__name__, **fields)
            raise
        logger.info("audit.write", outcome="ok", **fields)
        return result


async def _is_read_only(context: MiddlewareContext[Any], tool_name: str) -> bool:
    server = getattr(context, "fastmcp_context", None)
    server = getattr(server, "fastmcp", None)
    if server is None:
        return False
    try:
        tool = await server.get_tool(tool_name)
    except Exception:
        return False
    annotations = getattr(tool, "annotations", None)
    return bool(getattr(annotations, "readOnlyHint", False))


__all__ = ["WriteAuditMiddleware"]
