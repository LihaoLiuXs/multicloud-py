"""Unit tests for async does_object_exist and does_bucket_exist APIs.

Tests validate the client and driver layer:
- does_object_exist returns True when object found
- does_object_exist returns False for 404
- does_object_exist with version_id
- does_object_exist raises for auth failures
- does_object_exist raises InvalidArgumentException for empty key
- does_bucket_exist returns True
- does_bucket_exist returns False for 404
- does_bucket_exist raises for auth failures
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
    BlobMetadata,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
    UnAuthorizedException,
    UnknownException,
)

# Test provider for exists tests


class FakeAsyncBlobStore(BaseAsyncBlobStore):
    """Fake provider for testing existence checks."""

    provider_id = "fake-exists-test"

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
        self.closed = False
        # Track calls for verification
        self.object_exist_calls: list[dict] = []
        self.bucket_exist_calls: int = 0
        # Control test behavior
        self.object_exists_response: Optional[bool] = None
        self.bucket_exists_response: Optional[bool] = None
        self.object_exists_error: Optional[Exception] = None
        self.bucket_exists_error: Optional[Exception] = None

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        if isinstance(exc, SubstrateSdkException):
            return exc
        return UnknownException(str(exc))

    async def do_upload(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: Optional[str],
        content_length: int,
        metadata: Mapping[str, str],
    ) -> AsyncUploadResponse:
        return AsyncUploadResponse(key=key, e_tag="fake-etag", version_id="fake-v1")

    async def do_download(self, request: AsyncDownloadRequest) -> AsyncDownloadResponse:
        return AsyncDownloadResponse(
            key=request.key,
            content=b"fake",
            metadata=BlobMetadata(key=request.key, content_length=4),
        )

    async def do_delete(self, key: str, *, version_id: Optional[str] = None) -> None:
        pass

    async def do_delete_objects(self, identifiers):
        from multicloud.blob.blob_client.async_blob.async_driver import (
            BulkDeleteResponse,
            DeletedBlob,
        )

        return BulkDeleteResponse(
            deleted=[
                DeletedBlob(key=i.key, version_id=i.version_id) for i in identifiers
            ],
            errors=[],
        )

    async def do_get_tags(self, key: str) -> Mapping[str, str]:
        return {}

    async def do_set_tags(self, key: str, tags: Mapping[str, str]) -> None:
        pass

    async def do_copy(self, request, *, correlation_id):
        from multicloud.blob.blob_client.async_blob.async_driver import CopyResponse

        return CopyResponse(key=request.destination_key, correlation_id=correlation_id)

    async def do_upload_directory(self, local_source_directory, **kwargs):
        raise NotImplementedError

    async def do_download_directory(
        self, prefix, local_destination_directory, **kwargs
    ):
        raise NotImplementedError

    async def do_delete_directory(self, prefix, **kwargs):
        raise NotImplementedError

    async def do_does_object_exist(
        self, key: str, *, version_id: Optional[str]
    ) -> bool:
        self.object_exist_calls.append({"key": key, "version_id": version_id})
        if self.object_exists_error is not None:
            raise self.object_exists_error
        if self.object_exists_response is not None:
            return self.object_exists_response
        return True

    async def do_get_metadata(self, key, *, version_id=None):
        from multicloud.blob.blob_client.async_blob.async_driver import BlobMetadata

        return BlobMetadata(key=key)

    async def do_does_bucket_exist(self) -> bool:
        self.bucket_exist_calls += 1
        if self.bucket_exists_error is not None:
            raise self.bucket_exists_error
        if self.bucket_exists_response is not None:
            return self.bucket_exists_response
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


# Register the fake provider for tests
ASYNC_BLOB_REGISTRY.register("fake-exists-test")(FakeAsyncBlobStore)


# Fixtures


@pytest.fixture
def client():
    """Create a test AsyncBucketClient."""
    return AsyncBucketClient(
        provider="fake-exists-test",
        bucket="test-bucket",
        region="us-west-2",
    )


@pytest.fixture
def store():
    """Create a test FakeAsyncBlobStore directly."""
    return FakeAsyncBlobStore(bucket="test-bucket")


# Tests for does_object_exist - Client layer


@pytest.mark.asyncio
class TestAsyncClientObjectExists:
    """Tests for AsyncBucketClient.does_object_exist."""

    async def test_object_exists_returns_true(self, client):
        """Object exists returns True."""
        result = await client.does_object_exist("test-key.txt")
        assert result is True

    async def test_object_exists_returns_false(self, client):
        """Object not found returns False."""
        # Configure the underlying store to return False
        store = client._store
        store.object_exists_response = False
        result = await client.does_object_exist("missing-key.txt")
        assert result is False

    async def test_object_exists_with_version_id(self, client):
        """Object exists with version_id."""
        result = await client.does_object_exist("test-key.txt", version_id="v123")
        assert result is True
        # Verify version_id was passed through
        store = client._store
        assert len(store.object_exist_calls) == 1
        assert store.object_exist_calls[0]["key"] == "test-key.txt"
        assert store.object_exist_calls[0]["version_id"] == "v123"

    async def test_object_exists_raises_for_empty_key(self, client):
        """Empty key raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException, match="Object name cannot be null or empty"
        ):
            await client.does_object_exist("")

    async def test_object_exists_raises_for_auth_error(self, client):
        """Auth failure raises UnAuthorizedException."""
        store = client._store
        store.object_exists_error = UnAuthorizedException("forbidden")
        with pytest.raises(UnAuthorizedException, match="forbidden"):
            await client.does_object_exist("test-key.txt")

    async def test_object_exists_translates_non_sdk_exceptions(self, client):
        """Non-SDK exceptions are translated."""
        store = client._store
        store.object_exists_error = RuntimeError("network error")
        with pytest.raises(UnknownException, match="network error"):
            await client.does_object_exist("test-key.txt")


# Tests for does_bucket_exist - Client layer


@pytest.mark.asyncio
class TestAsyncClientBucketExists:
    """Tests for AsyncBucketClient.does_bucket_exist."""

    async def test_bucket_exists_returns_true(self, client):
        """Bucket exists returns True."""
        result = await client.does_bucket_exist()
        assert result is True

    async def test_bucket_exists_returns_false(self, client):
        """Bucket not found returns False."""
        store = client._store
        store.bucket_exists_response = False
        result = await client.does_bucket_exist()
        assert result is False

    async def test_bucket_exists_raises_for_auth_error(self, client):
        """Auth failure raises UnAuthorizedException."""
        store = client._store
        store.bucket_exists_error = UnAuthorizedException("forbidden")
        with pytest.raises(UnAuthorizedException, match="forbidden"):
            await client.does_bucket_exist()

    async def test_bucket_exists_translates_non_sdk_exceptions(self, client):
        """Non-SDK exceptions are translated."""
        store = client._store
        store.bucket_exists_error = RuntimeError("network error")
        with pytest.raises(UnknownException, match="network error"):
            await client.does_bucket_exist()


# Tests for does_object_exist - Driver layer


@pytest.mark.asyncio
class TestAsyncDriverObjectExists:
    """Tests for BaseAsyncBlobStore.does_object_exist."""

    async def test_object_exists_validates_empty_key(self, store):
        """Empty key raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException, match="Object name cannot be null or empty"
        ):
            await store.does_object_exist("")

    async def test_object_exists_delegates_to_do_method(self, store):
        """does_object_exist delegates to do_does_object_exist."""
        result = await store.does_object_exist("test-key.txt")
        assert result is True
        assert len(store.object_exist_calls) == 1
        assert store.object_exist_calls[0]["key"] == "test-key.txt"
        assert store.object_exist_calls[0]["version_id"] is None

    async def test_object_exists_passes_version_id(self, store):
        """version_id is passed to do_does_object_exist."""
        result = await store.does_object_exist("test-key.txt", version_id="v123")
        assert result is True
        assert len(store.object_exist_calls) == 1
        assert store.object_exist_calls[0]["version_id"] == "v123"


# Tests for does_bucket_exist - Driver layer


@pytest.mark.asyncio
class TestAsyncDriverBucketExists:
    """Tests for BaseAsyncBlobStore.does_bucket_exist."""

    async def test_bucket_exists_delegates_to_do_method(self, store):
        """does_bucket_exist delegates to do_does_bucket_exist."""
        result = await store.does_bucket_exist()
        assert result is True
        assert store.bucket_exist_calls == 1
