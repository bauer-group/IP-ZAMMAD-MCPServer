"""The byte-preserving path.

_DecodingCtx.request routes everything through bg-mcpcore's request_json,
which falls back to response.text - a lossy UTF-8 decode. That is why binary
attachments were unreachable. request_raw hands back the untouched response so
the bytes survive, while raising the same typed errors as request.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from server import _DecodingCtx
from zammad.errors import ZammadForbidden, ZammadNotFound

PNG = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff\xfe"


class FakeCoreCtx:
    """Stands in for bg-mcpcore's ToolContext: request returns httpx.Response."""

    def __init__(self, response: httpx.Response) -> None:
        self.settings = None
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._response = response

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, path, kwargs))
        return self._response

    async def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        raise AssertionError("request_raw must not go through request_json")


def _response(status: int, content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://zammad.example/api/v1/x"),
    )


async def test_raw_bytes_survive_intact() -> None:
    core = FakeCoreCtx(_response(200, PNG, "image/png"))
    ctx = _DecodingCtx(core)

    response = await ctx.request_raw("GET", "/ticket_attachment/5/42/7")

    assert response.content == PNG, "every byte must survive"
    assert core.calls == [("GET", "/ticket_attachment/5/42/7", {})]


async def test_headers_are_reachable_for_the_charset() -> None:
    core = FakeCoreCtx(_response(200, b"hallo", "text/plain; charset=iso-8859-1"))
    response = await _DecodingCtx(core).request_raw("GET", "/x")
    assert response.headers["content-type"] == "text/plain; charset=iso-8859-1"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, ZammadForbidden), (404, ZammadNotFound)],
)
async def test_non_2xx_raises_the_same_typed_errors_as_request(
    status: int, expected: type[Exception]
) -> None:
    core = FakeCoreCtx(_response(status, b'{"error":"nope"}', "application/json"))
    with pytest.raises(expected):
        await _DecodingCtx(core).request_raw("GET", "/x")


async def test_a_non_json_error_body_does_not_break_the_error_path() -> None:
    core = FakeCoreCtx(_response(404, b"<html>gone</html>", "text/html"))
    with pytest.raises(ZammadNotFound):
        await _DecodingCtx(core).request_raw("GET", "/x")
