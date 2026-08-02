"""Behaviour tests for the Zammad AI-assistance tools.

Everything interesting about these two tools is in what they do with a
*successful* HTTP response that carries no answer, and with the two different
statuses Zammad uses for "this feature is not available here". Those are the
paths that decide whether the model concludes "no summary exists" or "ask again
in a moment", so they are what these tests pin down.

The poll interval is patched to zero throughout - the number of attempts is the
behaviour under test, the wall-clock wait is not.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastmcp import FastMCP

from tests.test_tools_inventory import RecordingCtx
from zammad.errors import ZammadForbidden, ZammadServerError, ZammadValidationError
from zammad.tools import ai

EXPECTED_TOOLS = sorted(["summarize_ticket", "suggest_kb_answers"])

# Tools from other modules this module's descriptions are allowed to point at.
CROSS_MODULE_TOOLS = {"list_ticket_articles", "search_knowledge_base", "get_kb_answer"}

SUMMARY_PENDING: dict[str, Any] = {"result": None}
SUMMARY_READY: dict[str, Any] = {"result": "Printer smokes.", "analytics": {"run_id": 5}}
RELATED_PENDING: dict[str, Any] = {"result": {"pending": True}}
RELATED_READY: dict[str, Any] = {
    "result": {"pending": False, "answer_translation_ids": [7], "excerpts": {"7": "..."}},
    "assets": {},
}


class SequenceCtx(RecordingCtx):
    """A RecordingCtx that replays a scripted response - or raises - per call.

    The last entry repeats, so a test can script "pending forever" with one item.
    """

    def __init__(self, responses: list[Any]) -> None:
        super().__init__()
        self._queue = list(responses)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        item = self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_polling_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "POLL_INTERVAL_SECONDS", 0)


def _register(responses: list[Any]) -> tuple[FastMCP, SequenceCtx]:
    mcp: FastMCP = FastMCP("test-ai")
    ctx = SequenceCtx(responses)
    ai.register(mcp, ctx)
    return mcp, ctx


async def _tools(mcp: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in await mcp.list_tools(run_middleware=False)}


async def _call(mcp: FastMCP, name: str, **kwargs: Any) -> None:
    await (await _tools(mcp))[name].run(kwargs)


# ── inventory, annotations, descriptions ─────────────────────────────────────


async def test_registers_exactly_the_expected_tools() -> None:
    mcp, _ = _register([{}])
    assert sorted(await _tools(mcp)) == EXPECTED_TOOLS


async def test_declared_count_matches_registrations() -> None:
    mcp: FastMCP = FastMCP("test-ai-count")
    declared = ai.register(mcp, RecordingCtx())
    assert declared == len(await mcp.list_tools(run_middleware=False))


async def test_both_tools_are_additive_writes_not_reads() -> None:
    """The first call enqueues a generation job, so neither is read-only.

    Nothing is overwritten either, so neither may claim destructiveHint.
    """
    mcp, _ = _register([{}])
    for name, tool in (await _tools(mcp)).items():
        assert tool.annotations is not None, f"{name} has no annotations"
        assert tool.annotations.readOnlyHint is False, f"{name} enqueues a job"
        assert tool.annotations.destructiveHint is False, f"{name} overwrites nothing"
        assert tool.annotations.idempotentHint is True, f"{name} returns the stored result"


async def test_descriptions_only_name_real_parameters() -> None:
    mcp, _ = _register([{}])
    problems: list[str] = []
    for tool_name, tool in (await _tools(mcp)).items():
        params = set((tool.parameters or {}).get("properties", {}))
        for token in re.findall(r"`([a-z][a-z0-9_]{2,})`", tool.description or ""):
            if token in params or token in EXPECTED_TOOLS or token in CROSS_MODULE_TOOLS:
                continue
            problems.append(f"{tool_name}: description references `{token}`, not a parameter")
    assert not problems, "\n".join(problems)


# ── summarize_ticket ─────────────────────────────────────────────────────────


async def test_summarize_posts_to_the_ticket_summarize_endpoint() -> None:
    mcp, ctx = _register([SUMMARY_READY])
    await _call(mcp, "summarize_ticket", ticket_id=7)
    assert ctx.calls == [{"method": "POST", "path": "/tickets/7/summarize"}]


async def test_summarize_polls_until_the_background_job_is_done() -> None:
    """The first POST only enqueues the job and answers {"result": null}."""
    mcp, ctx = _register([SUMMARY_PENDING, SUMMARY_PENDING, SUMMARY_READY])
    await _call(mcp, "summarize_ticket", ticket_id=7)
    assert len(ctx.calls) == 3


async def test_summarize_raises_rather_than_returning_an_empty_summary() -> None:
    """A null result handed back verbatim reads as "this ticket has no summary"."""
    mcp, ctx = _register([SUMMARY_PENDING])
    with pytest.raises(Exception, match="still generating the summary"):
        await _call(mcp, "summarize_ticket", ticket_id=7)
    assert len(ctx.calls) == ai.POLL_ATTEMPTS


async def test_summarize_surfaces_a_failure_reported_with_http_200() -> None:
    """Zammad reports a failed generation as 200 with error: true."""
    mcp, _ = _register([{"result": None, "error": True, "error_message": "provider timed out"}])
    with pytest.raises(Exception, match="provider timed out"):
        await _call(mcp, "summarize_ticket", ticket_id=7)


@pytest.mark.parametrize(
    "error",
    [
        # The assistance setting is off: Exceptions::UnprocessableContent -> 422.
        ZammadValidationError("This feature is not enabled.", status_code=422),
        # No AI provider: a plain StandardError -> 500, masked for non-admins.
        ZammadServerError("Please contact your administrator.", status_code=500),
    ],
)
async def test_summarize_degrades_gracefully_when_the_feature_is_off(error: Exception) -> None:
    mcp, ctx = _register([error])
    with pytest.raises(Exception, match="cannot summarise tickets"):
        await _call(mcp, "summarize_ticket", ticket_id=7)
    # A disabled feature must not be retried three more times.
    assert len(ctx.calls) == 1


# ── suggest_kb_answers ───────────────────────────────────────────────────────


async def test_suggest_posts_to_the_related_answers_endpoint() -> None:
    mcp, ctx = _register([RELATED_READY])
    await _call(mcp, "suggest_kb_answers", ticket_id=7)
    assert ctx.calls == [
        {"method": "POST", "path": "/tickets/7/knowledge_base_answers"}
    ]


async def test_suggest_polls_while_the_ticket_summary_is_generated() -> None:
    mcp, ctx = _register([RELATED_PENDING, RELATED_READY])
    await _call(mcp, "suggest_kb_answers", ticket_id=7)
    assert len(ctx.calls) == 2


async def test_suggest_raises_rather_than_returning_a_pending_result() -> None:
    mcp, ctx = _register([RELATED_PENDING])
    with pytest.raises(Exception, match="still preparing"):
        await _call(mcp, "suggest_kb_answers", ticket_id=7)
    assert len(ctx.calls) == ai.POLL_ATTEMPTS


@pytest.mark.parametrize(
    "error",
    [
        ZammadValidationError("Knowledge base vector search is not available.", status_code=422),
        ZammadServerError("Please contact your administrator.", status_code=500),
    ],
)
async def test_suggest_degrades_gracefully_when_the_feature_is_off(error: Exception) -> None:
    mcp, ctx = _register([error])
    with pytest.raises(Exception, match="cannot suggest knowledge base answers"):
        await _call(mcp, "suggest_kb_answers", ticket_id=7)
    assert len(ctx.calls) == 1


async def test_suggest_explains_the_bare_not_authorized_403() -> None:
    """Zammad's 403 body is "Not authorized" and names no permission."""
    mcp, _ = _register([ZammadForbidden("Not authorized", status_code=403)])
    with pytest.raises(Exception, match=re.escape("knowledge_base.reader")):
        await _call(mcp, "suggest_kb_answers", ticket_id=7)
