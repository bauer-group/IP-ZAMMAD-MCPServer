"""
Knowledge tools - grounding a reply in the organisation's own material.

Endpoints (all under /api/v1/), with the Zammad permission each needs:
  POST /knowledge_bases/search                        any authenticated user
  GET  /knowledge_bases/{kb_id}/answers/{answer_id}   knowledge_base.* (reader or editor)
  GET  /text_modules                                  ticket.agent or admin.text_module
  GET  /text_modules/search                           admin.text_module ONLY

Why the search tool rewrites its own response
---------------------------------------------
Zammad's knowledge base search never returns an answer ID. Its unit of indexing
is the ``KnowledgeBase::Answer::Translation`` - one answer holds one translation
per locale - so every hit is identified by a translation ID, and no endpoint
turns a translation ID back into an answer ID. The answer ID appears in exactly
one place: the ``url`` Zammad renders for each hit, and only when the request
asks for ``url_type=agent``, where that URL is the answer's own API path
(``/api/v1/knowledge_bases/1/answers/42``). This module therefore pins
``url_type=agent`` and lifts both IDs out of the URL onto each hit, so the model
can go from a search hit straight to ``get_kb_answer`` instead of guessing.

Why get_kb_answer issues two requests
-------------------------------------
``GET .../answers/{id}`` returns an asset graph with no article body in it: the
body lives on a separate ``KnowledgeBase::Answer::Translation::Content`` record
that Zammad only serialises when the request names its ID in
``include_contents``, and those IDs are only discoverable from the first
response. A single request would hand the model a title and no text.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..projection import envelope
from . import ToolContext

if TYPE_CHECKING:
    from fastmcp import FastMCP

# The record class Zammad indexes, uses as the search result type, and keys the
# asset graph by. Not an answer - one of its locale translations.
ANSWER_TRANSLATION = "KnowledgeBase::Answer::Translation"

# 'agent' widens the result set to internal (unpublished) answers for holders of
# knowledge_base.reader; 'public' restricts it to published ones for everyone.
SEARCH_FLAVORS = ("agent", "public")

# KnowledgeBase::SearchController does not call paginate_with, so Zammad's own
# ceiling is CanPaginate's default of 1000. We stop far short of it because
# Zammad asks Elasticsearch for per_page * 99 candidates before filtering by
# permission - a large page is a very expensive query for a marginal gain.
KB_SEARCH_MAX_PER_PAGE = 100

# model_index_render calls paginate_with(default: 500) with no max, so the
# effective ceiling is CanPaginate's 1000 - not the 100 that /tickets uses.
TEXT_MODULE_INDEX_MAX_PER_PAGE = 1000

# model_search_render calls paginate_with(max: 200, default: 50).
TEXT_MODULE_SEARCH_MAX_PER_PAGE = 200

_ANSWER_API_PATH = re.compile(r"/knowledge_bases/(\d+)/answers/(\d+)")


def _annotate_answer_ids(payload: Any) -> Any:
    """Lift the knowledge base and answer IDs out of each hit's agent URL.

    See the module docstring: a hit identifies a translation, and the agent URL
    is the only carrier of the answer ID. Hits that are not answers (categories,
    knowledge bases) have a different URL shape and are left untouched.
    """
    if not isinstance(payload, dict):
        return payload
    for detail in payload.get("details") or []:
        if not isinstance(detail, dict):
            continue
        match = _ANSWER_API_PATH.search(str(detail.get("url") or ""))
        if match:
            detail["knowledge_base_id"] = int(match.group(1))
            detail["answer_id"] = int(match.group(2))
    return payload


def _content_ids_of(payload: Any, answer_id: int) -> list[str]:
    """Collect the content record IDs of one answer's locale translations.

    Only translations of the requested answer are taken: Zammad's asset graph
    also carries neighbouring records (the category, and any answer linked
    inline from the body), whose contents we have no reason to pull in.
    """
    assets = payload.get("assets") if isinstance(payload, dict) else None
    translations = assets.get(ANSWER_TRANSLATION) if isinstance(assets, dict) else None
    if not isinstance(translations, dict):
        return []
    content_ids: list[str] = []
    for translation in translations.values():
        if not isinstance(translation, dict) or translation.get("answer_id") != answer_id:
            continue
        content_id = translation.get("content_id")
        if content_id is not None:
            content_ids.append(str(content_id))
    return content_ids


def register(mcp: FastMCP, ctx: ToolContext) -> int:
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

    @mcp.tool(
        name="search_knowledge_base",
        description=(
            "Search the organisation's own knowledge base and ground your "
            "answer in what it says, rather than improvising one. Any "
            "authenticated user may call this - Zammad filters the hits by "
            "what the caller may read, so an agent holding "
            "'knowledge_base.reader' also sees internal (unpublished) answers "
            "when `flavor` is 'agent', while everyone else sees published "
            "answers only. Each hit identifies a LOCALE TRANSLATION and "
            "carries a ~100-character excerpt, not the article; this tool adds "
            "answer_id and knowledge_base_id to every answer hit, so pass "
            "those to `get_kb_answer` to read the full text before quoting it. "
            "Without Elasticsearch Zammad falls back to a SQL LIKE over title "
            "and body, so search a distinctive phrase rather than a long "
            "natural-language question."
        ),
        annotations=read_only,
    )
    async def search_knowledge_base(
        query: Annotated[str, Field(min_length=1, description="Words to search for")],
        flavor: Annotated[
            str,
            Field(
                description=(
                    "'agent' (default) to include internal answers the caller "
                    "is allowed to see, 'public' for published answers only."
                )
            ),
        ] = "agent",
        knowledge_base_id: Annotated[
            int | None,
            Field(
                ge=1,
                description=(
                    "Restrict to one knowledge base. Omit to search every "
                    "active one - most instances only have a single one."
                ),
            ),
        ] = None,
        locale: Annotated[
            str | None,
            Field(
                description=(
                    "Restrict to one locale, e.g. 'de-de' or 'en-us'. Omit to "
                    "search every locale the knowledge base is translated into."
                )
            ),
        ] = None,
        answers_only: Annotated[
            bool,
            Field(
                description=(
                    "Return only answers (default). Set false to also match "
                    "category and knowledge base titles."
                )
            ),
        ] = True,
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int,
            Field(ge=1, le=KB_SEARCH_MAX_PER_PAGE, description="Hits per page (max 100)"),
        ] = 10,
    ) -> Any:
        if flavor not in SEARCH_FLAVORS:
            raise ToolError(
                f"flavor must be one of {', '.join(SEARCH_FLAVORS)} (got {flavor!r}). "
                "Use 'agent' to include internal answers, 'public' for published ones."
            )
        payload: dict[str, Any] = {
            "query": query,
            "flavor": flavor,
            "page": page,
            "per_page": per_page,
            # Not cosmetic: see the module docstring. The agent URL is the only
            # place Zammad exposes the answer ID a follow-up call needs.
            "url_type": "agent",
            # Two cheap extras that turn a bare title into usable context: the
            # category path the answer sits under, and its tags.
            "include_subtitle": True,
            "include_tags": True,
        }
        if answers_only:
            payload["index"] = ANSWER_TRANSLATION
        if knowledge_base_id is not None:
            payload["knowledge_base_id"] = knowledge_base_id
        if locale is not None:
            payload["locale"] = locale
        body = _annotate_answer_ids(
            await ctx.request("POST", "/knowledge_bases/search", json=payload)
        )
        # Zammad puts KB hits under `details` (not `records`) and ignores
        # with_total_count here, so the total is genuinely unavailable — which
        # the envelope reports as None rather than implying completeness.
        hits = body.get("details") if isinstance(body, dict) else None
        return envelope(hits if isinstance(hits, list) else [], page=page, per_page=per_page)

    @mcp.tool(
        name="get_kb_answer",
        description=(
            "Read the FULL text of one knowledge base answer, in every locale "
            "it has been translated into. Needs 'knowledge_base.reader' or "
            "'knowledge_base.editor'. Pass the answer_id that "
            "`search_knowledge_base` attaches to each hit - NOT the hit's own "
            "id, which identifies a locale translation and would silently "
            "address a different answer here. The text arrives in Zammad's "
            "asset graph: the title on each KnowledgeBase::Answer::Translation "
            "record and the HTML body on the matching "
            "KnowledgeBase::Answer::Translation::Content record."
        ),
        annotations=read_only,
    )
    async def get_kb_answer(
        answer_id: Annotated[
            int, Field(ge=1, description="ID of the answer, not of one of its translations")
        ],
        knowledge_base_id: Annotated[
            int,
            Field(
                ge=1,
                description=(
                    "Only used to build the URL - Zammad looks the answer up "
                    "globally and ignores this value. Almost every instance "
                    "has exactly one knowledge base, and its id is 1."
                ),
            ),
        ] = 1,
    ) -> Any:
        path = f"/knowledge_bases/{knowledge_base_id}/answers/{answer_id}"
        answer = await ctx.request("GET", path)
        content_ids = _content_ids_of(answer, answer_id)
        if not content_ids:
            # No translation, or a shape we do not recognise. The first response
            # still carries the answer's metadata, which beats raising.
            return answer
        return await ctx.request("GET", path, params={"include_contents": ",".join(content_ids)})

    @mcp.tool(
        name="list_text_modules",
        description=(
            "List this Zammad's text modules - the house-approved, pre-written "
            "wording for replies: greetings, closings, standard explanations, "
            "legal boilerplate. PREFER one of these over composing a reply from "
            "scratch; they are what the organisation has agreed to say, in the "
            "tone and legal wording it has settled on. Their content is HTML "
            "and may embed Zammad placeholders such as "
            "#{ticket.customer.firstname}, which only expand when a human "
            "inserts the module through Zammad's editor - substitute real "
            "values yourself before sending the text via `reply_to_customer`. "
            "Retired modules are included in the listing, so check each one's "
            "active flag. Needs 'ticket.agent' or 'admin.text_module'."
        ),
        annotations=read_only,
    )
    async def list_text_modules(
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int,
            Field(
                ge=1,
                le=TEXT_MODULE_INDEX_MAX_PER_PAGE,
                description="Items per page (max 1000)",
            ),
        ] = 50,
        expand: Annotated[
            bool, Field(description="Inline owner/group names instead of IDs")
        ] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/text_modules",
            params={"page": page, "per_page": per_page, "expand": str(expand).lower()},
        )

    @mcp.tool(
        name="search_text_modules",
        description=(
            "Search the house-approved reply wording by keyword. WARNING: "
            "unlike `list_text_modules`, this endpoint is reserved for "
            "'admin.text_module' - a plain 'ticket.agent' gets HTTP 403 here. "
            "If that happens, call `list_text_modules` and pick from the "
            "listing yourself rather than giving up on the house wording. "
            "Without Elasticsearch Zammad falls back to a SQL LIKE over the "
            "string columns only, which covers the module's name and keywords "
            "but NOT its body, so search by name or keyword rather than by a "
            "phrase you expect to find inside the text."
        ),
        annotations=read_only,
    )
    async def search_text_modules(
        query: Annotated[str, Field(min_length=1, description="Words to search for")],
        page: Annotated[int, Field(ge=1, description="1-indexed page number")] = 1,
        per_page: Annotated[
            int,
            Field(
                ge=1,
                le=TEXT_MODULE_SEARCH_MAX_PER_PAGE,
                description="Results per page (max 200)",
            ),
        ] = 25,
        expand: Annotated[
            bool, Field(description="Inline owner/group names instead of IDs")
        ] = True,
        with_total_count: Annotated[
            bool,
            Field(
                description=(
                    "Include the total number of matches so you can tell a "
                    "complete result from a truncated one. Wraps the response "
                    "in an object with a total_count field."
                )
            ),
        ] = True,
    ) -> Any:
        return await ctx.request(
            "GET",
            "/text_modules/search",
            params={
                "query": query,
                "page": page,
                "per_page": per_page,
                "expand": str(expand).lower(),
                "with_total_count": str(with_total_count).lower(),
            },
        )

    return 4
