"""Unit tests for the GCP async download_stream implementation.

Tests validate GcpAsyncBlobStore.do_download_stream:
- the body is NOT read before returning
- range, generation, and alt=media reach the streaming call
- content_length comes from the response, other metadata from the resource
- closing the response closes the underlying connection
- a missing object raises BlobNotFoundError
"""

from __future__ import annotations

import json
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from multicloud.blob.blob_client.async_blob.async_driver import BlobNotFoundError
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import (
    _STREAM_CHUNK_SIZE,
    GcpAsyncBlobStore,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceNotFoundException,
    SubstrateSdkException,
)

try:
    import aiohttp
except ImportError:
    pytest.skip("aiohttp not installed", allow_module_level=True)


RESOURCE = {
    "name": "k.bin",
    "size": "10240",
    "contentType": "application/json",
    "etag": "CJW3vv26q5YDEAE=",
    "generation": "1787000000000001",
    "metadata": {"team": "multicloud"},
    "updated": "2026-08-18T12:00:00.000Z",
    "timeCreated": "2026-08-17T09:00:00.000Z",
}


def _client_response_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        request_info=MagicMock(), history=(), status=status, message="error"
    )


def _stream_response(*chunks: bytes, content_length: Optional[int] = None) -> MagicMock:
    """A stand-in for gcloud-aio's StreamResponse.

    Honours the requested size the way the real reader does, and records the
    sizes asked for, so the production chunk size cannot change unnoticed. The
    per-call yield is capped at the caller's chunk boundaries so a short read is
    exercised rather than assumed away.
    """
    pending = list(chunks)
    stream = MagicMock()
    stream.requested_sizes = []
    response = MagicMock()
    response.close = MagicMock()
    response.release = MagicMock()
    joined = b"".join(chunks)
    response.headers = {}
    if content_length is not None:
        response.headers["content-length"] = str(content_length)

    async def read(size=-1):
        stream.requested_sizes.append(size)
        if size is not None and size >= 0:
            if not pending:
                return b""
            head = pending.pop(0)
            if len(head) > size:
                pending.insert(0, head[size:])
                return head[:size]
            return head
        remainder, pending[:] = b"".join(pending), []
        return remainder

    stream.read = read
    stream._response = response
    stream.joined = joined
    return stream


@pytest.fixture
def mock_storage():
    mock = AsyncMock()
    mock.download_metadata = AsyncMock(return_value=dict(RESOURCE))
    mock._download = AsyncMock()
    mock._download_stream = AsyncMock()
    mock.download = AsyncMock(
        side_effect=AssertionError("the buffered download must not be used")
    )
    mock.close = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def store(mock_storage):
    store = GcpAsyncBlobStore(bucket="stream-bucket", _storage=mock_storage)
    yield store
    await store.close()


@pytest.mark.asyncio
class TestGcpAsyncDownloadStream:
    """Tests for GcpAsyncBlobStore.do_download_stream."""

    async def test_body_is_not_buffered_before_returning(self, store, mock_storage):
        """The buffered download path must not be used to fake streaming."""
        mock_storage._download_stream.return_value = _stream_response(
            b"abc", b"def", content_length=6
        )

        stream = await store.do_download_stream("k.bin")

        assert await stream.read(-1) == b"abcdef"
        mock_storage.download.assert_not_awaited()

    async def test_chunks_reach_the_caller_in_order(self, store, mock_storage):
        mock_storage._download_stream.return_value = _stream_response(
            b"one", b"two", b"three", content_length=11
        )

        stream = await store.do_download_stream("k.bin")

        assert [chunk async for chunk in stream] == [b"one", b"two", b"three"]

    async def test_media_alt_is_requested(self, store, mock_storage):
        """Without alt=media the JSON resource comes back instead of the bytes."""
        mock_storage._download_stream.return_value = _stream_response(b"x")

        await store.do_download_stream("k.bin")

        assert (
            mock_storage._download_stream.call_args.kwargs["params"]["alt"] == "media"
        )

    async def test_metadata_mixes_the_resource_and_the_response(
        self, store, mock_storage
    ):
        """Length describes this response; everything else describes the object."""
        mock_storage._download_stream.return_value = _stream_response(
            b"x", content_length=10
        )

        stream = await store.do_download_stream("k.bin")

        # The resource says 10240; the response says 10, which is what will
        # actually arrive on a ranged read.
        assert stream.metadata.content_length == 10
        assert stream.metadata.content_type == "application/json"
        assert stream.metadata.e_tag == "CJW3vv26q5YDEAE="
        assert stream.metadata.version_id == "1787000000000001"
        assert stream.metadata.metadata == {"team": "multicloud"}
        assert stream.metadata.last_modified is not None
        assert stream.metadata.created_time is not None

    async def test_range_is_sent_for_an_inclusive_window(self, store, mock_storage):
        mock_storage._download_stream.return_value = _stream_response(b"x")

        await store.do_download_stream("k.bin", start=10, end=19)

        headers = mock_storage._download_stream.call_args.kwargs["headers"]
        assert headers["Range"] == "bytes=10-19"

    async def test_end_only_is_sent_as_a_suffix_range(self, store, mock_storage):
        """Omitting start means the LAST end bytes, not the first."""
        mock_storage._download_stream.return_value = _stream_response(b"x")

        await store.do_download_stream("k.bin", end=5)

        headers = mock_storage._download_stream.call_args.kwargs["headers"]
        assert headers["Range"] == "bytes=-5"

    async def test_no_range_header_without_bounds(self, store, mock_storage):
        mock_storage._download_stream.return_value = _stream_response(b"x")

        await store.do_download_stream("k.bin")

        assert mock_storage._download_stream.call_args.kwargs["headers"] is None

    async def test_version_pins_the_resource_read_and_the_stream(
        self, store, mock_storage
    ):
        """Both requests must target the requested generation."""
        mock_storage._download.return_value = json.dumps(RESOURCE).encode()
        mock_storage._download_stream.return_value = _stream_response(b"x")

        await store.do_download_stream("k.bin", version_id="1787000000000001")

        read_params = mock_storage._download.call_args.kwargs["params"]
        assert read_params["generation"] == "1787000000000001"
        mock_storage.download_metadata.assert_not_awaited()

        stream_params = mock_storage._download_stream.call_args.kwargs["params"]
        assert stream_params["generation"] == "1787000000000001"

    async def test_the_media_read_is_pinned_to_the_resource_generation(
        self, store, mock_storage
    ):
        """Even with no version_id, the bytes must come from the object read.

        Otherwise the metadata describes one generation and the content another,
        with nothing to detect an overwrite landing between the two requests.
        """
        mock_storage._download_stream.return_value = _stream_response(b"x")

        stream = await store.do_download_stream("k.bin")

        params = mock_storage._download_stream.call_args.kwargs["params"]
        assert params["generation"] == RESOURCE["generation"]
        assert stream.metadata.version_id == RESOURCE["generation"]

    async def test_the_configured_chunk_size_is_used(self, store, mock_storage):
        """Otherwise the size could change to 1 and nothing would notice."""
        response = _stream_response(b"abc", content_length=3)
        mock_storage._download_stream.return_value = response

        stream = await store.do_download_stream("k.bin")
        await stream.read(-1)

        assert set(response.requested_sizes) == {_STREAM_CHUNK_SIZE}

    async def test_a_short_read_does_not_truncate_the_stream(self, store, mock_storage):
        """The reader may return fewer bytes than asked without being at EOF."""
        response = _stream_response(b"ab", b"cd", b"ef", content_length=6)
        mock_storage._download_stream.return_value = response

        stream = await store.do_download_stream("k.bin")

        assert await stream.read(-1) == b"abcdef"

    async def test_a_failure_mid_stream_is_translated(self, store, mock_storage):
        """The client's catch wraps only the open, so the generator translates."""
        response = _stream_response(b"abc", content_length=99)

        async def failing_read(size=-1):
            raise aiohttp.ClientPayloadError("body truncated")

        response.read = failing_read
        mock_storage._download_stream.return_value = response

        stream = await store.do_download_stream("k.bin")
        with pytest.raises(SubstrateSdkException):
            await stream.read(-1)

    async def test_a_missing_content_length_is_reported_as_unknown(
        self, store, mock_storage
    ):
        """Not zero: GCS omits the header and chunks the body when transcoding.

        Zero would read as a real length, which is worse than saying nothing.
        """
        mock_storage._download_stream.return_value = _stream_response(b"abc")

        stream = await store.do_download_stream("k.bin")

        assert stream.metadata.content_length is None

    async def test_non_integer_version_is_rejected_before_streaming(
        self, store, mock_storage
    ):
        with pytest.raises(InvalidArgumentException):
            await store.do_download_stream("k.bin", version_id="not-a-number")

        mock_storage._download_stream.assert_not_awaited()

    async def test_closing_releases_the_connection_to_the_pool(
        self, store, mock_storage
    ):
        """release(), not close(): a drained body's connection stays reusable.

        release() degrades to a close on its own when the payload is not at EOF,
        so it is safe for an abandoned stream too, whereas close() always
        destroys the transport.
        """
        response = _stream_response(b"abc", content_length=3)
        mock_storage._download_stream.return_value = response

        stream = await store.do_download_stream("k.bin")
        await stream.close()

        response._response.release.assert_called_once()
        response._response.close.assert_not_called()

    async def test_abandoning_early_still_releases_the_connection(
        self, store, mock_storage
    ):
        response = _stream_response(b"abc", b"def", content_length=6)
        mock_storage._download_stream.return_value = response

        async with await store.do_download_stream("k.bin") as stream:
            assert await stream.read(2) == b"ab"

        response._response.release.assert_called_once()

    async def test_missing_object_raises_blob_not_found(self, store, mock_storage):
        mock_storage.download_metadata.side_effect = _client_response_error(404)

        with pytest.raises(BlobNotFoundError) as exc_info:
            await store.do_download_stream("gone.bin")

        assert exc_info.value.key == "gone.bin"
        mock_storage._download_stream.assert_not_awaited()

    async def test_missing_object_is_catchable_as_resource_not_found(
        self, store, mock_storage
    ):
        mock_storage.download_metadata.side_effect = _client_response_error(404)

        with pytest.raises(ResourceNotFoundException):
            await store.do_download_stream("gone.bin")

    async def test_object_vanishing_between_the_two_calls_is_reported(
        self, store, mock_storage
    ):
        """The resource read can succeed and the stream still 404."""
        mock_storage._download_stream.side_effect = _client_response_error(404)

        with pytest.raises(BlobNotFoundError):
            await store.do_download_stream("racing.bin")

    async def test_other_errors_propagate(self, store, mock_storage):
        mock_storage._download_stream.side_effect = _client_response_error(403)

        with pytest.raises(aiohttp.ClientResponseError) as exc_info:
            await store.do_download_stream("denied.bin")

        assert exc_info.value.status == 403
