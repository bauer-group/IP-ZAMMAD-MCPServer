"""
Zammad 7 AI-assistance tools - and their graceful degradation.

Endpoints (all under /api/v1/), with the Zammad permission each needs:
  POST /tickets/{id}/summarize                        ticket.agent, read access to the group
  POST /tickets/{id}/knowledge_base_answers           ticket.agent + knowledge_base.*

Both are optional Zammad features that may simply not be switched on here.
Neither returns its result on the first call, and neither fails in a way an LLM
would read as failure - which is what this module exists to fix.

Asynchronous by design
----------------------
Zammad computes both answers in a background job. The first POST enqueues that
job and replies HTTP 200 with ``{"result": null}`` (summarize) or
``{"result": {"pending": true}}`` (related answers); a later POST returns the
stored result. Handed back verbatim, that first response reads as "this ticket
has no summary" / "nothing in the knowledge base is relevant" - a confident and
wrong conclusion. Both tools therefore poll a few times and, if the job is still
running, raise a ToolError that says exactly that.

Feature gates, and why 422 is not the whole story
-------------------------------------------------
Both endpoints call ``Service::CheckFeatureEnabled`` twice. The assistance
setting (``ai_assistance_ticket_summary``) is checked with
``custom_exception_class: Exceptions::UnprocessableContent``, which Rails maps
to HTTP 422. The AI-provider check is not: it raises the service's plain
``StandardError`` subclass, and ``ApplicationController::HandlesErrors`` maps
StandardError to HTTP 500 - with the message masked to "Please contact your
administrator" for anyone who is not an admin. So an instance with the feature
enabled but no provider configured answers 500, not 422. Both statuses are
handled here, because to the caller they mean one thing: this Zammad cannot do
it, retrying will not help, do the work yourself.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..errors import (
    ZammadError,
    ZammadForbidden,
    ZammadServerError,
    ZammadValidationError,
)
from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Poll budget for the background jobs. Zammad offers no completion callback and
# no ETA, so this is bounded by what an MCP client will sit through rather than
# by anything the API tells us: three waits of three seconds, ~9s worst case,
# then an honest "not ready yet" the model can act on.
POLL_ATTEMPTS = 4
POLL_INTERVAL_SECONDS = 3.0


def _ai_unavailable(exc: ZammadError, capability: str, fallback: str) -> ToolError:
    """Collapse both feature-gate failure modes into one actionable error.

    See the module docstring: the assistance setting fails with 422 and the
    missing AI provider with 500, and on a non-admin session the 500 body is
    masked. Neither is worth distinguishing to the caller - both mean "not
    available on this instance", and the only useful next step is the fallback.
    """
    return ToolError(
        f"This Zammad cannot {capability}: the AI assistance feature is "
        f"switched off or no AI provider is configured (HTTP {exc.status_code}: "
        f"{exc.message}). Retrying will not help - {fallback}"
    )


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    # Both tools enqueue a background job on their first call, so neither is
    # read-only; both are additive (they store an AI result, they overwrite
    # nothing) and idempotent (repeat calls return the same stored result).
    generates = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )

    @mcp.tool(
        name="summarize_ticket",
        description=(
            "Ask Zammad's own AI assistant to summarise a ticket - the "
            "customer's problem, what has been done so far, and what is still "
            "open. Needs 'ticket.agent' plus read access to the ticket's "
            "group. This is an OPTIONAL Zammad feature: when the ticket-summary "
            "assistance setting is off or no AI provider is configured, the "
            "tool fails with an explicit message, and the right response is to "
            "read the thread with `list_ticket_articles` and summarise it "
            "yourself. Zammad generates the summary in a background job, so "
            "this tool waits and polls for a few seconds; if it is still not "
            "ready the tool says so instead of returning an empty summary, and "
            "calling it again shortly afterwards usually returns the text."
        ),
        annotations=generates,
    )
    async def summarize_ticket(
        ticket_id: Annotated[int, Field(ge=1)],
    ) -> Any:
        path = f"/tickets/{ticket_id}/summarize"
        for attempt in range(POLL_ATTEMPTS):
            try:
                body = await ctx.request("POST", path)
            except (ZammadValidationError, ZammadServerError) as exc:
                raise _ai_unavailable(
                    exc,
                    "summarise tickets",
                    "read the ticket's articles and write the summary yourself.",
                ) from exc
            if isinstance(body, dict):
                # A failed generation is reported with HTTP 200 and error: true,
                # so nothing above this line has caught it.
                if body.get("error"):
                    raise ToolError(
                        "Zammad's summary generation failed: "
                        f"{body.get('error_message') or 'no reason given'}. "
                        "Read the ticket's articles and summarise them yourself."
                    )
                if body.get("result"):
                    return body
            if attempt + 1 < POLL_ATTEMPTS:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        raise ToolError(
            f"Zammad is still generating the summary for ticket {ticket_id} - "
            "it did not finish in time. This is NOT an empty summary. Call "
            "`summarize_ticket` again in a few seconds, or read the ticket's "
            "articles and summarise them yourself. Zammad answers the same way "
            "when summaries are disabled for this ticket's group, so if a "
            "second attempt also comes back unfinished, write it yourself."
        )

    @mcp.tool(
        name="suggest_kb_answers",
        description=(
            "Ask Zammad which knowledge base answers are relevant to a ticket, "
            "using its AI vector search. Use it to ground a reply in existing "
            "house material before writing anything new. Needs 'ticket.agent' "
            "plus a knowledge-base permission ('knowledge_base.reader' or "
            "'knowledge_base.editor'); without the latter Zammad answers HTTP "
            "403. It is an OPTIONAL feature needing both a configured AI "
            "provider and an enabled vector store - when either is missing the "
            "tool fails with an explicit message and you should fall back to "
            "`search_knowledge_base` with keywords taken from the ticket. The "
            "vector search runs off the ticket's AI summary, so the first call "
            "usually reports the work as pending while that summary is "
            "generated; this tool waits and polls for a few seconds. Hits are "
            "locale TRANSLATIONS: the excerpts map is keyed by translation id, "
            "and the assets block resolves each translation to the answer_id "
            "that `get_kb_answer` accepts."
        ),
        annotations=generates,
    )
    async def suggest_kb_answers(
        ticket_id: Annotated[int, Field(ge=1)],
    ) -> Any:
        path = f"/tickets/{ticket_id}/knowledge_base_answers"
        for attempt in range(POLL_ATTEMPTS):
            try:
                body = await ctx.request("POST", path)
            except ZammadForbidden as exc:
                # Zammad's 403 body here is a bare "Not authorized", which does
                # not hint that a knowledge-base permission is what is missing.
                raise ToolError(
                    "Not allowed to run the knowledge base vector search: this "
                    "account is missing a knowledge-base permission "
                    "('knowledge_base.reader' or 'knowledge_base.editor'). Use "
                    "`search_knowledge_base` instead - it is open to every "
                    "authenticated user."
                ) from exc
            except (ZammadValidationError, ZammadServerError) as exc:
                raise _ai_unavailable(
                    exc,
                    "suggest knowledge base answers",
                    "use `search_knowledge_base` with keywords from the ticket instead.",
                ) from exc
            result = body.get("result") if isinstance(body, dict) else None
            if isinstance(result, dict) and not result.get("pending"):
                return body
            if attempt + 1 < POLL_ATTEMPTS:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        raise ToolError(
            f"Zammad is still preparing the knowledge base suggestions for "
            f"ticket {ticket_id} - it did not finish in time. This is NOT an "
            "empty result: the vector search waits for the ticket's AI summary "
            "to be generated first. Call `suggest_kb_answers` again in a few "
            "seconds, or search the knowledge base directly with "
            "`search_knowledge_base`."
        )

    return 2
