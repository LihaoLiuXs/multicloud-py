"""Unit tests for the async multipart upload lifecycle.

Covers the cloud-agnostic layers — the driver's validation and delegation, the
default hooks, and the client's error translation. Provider behaviour is tested
in test_async_aws_multipart.py and test_async_gcp_multipart.py.
"""

from __future__ import annotations

import io
from typing import List, Mapping, Optional
from unittest.mock import AsyncMock

import pytest
from multicloud.blob.blob_client.async_blob.async_client import AsyncBucketClient
from multicloud.blob.blob_client.async_blob.async_driver import (
    ASYNC_BLOB_REGISTRY,
    BaseAsyncBlobStore,
    MultipartPart,
    MultipartUpload,
    MultipartUploadResponse,
    UploadPartResponse,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
    UnknownException,
    UnsupportedOperationException,
)

BUCKET = "test-bucket"


class RecordingStore(BaseAsyncBlobStore):
    """Store that records what the driver passed to each hook."""

    provider_id = "recording-mpu"

    def __init__(self, **kwargs) -> None:
        super().__init__(
            bucket=kwargs.get("bucket", BUCKET),
            region=kwargs.get("region", "us-west-2"),
        )
        self.calls: list[tuple] = []

    async def do_initiate_multipart_upload(
        self,
        key: str,
        *,
        metadata: Mapping[str, str],
        content_type: Optional[str],
        tags: Mapping[str, str],
        correlation_id: str,
    ) -> MultipartUpload:
        self.calls.append(("initiate", key, dict(metadata), content_type, dict(tags)))
        return MultipartUpload(
            bucket=self._bucket,
            key=key,
            upload_id="upload-1",
            metadata=metadata,
            content_type=content_type,
            tags=tags,
            correlation_id=correlation_id,
        )

    async def do_upload_part(
        self, mpu: MultipartUpload, part: MultipartPart, *, correlation_id: str
    ) -> UploadPartResponse:
        self.calls.append(("upload_part", part.part_number))
        return UploadPartResponse(
            part_number=part.part_number,
            e_tag=f"etag-{part.part_number}",
            size_in_bytes=part.content_length or 0,
            correlation_id=correlation_id,
        )

    async def do_complete_multipart_upload(
        self,
        mpu: MultipartUpload,
        parts: List[UploadPartResponse],
        *,
        correlation_id: str,
    ) -> MultipartUploadResponse:
        self.calls.append(("complete", [p.part_number for p in parts]))
        return MultipartUploadResponse(
            e_tag="final", key=mpu.key, correlation_id=correlation_id
        )

    async def do_abort_multipart_upload(
        self, mpu: MultipartUpload, *, correlation_id: str
    ) -> None:
        self.calls.append(("abort", mpu.upload_id))

    # Unused abstract surface.
    async def do_upload(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_download(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_delete(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_delete_objects(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_copy(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_get_metadata(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_list(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_get_tags(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_set_tags(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_does_object_exist(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_does_bucket_exist(self):  # pragma: no cover
        raise NotImplementedError

    async def do_list_page(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_upload_directory(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_download_directory(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def do_delete_directory(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        if isinstance(exc, SubstrateSdkException):
            return exc
        return UnknownException(str(exc))

    async def close(self) -> None:
        return None


class BareStore(RecordingStore):
    """Store that does not implement multipart, to exercise the defaults."""

    do_initiate_multipart_upload = (
        BaseAsyncBlobStore.do_initiate_multipart_upload  # type: ignore[assignment]
    )
    do_upload_part = BaseAsyncBlobStore.do_upload_part  # type: ignore[assignment]
    do_complete_multipart_upload = (
        BaseAsyncBlobStore.do_complete_multipart_upload  # type: ignore[assignment]
    )
    do_abort_multipart_upload = (
        BaseAsyncBlobStore.do_abort_multipart_upload  # type: ignore[assignment]
    )


@pytest.fixture
def store() -> RecordingStore:
    return RecordingStore()


@pytest.fixture
def mpu() -> MultipartUpload:
    return MultipartUpload(bucket=BUCKET, key="obj.bin", upload_id="upload-1")


class TestMultipartPartModel:
    """MultipartPart infers or checks content_length so providers need not."""

    def test_length_is_inferred_from_bytes(self):
        part = MultipartPart(part_number=1, content=b"hello")
        assert part.content_length == 5

    def test_mismatched_length_is_rejected(self):
        with pytest.raises(ValueError, match="does not match"):
            MultipartPart(part_number=1, content=b"hello", content_length=99)

    def test_stream_without_length_is_rejected(self):
        with pytest.raises(ValueError, match="content_length is required"):
            MultipartPart(part_number=1, content=io.BytesIO(b"hello"))

    def test_stream_with_length_is_accepted(self):
        part = MultipartPart(
            part_number=1, content=io.BytesIO(b"hello"), content_length=5
        )
        assert part.content_length == 5


class TestDriverValidation:
    """The driver rejects bad input before any request is made."""

    @pytest.mark.asyncio
    async def test_blank_key_is_rejected(self, store):
        with pytest.raises(SubstrateSdkException):
            await store.initiate_multipart_upload("")
        assert store.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, -1])
    async def test_part_number_below_one_is_rejected(self, store, mpu, bad):
        with pytest.raises(InvalidArgumentException, match="part_number must be"):
            await store.upload_part(mpu, MultipartPart(part_number=bad, content=b"x"))
        assert store.calls == []

    @pytest.mark.asyncio
    async def test_the_ceiling_is_left_to_the_provider(self, store, mpu):
        """Providers cap part counts differently, so the driver does not guess."""
        result = await store.upload_part(
            mpu, MultipartPart(part_number=100_000, content=b"x")
        )
        assert result.part_number == 100_000

    @pytest.mark.asyncio
    async def test_completing_with_no_parts_is_rejected(self, store, mpu):
        with pytest.raises(InvalidArgumentException, match="parts is required"):
            await store.complete_multipart_upload(mpu, [])
        assert store.calls == []

    @pytest.mark.asyncio
    async def test_completing_validates_each_part_number(self, store, mpu):
        parts = [
            UploadPartResponse(part_number=1, e_tag="a", size_in_bytes=1),
            UploadPartResponse(part_number=0, e_tag="b", size_in_bytes=1),
        ]
        with pytest.raises(InvalidArgumentException, match="part_number must be"):
            await store.complete_multipart_upload(mpu, parts)
        assert store.calls == []

    @pytest.mark.asyncio
    async def test_handle_from_another_bucket_is_rejected(self, store):
        foreign = MultipartUpload(
            bucket="someone-elses-bucket", key="obj.bin", upload_id="u"
        )
        with pytest.raises(SubstrateSdkException):
            await store.abort_multipart_upload(foreign)
        assert store.calls == []


class TestDriverDelegation:
    """The driver forwards normalized arguments and mints correlation ids."""

    @pytest.mark.asyncio
    async def test_initiate_defaults_metadata_and_tags_to_empty(self, store):
        await store.initiate_multipart_upload("obj.bin")
        assert store.calls == [("initiate", "obj.bin", {}, None, {})]

    @pytest.mark.asyncio
    async def test_initiate_forwards_metadata_content_type_and_tags(self, store):
        await store.initiate_multipart_upload(
            "obj.bin", metadata={"a": "1"}, content_type="text/plain", tags={"t": "v"}
        )
        assert store.calls == [
            ("initiate", "obj.bin", {"a": "1"}, "text/plain", {"t": "v"})
        ]

    @pytest.mark.asyncio
    async def test_each_call_gets_a_distinct_correlation_id(self, store):
        first = await store.initiate_multipart_upload("a.bin")
        second = await store.initiate_multipart_upload("b.bin")
        assert first.correlation_id and second.correlation_id
        assert first.correlation_id != second.correlation_id

    @pytest.mark.asyncio
    async def test_complete_passes_parts_through(self, store, mpu):
        parts = [
            UploadPartResponse(part_number=2, e_tag="b", size_in_bytes=1),
            UploadPartResponse(part_number=1, e_tag="a", size_in_bytes=1),
        ]
        result = await store.complete_multipart_upload(mpu, parts)
        assert store.calls == [("complete", [2, 1])]
        assert result.e_tag == "final"


class TestDefaultHooks:
    """A provider without multipart reports it rather than failing obscurely."""

    @pytest.mark.asyncio
    async def test_initiate_is_unsupported(self):
        with pytest.raises(UnsupportedOperationException, match="initiate"):
            await BareStore().initiate_multipart_upload("obj.bin")

    @pytest.mark.asyncio
    async def test_upload_part_is_unsupported(self, mpu):
        with pytest.raises(UnsupportedOperationException, match="upload_part"):
            await BareStore().upload_part(
                mpu, MultipartPart(part_number=1, content=b"x")
            )

    @pytest.mark.asyncio
    async def test_complete_is_unsupported(self, mpu):
        with pytest.raises(UnsupportedOperationException, match="complete"):
            await BareStore().complete_multipart_upload(
                mpu, [UploadPartResponse(part_number=1, e_tag="a", size_in_bytes=1)]
            )

    @pytest.mark.asyncio
    async def test_abort_is_unsupported(self, mpu):
        with pytest.raises(UnsupportedOperationException, match="abort"):
            await BareStore().abort_multipart_upload(mpu)


ASYNC_BLOB_REGISTRY.register("recording-mpu")(RecordingStore)


class TestClientForwarding:
    """The client forwards to the store and translates provider errors."""

    @pytest.fixture
    def client(self) -> AsyncBucketClient:
        return AsyncBucketClient(
            provider="recording-mpu", bucket=BUCKET, region="us-west-2"
        )

    @pytest.fixture
    def store(self, client) -> RecordingStore:
        return client._store

    @pytest.mark.asyncio
    async def test_lifecycle_reaches_the_store(self, client, store):
        mpu = await client.initiate_multipart_upload("obj.bin")
        part = await client.upload_part(
            mpu, MultipartPart(part_number=1, content=b"data")
        )
        await client.complete_multipart_upload(mpu, [part])
        await client.abort_multipart_upload(mpu)
        assert [c[0] for c in store.calls] == [
            "initiate",
            "upload_part",
            "complete",
            "abort",
        ]

    @pytest.mark.asyncio
    async def test_sdk_exceptions_pass_through_untranslated(self, client, store):
        boom = InvalidArgumentException("already an sdk error")
        store.do_initiate_multipart_upload = AsyncMock(side_effect=boom)
        with pytest.raises(InvalidArgumentException) as caught:
            await client.initiate_multipart_upload("obj.bin")
        assert caught.value is boom

    @pytest.mark.asyncio
    async def test_provider_errors_are_translated(self, client, store):
        store.do_upload_part = AsyncMock(side_effect=RuntimeError("connection reset"))
        mpu = MultipartUpload(bucket=BUCKET, key="obj.bin", upload_id="u")
        with pytest.raises(UnknownException):
            await client.upload_part(mpu, MultipartPart(part_number=1, content=b"data"))


class TestDuplicatePartNumbers:
    """A repeated part number is refused before it reaches a provider.

    One provider rejects it server-side; another builds its assembly list from
    part numbers and would include the same part twice, producing a corrupt
    object with no error.
    """

    @pytest.mark.asyncio
    async def test_repeated_part_number_is_rejected(self, store, mpu):
        parts = [
            UploadPartResponse(part_number=1, e_tag="a", size_in_bytes=1),
            UploadPartResponse(part_number=2, e_tag="b", size_in_bytes=1),
            UploadPartResponse(part_number=1, e_tag="c", size_in_bytes=1),
        ]
        with pytest.raises(InvalidArgumentException, match="appears more than once"):
            await store.complete_multipart_upload(mpu, parts)
        assert store.calls == []

    @pytest.mark.asyncio
    async def test_distinct_part_numbers_are_accepted_in_any_order(self, store, mpu):
        parts = [
            UploadPartResponse(part_number=3, e_tag="c", size_in_bytes=1),
            UploadPartResponse(part_number=1, e_tag="a", size_in_bytes=1),
        ]
        await store.complete_multipart_upload(mpu, parts)
        assert store.calls == [("complete", [3, 1])]


class TestAbortCorrelationId:
    """Abort is the cleanup path, so it needs to be traceable like the rest."""

    @pytest.mark.asyncio
    async def test_abort_receives_a_correlation_id(self, mpu):
        received = {}

        class Store(RecordingStore):
            async def do_abort_multipart_upload(self, mpu, *, correlation_id):
                received["id"] = correlation_id

        await Store().abort_multipart_upload(mpu)
        assert received["id"]

    @pytest.mark.asyncio
    async def test_each_abort_gets_its_own(self, mpu):
        seen = []

        class Store(RecordingStore):
            async def do_abort_multipart_upload(self, mpu, *, correlation_id):
                seen.append(correlation_id)

        store = Store()
        await store.abort_multipart_upload(mpu)
        await store.abort_multipart_upload(mpu)
        assert len(set(seen)) == 2
