"""Unit tests for GcpAsyncBlobStore delete operations.

Tests validate the GCP provider correctly:
- Calls storage.delete for single deletes
- Uses asyncio.gather for concurrent batch deletes
- Treats 404 as success (idempotent)
- Passes generation parameter for versioned deletes
- Translates aiohttp errors into SDK exceptions
- Respects max_concurrency from transfer config
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from multicloud.blob.blob_client.async_blob.async_driver import (
    AsyncTransferConfig,
    BlobAsyncError,
    BlobIdentifier,
    BulkDeleteResponse,
    DeletedBlob,
    DeleteError,
)
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import GcpAsyncBlobStore
from multicloud.common.exceptions import (
    ResourceNotFoundException,
    UnAuthorizedException,
    UnknownException,
)


def _make_response_error(
    status: int = 404, message: str = "Not Found"
) -> aiohttp.ClientResponseError:
    """Create an aiohttp ClientResponseError for testing."""
    mock_request_info = MagicMock()
    mock_request_info.real_url = "https://storage.googleapis.com/test"
    return aiohttp.ClientResponseError(
        request_info=mock_request_info,
        history=(),
        status=status,
        message=message,
    )


def _make_store(
    mock_storage: AsyncMock,
    transfer_config: AsyncTransferConfig | None = None,
) -> GcpAsyncBlobStore:
    """Create a GcpAsyncBlobStore with a mocked Storage client."""
    store = GcpAsyncBlobStore(
        bucket="test-bucket",
        region="us-central1",
        transfer_config=transfer_config,
        _storage=mock_storage,
    )
    return store


class TestGcpAsyncBlobStoreDelete:
    """Single object delete tests."""

    @pytest.mark.asyncio
    async def test_delete_calls_storage_delete(self):
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(return_value=None)
        store = _make_store(mock_storage)

        await store.do_delete("path/to/file.txt")

        mock_storage.delete.assert_called_once_with("test-bucket", "path/to/file.txt")

    @pytest.mark.asyncio
    async def test_delete_with_version_id_passes_generation(self):
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(return_value=None)
        store = _make_store(mock_storage)

        await store.do_delete("file.txt", version_id="12345")

        mock_storage.delete.assert_called_once_with(
            "test-bucket", "file.txt", params={"generation": "12345"}
        )

    @pytest.mark.asyncio
    async def test_delete_404_is_idempotent(self):
        """GCS 404 on delete is treated as success (idempotent)."""
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(
            side_effect=_make_response_error(404, "Not Found")
        )
        store = _make_store(mock_storage)

        # Should NOT raise
        await store.do_delete("ghost-file.txt")

    @pytest.mark.asyncio
    async def test_delete_403_propagates_to_client_layer(self):
        """Non-404 ClientResponseError propagates raw to client layer."""
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(
            side_effect=_make_response_error(403, "Forbidden")
        )
        store = _make_store(mock_storage)

        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_delete("secret.txt")

    @pytest.mark.asyncio
    async def test_delete_500_propagates_to_client_layer(self):
        """500 ClientResponseError propagates raw to client layer."""
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(
            side_effect=_make_response_error(500, "Internal Server Error")
        )
        store = _make_store(mock_storage)

        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_delete("file.txt")

    @pytest.mark.asyncio
    async def test_delete_network_error_propagates_to_client_layer(self):
        """Non-response aiohttp errors propagate raw to client layer."""
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(
            side_effect=aiohttp.ClientError("Connection reset")
        )
        store = _make_store(mock_storage)

        with pytest.raises(aiohttp.ClientError):
            await store.do_delete("file.txt")


class TestGcpAsyncBlobStoreDeleteObjects:
    """Batch object delete tests."""

    @pytest.mark.asyncio
    async def test_delete_objects_all_success(self):
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(return_value=None)
        store = _make_store(mock_storage)

        identifiers = [
            BlobIdentifier(key="a.txt"),
            BlobIdentifier(key="b.txt"),
            BlobIdentifier(key="c.txt"),
        ]
        result = await store.do_delete_objects(identifiers)

        assert isinstance(result, BulkDeleteResponse)
        assert result.has_errors is False
        assert len(result.deleted) == 3
        assert mock_storage.delete.call_count == 3

    @pytest.mark.asyncio
    async def test_delete_objects_with_version_ids(self):
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(return_value=None)
        store = _make_store(mock_storage)

        identifiers = [
            BlobIdentifier(key="a.txt", version_id="gen1"),
            BlobIdentifier(key="b.txt"),
        ]
        result = await store.do_delete_objects(identifiers)

        # Check that generation param was passed for versioned delete
        calls = mock_storage.delete.call_args_list
        # One of the calls should have params with generation
        versioned_call = [c for c in calls if "params" in c.kwargs]
        assert len(versioned_call) == 1
        assert versioned_call[0].kwargs["params"] == {"generation": "gen1"}

    @pytest.mark.asyncio
    async def test_delete_objects_404_treated_as_success(self):
        """Non-existent objects are reported as deleted (idempotent)."""
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(
            side_effect=_make_response_error(404, "Not Found")
        )
        store = _make_store(mock_storage)

        result = await store.do_delete_objects(
            [BlobIdentifier(key="ghost1.txt"), BlobIdentifier(key="ghost2.txt")]
        )

        assert result.has_errors is False
        assert len(result.deleted) == 2

    @pytest.mark.asyncio
    async def test_delete_objects_partial_failure(self):
        """Mix of successes and failures reported correctly."""
        call_count = {"n": 0}

        async def _side_effect(bucket, key, **kwargs):
            call_count["n"] += 1
            if key == "bad.txt":
                raise _make_response_error(403, "Forbidden")
            return None

        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(side_effect=_side_effect)
        store = _make_store(mock_storage)

        identifiers = [
            BlobIdentifier(key="good.txt"),
            BlobIdentifier(key="bad.txt"),
            BlobIdentifier(key="also-good.txt"),
        ]
        result = await store.do_delete_objects(identifiers)

        assert result.has_errors is True
        assert len(result.deleted) == 2
        assert len(result.errors) == 1
        assert result.errors[0].key == "bad.txt"
        assert result.errors[0].code == "403"

    @pytest.mark.asyncio
    async def test_delete_objects_network_error_reported(self):
        """Network errors reported as DeleteError, not raised."""
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(side_effect=aiohttp.ClientError("timeout"))
        store = _make_store(mock_storage)

        result = await store.do_delete_objects([BlobIdentifier(key="file.txt")])

        assert result.has_errors is True
        assert len(result.errors) == 1
        assert result.errors[0].code == "NetworkError"

    @pytest.mark.asyncio
    async def test_delete_objects_unknown_exception_reported(self):
        """Unknown exceptions reported as DeleteError."""
        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(side_effect=ValueError("weird error"))
        store = _make_store(mock_storage)

        result = await store.do_delete_objects([BlobIdentifier(key="file.txt")])

        assert result.has_errors is True
        assert result.errors[0].code == "Unknown"
        assert "weird error" in result.errors[0].message

    @pytest.mark.asyncio
    async def test_delete_objects_respects_concurrency(self):
        """Semaphore bounds concurrent deletes to max_concurrency."""
        import asyncio

        max_concurrent = 0
        current_concurrent = 0

        async def _tracking_delete(bucket, key, **kwargs):
            nonlocal max_concurrent, current_concurrent
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.01)  # Simulate I/O
            current_concurrent -= 1

        mock_storage = AsyncMock()
        mock_storage.delete = AsyncMock(side_effect=_tracking_delete)

        config = AsyncTransferConfig(max_concurrency=3)
        store = _make_store(mock_storage, transfer_config=config)

        identifiers = [BlobIdentifier(key=f"file-{i}.txt") for i in range(10)]
        result = await store.do_delete_objects(identifiers)

        assert result.has_errors is False
        assert len(result.deleted) == 10
        assert max_concurrent <= 3  # Never exceeded concurrency limit

    @pytest.mark.asyncio
    async def test_delete_objects_storage_not_initialized_raises(self):
        """If storage is None after _ensure_client, raises RuntimeError."""
        mock_storage = AsyncMock()
        store = _make_store(mock_storage)
        # Simulate _ensure_client completing but storage still being None
        # (edge case: close() called concurrently between ensure and use)
        store._ensure_client = AsyncMock()  # Patch to no-op
        store._storage = None

        with pytest.raises(RuntimeError, match="not initialized"):
            await store.do_delete_objects([BlobIdentifier(key="a")])
