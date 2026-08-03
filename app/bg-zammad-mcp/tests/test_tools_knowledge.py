"""Request-shape tests for the knowledge tools.

The two behaviours worth guarding here are the ones a reviewer cannot verify by
reading a URL: ``search_knowledge_base`` must keep asking for ``url_type=agent``
(the only carrier of the answer ID a follow-up call needs), and
``get_kb_answer`` must keep issuing its second request (without it the model
gets a title and no article body).

Reuses the recording context from the golden-inventory suite, so no HTTP layer
is involved.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.tools import knowledge
from zammad.tools.knowledge import (
    ANSWER_TRANSLATION_ASSET,
    ANSWER_TRANSLATION_INDEX,
    _annotate_answer_ids,
    _content_ids_of,
)

EXPECTED_TOOLS = sorted(
    [
        "search_knowledge_base",
        "get_kb_answer",
        "list_text_modules",
        "search_text_modules",
    ]
)

# Tools from other modules this module's descriptions are allowed to point at.
CROSS_MODULE_TOOLS = {"reply_to_customer"}


class SequenceCtx(RecordingCtx):
    """A RecordingCtx that replays a scripted response per call."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__()
        self._queue = list(responses)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        return self._queue.pop(0) if self._queue else {}


@pytest.fixture
def mcp_and_ctx() -> tuple[FastMCP, RecordingCtx]:
    mcp: FastMCP = FastMCP("test-knowledge")
    ctx = RecordingCtx()
    knowledge.register(mcp, ctx)
    return mcp, ctx


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, **kwargs: Any) -> None:
    await (await _tools(mcp))[name].run(kwargs)


# ── inventory, annotations, descriptions ─────────────────────────────────────


async def test_registers_exactly_the_expected_tools(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    assert sorted(await _tools(mcp)) == EXPECTED_TOOLS


async def test_declared_count_matches_registrations() -> None:
    mcp: FastMCP = FastMCP("test-knowledge-count")
    declared = knowledge.register(mcp, RecordingCtx())
    assert declared == len(await mcp.list_tools(run_middleware=False))


async def test_every_tool_is_read_only(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """All four only read - including the search, which is a POST."""
    mcp, _ = mcp_and_ctx
    for name, tool in (await _tools(mcp)).items():
        assert tool.annotations is not None, f"{name} has no annotations"
        assert tool.annotations.readOnlyHint is True, f"{name} should be readOnlyHint"
        assert tool.annotations.destructiveHint is False, f"{name} should not be destructive"


async def test_descriptions_only_name_real_parameters(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """A backticked identifier must be a parameter of that tool or a tool name.

    Anything else tells the model to pass an argument the schema rejects.
    """
    mcp, _ = mcp_and_ctx
    problems: list[str] = []
    for tool_name, tool in (await _tools(mcp)).items():
        params = set((tool.parameters or {}).get("properties", {}))
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            if token in params or token in EXPECTED_TOOLS or token in CROSS_MODULE_TOOLS:
                continue
            problems.append(f"{tool_name}: description references `{token}`, not a parameter")
    assert not problems, "\n".join(problems)


# ── search_knowledge_base ────────────────────────────────────────────────────


async def test_search_posts_and_pins_the_agent_url_type(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """url_type=agent is what makes the answer ID recoverable at all.

    With the default (public) Zammad renders a /help slug instead of the
    answer's API path, and the follow-up get_kb_answer call becomes impossible.
    """
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "search_knowledge_base", query="vpn setup", page=3, per_page=5)
    assert ctx.last["method"] == "POST"
    assert ctx.last["path"] == "/knowledge_bases/search"
    payload = ctx.last["json"]
    assert payload["url_type"] == "agent"
    assert payload["query"] == "vpn setup"
    assert payload["flavor"] == "agent"
    assert payload["page"] == 3
    assert payload["per_page"] == 5
    assert payload["index"] == ANSWER_TRANSLATION_INDEX
    assert payload["include_subtitle"] is True
    assert payload["include_tags"] is True
    # Optional narrowing must stay out of the body unless asked for.
    assert "knowledge_base_id" not in payload
    assert "locale" not in payload


async def test_search_can_widen_beyond_answers_and_narrow_by_locale(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(
        mcp,
        "search_knowledge_base",
        query="vpn",
        answers_only=False,
        locale="de-de",
        knowledge_base_id=2,
        flavor="public",
    )
    payload = ctx.last["json"]
    assert "index" not in payload
    assert payload["locale"] == "de-de"
    assert payload["knowledge_base_id"] == 2
    assert payload["flavor"] == "public"


async def test_search_rejects_an_unknown_flavor(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, _ = mcp_and_ctx
    with pytest.raises(Exception, match="flavor must be one of"):
        await _call(mcp, "search_knowledge_base", query="vpn", flavor="internal")


def test_annotate_answer_ids_lifts_both_ids_out_of_the_agent_url() -> None:
    """Zammad indexes translations, so a hit's own id is useless downstream."""
    payload = _annotate_answer_ids(
        {
            "result": [{"id": 7, "type": ANSWER_TRANSLATION_INDEX}],
            "details": [
                {
                    "id": 7,
                    "type": ANSWER_TRANSLATION_INDEX,
                    "url": "/api/v1/knowledge_bases/1/answers/42?include_contents=7",
                },
                {"id": 3, "type": "KnowledgeBase::Category::Translation", "url": "/help/en-us/x"},
            ],
        }
    )
    assert payload["details"][0]["answer_id"] == 42
    assert payload["details"][0]["knowledge_base_id"] == 1
    # A non-answer hit has no answer to point at and must be left alone.
    assert "answer_id" not in payload["details"][1]


def test_annotate_answer_ids_tolerates_unexpected_shapes() -> None:
    assert _annotate_answer_ids([]) == []
    assert _annotate_answer_ids({"details": None}) == {"details": None}
    assert _annotate_answer_ids({"details": ["not a dict"]}) == {"details": ["not a dict"]}
    assert _annotate_answer_ids({"details": [{"id": 7}]})["details"][0] == {"id": 7}


# ── get_kb_answer ────────────────────────────────────────────────────────────


def _answer_assets(answer_id: int) -> dict[str, Any]:
    """An answer payload shaped like the one Zammad 7.1.1 actually sends.

    The keys matter and were wrong here for a long time. This fixture used the
    Rails class names ("KnowledgeBase::Answer::Translation"), the code looked
    them up under the same constant, and the two agreed with each other while
    both disagreed with Zammad — whose asset serializer strips the namespace
    colons. `_content_ids_of` therefore always returned [], get_kb_answer never
    made its second request, and every answer came back with a title and no
    body. A fake built from the same misunderstanding as the code can only ever
    confirm it.

    Verified against a live instance:
        GET /api/v1/knowledge_bases/4/answers/3
        -> assets: KnowledgeBaseAnswer, KnowledgeBaseAnswerTranslation, ...
    """
    return {
        "id": answer_id,
        "assets": {
            "KnowledgeBaseAnswer": {str(answer_id): {"id": answer_id}},
            ANSWER_TRANSLATION_ASSET: {
                "7": {"id": 7, "answer_id": answer_id, "content_id": 11},
                "8": {"id": 8, "answer_id": answer_id, "content_id": 12},
                # A neighbour dragged in by an inline link - not ours to expand.
                "9": {"id": 9, "answer_id": 99, "content_id": 13},
            },
        },
    }


def test_the_asset_key_is_not_the_index_name() -> None:
    """Zammad spells this model two ways and they are not interchangeable.

    The search index wants the Rails class name; the asset map strips the
    colons. One constant served both, which is how get_kb_answer shipped
    returning answers without their content.
    """
    assert ANSWER_TRANSLATION_INDEX == "KnowledgeBase::Answer::Translation"
    assert ANSWER_TRANSLATION_ASSET == "KnowledgeBaseAnswerTranslation"
    assert "::" not in ANSWER_TRANSLATION_ASSET, (
        "asset-graph keys never carry namespace colons - compare the "
        "Checklist / ChecklistItem / User lookups in the other modules"
    )


async def test_get_kb_answer_refetches_with_the_content_ids() -> None:
    """Without the second request the model gets a title and no body."""
    mcp: FastMCP = FastMCP("test-knowledge-answer")
    ctx = SequenceCtx([_answer_assets(42), {"id": 42, "assets": {}}])
    knowledge.register(mcp, ctx)

    await _call(mcp, "get_kb_answer", answer_id=42)

    assert len(ctx.calls) == 2
    assert ctx.calls[0] == {"method": "GET", "path": "/knowledge_bases/1/answers/42"}
    assert ctx.calls[1]["path"] == "/knowledge_bases/1/answers/42"
    # Only the requested answer's translations, in discovery order.
    assert ctx.calls[1]["params"] == {"include_contents": "11,12"}


async def test_get_kb_answer_honours_an_explicit_knowledge_base(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "get_kb_answer", answer_id=42, knowledge_base_id=3)
    assert ctx.calls[0]["path"] == "/knowledge_bases/3/answers/42"


async def test_get_kb_answer_does_not_refetch_without_content_ids(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """The default RecordingCtx returns {} - an answer with no translations."""
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "get_kb_answer", answer_id=42)
    assert len(ctx.calls) == 1


def test_content_ids_of_ignores_foreign_and_malformed_translations() -> None:
    assert _content_ids_of(_answer_assets(42), 42) == ["11", "12"]
    assert _content_ids_of(_answer_assets(42), 99) == ["13"]
    assert _content_ids_of({"assets": {ANSWER_TRANSLATION_ASSET: ["nope"]}}, 42) == []
    assert _content_ids_of({"assets": {}}, 42) == []
    assert _content_ids_of("not a dict", 42) == []


# ── text modules ─────────────────────────────────────────────────────────────


async def test_list_text_modules_paginates(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "list_text_modules", page=2, per_page=10, expand=False)
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/text_modules"
    # Zammad casts the STRING "false" correctly; a Python False would be "False".
    assert ctx.last["params"] == {"page": 2, "per_page": 10, "expand": "false"}


async def test_search_text_modules_sends_page_so_pagination_works(mcp_and_ctx) -> None:  # type: ignore[no-untyped-def]
    """Zammad computes offset = (page - 1) * limit and defaults page to 1.

    A search that never sends page is pinned to the first result set forever.
    """
    mcp, ctx = mcp_and_ctx
    await _call(mcp, "search_text_modules", query="greeting", page=4)
    assert ctx.last["method"] == "GET"
    assert ctx.last["path"] == "/text_modules/search"
    params = ctx.last["params"]
    assert params["query"] == "greeting"
    assert params["page"] == 4
    assert params["with_total_count"] == "true"
    assert params["expand"] == "true"
