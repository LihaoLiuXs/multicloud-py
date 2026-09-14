"""Unit tests for GCP async existence check implementation.

Tests validate GcpAsyncBlobStore.do_does_object_exist and do_does_bucket_exist:
- GCP download_metadata success → True
- GCP download_metadata 404 → False
- GCP download_metadata with generation
- GCP invalid version_id (not integer) → raises InvalidArgumentException
- GCP 403 → raises
- GCP network error → raises BlobAsyncError
- GCP bucket exists (get_bucket_metadata success) → True
- GCP bucket not found (get_bucket_metadata 404) → False
- GCP bucket 403 → raises
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import GcpAsyncBlobStore
from multicloud.common.exceptions import InvalidArgumentException

# Mock aiohttp exceptions
try:
    import aiohttp
except ImportError:
    pytest.skip("aiohttp not installed", allow_module_level=True)


# Fixtures


@pytest.fixture
def mock_storage():
    """Create a mock Storage client."""
    mock = AsyncMock()
    mock.download_metadata = AsyncMock()
    mock._download = AsyncMock()
    mock.get_bucket_metadata = AsyncMock()
    mock.close = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def store(mock_storage):
    """Create a GcpAsyncBlobStore with a mocked Storage client."""
    store = GcpAsyncBlobStore(
        bucket="test-bucket", region="us-central1", _storage=mock_storage
    )
    yield store
    await store.close()


# Tests for do_does_object_exist


@pytest.mark.asyncio
class TestGcpAsyncObjectExists:
    """Tests for GcpAsyncBlobStore.do_does_object_exist."""

    async def test_download_metadata_success_returns_true(self, store, mock_storage):
        """download_metadata success returns True."""
        mock_storage.download_metadata.return_value = {
            "name": "test-key.txt",
            "size": "1024",
            "contentType": "text/plain",
            "etag": "CNCJo5bv1PUCEAK=",
            "generation": "1234567890",
        }
        result = await store.do_does_object_exist("test-key.txt", version_id=None)
        assert result is True
        mock_storage.download_metadata.assert_awaited_once_with(
            "test-bucket", "test-key.txt"
        )

    async def test_download_metadata_404_returns_false(self, store, mock_storage):
        """download_metadata 404 returns False."""
        mock_storage.download_metadata.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=404,
            message="Not Found",
        )
        result = await store.do_does_object_exist("missing-key.txt", version_id=None)
        assert result is False

    async def test_download_metadata_with_generation(self, store, mock_storage):
        """_download with generation parameter (download_metadata doesn't accept params)."""
        import json

        mock_storage._download.return_value = json.dumps(
            {
                "name": "test-key.txt",
                "generation": "1234567890",
            }
        ).encode()
        result = await store.do_does_object_exist(
            "test-key.txt", version_id="1234567890"
        )
        assert result is True
        mock_storage._download.assert_awaited_once_with(
            "test-bucket",
            "test-key.txt",
            params={"alt": "json", "generation": "1234567890"},
        )

    async def test_download_metadata_invalid_version_id_raises(
        self, store, mock_storage
    ):
        """Non-integer version_id raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException,
            match="GCP version_id must be an integer generation; got 'not-an-integer'",
        ):
            await store.do_does_object_exist(
                "test-key.txt", version_id="not-an-integer"
            )

    async def test_download_metadata_403_raises(self, store, mock_storage):
        """download_metadata 403 propagates raw to client layer."""
        mock_storage.download_metadata.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=403,
            message="Forbidden",
        )
        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_does_object_exist("test-key.txt", version_id=None)

    async def test_download_metadata_network_error_raises(self, store, mock_storage):
        """download_metadata network error propagates raw to client layer."""
        mock_storage.download_metadata.side_effect = aiohttp.ClientError(
            "Connection failed"
        )
        with pytest.raises(aiohttp.ClientError):
            await store.do_does_object_exist("test-key.txt", version_id=None)

    async def test_download_metadata_unknown_exception_raises(
        self, store, mock_storage
    ):
        """download_metadata unknown exception propagates raw to client layer."""
        mock_storage.download_metadata.side_effect = RuntimeError("unexpected error")
        with pytest.raises(RuntimeError, match="unexpected error"):
            await store.do_does_object_exist("test-key.txt", version_id=None)

    async def test_download_metadata_500_raises(self, store, mock_storage):
        """download_metadata 500 raises (not False)."""
        mock_storage.download_metadata.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Internal Server Error",
        )
        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_does_object_exist("test-key.txt", version_id=None)

    async def test_download_metadata_with_zero_generation(self, store, mock_storage):
        """_download with generation="0" (valid integer)."""
        import json

        mock_storage._download.return_value = json.dumps(
            {
                "name": "test-key.txt",
                "generation": "0",
            }
        ).encode()
        result = await store.do_does_object_exist("test-key.txt", version_id="0")
        assert result is True
        mock_storage._download.assert_awaited_once_with(
            "test-bucket",
            "test-key.txt",
            params={"alt": "json", "generation": "0"},
        )


# Tests for do_does_bucket_exist


@pytest.mark.asyncio
class TestGcpAsyncBucketExists:
    """Tests for GcpAsyncBlobStore.do_does_bucket_exist."""

    async def test_get_bucket_metadata_success_returns_true(self, store, mock_storage):
        """get_bucket_metadata success returns True."""
        mock_storage.get_bucket_metadata.return_value = {
            "kind": "storage#bucket",
            "name": "test-bucket",
        }
        result = await store.do_does_bucket_exist()
        assert result is True
        mock_storage.get_bucket_metadata.assert_awaited_once_with("test-bucket")

    async def test_get_bucket_metadata_404_returns_false(self, store, mock_storage):
        """get_bucket_metadata 404 returns False."""
        mock_storage.get_bucket_metadata.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=404,
            message="Not Found",
        )
        result = await store.do_does_bucket_exist()
        assert result is False

    async def test_get_bucket_metadata_403_raises(self, store, mock_storage):
        """get_bucket_metadata 403 propagates raw to client layer."""
        mock_storage.get_bucket_metadata.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=403,
            message="Forbidden",
        )
        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_does_bucket_exist()

    async def test_get_bucket_metadata_network_error_raises(self, store, mock_storage):
        """get_bucket_metadata network error propagates raw to client layer."""
        mock_storage.get_bucket_metadata.side_effect = aiohttp.ClientError(
            "Connection failed"
        )
        with pytest.raises(aiohttp.ClientError):
            await store.do_does_bucket_exist()

    async def test_get_bucket_metadata_unknown_exception_raises(
        self, store, mock_storage
    ):
        """get_bucket_metadata unknown exception propagates raw to client layer."""
        mock_storage.get_bucket_metadata.side_effect = RuntimeError("unexpected error")
        with pytest.raises(RuntimeError, match="unexpected error"):
            await store.do_does_bucket_exist()

    async def test_get_bucket_metadata_500_raises(self, store, mock_storage):
        """get_bucket_metadata 500 raises (not False)."""
        mock_storage.get_bucket_metadata.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=500,
            message="Internal Server Error",
        )
        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_does_bucket_exist()
