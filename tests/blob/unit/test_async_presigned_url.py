"""Unit tests for async presigned URL generation.

Tests validate:
- AsyncBucketClient.generate_presigned_url delegation to store
- BaseAsyncBlobStore validation (operation, expiration, key)
- Error handling and exceptions
- Result structure (url, expiration, correlation_id, http_method, required_headers)
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from multicloud.blob.blob_client.async_blob.async_client import AsyncBucketClient
from multicloud.blob.blob_client.async_blob.async_driver import (
    ASYNC_BLOB_REGISTRY,
    AsyncDownloadRequest,
    AsyncDownloadResponse,
    AsyncUploadResponse,
    BaseAsyncBlobStore,
    BlobAsyncError,
    CopyRequest,
    CopyResponse,
    PresignedUrlResult,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
)

# Fake provider for testing


class FakeAsyncBlobStoreWithPresigned(BaseAsyncBlobStore):
    """Fake provider with presigned URL support."""

    provider_id = "fake-presigned"

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
        self.presigned_calls: list[dict] = []
        self.closed = False

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        from multicloud.common.exceptions import UnknownException

        return UnknownException(str(exc))

    async def do_upload(
        self, key, payload, *, content_type, content_length, metadata
    ) -> AsyncUploadResponse:
        return AsyncUploadResponse(key=key, e_tag="fake-etag")

    async def do_download(self, request) -> AsyncDownloadResponse:
        from multicloud.blob.blob_client.async_blob.async_driver import BlobMetadata

        return AsyncDownloadResponse(
            key=request.key,
            content=b"fake",
            metadata=BlobMetadata(key=request.key),
        )

    async def do_copy(self, request, *, correlation_id) -> CopyResponse:
        return CopyResponse(
            key=request.destination_key,
            correlation_id=correlation_id,
        )

    async def do_does_object_exist(self, key, *, version_id) -> bool:
        return True

    async def do_get_metadata(self, key, *, version_id=None):
        from multicloud.blob.blob_client.async_blob.async_driver import BlobMetadata

        return BlobMetadata(key=key)

    async def do_does_bucket_exist(self) -> bool:
        return True

    async def do_delete(self, key: str, *, version_id: Optional[str]) -> None:
        """Stub delete implementation."""
        pass

    async def do_delete_objects(self, identifiers) -> Any:
        """Stub bulk delete implementation."""
        pass

    async def do_get_tags(self, key: str) -> Mapping[str, str]:
        """Stub get tags implementation."""
        return {}

    async def do_set_tags(self, key: str, tags: Mapping[str, str]) -> None:
        """Stub set tags implementation."""
        pass

    async def do_upload_directory(self, request) -> Any:
        """Stub upload directory implementation."""
        pass

    async def do_download_directory(self, request) -> Any:
        """Stub download directory implementation."""
        pass

    async def do_delete_directory(self, prefix, **kwargs) -> Any:
        """Stub delete directory implementation."""
        pass

    async def do_list_page(
        self, *, prefix=None, delimiter=None, max_results=1000, pagination_token=None
    ):
        from multicloud.blob.blob_client.async_blob.async_driver import (
            ListBlobsPageResponse,
        )

        return ListBlobsPageResponse()

    async def do_generate_presigned_url(
        self,
        key,
        operation,
        *,
        expiration_seconds,
        version_id,
        content_type,
        metadata,
        tags,
        kms_key_id,
        content_disposition,
        correlation_id,
    ) -> PresignedUrlResult:
        """Record the call and return a fake result."""
        self.presigned_calls.append(
            {
                "key": key,
                "operation": operation,
                "expiration_seconds": expiration_seconds,
                "version_id": version_id,
                "content_type": content_type,
                "metadata": dict(metadata),
                "tags": dict(tags),
                "kms_key_id": kms_key_id,
                "content_disposition": content_disposition,
                "correlation_id": correlation_id,
            }
        )
        return PresignedUrlResult(
            url=f"https://fake.example.com/{key}?signature=abc123",
            expiration=datetime.now(timezone.utc),
            correlation_id=correlation_id,
            http_method="PUT" if operation == "PUT" else "GET",
            required_headers=MappingProxyType(
                {"Content-Type": content_type} if content_type else {}
            ),
        )

    async def close(self) -> None:
        self.closed = True


ASYNC_BLOB_REGISTRY.register("fake-presigned")(FakeAsyncBlobStoreWithPresigned)


# Fixtures


@pytest.fixture
def client():
    """AsyncBucketClient using the fake-presigned provider."""
    return AsyncBucketClient(
        provider="fake-presigned",
        bucket="test-bucket",
        region="us-east-1",
    )


# Tests: AsyncBucketClient.generate_presigned_url


class TestAsyncBucketClientPresignedUrl:
    """Tests for AsyncBucketClient.generate_presigned_url delegation."""

    @pytest.mark.asyncio
    async def test_generate_get_url(self, client):
        """GET operation returns a PresignedUrlResult."""
        result = await client.generate_presigned_url("myfile.txt", "GET")

        assert isinstance(result, PresignedUrlResult)
        assert result.url.startswith("https://fake.example.com/myfile.txt")
        assert result.http_method == "GET"
        assert result.correlation_id
        assert isinstance(result.expiration, datetime)

    @pytest.mark.asyncio
    async def test_generate_put_url(self, client):
        """PUT operation returns a PresignedUrlResult."""
        result = await client.generate_presigned_url("myfile.txt", "PUT")

        assert isinstance(result, PresignedUrlResult)
        assert result.http_method == "PUT"
        assert result.url.startswith("https://fake.example.com/myfile.txt")

    @pytest.mark.asyncio
    async def test_default_operation_is_get(self, client):
        """Default operation is GET."""
        result = await client.generate_presigned_url("myfile.txt")
        assert result.http_method == "GET"

    @pytest.mark.asyncio
    async def test_default_expiration_is_3600(self, client):
        """Default expiration is 3600 seconds."""
        await client.generate_presigned_url("myfile.txt")
        store = client._store
        call = store.presigned_calls[0]
        assert call["expiration_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_custom_expiration(self, client):
        """Custom expiration_seconds is passed through."""
        await client.generate_presigned_url("myfile.txt", expiration_seconds=7200)
        store = client._store
        call = store.presigned_calls[0]
        assert call["expiration_seconds"] == 7200

    @pytest.mark.asyncio
    async def test_with_version_id(self, client):
        """version_id is passed through."""
        await client.generate_presigned_url("myfile.txt", version_id="v123")
        store = client._store
        call = store.presigned_calls[0]
        assert call["version_id"] == "v123"

    @pytest.mark.asyncio
    async def test_with_content_type(self, client):
        """content_type is passed through."""
        result = await client.generate_presigned_url(
            "myfile.txt", "PUT", content_type="text/csv"
        )
        store = client._store
        call = store.presigned_calls[0]
        assert call["content_type"] == "text/csv"
        assert result.required_headers.get("Content-Type") == "text/csv"

    @pytest.mark.asyncio
    async def test_with_metadata(self, client):
        """metadata is passed through."""
        metadata = {"team": "multicloud", "env": "dev"}
        await client.generate_presigned_url("myfile.txt", "PUT", metadata=metadata)
        store = client._store
        call = store.presigned_calls[0]
        assert call["metadata"] == metadata

    @pytest.mark.asyncio
    async def test_with_tags(self, client):
        """tags is passed through."""
        tags = {"project": "multicloud", "stage": "test"}
        await client.generate_presigned_url("myfile.txt", "PUT", tags=tags)
        store = client._store
        call = store.presigned_calls[0]
        assert call["tags"] == tags

    @pytest.mark.asyncio
    async def test_with_kms_key_id(self, client):
        """kms_key_id is passed through."""
        await client.generate_presigned_url(
            "myfile.txt", "PUT", kms_key_id="arn:aws:kms:us-east-1:..."
        )
        store = client._store
        call = store.presigned_calls[0]
        assert call["kms_key_id"] == "arn:aws:kms:us-east-1:..."

    @pytest.mark.asyncio
    async def test_with_content_disposition(self, client):
        """content_disposition is passed through."""
        await client.generate_presigned_url(
            "myfile.txt", content_disposition='attachment; filename="file.txt"'
        )
        store = client._store
        call = store.presigned_calls[0]
        assert call["content_disposition"] == 'attachment; filename="file.txt"'

    @pytest.mark.asyncio
    async def test_result_has_correlation_id(self, client):
        """Result includes a unique correlation_id."""
        result1 = await client.generate_presigned_url("file1.txt")
        result2 = await client.generate_presigned_url("file2.txt")
        assert result1.correlation_id != result2.correlation_id

    @pytest.mark.asyncio
    async def test_result_has_expiration_datetime(self, client):
        """Result includes an expiration datetime in UTC."""
        result = await client.generate_presigned_url("myfile.txt")
        assert isinstance(result.expiration, datetime)
        assert result.expiration.tzinfo == timezone.utc

    @pytest.mark.asyncio
    async def test_result_has_required_headers(self, client):
        """Result includes required_headers as an immutable mapping."""
        result = await client.generate_presigned_url(
            "myfile.txt", "PUT", content_type="application/json"
        )
        assert isinstance(result.required_headers, Mapping)
        assert result.required_headers.get("Content-Type") == "application/json"


# Tests: Validation in BaseAsyncBlobStore


class TestBaseAsyncBlobStoreValidation:
    """Tests for validation in BaseAsyncBlobStore.generate_presigned_url."""

    @pytest.mark.asyncio
    async def test_empty_key_raises_invalid_argument(self, client):
        """Empty key raises InvalidArgumentException."""
        with pytest.raises(InvalidArgumentException, match="key is required"):
            await client.generate_presigned_url("", "GET")

    @pytest.mark.asyncio
    async def test_unsupported_operation_raises_invalid_argument(self, client):
        """Unsupported operation raises InvalidArgumentException."""
        with pytest.raises(InvalidArgumentException, match="Unsupported operation"):
            await client.generate_presigned_url("myfile.txt", "DELETE")

    @pytest.mark.asyncio
    async def test_expiration_below_min_raises_invalid_argument(self, client):
        """expiration_seconds < 1 raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException, match="expiration_seconds must be between"
        ):
            await client.generate_presigned_url("myfile.txt", expiration_seconds=0)

    @pytest.mark.asyncio
    async def test_expiration_above_max_raises_invalid_argument(self, client):
        """expiration_seconds > 604800 raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException, match="expiration_seconds must be between"
        ):
            await client.generate_presigned_url("myfile.txt", expiration_seconds=604801)

    @pytest.mark.asyncio
    async def test_expiration_at_min_boundary(self, client):
        """expiration_seconds = 1 is valid."""
        result = await client.generate_presigned_url("myfile.txt", expiration_seconds=1)
        assert result.url

    @pytest.mark.asyncio
    async def test_expiration_at_max_boundary(self, client):
        """expiration_seconds = 604800 (7 days) is valid."""
        result = await client.generate_presigned_url(
            "myfile.txt", expiration_seconds=604800
        )
        assert result.url

    @pytest.mark.asyncio
    async def test_get_operation_accepted(self, client):
        """GET operation is valid."""
        result = await client.generate_presigned_url("myfile.txt", "GET")
        assert result.http_method == "GET"

    @pytest.mark.asyncio
    async def test_put_operation_accepted(self, client):
        """PUT operation is valid."""
        result = await client.generate_presigned_url("myfile.txt", "PUT")
        assert result.http_method == "PUT"


# Tests: Error handling


class TestAsyncPresignedUrlErrorHandling:
    """Tests for error handling in presigned URL generation."""

    @pytest.mark.asyncio
    async def test_validation_error_propagates_directly(self, client):
        """Validation errors propagate as InvalidArgumentException, unwrapped."""
        with pytest.raises(InvalidArgumentException, match="Unsupported operation"):
            await client.generate_presigned_url("key", "PATCH")

    @pytest.mark.asyncio
    async def test_substrate_sdk_exception_propagates(self, client):
        """SubstrateSdkException from store propagates without wrapping."""
        from multicloud.common.exceptions import ResourceNotFoundException

        client._store.do_generate_presigned_url = AsyncMock(
            side_effect=ResourceNotFoundException("not found")
        )

        with pytest.raises(ResourceNotFoundException, match="not found"):
            await client.generate_presigned_url("key")

    @pytest.mark.asyncio
    async def test_generic_exception_wrapped_as_blob_async_error(self, client):
        """Generic exceptions from store are translated."""
        client._store.do_generate_presigned_url = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        # The translate_error method converts it to UnknownException
        from multicloud.common.exceptions import UnknownException

        with pytest.raises(UnknownException):
            await client.generate_presigned_url("key")


# Tests: Context manager


class TestPresignedUrlWithContextManager:
    """Tests for presigned URL generation with context manager."""

    @pytest.mark.asyncio
    async def test_generate_presigned_url_with_context_manager(self):
        """Presigned URL generation works inside async context manager."""
        async with AsyncBucketClient(provider="fake-presigned", bucket="b") as client:
            result = await client.generate_presigned_url("key.txt")
            assert result.url

        # Client should be closed after exiting context
        assert client._store.closed is True


# Tests: provider without presigned support


class FakeAsyncBlobStoreNoPresigned(FakeAsyncBlobStoreWithPresigned):
    """Provider that inherits the base default for do_generate_presigned_url.

    Deleting the override makes this class use BaseAsyncBlobStore's default
    implementation, which providers without presigned support fall back to.
    """

    provider_id = "fake-no-presigned"

    # Inherit the base default rather than the parent's override.
    do_generate_presigned_url = BaseAsyncBlobStore.do_generate_presigned_url


class TestPresignedUrlNotSupported:
    """A provider that doesn't override the hook must raise NotImplementedError."""

    @pytest.mark.asyncio
    async def test_default_raises_not_implemented(self):
        """The base default rejects presigned generation after validation passes."""
        store = FakeAsyncBlobStoreNoPresigned(bucket="test-bucket")

        # Valid key/operation/expiration so validation passes and control
        # reaches the base-class default hook.
        with pytest.raises(
            NotImplementedError, match="does not support presigned URL generation"
        ):
            await store.generate_presigned_url("myfile.txt", "GET")

    @pytest.mark.asyncio
    async def test_error_names_the_provider_class(self):
        """The NotImplementedError message identifies the concrete class."""
        store = FakeAsyncBlobStoreNoPresigned(bucket="test-bucket")

        with pytest.raises(NotImplementedError, match="FakeAsyncBlobStoreNoPresigned"):
            await store.generate_presigned_url("myfile.txt", "PUT")
