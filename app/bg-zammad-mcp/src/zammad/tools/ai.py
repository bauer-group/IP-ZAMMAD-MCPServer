"""
Zammad 7 AI-assistance tools - and their graceful degradation.

Endpoints (all under /api/v1/), with the Zammad permission each needs:
  POST /tickets/{id}/summarize                        ticket.agent, read access to the group
  POST /tickets/{id}/knowledge_base_answers           ticket.agent + a KB editor role

Note what the second one is NOT. Its name reads like a lookup, but
``Ticket::KnowledgeBaseAnswersController#create`` DRAFTS A NEW knowledge-base
article from the ticket - it writes, it does not search. Zammad does have a
vector search over existing answers, but it lives in a controller with no route
(verified against a running 7.1.1: ``rails routes`` has no
``related_knowledge_base_answers`` entry), so it is unreachable over the REST
API. Finding existing material is therefore ``search_knowledge_base``'s job.

Both are optional Zammad features that may simply not be switched on here.
Neither returns its result on the first call, and neither fails in a way an LLM
would read as failure - which is what this module exists to fix.

Asynchronous by design
----------------------
Zammad does both jobs in the background. The first POST enqueues the job and
replies HTTP 200 with no result yet; a later POST returns the stored one. Handed
back verbatim, that first response reads as "this ticket has no summary" or "the
draft is empty" - a confident and wrong conclusion. Both tools therefore poll a
few times and, if the job is still running, raise a ToolError that says exactly
that.

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
        name="draft_kb_answer_from_ticket",
        description=(
            "Ask Zammad's AI to DRAFT A NEW knowledge base article out of a "
            "ticket, so a solution worked out once can be reused. This WRITES "
            "a draft into the knowledge base - it does not look anything up. "
            "To find material that already exists, use `search_knowledge_base` "
            "instead. Needs 'ticket.agent' plus editor rights on at least one "
            "knowledge-base category; without them Zammad answers HTTP 403 or "
            "reports that no editable category is available. The draft is "
            "produced by a background job, so this tool polls for a few "
            "seconds and tells you if it is still running rather than "
            "returning an empty result."
        ),
        annotations=generates,
    )
    async def draft_kb_answer_from_ticket(
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
                    "Not allowed to draft a knowledge base article from this "
                    "ticket: the account needs editor rights on at least one "
                    "knowledge-base category ('knowledge_base.editor'). To "
                    "read existing material instead, use "
                    "`search_knowledge_base`, which is open to every "
                    "authenticated user."
                ) from exc
            except (ZammadValidationError, ZammadServerError) as exc:
                raise _ai_unavailable(
                    exc,
                    "draft knowledge base articles from tickets",
                    "write the article yourself, or search existing material with "
                    "`search_knowledge_base`.",
                ) from exc
            result = body.get("result") if isinstance(body, dict) else None
            if isinstance(result, dict) and not result.get("pending"):
                return body
            if attempt + 1 < POLL_ATTEMPTS:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        raise ToolError(
            f"Zammad is still drafting the knowledge base article for ticket "
            f"{ticket_id} - it did not finish in time. This is NOT a failure "
            "and NOT an empty draft; the job is still running. Call "
            "`draft_kb_answer_from_ticket` again in a few seconds."
        )

    return 2
