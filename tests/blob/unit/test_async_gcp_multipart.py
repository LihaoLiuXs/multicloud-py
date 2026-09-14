"""Unit tests for GcpAsyncBlobStore multipart, with gcloud-aio mocked.

GCS has no multipart API, so these focus on the emulation: scratch objects per
part, compose batching, and cleanup.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

pytest.importorskip(
    "gcloud.aio.storage",
    reason="gcloud-aio-storage not installed (optional async-gcp dependency)",
)

from multicloud.blob.blob_client.async_blob.async_driver import (
    MultipartPart,
    MultipartUpload,
    UploadPartResponse,
)
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import GcpAsyncBlobStore
from multicloud.common.exceptions import InvalidArgumentException

BUCKET = "test-bucket"
KEY = "obj.bin"
UPLOAD_ID = "abc123"


def _make_store() -> tuple[GcpAsyncBlobStore, Any]:
    storage = AsyncMock()
    storage.upload.return_value = {"etag": "part-etag", "generation": "1"}
    storage.compose.return_value = {"etag": "final-etag", "generation": "42"}
    storage.list_objects.return_value = {"items": []}
    store = GcpAsyncBlobStore(bucket=BUCKET, region="us-west1", _storage=storage)
    return store, storage


@pytest.fixture
def store_and_storage():
    return _make_store()


@pytest.fixture
def mpu() -> MultipartUpload:
    return MultipartUpload(bucket=BUCKET, key=KEY, upload_id=UPLOAD_ID)


def _request_info():
    import aiohttp

    return aiohttp.RequestInfo(
        url="https://storage.googleapis.com/x",
        method="DELETE",
        headers=None,
        real_url="https://storage.googleapis.com/x",
    )


def _part_name(part_number: int) -> str:
    return f"{KEY}__multipart__{UPLOAD_ID}__{part_number}"


class TestInitiate:
    @pytest.mark.asyncio
    async def test_makes_no_request(self, store_and_storage):
        store, storage = store_and_storage

        result = await store.do_initiate_multipart_upload(
            KEY, metadata={}, content_type=None, tags={}, correlation_id="cid"
        )

        storage.upload.assert_not_awaited()
        storage.compose.assert_not_awaited()
        assert result.upload_id
        assert result.bucket == BUCKET

    @pytest.mark.asyncio
    async def test_each_session_gets_a_distinct_upload_id(self, store_and_storage):
        store, _ = store_and_storage

        first = await store.do_initiate_multipart_upload(
            KEY, metadata={}, content_type=None, tags={}, correlation_id="c1"
        )
        second = await store.do_initiate_multipart_upload(
            KEY, metadata={}, content_type=None, tags={}, correlation_id="c2"
        )

        assert first.upload_id != second.upload_id

    @pytest.mark.asyncio
    async def test_carries_metadata_for_completion_to_apply(self, store_and_storage):
        store, _ = store_and_storage

        result = await store.do_initiate_multipart_upload(
            KEY,
            metadata={"owner": "team"},
            content_type="text/plain",
            tags={"t": "v"},
            correlation_id="cid",
        )

        assert result.metadata == {"owner": "team"}
        assert result.content_type == "text/plain"
        assert result.tags == {"t": "v"}


class TestUploadPart:
    @pytest.mark.asyncio
    async def test_part_is_stored_as_its_own_object(self, store_and_storage, mpu):
        store, storage = store_and_storage

        result = await store.do_upload_part(
            mpu, MultipartPart(part_number=2, content=b"hello"), correlation_id="cid"
        )

        storage.upload.assert_awaited_once_with(
            bucket=BUCKET, object_name=_part_name(2), file_data=b"hello"
        )
        assert result == UploadPartResponse(
            part_number=2, e_tag="part-etag", size_in_bytes=5, correlation_id="cid"
        )

    @pytest.mark.asyncio
    async def test_stream_content_is_read_before_upload(self, store_and_storage, mpu):
        store, storage = store_and_storage

        await store.do_upload_part(
            mpu,
            MultipartPart(
                part_number=1, content=io.BytesIO(b"hello"), content_length=5
            ),
            correlation_id="cid",
        )

        assert storage.upload.await_args.kwargs["file_data"] == b"hello"

    @pytest.mark.asyncio
    async def test_part_number_beyond_the_compose_ceiling_is_rejected(
        self, store_and_storage, mpu
    ):
        store, storage = store_and_storage

        with pytest.raises(InvalidArgumentException, match="at most 1024"):
            await store.do_upload_part(
                mpu, MultipartPart(part_number=1025, content=b"x"), correlation_id="cid"
            )
        storage.upload.assert_not_awaited()


class TestComplete:
    @pytest.mark.asyncio
    async def test_composes_parts_in_ascending_order(self, store_and_storage, mpu):
        store, storage = store_and_storage
        parts = [
            UploadPartResponse(part_number=3, e_tag="c", size_in_bytes=1),
            UploadPartResponse(part_number=1, e_tag="a", size_in_bytes=1),
            UploadPartResponse(part_number=2, e_tag="b", size_in_bytes=1),
        ]

        result = await store.do_complete_multipart_upload(
            mpu, parts, correlation_id="cid"
        )

        storage.compose.assert_awaited_once()
        assert storage.compose.await_args.kwargs["source_object_names"] == [
            _part_name(1),
            _part_name(2),
            _part_name(3),
        ]
        assert result.e_tag == "final-etag"
        assert result.key == KEY
        assert result.version_id == "42"

    @pytest.mark.asyncio
    async def test_scratch_objects_are_deleted(self, store_and_storage, mpu):
        store, storage = store_and_storage
        parts = [
            UploadPartResponse(part_number=n, e_tag="e", size_in_bytes=1)
            for n in (1, 2)
        ]

        await store.do_complete_multipart_upload(mpu, parts, correlation_id="cid")

        deleted = {call.args[1] for call in storage.delete.await_args_list}
        assert deleted == {_part_name(1), _part_name(2)}

    @pytest.mark.asyncio
    async def test_scratch_objects_are_deleted_even_when_compose_fails(
        self, store_and_storage, mpu
    ):
        store, storage = store_and_storage
        storage.compose.side_effect = RuntimeError("compose exploded")
        parts = [UploadPartResponse(part_number=1, e_tag="e", size_in_bytes=1)]

        with pytest.raises(RuntimeError, match="compose exploded"):
            await store.do_complete_multipart_upload(mpu, parts, correlation_id="cid")

        deleted = {call.args[1] for call in storage.delete.await_args_list}
        assert _part_name(1) in deleted

    @pytest.mark.asyncio
    async def test_more_than_32_parts_are_composed_in_batches(
        self, store_and_storage, mpu
    ):
        store, storage = store_and_storage
        parts = [
            UploadPartResponse(part_number=n, e_tag="e", size_in_bytes=1)
            for n in range(1, 40)
        ]

        await store.do_complete_multipart_upload(mpu, parts, correlation_id="cid")

        # 39 parts: one batch of 32 into an intermediate, then that plus the
        # remaining 7 into the destination.
        assert storage.compose.await_count == 2
        first, second = storage.compose.await_args_list
        assert len(first.kwargs["source_object_names"]) == 32
        assert first.kwargs["object_name"].endswith("__intermediate_0")
        assert second.kwargs["object_name"] == KEY
        assert len(second.kwargs["source_object_names"]) == 8
        assert second.kwargs["source_object_names"][0].endswith("__intermediate_0")

    @pytest.mark.asyncio
    async def test_intermediates_are_cleaned_up(self, store_and_storage, mpu):
        store, storage = store_and_storage
        parts = [
            UploadPartResponse(part_number=n, e_tag="e", size_in_bytes=1)
            for n in range(1, 40)
        ]

        await store.do_complete_multipart_upload(mpu, parts, correlation_id="cid")

        deleted = {call.args[1] for call in storage.delete.await_args_list}
        assert any(name.endswith("__intermediate_0") for name in deleted)

    @pytest.mark.asyncio
    async def test_too_many_parts_is_rejected(self, store_and_storage, mpu):
        store, storage = store_and_storage
        parts = [
            UploadPartResponse(part_number=n, e_tag="e", size_in_bytes=1)
            for n in range(1, 1026)
        ]

        with pytest.raises(InvalidArgumentException, match="at most 1024"):
            await store.do_complete_multipart_upload(mpu, parts, correlation_id="cid")
        storage.compose.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_content_type_is_applied_to_the_composed_object(
        self, store_and_storage
    ):
        store, storage = store_and_storage
        mpu = MultipartUpload(
            bucket=BUCKET, key=KEY, upload_id=UPLOAD_ID, content_type="text/plain"
        )

        await store.do_complete_multipart_upload(
            mpu,
            [UploadPartResponse(part_number=1, e_tag="e", size_in_bytes=1)],
            correlation_id="cid",
        )

        assert storage.compose.await_args.kwargs["content_type"] == "text/plain"

    @pytest.mark.asyncio
    async def test_metadata_and_tags_are_patched_on_after_compose(
        self, store_and_storage
    ):
        store, storage = store_and_storage
        mpu = MultipartUpload(
            bucket=BUCKET,
            key=KEY,
            upload_id=UPLOAD_ID,
            metadata={"owner": "team"},
            tags={"env": "test"},
        )

        await store.do_complete_multipart_upload(
            mpu,
            [UploadPartResponse(part_number=1, e_tag="e", size_in_bytes=1)],
            correlation_id="cid",
        )

        patched = storage.patch_metadata.await_args.kwargs["metadata"]["metadata"]
        assert patched == {"owner": "team", "gcp-tag-env": "test"}

    @pytest.mark.asyncio
    async def test_no_patch_when_there_is_nothing_to_set(self, store_and_storage, mpu):
        store, storage = store_and_storage

        await store.do_complete_multipart_upload(
            mpu,
            [UploadPartResponse(part_number=1, e_tag="e", size_in_bytes=1)],
            correlation_id="cid",
        )

        storage.patch_metadata.assert_not_awaited()


class TestAbort:
    @pytest.mark.asyncio
    async def test_deletes_every_scratch_object_for_the_session(
        self, store_and_storage, mpu
    ):
        store, storage = store_and_storage
        storage.list_objects.return_value = {
            "items": [{"name": _part_name(1)}, {"name": _part_name(2)}]
        }

        await store.do_abort_multipart_upload(mpu, correlation_id="cid")

        assert storage.list_objects.await_args.kwargs["params"]["prefix"] == (
            f"{KEY}__multipart__{UPLOAD_ID}__"
        )
        deleted = {call.args[1] for call in storage.delete.await_args_list}
        assert deleted == {_part_name(1), _part_name(2)}

    @pytest.mark.asyncio
    async def test_aborting_a_session_with_no_parts_is_a_no_op(
        self, store_and_storage, mpu
    ):
        store, storage = store_and_storage
        storage.list_objects.return_value = {"items": []}

        await store.do_abort_multipart_upload(mpu, correlation_id="cid")

        storage.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_part_deleted_concurrently_does_not_fail_the_abort(
        self, store_and_storage, mpu
    ):
        import aiohttp

        store, storage = store_and_storage
        storage.list_objects.return_value = {"items": [{"name": _part_name(1)}]}
        storage.delete.side_effect = aiohttp.ClientResponseError(
            _request_info(), (), status=404, message="Not Found"
        )

        await store.do_abort_multipart_upload(mpu, correlation_id="cid")

    @pytest.mark.asyncio
    async def test_a_delete_that_was_refused_fails_the_abort(
        self, store_and_storage, mpu
    ):
        """Reporting success here would say the parts were discarded when the
        caller still owns them."""
        import aiohttp

        store, storage = store_and_storage
        storage.list_objects.return_value = {"items": [{"name": _part_name(1)}]}
        storage.delete.side_effect = aiohttp.ClientResponseError(
            _request_info(), (), status=403, message="Forbidden"
        )

        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_abort_multipart_upload(mpu, correlation_id="cid")

    @pytest.mark.asyncio
    async def test_every_page_of_parts_is_deleted(self, store_and_storage, mpu):
        """A page caps at 1000 items, so a larger upload needs following."""
        store, storage = store_and_storage
        storage.list_objects.side_effect = [
            {"items": [{"name": _part_name(1)}], "nextPageToken": "page-2"},
            {"items": [{"name": _part_name(2)}]},
        ]

        await store.do_abort_multipart_upload(mpu, correlation_id="cid")

        assert storage.list_objects.await_count == 2
        assert storage.list_objects.await_args.kwargs["params"]["pageToken"] == "page-2"
        deleted = {call.args[1] for call in storage.delete.await_args_list}
        assert deleted == {_part_name(1), _part_name(2)}


class TestUploadPartStreams:
    """A stream part is read off the event loop, and short reads are refused."""

    @pytest.mark.asyncio
    async def test_stream_is_read_without_blocking_the_loop(
        self, store_and_storage, mpu
    ):
        import asyncio

        store, storage = store_and_storage
        calls = []
        real = asyncio.to_thread

        async def spy(fn, *a, **kw):
            calls.append(fn)
            return await real(fn, *a, **kw)

        with patch("asyncio.to_thread", spy):
            await store.do_upload_part(
                mpu,
                MultipartPart(
                    part_number=1, content=io.BytesIO(b"hello"), content_length=5
                ),
                correlation_id="cid",
            )
        assert calls, "stream read was not offloaded"
        assert storage.upload.await_args.kwargs["file_data"] == b"hello"

    @pytest.mark.asyncio
    async def test_an_exhausted_stream_is_refused_rather_than_truncating(
        self, store_and_storage, mpu
    ):
        store, storage = store_and_storage
        spent = io.BytesIO(b"hello")
        spent.read()

        with pytest.raises(InvalidArgumentException, match="cannot be"):
            await store.do_upload_part(
                mpu,
                MultipartPart(part_number=1, content=spent, content_length=5),
                correlation_id="cid",
            )
        storage.upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_size_reported_is_what_was_sent(self, store_and_storage, mpu):
        store, storage = store_and_storage

        result = await store.do_upload_part(
            mpu, MultipartPart(part_number=1, content=b"1234567"), correlation_id="cid"
        )

        assert result.size_in_bytes == 7


class TestCleanupFailuresAreVisible:
    """Scratch objects left behind are billed, so cleanup failures are logged."""

    @pytest.mark.asyncio
    async def test_a_failed_cleanup_warns_rather_than_passing_silently(
        self, store_and_storage, mpu, caplog
    ):
        import logging

        store, storage = store_and_storage
        storage.delete.side_effect = aiohttp.ClientResponseError(
            _request_info(), (), status=403, message="Forbidden"
        )
        parts = [UploadPartResponse(part_number=1, e_tag="e", size_in_bytes=1)]

        with caplog.at_level(logging.WARNING):
            await store.do_complete_multipart_upload(mpu, parts, correlation_id="cid")

        assert any("left behind" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_cleanup_failure_does_not_fail_a_completed_upload(
        self, store_and_storage, mpu
    ):
        """The object is already durable; raising would misreport it."""
        store, storage = store_and_storage
        storage.delete.side_effect = RuntimeError("transport blew up")
        parts = [UploadPartResponse(part_number=1, e_tag="e", size_in_bytes=1)]

        result = await store.do_complete_multipart_upload(
            mpu, parts, correlation_id="cid"
        )

        assert result.e_tag == "final-etag"


class TestUninitialisedClient:
    """Each multipart hook reports an uninitialised client rather than an
    AttributeError, since the client is created lazily."""

    @pytest.fixture
    def bare(self):
        store, _ = _make_store()
        store._storage = None
        store._ensure_client = AsyncMock()
        return store

    @pytest.mark.asyncio
    async def test_upload_part(self, bare, mpu):
        with pytest.raises(RuntimeError, match="not initialized"):
            await bare.do_upload_part(
                mpu, MultipartPart(part_number=1, content=b"x"), correlation_id="cid"
            )

    @pytest.mark.asyncio
    async def test_complete(self, bare, mpu):
        with pytest.raises(RuntimeError, match="not initialized"):
            await bare.do_complete_multipart_upload(
                mpu,
                [UploadPartResponse(part_number=1, e_tag="e", size_in_bytes=1)],
                correlation_id="cid",
            )

    @pytest.mark.asyncio
    async def test_abort(self, bare, mpu):
        with pytest.raises(RuntimeError, match="not initialized"):
            await bare.do_abort_multipart_upload(mpu, correlation_id="cid")

    @pytest.mark.asyncio
    async def test_the_delete_helpers_are_inert_without_a_client(self, bare):
        """Reached from a finally block, so they must not raise on their own."""
        await bare._delete_scratch_objects("b", ["k"])
        await bare._delete_objects_quietly("b", ["k"])
