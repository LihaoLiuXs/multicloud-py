"""Unit tests for AsyncBucketClient delete and delete_objects operations.

Tests validate:
- Single delete delegates to store and is idempotent
- Batch delete delegates to store and returns BulkDeleteResponse
- Input validation (empty key, empty identifiers)
- Error translation for non-SDK exceptions
- BlobIdentifier, DeletedBlob, DeleteError, BulkDeleteResponse models
"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from unittest.mock import AsyncMock

import pytest
from multicloud.blob.blob_client.async_blob.async_client import AsyncBucketClient
from multicloud.blob.blob_client.async_blob.async_driver import (
    ASYNC_BLOB_REGISTRY,
    AsyncDownloadRequest,
    AsyncDownloadResponse,
    AsyncUploadResponse,
    BaseAsyncBlobStore,
    BlobAsyncError,
    BlobIdentifier,
    BlobMetadata,
    BlobNotFoundError,
    BulkDeleteResponse,
    DeletedBlob,
    DeleteError,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
    UnknownException,
)


class FakeDeleteStore(BaseAsyncBlobStore):
    """Fake provider that tracks delete calls for testing."""

    provider_id = "fake-delete-test"

    def __init__(self, **kwargs):
        bucket = kwargs.get("bucket", "test-bucket")
        region = kwargs.get("region")
        endpoint_url = kwargs.get("endpoint_url")
        transfer_config = kwargs.get("transfer_config")
        super().__init__(
            bucket=bucket,
            region=region,
            endpoint_url=endpoint_url,
            transfer_config=transfer_config,
        )
        self.single_deletes: list[dict] = []
        self.bulk_deletes: list[list[BlobIdentifier]] = []
        self.closed = False
        # Configure behavior
        self._raise_on_delete: Optional[Exception] = None
        self._bulk_response: Optional[BulkDeleteResponse] = None

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        return UnknownException(str(exc))

    async def do_upload(self, key, payload, *, content_type, content_length, metadata):
        return AsyncUploadResponse(key=key, e_tag="fake", version_id=None)

    async def do_download(self, request):
        raise BlobNotFoundError(f"Not found: {request.key}", key=request.key)

    async def do_delete(self, key: str, *, version_id: Optional[str] = None) -> None:
        if self._raise_on_delete:
            raise self._raise_on_delete
        self.single_deletes.append({"key": key, "version_id": version_id})

    async def do_delete_objects(
        self, identifiers: list[BlobIdentifier]
    ) -> BulkDeleteResponse:
        self.bulk_deletes.append(list(identifiers))
        if self._bulk_response is not None:
            return self._bulk_response
        # Default: all succeed
        return BulkDeleteResponse(
            deleted=[
                DeletedBlob(key=ident.key, version_id=ident.version_id)
                for ident in identifiers
            ],
            errors=[],
        )

    async def do_copy(self, request, *, correlation_id):
        raise NotImplementedError("copy not used in delete tests")

    async def do_get_tags(self, key: str) -> dict[str, str]:
        return {}

    async def do_set_tags(self, key: str, tags: Mapping[str, str]) -> None:
        pass

    async def do_upload_directory(self, local_source_directory, **kwargs):
        raise NotImplementedError

    async def do_download_directory(
        self, prefix, local_destination_directory, **kwargs
    ):
        raise NotImplementedError

    async def do_delete_directory(self, prefix, **kwargs):
        raise NotImplementedError

    async def do_does_object_exist(self, key: str, *, version_id=None) -> bool:
        return True

    async def do_get_metadata(self, key, *, version_id=None):
        from multicloud.blob.blob_client.async_blob.async_driver import BlobMetadata

        return BlobMetadata(key=key)

    async def do_does_bucket_exist(self) -> bool:
        return True

    async def do_list_page(
        self, *, prefix=None, delimiter=None, max_results=1000, pagination_token=None
    ):
        from multicloud.blob.blob_client.async_blob.async_driver import (
            ListBlobsPageResponse,
        )

        return ListBlobsPageResponse()

    async def close(self) -> None:
        self.closed = True


# Register the fake provider
ASYNC_BLOB_REGISTRY.register("fake-delete-test")(FakeDeleteStore)


@pytest.fixture
def client():
    """AsyncBucketClient using the fake-delete-test provider."""
    return AsyncBucketClient(
        provider="fake-delete-test",
        bucket="my-bucket",
        region="us-east-1",
    )


@pytest.fixture
def store(client):
    """Access the underlying FakeDeleteStore."""
    return client._store


class TestBlobIdentifier:
    """BlobIdentifier model tests."""

    def test_creation_with_key_only(self):
        ident = BlobIdentifier(key="path/to/file.txt")
        assert ident.key == "path/to/file.txt"
        assert ident.version_id is None

    def test_creation_with_key_and_version(self):
        ident = BlobIdentifier(key="file.txt", version_id="v1")
        assert ident.key == "file.txt"
        assert ident.version_id == "v1"

    def test_is_frozen(self):
        ident = BlobIdentifier(key="k")
        with pytest.raises(AttributeError):
            ident.key = "other"  # type: ignore[misc]


class TestDeletedBlob:
    """DeletedBlob model tests."""

    def test_creation(self):
        d = DeletedBlob(key="hello.txt", version_id="abc123")
        assert d.key == "hello.txt"
        assert d.version_id == "abc123"

    def test_creation_no_version(self):
        d = DeletedBlob(key="hello.txt")
        assert d.version_id is None

    def test_is_frozen(self):
        d = DeletedBlob(key="k")
        with pytest.raises(AttributeError):
            d.key = "other"  # type: ignore[misc]


class TestDeleteError:
    """DeleteError model tests."""

    def test_creation_full(self):
        e = DeleteError(
            key="bad.txt", version_id="v2", code="AccessDenied", message="No access"
        )
        assert e.key == "bad.txt"
        assert e.version_id == "v2"
        assert e.code == "AccessDenied"
        assert e.message == "No access"

    def test_creation_minimal(self):
        e = DeleteError(key="file.txt")
        assert e.code == ""
        assert e.message == ""
        assert e.version_id is None

    def test_is_frozen(self):
        e = DeleteError(key="k")
        with pytest.raises(AttributeError):
            e.key = "other"  # type: ignore[misc]


class TestBulkDeleteResponse:
    """BulkDeleteResponse model tests."""

    def test_no_errors(self):
        resp = BulkDeleteResponse(
            deleted=[DeletedBlob(key="a"), DeletedBlob(key="b")],
            errors=[],
        )
        assert resp.has_errors is False
        assert len(resp.deleted) == 2
        assert len(resp.errors) == 0

    def test_with_errors(self):
        resp = BulkDeleteResponse(
            deleted=[DeletedBlob(key="a")],
            errors=[DeleteError(key="b", code="403", message="Denied")],
        )
        assert resp.has_errors is True
        assert len(resp.deleted) == 1
        assert len(resp.errors) == 1

    def test_empty_response(self):
        resp = BulkDeleteResponse()
        assert resp.has_errors is False
        assert resp.deleted == []
        assert resp.errors == []

    def test_is_frozen(self):
        resp = BulkDeleteResponse()
        with pytest.raises(AttributeError):
            resp.deleted = []  # type: ignore[misc]


class TestAsyncBucketClientDelete:
    """Single delete operation tests."""

    @pytest.mark.asyncio
    async def test_delete_success(self, client, store):
        await client.delete("path/to/file.txt")

        assert len(store.single_deletes) == 1
        assert store.single_deletes[0] == {
            "key": "path/to/file.txt",
            "version_id": None,
        }

    @pytest.mark.asyncio
    async def test_delete_with_version_id(self, client, store):
        await client.delete("file.txt", version_id="version-123")

        assert store.single_deletes[0] == {
            "key": "file.txt",
            "version_id": "version-123",
        }

    @pytest.mark.asyncio
    async def test_delete_idempotent_no_error(self, client, store):
        """Deleting same key twice does not raise."""
        await client.delete("file.txt")
        await client.delete("file.txt")

        assert len(store.single_deletes) == 2

    @pytest.mark.asyncio
    async def test_delete_empty_key_raises(self, client):
        with pytest.raises(InvalidArgumentException, match="cannot be null or empty"):
            await client.delete("")

    @pytest.mark.asyncio
    async def test_delete_propagates_sdk_exception(self, client, store):
        """SubstrateSdkException from store re-raises directly."""
        from multicloud.common.exceptions import ResourceNotFoundException

        store._raise_on_delete = ResourceNotFoundException("bucket gone")

        with pytest.raises(ResourceNotFoundException, match="bucket gone"):
            await client.delete("key")

    @pytest.mark.asyncio
    async def test_delete_translates_generic_exception(self, client, store):
        """Non-SDK exceptions are translated via translate_error."""
        store._raise_on_delete = RuntimeError("connection reset")

        with pytest.raises(UnknownException, match="connection reset"):
            await client.delete("key")

    @pytest.mark.asyncio
    async def test_delete_propagates_blob_async_error(self, client, store):
        """BlobAsyncError (which is a SubstrateSdkException) re-raises."""
        store._raise_on_delete = BlobAsyncError("timeout", cause=None)

        with pytest.raises(BlobAsyncError, match="timeout"):
            await client.delete("key")


class TestAsyncBucketClientDeleteObjects:
    """Bulk delete operation tests."""

    @pytest.mark.asyncio
    async def test_delete_objects_all_success(self, client, store):
        identifiers = [
            BlobIdentifier(key="a.txt"),
            BlobIdentifier(key="b.txt"),
            BlobIdentifier(key="c.txt", version_id="v1"),
        ]
        result = await client.delete_objects(identifiers)

        assert len(store.bulk_deletes) == 1
        assert len(store.bulk_deletes[0]) == 3
        assert result.has_errors is False
        assert len(result.deleted) == 3
        assert result.deleted[0].key == "a.txt"
        assert result.deleted[2].version_id == "v1"

    @pytest.mark.asyncio
    async def test_delete_objects_partial_failure(self, client, store):
        """Partial failure reported via result, no exception raised."""
        store._bulk_response = BulkDeleteResponse(
            deleted=[DeletedBlob(key="a.txt")],
            errors=[
                DeleteError(key="b.txt", code="AccessDenied", message="No permission")
            ],
        )

        identifiers = [
            BlobIdentifier(key="a.txt"),
            BlobIdentifier(key="b.txt"),
        ]
        result = await client.delete_objects(identifiers)

        assert result.has_errors is True
        assert len(result.deleted) == 1
        assert len(result.errors) == 1
        assert result.errors[0].key == "b.txt"
        assert result.errors[0].code == "AccessDenied"

    @pytest.mark.asyncio
    async def test_delete_objects_all_fail(self, client, store):
        """All objects fail — still no exception, reported via errors."""
        store._bulk_response = BulkDeleteResponse(
            deleted=[],
            errors=[
                DeleteError(key="x.txt", code="InternalError", message="oops"),
                DeleteError(key="y.txt", code="InternalError", message="oops"),
            ],
        )

        result = await client.delete_objects(
            [BlobIdentifier(key="x.txt"), BlobIdentifier(key="y.txt")]
        )

        assert result.has_errors is True
        assert len(result.deleted) == 0
        assert len(result.errors) == 2

    @pytest.mark.asyncio
    async def test_delete_objects_empty_list_raises(self, client):
        with pytest.raises(InvalidArgumentException, match="cannot be empty"):
            await client.delete_objects([])

    @pytest.mark.asyncio
    async def test_delete_objects_empty_key_in_identifier_raises(self, client):
        with pytest.raises(InvalidArgumentException, match="has an empty key"):
            await client.delete_objects([BlobIdentifier(key="")])

    @pytest.mark.asyncio
    async def test_delete_objects_propagates_sdk_exception(self, client, store):
        """Total failure (e.g., bucket not found) raises."""
        from multicloud.common.exceptions import ResourceNotFoundException

        # Make store's delete_objects raise instead of returning
        original = store.do_delete_objects

        async def _raise(*args, **kwargs):
            raise ResourceNotFoundException("no such bucket")

        store.do_delete_objects = _raise

        with pytest.raises(ResourceNotFoundException, match="no such bucket"):
            await client.delete_objects([BlobIdentifier(key="a")])

    @pytest.mark.asyncio
    async def test_delete_objects_translates_generic_exception(self, client, store):
        """Non-SDK exceptions are translated."""

        async def _raise(*args, **kwargs):
            raise RuntimeError("network failure")

        store.do_delete_objects = _raise

        with pytest.raises(UnknownException, match="network failure"):
            await client.delete_objects([BlobIdentifier(key="a")])


class TestBaseAsyncBlobStoreDeleteValidation:
    """Test validation at the driver layer."""

    @pytest.mark.asyncio
    async def test_driver_delete_empty_key_raises(self, store):
        with pytest.raises(InvalidArgumentException, match="cannot be null or empty"):
            await store.delete("")

    @pytest.mark.asyncio
    async def test_driver_delete_objects_empty_list_raises(self, store):
        with pytest.raises(InvalidArgumentException, match="cannot be empty"):
            await store.delete_objects([])

    @pytest.mark.asyncio
    async def test_driver_delete_objects_blank_key_raises(self, store):
        with pytest.raises(InvalidArgumentException, match="has an empty key"):
            await store.delete_objects([BlobIdentifier(key="")])

    @pytest.mark.asyncio
    async def test_driver_delete_delegates_to_do_delete(self, store):
        await store.delete("mykey", version_id="v5")

        assert store.single_deletes == [{"key": "mykey", "version_id": "v5"}]

    @pytest.mark.asyncio
    async def test_driver_delete_objects_delegates(self, store):
        idents = [BlobIdentifier(key="a"), BlobIdentifier(key="b", version_id="v1")]
        result = await store.delete_objects(idents)

        assert len(store.bulk_deletes) == 1
        assert result.has_errors is False
        assert len(result.deleted) == 2
