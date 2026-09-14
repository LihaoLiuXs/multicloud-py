"""Unit tests for async download_stream: the response type, driver, and client.

Tests validate:
- read() returns exactly the requested size across chunk boundaries, and the
  remainder then empty bytes at the end
- iteration hands back whatever read() buffered before continuing
- close is idempotent, releases once, and closes the stream to further use
- the driver validates the key and range before opening anything
- the client forwards every argument and translates provider errors
"""

from __future__ import annotations

from typing import AsyncIterator, Optional
from unittest.mock import patch

import pytest
from multicloud.blob.blob_client.async_blob.async_client import AsyncBucketClient
from multicloud.blob.blob_client.async_blob.async_driver import (
    ASYNC_BLOB_REGISTRY,
    AsyncStreamingDownloadResponse,
    BaseAsyncBlobStore,
    BlobMetadata,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
    UnknownException,
    UnsupportedOperationException,
)

METADATA = BlobMetadata(key="k.bin", content_length=12, content_type="text/plain")


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def _response(*parts: bytes, close_func=None) -> AsyncStreamingDownloadResponse:
    return AsyncStreamingDownloadResponse(
        key="k.bin", metadata=METADATA, stream=_chunks(*parts), close_func=close_func
    )


@pytest.mark.asyncio
class TestAsyncStreamingDownloadResponse:
    """Tests for the streaming response type itself."""

    async def test_read_all_returns_every_chunk_joined(self):
        assert await _response(b"abc", b"def", b"gh").read(-1) == b"abcdefgh"

    async def test_read_all_on_an_empty_stream_is_empty(self):
        assert await _response().read(-1) == b""

    async def test_read_returns_exactly_the_requested_size(self):
        """Even when the size falls inside a chunk."""
        stream = _response(b"abcde", b"fghij")

        assert await stream.read(3) == b"abc"
        assert await stream.read(3) == b"de" + b"f"

    async def test_read_spans_multiple_chunks(self):
        """A size larger than one chunk pulls further chunks in."""
        stream = _response(b"ab", b"cd", b"ef", b"gh")

        assert await stream.read(7) == b"abcdefg"

    async def test_read_returns_the_remainder_then_empty(self):
        stream = _response(b"abcd")

        assert await stream.read(3) == b"abc"
        assert await stream.read(10) == b"d"
        assert await stream.read(10) == b""

    async def test_read_all_after_a_partial_read_includes_the_buffer(self):
        """The surplus a sized read left behind must not be lost.

        Covered separately from iteration, since read(-1) takes its own path
        through the buffer.
        """
        stream = _response(b"abcdef", b"ghij")

        head = await stream.read(2)
        rest = await stream.read(-1)

        assert head == b"ab"
        assert rest == b"cdefghij"
        assert head + rest == b"abcdefghij"
        # The buffer must be consumed, not merely copied out of.
        assert await stream.read(-1) == b""

    async def test_read_all_twice_returns_empty_the_second_time(self):
        stream = _response(b"abc")

        assert await stream.read(-1) == b"abc"
        assert await stream.read(-1) == b""

    @pytest.mark.parametrize("size", [-1, -2, -100])
    async def test_any_negative_size_reads_everything(self, size):
        """io treats any negative size as read-all, so this must too.

        Treating only -1 that way makes read(-2) return a truncated slice, and
        on a fresh stream an empty one, which is indistinguishable from EOF.
        """
        assert await _response(b"abc", b"def").read(size) == b"abcdef"

    @pytest.mark.parametrize("size", [-1, -2, -100])
    async def test_any_negative_size_includes_the_buffer(self, size):
        stream = _response(b"abcdef")

        head = await stream.read(2)

        assert head == b"ab"
        assert await stream.read(size) == b"cdef"

    async def test_read_zero_consumes_nothing(self):
        stream = _response(b"abcd")

        assert await stream.read(0) == b""
        assert await stream.read(-1) == b"abcd"

    async def test_iteration_yields_the_underlying_chunks(self):
        stream = _response(b"one", b"two", b"three")

        assert [chunk async for chunk in stream] == [b"one", b"two", b"three"]

    async def test_iteration_after_a_partial_read_returns_the_buffer_first(self):
        """The surplus a read left behind must not be skipped or repeated."""
        stream = _response(b"abcdef", b"ghij")

        head = await stream.read(2)
        tail = b"".join([chunk async for chunk in stream])

        assert head == b"ab"
        assert head + tail == b"abcdefghij"

    async def test_close_invokes_the_release_once(self):
        calls = []

        async def close_func():
            calls.append("released")

        stream = _response(b"abc", close_func=close_func)
        await stream.close()
        await stream.close()

        assert calls == ["released"]
        assert stream.closed is True

    async def test_close_without_a_release_function_is_fine(self):
        stream = _response(b"abc")

        await stream.close()

        assert stream.closed is True

    async def test_read_after_close_is_refused(self):
        stream = _response(b"abc")
        await stream.close()

        with pytest.raises(ValueError):
            await stream.read(1)

    async def test_iteration_after_close_is_refused(self):
        stream = _response(b"abc")
        await stream.close()

        with pytest.raises(ValueError):
            [chunk async for chunk in stream]

    async def test_context_manager_closes_on_exit(self):
        calls = []

        async def close_func():
            calls.append("released")

        stream = _response(b"abc", close_func=close_func)
        async with stream as opened:
            assert opened is stream
            assert await opened.read(2) == b"ab"

        assert stream.closed is True
        assert calls == ["released"]

    async def test_context_manager_closes_when_the_body_raises(self):
        """An abandoned stream must still release its connection."""
        calls = []

        async def close_func():
            calls.append("released")

        stream = _response(b"abc", close_func=close_func)
        with pytest.raises(RuntimeError):
            async with stream:
                raise RuntimeError("caller gave up")

        assert calls == ["released"]

    async def test_key_and_metadata_are_exposed(self):
        stream = _response(b"abc")

        assert stream.key == "k.bin"
        assert stream.metadata.content_length == 12
        assert stream.closed is False


class RecordingStreamStore(BaseAsyncBlobStore):
    """Records what the driver forwards, and hands back a canned stream."""

    provider_id = "recording-stream"

    def __init__(self, **kwargs):
        super().__init__(bucket=kwargs.get("bucket", "b"))
        self.calls: list[dict] = []
        self.raise_native: Optional[Exception] = None

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        if isinstance(exc, SubstrateSdkException):
            return exc
        return UnknownException(str(exc))

    async def do_download_stream(
        self, key, *, version_id=None, start=None, end=None
    ) -> AsyncStreamingDownloadResponse:
        self.calls.append(
            {"key": key, "version_id": version_id, "start": start, "end": end}
        )
        if self.raise_native is not None:
            raise self.raise_native
        return _response(b"streamed")

    async def do_upload(self, key, payload, **kwargs):
        raise NotImplementedError

    async def do_download(self, request):
        raise NotImplementedError

    async def do_copy(self, request, *, correlation_id):
        raise NotImplementedError

    async def do_delete(self, key, *, version_id=None):
        raise NotImplementedError

    async def do_delete_objects(self, identifiers):
        raise NotImplementedError

    async def do_get_tags(self, key):
        raise NotImplementedError

    async def do_set_tags(self, key, tags):
        raise NotImplementedError

    async def do_upload_directory(self, local_source_directory, **kwargs):
        raise NotImplementedError

    async def do_download_directory(self, prefix, local_destination, **kwargs):
        raise NotImplementedError

    async def do_delete_directory(self, prefix, **kwargs):
        raise NotImplementedError

    async def do_get_metadata(self, key, *, version_id=None):
        raise NotImplementedError

    async def do_list_page(self, *, prefix, delimiter, max_results, pagination_token):
        raise NotImplementedError

    async def do_does_object_exist(self, key, *, version_id):
        raise NotImplementedError

    async def do_does_bucket_exist(self):
        raise NotImplementedError

    async def close(self) -> None:
        return None


class UnsupportedStreamStore(RecordingStreamStore):
    """Falls back to the driver default for the streaming hook."""

    provider_id = "unsupported-stream"

    do_download_stream = (
        BaseAsyncBlobStore.do_download_stream  # type: ignore[assignment]
    )


@pytest.mark.asyncio
class TestDriverDownloadStream:
    """Tests for BaseAsyncBlobStore.download_stream."""

    async def test_empty_key_is_rejected_before_opening_anything(self):
        store = RecordingStreamStore(bucket="b")

        with pytest.raises(InvalidArgumentException):
            await store.download_stream("")

        assert store.calls == []

    @pytest.mark.parametrize(
        "start,end",
        [(-1, None), (None, -1), (5, 1)],
        ids=["neg-start", "neg-end", "inverted"],
    )
    async def test_invalid_ranges_are_rejected(self, start, end):
        store = RecordingStreamStore(bucket="b")

        with pytest.raises(InvalidArgumentException):
            await store.download_stream("k.bin", start=start, end=end)

        assert store.calls == []

    async def test_range_and_version_reach_the_hook(self):
        store = RecordingStreamStore(bucket="b")

        await store.download_stream("k.bin", version_id="v-3", start=10, end=19)

        assert store.calls == [
            {"key": "k.bin", "version_id": "v-3", "start": 10, "end": 19}
        ]

    async def test_omitted_arguments_forward_as_none(self):
        store = RecordingStreamStore(bucket="b")

        await store.download_stream("k.bin")

        assert store.calls == [
            {"key": "k.bin", "version_id": None, "start": None, "end": None}
        ]

    async def test_provider_without_the_hook_reports_unsupported(self):
        store = UnsupportedStreamStore(bucket="b")

        with pytest.raises(UnsupportedOperationException):
            await store.download_stream("k.bin")


@pytest.mark.asyncio
class TestClientDownloadStream:
    """Tests for AsyncBucketClient.download_stream."""

    async def _client(self, store: RecordingStreamStore) -> AsyncBucketClient:
        with patch.object(ASYNC_BLOB_REGISTRY, "get", return_value=lambda **kw: store):
            return AsyncBucketClient(provider="recording-stream", bucket="b")

    async def test_every_argument_reaches_the_store(self):
        """The client is a forwarder, so each value is asserted individually."""
        store = RecordingStreamStore(bucket="b")
        client = await self._client(store)

        await client.download_stream("k.bin", version_id="v-9", start=3, end=7)

        assert store.calls == [
            {"key": "k.bin", "version_id": "v-9", "start": 3, "end": 7}
        ]

    async def test_the_stream_is_returned_to_the_caller(self):
        store = RecordingStreamStore(bucket="b")
        client = await self._client(store)

        async with await client.download_stream("k.bin") as stream:
            assert await stream.read(-1) == b"streamed"

        assert stream.closed is True

    async def test_native_error_is_translated(self):
        store = RecordingStreamStore(bucket="b")
        store.raise_native = RuntimeError("native boom")
        client = await self._client(store)

        with pytest.raises(UnknownException):
            await client.download_stream("k.bin")

    async def test_validation_error_keeps_its_type(self):
        store = RecordingStreamStore(bucket="b")
        client = await self._client(store)

        with pytest.raises(InvalidArgumentException):
            await client.download_stream("", start=1, end=0)
