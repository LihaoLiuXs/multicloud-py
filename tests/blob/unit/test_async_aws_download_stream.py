"""Unit tests for the AWS async download_stream implementation.

Tests validate AwsAsyncBlobStore.do_download_stream:
- the body is NOT read before returning
- range and version reach get_object
- metadata is mapped from the response headers
- closing the response releases the body
- a missing object raises BlobNotFoundError
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import (
    _STREAM_CHUNK_SIZE,
    AwsAsyncBlobStore,
)
from multicloud.blob.blob_client.async_blob.async_driver import BlobNotFoundError
from multicloud.common.exceptions import (
    ResourceNotFoundException,
    SubstrateSdkException,
)

try:
    from botocore.exceptions import ClientError, IncompleteReadError
except ImportError:
    pytest.skip("aiobotocore not installed", allow_module_level=True)


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )


def _raising(exc):
    async def _call(**kwargs):
        raise exc

    return _call


def _body(*chunks: bytes) -> MagicMock:
    """A stand-in for aiobotocore's streaming body.

    Records the chunk size it was asked for, so the production call cannot
    quietly change it, and refuses an eager read.
    """
    body = MagicMock()
    body.requested_chunk_sizes = []

    async def iter_chunks(size):
        body.requested_chunk_sizes.append(size)
        for chunk in chunks:
            yield chunk

    body.iter_chunks = iter_chunks
    body.read = AsyncMock(side_effect=AssertionError("body must not be read eagerly"))
    body.release = MagicMock()
    body.close = MagicMock()
    return body


@pytest.fixture
def mock_s3_client():
    mock = AsyncMock()
    mock.get_object = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def store(mock_s3_client):
    with patch(
        "multicloud.blob.blob_aws.async_blob.aws_async_blob_store.get_aio_session"
    ):
        store = AwsAsyncBlobStore(bucket="stream-bucket", region="us-west-2")
        store._client = mock_s3_client
        store._client_cm = MagicMock()
        yield store
        await store.close()


@pytest.mark.asyncio
class TestAwsAsyncDownloadStream:
    """Tests for AwsAsyncBlobStore.do_download_stream."""

    async def test_body_is_not_read_before_returning(self, store, mock_s3_client):
        """Opening a stream must not pull the object into memory.

        The fake body raises if read() is awaited, so an implementation that
        buffers fails here rather than silently regressing to download().
        """
        body = _body(b"abc", b"def")
        mock_s3_client.get_object.return_value = {"Body": body, "ContentLength": 6}

        stream = await store.do_download_stream("k.bin")

        assert stream.closed is False
        assert await stream.read(-1) == b"abcdef"

    async def test_chunks_reach_the_caller_in_order(self, store, mock_s3_client):
        mock_s3_client.get_object.return_value = {
            "Body": _body(b"one", b"two", b"three"),
            "ContentLength": 11,
        }

        stream = await store.do_download_stream("k.bin")

        assert [chunk async for chunk in stream] == [b"one", b"two", b"three"]

    async def test_metadata_is_mapped_from_the_response(self, store, mock_s3_client):
        mock_s3_client.get_object.return_value = {
            "Body": _body(b"abc"),
            "ContentLength": 3,
            "ContentType": "application/json",
            "ETag": '"abc123"',
            "VersionId": "v-42",
            "Metadata": {"team": "multicloud"},
        }

        stream = await store.do_download_stream("k.bin")

        assert stream.key == "k.bin"
        assert stream.metadata.content_length == 3
        assert stream.metadata.content_type == "application/json"
        assert stream.metadata.e_tag == "abc123"
        assert stream.metadata.version_id == "v-42"
        assert stream.metadata.metadata == {"team": "multicloud"}

    async def test_range_is_sent_for_an_inclusive_window(self, store, mock_s3_client):
        mock_s3_client.get_object.return_value = {
            "Body": _body(b"x"),
            "ContentLength": 1,
        }

        await store.do_download_stream("k.bin", start=10, end=19)

        assert mock_s3_client.get_object.call_args.kwargs["Range"] == "bytes=10-19"

    async def test_end_only_is_sent_as_a_suffix_range(self, store, mock_s3_client):
        """Omitting start means the LAST end bytes, not the first."""
        mock_s3_client.get_object.return_value = {
            "Body": _body(b"x"),
            "ContentLength": 1,
        }

        await store.do_download_stream("k.bin", end=5)

        assert mock_s3_client.get_object.call_args.kwargs["Range"] == "bytes=-5"

    async def test_start_only_reads_to_the_end(self, store, mock_s3_client):
        mock_s3_client.get_object.return_value = {
            "Body": _body(b"x"),
            "ContentLength": 1,
        }

        await store.do_download_stream("k.bin", start=7)

        assert mock_s3_client.get_object.call_args.kwargs["Range"] == "bytes=7-"

    async def test_no_range_header_without_bounds(self, store, mock_s3_client):
        mock_s3_client.get_object.return_value = {
            "Body": _body(b"x"),
            "ContentLength": 1,
        }

        await store.do_download_stream("k.bin")

        assert "Range" not in mock_s3_client.get_object.call_args.kwargs

    async def test_version_id_pins_the_read(self, store, mock_s3_client):
        mock_s3_client.get_object.return_value = {
            "Body": _body(b"x"),
            "ContentLength": 1,
        }

        await store.do_download_stream("k.bin", version_id="v-88")

        assert mock_s3_client.get_object.call_args.kwargs["VersionId"] == "v-88"

    async def test_omitted_version_sends_no_version(self, store, mock_s3_client):
        mock_s3_client.get_object.return_value = {
            "Body": _body(b"x"),
            "ContentLength": 1,
        }

        await store.do_download_stream("k.bin")

        assert "VersionId" not in mock_s3_client.get_object.call_args.kwargs

    async def test_closing_releases_the_body_to_the_pool(self, store, mock_s3_client):
        """release(), not close(): a drained body's connection stays reusable.

        release() degrades to a close on its own when the payload is not at
        EOF, so it is safe for an abandoned stream too, whereas close() always
        destroys the transport and costs the next request a handshake.
        """
        body = _body(b"abc")
        mock_s3_client.get_object.return_value = {"Body": body, "ContentLength": 3}

        stream = await store.do_download_stream("k.bin")
        await stream.close()

        body.release.assert_called_once()
        body.close.assert_not_called()

    async def test_abandoning_early_still_releases_the_body(
        self, store, mock_s3_client
    ):
        """A caller that reads one chunk and stops must not leak the connection."""
        body = _body(b"abc", b"def", b"ghi")
        mock_s3_client.get_object.return_value = {"Body": body, "ContentLength": 9}

        async with await store.do_download_stream("k.bin") as stream:
            assert await stream.read(2) == b"ab"

        body.release.assert_called_once()

    async def test_the_configured_chunk_size_is_used(self, store, mock_s3_client):
        """Otherwise the size could change to 1 and nothing would notice."""
        body = _body(b"abc")
        mock_s3_client.get_object.return_value = {"Body": body, "ContentLength": 3}

        stream = await store.do_download_stream("k.bin")
        await stream.read(-1)

        assert body.requested_chunk_sizes == [_STREAM_CHUNK_SIZE]

    async def test_a_failure_mid_stream_is_translated(self, store, mock_s3_client):
        """The client's catch wraps only the open, so the generator translates.

        A body that stops short of its declared length raises a native error
        during iteration, which must not reach the caller untranslated.
        """
        body = MagicMock()
        body.release = MagicMock()

        async def iter_chunks(size):
            yield b"abc"
            raise IncompleteReadError(actual_bytes=3, expected_bytes=99)

        body.iter_chunks = iter_chunks
        mock_s3_client.get_object.return_value = {"Body": body, "ContentLength": 99}

        stream = await store.do_download_stream("k.bin")
        with pytest.raises(SubstrateSdkException):
            await stream.read(-1)

    async def test_missing_object_raises_blob_not_found(self, store, mock_s3_client):
        mock_s3_client.get_object = _raising(_client_error("NoSuchKey", 404))

        with pytest.raises(BlobNotFoundError) as exc_info:
            await store.do_download_stream("gone.bin")

        assert exc_info.value.key == "gone.bin"

    async def test_missing_object_is_catchable_as_resource_not_found(
        self, store, mock_s3_client
    ):
        mock_s3_client.get_object = _raising(_client_error("NoSuchKey", 404))

        with pytest.raises(ResourceNotFoundException):
            await store.do_download_stream("gone.bin")

    async def test_other_errors_propagate(self, store, mock_s3_client):
        mock_s3_client.get_object = _raising(_client_error("AccessDenied", 403))

        with pytest.raises(ClientError) as exc_info:
            await store.do_download_stream("denied.bin")

        assert exc_info.value.response["Error"]["Code"] == "AccessDenied"
