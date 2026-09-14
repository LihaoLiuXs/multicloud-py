"""Unit tests for async ``list_parts``.

Covers the driver's validation and delegation and each provider's listing,
with pagination in particular: an upload can hold more parts than one page
returns, and a single request would silently report a short list.
"""

from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest
from multicloud.blob.blob_client.async_blob.async_driver import (
    BaseAsyncBlobStore,
    MultipartUpload,
    UploadPartResponse,
)
from multicloud.common.exceptions import (
    SubstrateSdkException,
    UnsupportedOperationException,
)

BUCKET = "test-bucket"
KEY = "obj.bin"
UPLOAD_ID = "abc123"


@pytest.fixture
def mpu() -> MultipartUpload:
    return MultipartUpload(bucket=BUCKET, key=KEY, upload_id=UPLOAD_ID)


class TestDriver:
    """The driver validates the handle and stamps a correlation id."""

    class Store(BaseAsyncBlobStore):
        provider_id = "list-parts-test"

        def __init__(self, **kwargs) -> None:
            super().__init__(
                bucket=kwargs.get("bucket", BUCKET),
                region=kwargs.get("region", "us-west-2"),
            )
            self.seen: list = []

        async def do_list_parts(self, mpu, *, correlation_id):
            self.seen.append(correlation_id)
            return [
                UploadPartResponse(
                    part_number=1,
                    e_tag="e",
                    size_in_bytes=1,
                    correlation_id=correlation_id,
                )
            ]

        def translate_error(self, exc: Exception) -> SubstrateSdkException:
            raise NotImplementedError

        async def do_upload(self, *a, **k):
            raise NotImplementedError

        async def do_download(self, *a, **k):
            raise NotImplementedError

        async def do_delete(self, *a, **k):
            raise NotImplementedError

        async def do_delete_objects(self, *a, **k):
            raise NotImplementedError

        async def do_copy(self, *a, **k):
            raise NotImplementedError

        async def do_get_metadata(self, *a, **k):
            raise NotImplementedError

        async def do_list(self, *a, **k):
            raise NotImplementedError

        async def do_list_page(self, *a, **k):
            raise NotImplementedError

        async def do_get_tags(self, *a, **k):
            raise NotImplementedError

        async def do_set_tags(self, *a, **k):
            raise NotImplementedError

        async def do_does_object_exist(self, *a, **k):
            raise NotImplementedError

        async def do_does_bucket_exist(self):
            raise NotImplementedError

        async def do_upload_directory(self, *a, **k):
            raise NotImplementedError

        async def do_download_directory(self, *a, **k):
            raise NotImplementedError

        async def do_delete_directory(self, *a, **k):
            raise NotImplementedError

        async def close(self) -> None:
            return None

    @pytest.mark.asyncio
    async def test_a_handle_from_another_bucket_is_rejected(self):
        store = self.Store()
        foreign = MultipartUpload(
            bucket="someone-elses-bucket", key=KEY, upload_id=UPLOAD_ID
        )
        with pytest.raises(SubstrateSdkException):
            await store.list_parts(foreign)
        assert store.seen == []

    @pytest.mark.asyncio
    async def test_each_call_gets_its_own_correlation_id(self, mpu):
        store = self.Store()
        await store.list_parts(mpu)
        await store.list_parts(mpu)
        assert len(set(store.seen)) == 2

    @pytest.mark.asyncio
    async def test_unsupported_by_default(self, mpu):
        """A provider without multipart reports it rather than failing oddly."""

        class Bare(self.Store):
            do_list_parts = BaseAsyncBlobStore.do_list_parts

        with pytest.raises(UnsupportedOperationException, match="list_parts"):
            await Bare().list_parts(mpu)


class TestAws:
    """S3 returns at most 1000 parts per response and marks truncation."""

    @staticmethod
    def _store():
        pytest.importorskip("aiobotocore")
        from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import (
            AwsAsyncBlobStore,
        )

        client = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.create_client.return_value = cm
        return (
            AwsAsyncBlobStore(bucket=BUCKET, region="us-west-2", _session=session),
            client,
        )

    @pytest.mark.asyncio
    async def test_no_parts_yet_is_an_empty_list(self, mpu):
        store, client = self._store()
        client.list_parts.return_value = {"Parts": [], "IsTruncated": False}

        assert await store.do_list_parts(mpu, correlation_id="cid") == []

    @pytest.mark.asyncio
    async def test_maps_part_fields_and_unquotes_the_etag(self, mpu):
        store, client = self._store()
        client.list_parts.return_value = {
            "Parts": [{"PartNumber": 2, "ETag": '"abc123"', "Size": 5242880}],
            "IsTruncated": False,
        }

        parts = await store.do_list_parts(mpu, correlation_id="cid")

        assert parts == [
            UploadPartResponse(
                part_number=2,
                e_tag="abc123",
                size_in_bytes=5242880,
                correlation_id="cid",
            )
        ]

    @pytest.mark.asyncio
    async def test_every_page_is_fetched(self, mpu):
        """A single request would report a short list for a long upload."""
        store, client = self._store()
        client.list_parts.side_effect = [
            {
                "Parts": [{"PartNumber": 1, "ETag": '"a"', "Size": 1}],
                "IsTruncated": True,
                "NextPartNumberMarker": 1,
            },
            {
                "Parts": [{"PartNumber": 2, "ETag": '"b"', "Size": 2}],
                "IsTruncated": False,
            },
        ]

        parts = await store.do_list_parts(mpu, correlation_id="cid")

        assert [p.part_number for p in parts] == [1, 2]
        assert client.list_parts.await_count == 2
        assert client.list_parts.await_args.kwargs["PartNumberMarker"] == 1

    @pytest.mark.asyncio
    async def test_result_is_ordered_by_part_number(self, mpu):
        store, client = self._store()
        client.list_parts.return_value = {
            "Parts": [
                {"PartNumber": 3, "ETag": '"c"', "Size": 30},
                {"PartNumber": 1, "ETag": '"a"', "Size": 10},
            ],
            "IsTruncated": False,
        }

        parts = await store.do_list_parts(mpu, correlation_id="cid")

        assert [p.part_number for p in parts] == [1, 3]

    @pytest.mark.asyncio
    async def test_each_part_keeps_its_own_size_and_etag(self, mpu):
        """Sorting must carry the fields with the part, not reindex them."""
        store, client = self._store()
        client.list_parts.return_value = {
            "Parts": [
                {"PartNumber": 3, "ETag": '"c"', "Size": 30},
                {"PartNumber": 1, "ETag": '"a"', "Size": 10},
                {"PartNumber": 2, "ETag": '"b"', "Size": 20},
            ],
            "IsTruncated": False,
        }

        parts = await store.do_list_parts(mpu, correlation_id="cid")

        assert [(p.part_number, p.size_in_bytes, p.e_tag) for p in parts] == [
            (1, 10, "a"),
            (2, 20, "b"),
            (3, 30, "c"),
        ]


class TestGcp:
    """Parts are scratch objects, so listing is a prefix scan."""

    @staticmethod
    def _store():
        pytest.importorskip("gcloud.aio.storage")
        from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import (
            GcpAsyncBlobStore,
        )

        storage = AsyncMock()
        return (
            GcpAsyncBlobStore(bucket=BUCKET, region="us-west1", _storage=storage),
            storage,
        )

    @staticmethod
    def _item(part_number, size=1):
        return {
            "name": f"{KEY}__multipart__{UPLOAD_ID}__{part_number}",
            "etag": f"etag-{part_number}",
            "size": str(size),
        }

    @pytest.mark.asyncio
    async def test_no_parts_yet_is_an_empty_list(self, mpu):
        store, storage = self._store()
        storage.list_objects.return_value = {"items": []}

        assert await store.do_list_parts(mpu, correlation_id="cid") == []

    @pytest.mark.asyncio
    async def test_maps_part_fields(self, mpu):
        store, storage = self._store()
        storage.list_objects.return_value = {"items": [self._item(2, size=5242880)]}

        parts = await store.do_list_parts(mpu, correlation_id="cid")

        assert parts == [
            UploadPartResponse(
                part_number=2,
                e_tag="etag-2",
                size_in_bytes=5242880,
                correlation_id="cid",
            )
        ]

    @pytest.mark.asyncio
    async def test_compose_intermediates_are_not_reported_as_parts(self, mpu):
        """A batched compose leaves objects under the same prefix."""
        store, storage = self._store()
        storage.list_objects.return_value = {
            "items": [
                self._item(1),
                {
                    "name": f"{KEY}__multipart__{UPLOAD_ID}__intermediate_0",
                    "etag": "x",
                    "size": "99",
                },
            ]
        }

        parts = await store.do_list_parts(mpu, correlation_id="cid")

        assert [p.part_number for p in parts] == [1]

    @pytest.mark.asyncio
    async def test_every_page_is_fetched(self, mpu):
        """A page caps at 1000 items while an upload may hold more."""
        store, storage = self._store()
        storage.list_objects.side_effect = [
            {"items": [self._item(1)], "nextPageToken": "page-2"},
            {"items": [self._item(2)]},
        ]

        parts = await store.do_list_parts(mpu, correlation_id="cid")

        assert [p.part_number for p in parts] == [1, 2]
        assert storage.list_objects.await_count == 2
        assert storage.list_objects.await_args.kwargs["params"]["pageToken"] == "page-2"

    @pytest.mark.asyncio
    async def test_result_is_ordered_by_part_number(self, mpu):
        store, storage = self._store()
        storage.list_objects.return_value = {
            "items": [self._item(10), self._item(2), self._item(1)]
        }

        parts = await store.do_list_parts(mpu, correlation_id="cid")

        assert [p.part_number for p in parts] == [1, 2, 10]

    @pytest.mark.asyncio
    async def test_each_part_keeps_its_own_size_and_etag(self, mpu):
        """Sorting must carry the fields with the part, not reindex them."""
        store, storage = self._store()
        storage.list_objects.return_value = {
            "items": [self._item(3, size=30), self._item(1, size=10)]
        }

        parts = await store.do_list_parts(mpu, correlation_id="cid")

        assert [(p.part_number, p.size_in_bytes, p.e_tag) for p in parts] == [
            (1, 10, "etag-1"),
            (3, 30, "etag-3"),
        ]

    @pytest.mark.asyncio
    async def test_reports_uninitialised_client(self, mpu):
        store, _ = self._store()
        store._storage = None
        store._ensure_client = AsyncMock()

        with pytest.raises(RuntimeError, match="not initialized"):
            await store.do_list_parts(mpu, correlation_id="cid")
