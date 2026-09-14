import pytest
from vcr.request import Request
from vcr.stubs import aiohttp_stubs
from vcr.stubs.aiohttp_stubs import MockClientResponse
from yarl import URL

import tests.blob.conformance.conftest  # noqa: F401


@pytest.mark.asyncio
async def test_replayed_response_content_is_consumed_across_reads():
    response = MockClientResponse("GET", URL("https://example.invalid/object"))
    response._body = b"abcdefgh"

    assert await response.content.read(3) == b"abc"
    assert await response.content.read(3) == b"def"
    assert await response.content.read(3) == b"gh"
    assert await response.content.read(3) == b""


@pytest.mark.asyncio
async def test_built_replay_response_has_a_readable_content_stream():
    request = Request("GET", "https://example.invalid/object", None, {})
    recorded = {
        "status": {"code": 200, "message": "OK"},
        "headers": {"Content-Length": ["8"]},
        "body": {"string": b"abcdefgh"},
    }

    response = aiohttp_stubs.build_response(request, recorded, [])

    assert await response.content.read(4) == b"abcd"
    assert await response.content.read(4) == b"efgh"
    assert await response.content.read(4) == b""
