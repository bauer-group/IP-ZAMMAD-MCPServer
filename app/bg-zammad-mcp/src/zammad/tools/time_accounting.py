"""
Time-accounting tools - the time booked against a ticket.

Endpoints (all under /api/v1/):
  GET  /tickets/{id}/time_accountings   list a ticket's entries   `ticket.agent` + update access,
                                                                  or `admin.time_accounting`
  POST /tickets/{id}/time_accountings   book time on a ticket     `ticket.agent` + update access,
                                                                  or `admin.time_accounting`

Editing and deleting an existing entry are deliberately NOT exposed: Zammad
gates both behind `admin.time_accounting`, so an agent token can only ever add
to the log. That matches how time accounting is meant to work - the log is an
audit trail, corrections are an administrator's job.

The unscoped /time_accountings routes and the /time_accounting/log/* reports are
admin-only too and answer for the whole instance, which is not something this
server should hand to a model on a per-ticket task.

The feature can be off
----------------------
Time accounting is a Zammad feature an administrator switches on. While it is
off, ``Ticket::TimeAccountingPolicy#create?`` refuses with HTTP 403 and the
message "Time Accounting is not enabled" - the SAME status a missing ticket ACL
produces. ``add_ticket_time_entry`` therefore catches the 403 and re-raises it
naming both causes, so the model stops retrying and says something useful.
Listing is not gated that way: with the feature off the log is simply empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ..errors import ZammadForbidden
from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# TimeAccountingsController#index paginates with paginate_with(default: 500);
# CanPaginate::Pagination caps an unset max at 1000.
INDEX_MAX_PER_PAGE = 1000


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    @mcp.tool(
        name="list_ticket_time_entries",
        description=(
            "List the time booked against one ticket: every entry with its "
            "time_unit, the article it was booked on, the activity type id and "
            "who booked it. Use it to answer 'how much time went into this "
            "ticket'. The numbers are bare time UNITS - the instance decides "
            "whether a unit means minutes, hours or days, so report them as "
            "units unless you know the setting. Needs `ticket.agent` plus "
            "update access to the ticket (or `admin.time_accounting`). An "
            "empty list is normal: it also means nobody booked time, or the "
            "Time Accounting feature is switched off. Paginated - pass `page` "
            "to go beyond the first `per_page` entries."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, openWorldHint=True
        ),
    )
    async def list_ticket_time_entries(
        ticket_id: Annotated[int, Field(ge=1)],
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int,
            Field(ge=1, le=INDEX_MAX_PER_PAGE, description="Entries per page (max 1000)"),
        ] = 50,
    ) -> Any:
        return await ctx.request(
            "GET",
            f"/tickets/{ticket_id}/time_accountings",
            params={"page": page, "per_page": per_page},
        )

    @mcp.tool(
        name="add_ticket_time_entry",
        description=(
            "Book time on a ticket. `time_unit` is a bare number in whatever "
            "unit this Zammad is configured for (minutes by default, possibly "
            "hours or days) - do not convert, pass what the user said and name "
            "the unit back to them. Optionally attach the booking to one "
            "article with `article_id` (the reply the work went into) "
            "and categorise it with `type_id` if the instance uses activity "
            "types; those ids come from an admin-only endpoint, so leave "
            "`type_id` out unless you were given one. Needs `ticket.agent` "
            "plus UPDATE access to the ticket (or `admin.time_accounting`). "
            "Entries add up rather than replace, and an agent token cannot "
            "correct or delete one afterwards - that is an administrator's "
            "job, so get the number right. An article can carry at most one "
            "booking: a second one for the same `article_id` is "
            "rejected with HTTP 422."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,  # additive: appends a log entry
            idempotentHint=False,  # calling twice books the time twice
            openWorldHint=True,
        ),
    )
    async def add_ticket_time_entry(
        ticket_id: Annotated[int, Field(ge=1)],
        time_unit: Annotated[
            float,
            Field(
                gt=0,
                description=(
                    "Amount of time to book, in the instance's configured unit "
                    "(commonly minutes). Must be positive."
                ),
            ),
        ],
        article_id: Annotated[
            int | None,
            Field(
                ge=1,
                description=(
                    "Article this time belongs to. Must be an article OF this "
                    "ticket, and it must not already have a booking."
                ),
            ),
        ] = None,
        type_id: Annotated[
            int | None,
            Field(ge=1, description="Activity type id, if the instance uses them"),
        ] = None,
    ) -> Any:
        payload: dict[str, Any] = {"time_unit": time_unit}
        if article_id is not None:
            payload["article_id"] = article_id
        if type_id is not None:
            payload["type_id"] = type_id
        try:
            return await ctx.request(
                "POST",
                f"/tickets/{ticket_id}/time_accountings",
                json=payload,
            )
        except ZammadForbidden as exc:
            # A disabled feature and a missing ticket ACL are the same 403 here,
            # and only the first one carries a self-explaining message. Name
            # both, because neither is something a retry can fix.
            raise ZammadForbidden(
                f"{exc.message} - booking time needs the Time Accounting "
                "feature enabled in Zammad AND write access to this ticket. "
                "Neither can be fixed by retrying; ask an administrator.",
                status_code=exc.status_code,
                error_code=exc.error_code,
                body=exc.body,
            ) from exc

    return 2
