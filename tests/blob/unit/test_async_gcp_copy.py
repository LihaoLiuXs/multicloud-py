"""Unit tests for GcpAsyncBlobStore.do_copy() with mocked gcloud-aio-storage.

Tests validate the GCP async provider correctly:
- Passes correct parameters to GCS copy
- Handles 404 errors as ResourceNotFoundException
- Handles metadata override via patch_metadata
- Supports same-bucket and cross-bucket copy
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip(
    "gcloud.aio.storage",
    reason="gcloud-aio-storage not installed (optional async-gcp dependency)",
)

import aiohttp
from multicloud.blob.blob_client.async_blob.async_driver import (
    CopyRequest,
    CopyResponse,
)
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import GcpAsyncBlobStore
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceNotFoundException,
)


def _make_client_response_error(status: int, message: str = "error"):
    """Create a mock aiohttp ClientResponseError."""
    mock_request_info = MagicMock()
    mock_request_info.real_url = "http://storage.googleapis.com/test"
    return aiohttp.ClientResponseError(
        request_info=mock_request_info,
        history=(),
        status=status,
        message=message,
    )


def _make_mock_storage(
    copy_response: dict | None = None,
    copy_side_effect: Any = None,
    patch_metadata_response: dict | None = None,
    patch_metadata_side_effect: Any = None,
):
    """Create a mock gcloud-aio-storage Storage instance."""
    mock_storage = AsyncMock()

    if copy_response is not None:
        mock_storage.copy.return_value = copy_response
    if copy_side_effect is not None:
        mock_storage.copy.side_effect = copy_side_effect

    if patch_metadata_response is not None:
        mock_storage.patch_metadata.return_value = patch_metadata_response
    if patch_metadata_side_effect is not None:
        mock_storage.patch_metadata.side_effect = patch_metadata_side_effect

    # Default patch_metadata to success
    if patch_metadata_response is None and patch_metadata_side_effect is None:
        mock_storage.patch_metadata.return_value = {}

    return mock_storage


@pytest.fixture
def copy_success_response():
    """Standard successful GCS copy response."""
    return {
        "resource": {
            "name": "dest/file.txt",
            "bucket": "dest-bucket",
            "etag": "CNCJo5bv1PUCEAK=",
            "generation": "1234567890",
            "md5Hash": "abc123==",
            "metadata": {},
        },
        "done": True,
    }


@pytest.fixture
def mock_storage(copy_success_response):
    return _make_mock_storage(copy_response=copy_success_response)


@pytest.fixture
def gcp_store(mock_storage):
    return GcpAsyncBlobStore(
        bucket="test-bucket",
        region="us-central1",
        _storage=mock_storage,
    )


@pytest.fixture
def same_bucket_request():
    return CopyRequest(
        source_bucket="test-bucket",
        source_key="src/file.txt",
        destination_bucket="test-bucket",
        destination_key="dest/file.txt",
    )


@pytest.fixture
def cross_bucket_request():
    return CopyRequest(
        source_bucket="source-bucket",
        source_key="data/report.pdf",
        destination_bucket="archive-bucket",
        destination_key="archived/report.pdf",
    )


class TestGcpAsyncCopySuccess:
    """Successful copy scenarios."""

    @pytest.mark.asyncio
    async def test_same_bucket_copy_returns_response(
        self, gcp_store, same_bucket_request
    ):
        result = await gcp_store.copy(same_bucket_request)

        assert isinstance(result, CopyResponse)
        assert result.key == "dest/file.txt"
        assert result.e_tag == "CNCJo5bv1PUCEAK="
        assert result.version_id == "1234567890"
        assert result.correlation_id is not None

    @pytest.mark.asyncio
    async def test_cross_bucket_copy_returns_response(
        self, gcp_store, cross_bucket_request
    ):
        result = await gcp_store.copy(cross_bucket_request)

        assert isinstance(result, CopyResponse)
        assert result.key == "archived/report.pdf"

    @pytest.mark.asyncio
    async def test_copy_passes_correct_params(
        self, mock_storage, gcp_store, same_bucket_request
    ):
        await gcp_store.copy(same_bucket_request)

        mock_storage.copy.assert_called_once_with(
            "test-bucket",
            "src/file.txt",
            "test-bucket",
            new_name="dest/file.txt",
        )

    @pytest.mark.asyncio
    async def test_cross_bucket_passes_correct_params(
        self, mock_storage, gcp_store, cross_bucket_request
    ):
        await gcp_store.copy(cross_bucket_request)

        mock_storage.copy.assert_called_once_with(
            "source-bucket",
            "data/report.pdf",
            "archive-bucket",
            new_name="archived/report.pdf",
        )

    @pytest.mark.asyncio
    async def test_copy_without_metadata_no_patch(
        self, mock_storage, gcp_store, same_bucket_request
    ):
        """Without metadata override, patch_metadata is not called."""
        await gcp_store.copy(same_bucket_request)
        mock_storage.patch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_copy_with_metadata_calls_patch(self, mock_storage, gcp_store):
        """With metadata override, patch_metadata is called."""
        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
            metadata={"env": "prod"},
        )
        await gcp_store.copy(req)

        mock_storage.patch_metadata.assert_called_once_with(
            "dst",
            "dkey",
            metadata={"metadata": {"env": "prod"}},
        )

    @pytest.mark.asyncio
    async def test_copy_no_generation_in_response(self):
        """Handle response without generation field."""
        storage = _make_mock_storage(
            copy_response={
                "resource": {
                    "name": "dkey",
                    "bucket": "dst",
                    "etag": "etag-val",
                },
                "done": True,
            }
        )
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        result = await store.copy(req)
        assert result.version_id is None

    @pytest.mark.asyncio
    async def test_copy_uses_etag_from_resource(self):
        """ETag is extracted from resource dict."""
        storage = _make_mock_storage(
            copy_response={
                "resource": {
                    "etag": "my-etag-value",
                    "generation": "999",
                },
            }
        )
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        result = await store.copy(req)
        assert result.e_tag == "my-etag-value"

    @pytest.mark.asyncio
    async def test_copy_falls_back_to_md5hash(self):
        """Falls back to md5Hash when etag is absent."""
        storage = _make_mock_storage(
            copy_response={
                "resource": {
                    "md5Hash": "md5-fallback",
                    "generation": "111",
                },
            }
        )
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        result = await store.copy(req)
        assert result.e_tag == "md5-fallback"


class TestGcpAsyncCopyErrors:
    """Error handling and translation for copy."""

    @pytest.mark.asyncio
    async def test_source_not_found_raises_resource_not_found(self):
        exc = _make_client_response_error(404, "Not Found")
        storage = _make_mock_storage(copy_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="missing",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(ResourceNotFoundException, match="Source object not found"):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_forbidden_propagates_as_client_response_error(self):
        """Non-404 ClientResponseError propagates raw for client-layer translation."""
        exc = _make_client_response_error(403, "Forbidden")
        storage = _make_mock_storage(copy_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(aiohttp.ClientResponseError):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_401_propagates_as_client_response_error(self):
        """401 ClientResponseError propagates raw for client-layer translation."""
        exc = _make_client_response_error(401, "Unauthorized")
        storage = _make_mock_storage(copy_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(aiohttp.ClientResponseError):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_500_propagates_as_client_response_error(self):
        """500 ClientResponseError propagates raw for client-layer translation."""
        exc = _make_client_response_error(500, "Internal Server Error")
        storage = _make_mock_storage(copy_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(aiohttp.ClientResponseError):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_network_error_propagates(self):
        """ClientConnectionError propagates raw for client-layer translation."""
        exc = aiohttp.ClientConnectionError("Connection refused")
        storage = _make_mock_storage(copy_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(aiohttp.ClientConnectionError):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_unexpected_error_propagates(self):
        """Unexpected exceptions propagate raw for client-layer translation."""
        storage = _make_mock_storage(copy_side_effect=RuntimeError("boom"))
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(RuntimeError, match="boom"):
            await store.copy(req)


class TestGcpAsyncCopyMetadata:
    """Metadata override behavior."""

    @pytest.mark.asyncio
    async def test_metadata_patch_failure_does_not_fail_copy(self):
        """Copy succeeds even if patch_metadata fails."""
        exc = _make_client_response_error(500, "Internal")
        storage = _make_mock_storage(
            copy_response={
                "resource": {"etag": "e", "generation": "1"},
            },
            patch_metadata_side_effect=exc,
        )
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
            metadata={"env": "prod"},
        )
        # Should not raise — metadata update is best-effort
        result = await store.copy(req)
        assert result.key == "dkey"

    @pytest.mark.asyncio
    async def test_empty_metadata_still_calls_patch(self):
        """Empty metadata dict triggers patch_metadata with empty dict."""
        storage = _make_mock_storage(
            copy_response={
                "resource": {"etag": "e", "generation": "1"},
            }
        )
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
            metadata={},
        )
        await store.copy(req)

        storage.patch_metadata.assert_called_once_with(
            "dst",
            "dkey",
            metadata={"metadata": {}},
        )


class TestGcpAsyncCopySourceVersionId:
    """source_version_id is not supported on GCP."""

    @pytest.mark.asyncio
    async def test_source_version_id_raises_invalid_argument(self):
        """source_version_id is not supported on GCP — must raise explicitly."""
        storage = _make_mock_storage()
        store = GcpAsyncBlobStore(bucket="b", _storage=storage)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
            source_version_id="12345",
        )
        with pytest.raises(InvalidArgumentException, match="source_version_id"):
            await store.copy(req)
        storage.copy.assert_not_called()


class TestGcpAsyncCopyRegistration:
    """Provider registry integration."""

    def test_gcp_registered_in_async_registry(self):
        from multicloud.blob.blob_client.async_blob.async_driver import (
            ASYNC_BLOB_REGISTRY,
        )

        assert ASYNC_BLOB_REGISTRY.get("gcp") is GcpAsyncBlobStore
