"""Unit tests for AsyncBucketClient.copy() integration.

Tests verify the public client API:
- Delegates to store.copy() correctly
- Translates exceptions from the store layer
- Returns CopyResponse properly
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from multicloud.blob.blob_client.async_blob.async_client import AsyncBucketClient
from multicloud.blob.blob_client.async_blob.async_driver import (
    ASYNC_BLOB_REGISTRY,
    BaseAsyncBlobStore,
    BlobAsyncError,
    CopyRequest,
    CopyResponse,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceNotFoundException,
    SubstrateSdkException,
    UnknownException,
)


class MockAsyncBlobStore(BaseAsyncBlobStore):
    """Mock store for testing client-level behavior."""

    def __init__(self, bucket="test-bucket"):
        super().__init__(bucket=bucket)
        self.copy_mock = AsyncMock()

    async def do_upload(self, key, payload, *, content_type, content_length, metadata):
        pass

    async def do_download(self, request):
        pass

    async def do_copy(self, request, *, correlation_id):
        # Default: return a response using the actual request fields
        if self.copy_mock.side_effect:
            return await self.copy_mock(request, correlation_id=correlation_id)
        await self.copy_mock(request, correlation_id=correlation_id)
        return CopyResponse(
            key=request.destination_key,
            version_id="ver-001",
            e_tag="etag123",
            correlation_id=correlation_id,
        )

    async def do_delete(self, key, *, version_id=None):
        pass

    async def do_delete_objects(self, identifiers):
        from multicloud.blob.blob_client.async_blob.async_driver import (
            BulkDeleteResponse,
        )

        return BulkDeleteResponse()

    async def do_get_tags(self, key):
        return {}

    async def do_set_tags(self, key, tags):
        pass

    async def do_upload_directory(self, local_source_directory, **kwargs):
        raise NotImplementedError

    async def do_download_directory(
        self, prefix, local_destination_directory, **kwargs
    ):
        raise NotImplementedError

    async def do_delete_directory(self, prefix, **kwargs):
        raise NotImplementedError

    async def do_does_object_exist(self, key, *, version_id=None):
        return True

    async def do_get_metadata(self, key, *, version_id=None):
        from multicloud.blob.blob_client.async_blob.async_driver import BlobMetadata

        return BlobMetadata(key=key)

    async def do_does_bucket_exist(self):
        return True

    async def do_list_page(
        self, *, prefix=None, delimiter=None, max_results=1000, pagination_token=None
    ):
        from multicloud.blob.blob_client.async_blob.async_driver import (
            ListBlobsPageResponse,
        )

        return ListBlobsPageResponse()

    async def close(self):
        pass

    @classmethod
    def get_provider_id(cls) -> str:
        return "mock"

    def translate_error(self, exc):
        return UnknownException(str(exc))


@pytest.fixture
def mock_store():
    return MockAsyncBlobStore()


@pytest.fixture
def client(mock_store):
    """AsyncBucketClient with mock store injected."""
    with patch.object(ASYNC_BLOB_REGISTRY, "get", return_value=MockAsyncBlobStore):
        c = AsyncBucketClient(provider="mock", bucket="test-bucket")
        c._store = mock_store
        return c


class TestAsyncClientCopySuccess:
    """Client correctly delegates to store."""

    @pytest.mark.asyncio
    async def test_copy_returns_response(self, client):
        result = await client.copy(
            "src/file.txt",
            dest_bucket="dest-bucket",
            dest_key="dest/file.txt",
        )

        assert isinstance(result, CopyResponse)
        assert result.key == "dest/file.txt"
        assert result.version_id == "ver-001"
        assert result.e_tag == "etag123"
        assert result.correlation_id is not None

    @pytest.mark.asyncio
    async def test_copy_delegates_request_to_store(self, client, mock_store):
        await client.copy(
            "src/file.txt",
            dest_bucket="dest-bucket",
            dest_key="dest/file.txt",
        )

        # The base class copy() validates and calls do_copy
        # Verify the mock was called
        mock_store.copy_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_copy_from_returns_response(self, client):
        result = await client.copy_from(
            "src-bucket",
            "src/file.txt",
            dest_key="dest/file.txt",
        )

        assert isinstance(result, CopyResponse)
        assert result.key == "dest/file.txt"
        assert result.version_id == "ver-001"
        assert result.e_tag == "etag123"
        assert result.correlation_id is not None

    @pytest.mark.asyncio
    async def test_copy_from_delegates_request_to_store(self, client, mock_store):
        await client.copy_from(
            "src-bucket",
            "src/file.txt",
            dest_key="dest/file.txt",
        )

        # The base class copy() validates and calls do_copy
        # Verify the mock was called
        mock_store.copy_mock.assert_called_once()


class TestAsyncClientCopyErrors:
    """Client correctly propagates errors from store."""

    @pytest.mark.asyncio
    async def test_invalid_argument_propagates(self, client):
        """InvalidArgumentException from validation propagates."""
        with pytest.raises(InvalidArgumentException):
            await client.copy(
                "key",
                dest_bucket="",
                dest_key="dkey",
            )

    @pytest.mark.asyncio
    async def test_resource_not_found_propagates(self, client, mock_store):
        """ResourceNotFoundException from store propagates."""
        mock_store.copy_mock.side_effect = ResourceNotFoundException("not found")

        with pytest.raises(ResourceNotFoundException):
            await client.copy(
                "src/file.txt",
                dest_bucket="dest-bucket",
                dest_key="dest/file.txt",
            )

    @pytest.mark.asyncio
    async def test_substrate_sdk_exception_propagates(self, client, mock_store):
        """SubstrateSdkException subclasses propagate directly."""
        mock_store.copy_mock.side_effect = BlobAsyncError("transport error")

        with pytest.raises(BlobAsyncError):
            await client.copy(
                "src/file.txt",
                dest_bucket="dest-bucket",
                dest_key="dest/file.txt",
            )

    @pytest.mark.asyncio
    async def test_unexpected_error_translated(self, client, mock_store):
        """Non-SDK exceptions are translated via translate_error."""
        mock_store.copy_mock.side_effect = RuntimeError("boom")

        # translate_error wraps in UnknownException
        with pytest.raises(UnknownException):
            await client.copy(
                "src/file.txt",
                dest_bucket="dest-bucket",
                dest_key="dest/file.txt",
            )


class TestAsyncClientCopyValidation:
    """Client-level validation tests."""

    @pytest.mark.asyncio
    async def test_empty_source_key(self, client):
        with pytest.raises(InvalidArgumentException, match="source_key"):
            await client.copy(
                "",
                dest_bucket="dst",
                dest_key="dkey",
            )

    @pytest.mark.asyncio
    async def test_empty_destination_bucket(self, client):
        with pytest.raises(InvalidArgumentException, match="destination_bucket"):
            await client.copy(
                "key",
                dest_bucket="",
                dest_key="dkey",
            )

    @pytest.mark.asyncio
    async def test_empty_destination_key(self, client):
        with pytest.raises(InvalidArgumentException, match="destination_key"):
            await client.copy(
                "key",
                dest_bucket="dst",
                dest_key="",
            )

    @pytest.mark.asyncio
    async def test_copy_from_empty_source_bucket(self, client):
        with pytest.raises(InvalidArgumentException, match="source_bucket"):
            await client.copy_from(
                "",
                "key",
                dest_key="dkey",
            )

    @pytest.mark.asyncio
    async def test_copy_from_empty_source_key(self, client):
        with pytest.raises(InvalidArgumentException, match="source_key"):
            await client.copy_from(
                "src",
                "",
                dest_key="dkey",
            )

    @pytest.mark.asyncio
    async def test_copy_from_empty_destination_key(self, client):
        with pytest.raises(InvalidArgumentException, match="destination_key"):
            await client.copy_from(
                "src",
                "key",
                dest_key="",
            )
