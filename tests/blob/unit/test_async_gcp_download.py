"""Unit tests for GcpAsyncBlobStore.do_download() with mocked gcloud-aio-storage.

Tests validate the GCP async provider correctly:
- Passes correct parameters to Storage.download()
- Handles Range headers for partial downloads
- Translates GCP/aiohttp errors to BlobNotFoundError
- Extracts metadata from download_metadata() response
- Verifies build_range_header() helper
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
import multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store as _gcp_store_mod
from multicloud.blob.blob_client.async_blob.async_driver import (
    AsyncDownloadRequest,
    AsyncDownloadResponse,
    BlobAsyncError,
    BlobMetadata,
    BlobNotFoundError,
    build_range_header,
)
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import (
    GcpAsyncBlobStore,
)
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
    ResourceNotFoundException,
    UnAuthorizedException,
    UnknownException,
)


def _make_mock_storage(
    download_response: bytes | None = None,
    download_side_effect: Any = None,
    metadata_response: dict | None = None,
    metadata_side_effect: Any = None,
):
    """Create a mock gcloud.aio.storage.Storage instance for downloads."""
    mock_storage = AsyncMock()

    if download_response is not None:
        mock_storage.download.return_value = download_response
    if download_side_effect is not None:
        mock_storage.download.side_effect = download_side_effect

    if metadata_response is not None:
        mock_storage.download_metadata.return_value = metadata_response
    else:
        mock_storage.download_metadata.return_value = {
            "name": "test-key",
            "size": "12",
            "contentType": "text/plain",
            "etag": "CJDe9fH3x4cDEAE=",
            "generation": "1234567890",
            "metadata": {},
        }
    if metadata_side_effect is not None:
        mock_storage.download_metadata.side_effect = metadata_side_effect

    mock_storage.close = AsyncMock()
    return mock_storage


def _make_client_response_error(status: int, message: str = "error"):
    """Create a mock aiohttp.ClientResponseError."""
    mock_request_info = MagicMock()
    mock_request_info.real_url = "https://storage.googleapis.com/..."
    return aiohttp.ClientResponseError(
        request_info=mock_request_info,
        history=(),
        status=status,
        message=message,
    )


@pytest.fixture
def mock_storage():
    """A mock Storage that returns successful download."""
    return _make_mock_storage(
        download_response=b"hello world",
        metadata_response={
            "name": "test.txt",
            "size": "11",
            "contentType": "text/plain",
            "etag": "etag-123",
            "generation": "9876543210",
            "metadata": {"env": "test"},
        },
    )


@pytest.fixture
def gcp_store(mock_storage):
    """GcpAsyncBlobStore with mocked storage."""
    return GcpAsyncBlobStore(
        bucket="test-bucket",
        region="us-central1",
        _storage=mock_storage,
    )


class TestGcpBuildRangeHeader:
    """Tests for the GCP build_range_header() helper function."""

    def test_both_none_returns_none(self):
        """No range requested returns None."""
        assert build_range_header(None, None) is None

    def test_start_and_end(self):
        """Both start and end produce 'bytes=start-end'."""
        assert build_range_header(0, 99) == "bytes=0-99"

    def test_start_only(self):
        """Start with no end produces 'bytes=start-' (open range)."""
        assert build_range_header(100, None) == "bytes=100-"

    def test_end_only(self):
        """End with no start produces 'bytes=-end' (suffix range)."""
        assert build_range_header(None, 500) == "bytes=-500"

    def test_start_zero_end_zero(self):
        """Range from 0 to 0 is a single byte."""
        assert build_range_header(0, 0) == "bytes=0-0"

    def test_large_range(self):
        """Large byte offsets work correctly."""
        assert build_range_header(1000000, 9999999) == "bytes=1000000-9999999"


class TestGcpAsyncDownloadSuccess:
    """Successful download scenarios."""

    @pytest.mark.asyncio
    async def test_download_returns_response(self, gcp_store):
        """Basic download returns AsyncDownloadResponse."""
        request = AsyncDownloadRequest(key="test.txt")
        result = await gcp_store.do_download(request)

        assert isinstance(result, AsyncDownloadResponse)
        assert result.key == "test.txt"
        assert result.content == b"hello world"

    @pytest.mark.asyncio
    async def test_download_extracts_metadata(self, gcp_store):
        """Download extracts full metadata from GCS metadata response."""
        request = AsyncDownloadRequest(key="test.txt")
        result = await gcp_store.do_download(request)

        assert result.metadata.key == "test.txt"
        assert result.metadata.content_length == 11
        assert result.metadata.content_type == "text/plain"
        assert result.metadata.e_tag == "etag-123"
        assert result.metadata.version_id == "9876543210"
        assert result.metadata.metadata == {"env": "test"}

    @pytest.mark.asyncio
    async def test_download_passes_bucket_and_key(self, mock_storage):
        """download() is called with correct bucket and key."""
        store = GcpAsyncBlobStore(bucket="my-bucket", _storage=mock_storage)

        request = AsyncDownloadRequest(key="path/to/file.bin")
        await store.do_download(request)

        mock_storage.download.assert_called_once_with(
            "my-bucket",
            "path/to/file.bin",
            headers=None,
        )

    @pytest.mark.asyncio
    async def test_download_with_range_start_end(self, mock_storage):
        """Range request with start/end passes Range header."""
        store = GcpAsyncBlobStore(bucket="b", _storage=mock_storage)

        request = AsyncDownloadRequest(key="file.txt", start=100, end=200)
        await store.do_download(request)

        mock_storage.download.assert_called_once_with(
            "b",
            "file.txt",
            headers={"Range": "bytes=100-200"},
        )

    @pytest.mark.asyncio
    async def test_download_with_range_start_only(self, mock_storage):
        """Range request with start only produces open-ended range."""
        store = GcpAsyncBlobStore(bucket="b", _storage=mock_storage)

        request = AsyncDownloadRequest(key="file.txt", start=500)
        await store.do_download(request)

        call_kwargs = mock_storage.download.call_args
        assert call_kwargs[1]["headers"] == {"Range": "bytes=500-"}

    @pytest.mark.asyncio
    async def test_download_with_range_end_only(self, mock_storage):
        """Range request with end only produces suffix range."""
        store = GcpAsyncBlobStore(bucket="b", _storage=mock_storage)

        request = AsyncDownloadRequest(key="file.txt", end=99)
        await store.do_download(request)

        call_kwargs = mock_storage.download.call_args
        assert call_kwargs[1]["headers"] == {"Range": "bytes=-99"}

    @pytest.mark.asyncio
    async def test_download_no_range_passes_none_headers(self, mock_storage):
        """No Range header when start/end are both None."""
        store = GcpAsyncBlobStore(bucket="b", _storage=mock_storage)

        request = AsyncDownloadRequest(key="file.txt")
        await store.do_download(request)

        call_kwargs = mock_storage.download.call_args
        assert call_kwargs[1]["headers"] is None

    @pytest.mark.asyncio
    async def test_download_metadata_called_with_correct_args(self, mock_storage):
        """download_metadata() is called with bucket and key."""
        store = GcpAsyncBlobStore(bucket="my-bucket", _storage=mock_storage)

        request = AsyncDownloadRequest(key="doc.pdf")
        await store.do_download(request)

        mock_storage.download_metadata.assert_called_once_with("my-bucket", "doc.pdf")

    @pytest.mark.asyncio
    async def test_download_with_version_id_raises(self, mock_storage):
        """Passing version_id raises InvalidArgumentException on GCP."""
        store = GcpAsyncBlobStore(bucket="b", _storage=mock_storage)

        request = AsyncDownloadRequest(key="file.txt", version_id="12345")
        with pytest.raises(InvalidArgumentException, match="version_id.*not supported"):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_download_no_generation_yields_none_version_id(self):
        """Missing generation in metadata yields None version_id."""
        mock = _make_mock_storage(
            download_response=b"data",
            metadata_response={
                "name": "file.txt",
                "size": "4",
                "contentType": "application/octet-stream",
                "etag": "e1",
            },
        )
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="file.txt")
        result = await store.do_download(request)

        assert result.metadata.version_id is None

    @pytest.mark.asyncio
    async def test_download_empty_metadata_field(self):
        """Empty metadata dict in GCS response is preserved."""
        mock = _make_mock_storage(
            download_response=b"data",
            metadata_response={
                "name": "file.txt",
                "size": "4",
                "contentType": "text/plain",
                "etag": "e1",
                "generation": "123",
                "metadata": {},
            },
        )
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="file.txt")
        result = await store.do_download(request)

        assert result.metadata.metadata == {}

    @pytest.mark.asyncio
    async def test_download_null_metadata_field(self):
        """None metadata field in GCS response becomes empty dict."""
        mock = _make_mock_storage(
            download_response=b"data",
            metadata_response={
                "name": "file.txt",
                "size": "4",
                "contentType": "text/plain",
                "etag": "e1",
                "generation": "123",
                "metadata": None,
            },
        )
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="file.txt")
        result = await store.do_download(request)

        assert result.metadata.metadata == {}

    @pytest.mark.asyncio
    async def test_download_empty_body(self):
        """Empty object returns empty bytes."""
        mock = _make_mock_storage(
            download_response=b"",
            metadata_response={
                "name": "empty.txt",
                "size": "0",
                "contentType": "text/plain",
                "etag": "e1",
                "generation": "123",
                "metadata": {},
            },
        )
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="empty.txt")
        result = await store.do_download(request)

        assert result.content == b""
        assert result.metadata.content_length == 0

    @pytest.mark.asyncio
    async def test_download_with_user_metadata(self):
        """User metadata from GCS response is preserved."""
        mock = _make_mock_storage(
            download_response=b"data",
            metadata_response={
                "name": "file.txt",
                "size": "4",
                "contentType": "text/plain",
                "etag": "e1",
                "generation": "123",
                "metadata": {"team": "multicloud", "env": "prod"},
            },
        )
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="file.txt")
        result = await store.do_download(request)

        assert result.metadata.metadata == {"team": "multicloud", "env": "prod"}


class TestGcpAsyncDownloadHeaders:
    """Request header construction on download."""

    @pytest.mark.asyncio
    async def test_no_range_omits_headers(self, mock_storage):
        store = GcpAsyncBlobStore(bucket="b", _storage=mock_storage)

        request = AsyncDownloadRequest(key="file.txt")
        await store.do_download(request)

        call_kwargs = mock_storage.download.call_args
        assert call_kwargs[1]["headers"] is None


class TestGcpAsyncDownloadIfMatch:
    """Conditional read (if_match) precondition handling."""

    @pytest.mark.asyncio
    async def test_if_match_sent_as_header(self, mock_storage):
        """if_match is forwarded as the If-Match download header."""
        store = GcpAsyncBlobStore(bucket="b", _storage=mock_storage)

        request = AsyncDownloadRequest(key="file.txt", if_match="etag-xyz")
        await store.do_download(request)

        call_kwargs = mock_storage.download.call_args
        assert call_kwargs[1]["headers"] == {"If-Match": "etag-xyz"}

    @pytest.mark.asyncio
    async def test_if_match_combined_with_range(self, mock_storage):
        """if_match and a byte range are both sent as headers."""
        store = GcpAsyncBlobStore(bucket="b", _storage=mock_storage)

        request = AsyncDownloadRequest(key="file.txt", start=0, end=9, if_match="e1")
        await store.do_download(request)

        headers = mock_storage.download.call_args[1]["headers"]
        assert headers == {"Range": "bytes=0-9", "If-Match": "e1"}

    @pytest.mark.asyncio
    async def test_no_if_match_omits_header(self, mock_storage):
        """No if_match means no If-Match header (None when no range either)."""
        store = GcpAsyncBlobStore(bucket="b", _storage=mock_storage)

        await store.do_download(AsyncDownloadRequest(key="file.txt"))

        assert mock_storage.download.call_args[1]["headers"] is None

    @pytest.mark.asyncio
    async def test_if_match_mismatch_maps_to_failed_precondition(self):
        """A 412 on read translates to FailedPreconditionException."""
        exc = _make_client_response_error(412, "Precondition Failed")
        mock = _make_mock_storage(download_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        # do_download re-raises the 412; the client layer translates it.
        result = store.translate_error(exc)
        assert isinstance(result, FailedPreconditionException)


class TestGcpAsyncDownloadReplicationStatus:
    """GCS has no cross-region replication concept — always None."""

    @pytest.mark.asyncio
    async def test_download_replication_status_is_none(self, gcp_store):
        request = AsyncDownloadRequest(key="test.txt")
        result = await gcp_store.do_download(request)
        assert result.metadata.replication_status is None


class TestGcpAsyncDownloadErrors:
    """Error handling and translation for downloads."""

    @pytest.mark.asyncio
    async def test_404_raises_blob_not_found(self):
        """404 ClientResponseError raises BlobNotFoundError with key."""
        exc = _make_client_response_error(404, "Not Found")
        mock = _make_mock_storage(download_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="missing.txt")
        with pytest.raises(BlobNotFoundError) as exc_info:
            await store.do_download(request)

        assert exc_info.value.key == "missing.txt"
        assert exc_info.value.cause is exc

    @pytest.mark.asyncio
    async def test_403_propagates_to_client(self):
        """403 ClientResponseError propagates to client layer for translation."""
        exc = _make_client_response_error(403, "Forbidden")
        mock = _make_mock_storage(download_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="secret.txt")
        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_401_propagates_to_client(self):
        """401 ClientResponseError propagates to client layer for translation."""
        exc = _make_client_response_error(401, "Unauthorized")
        mock = _make_mock_storage(download_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="file.txt")
        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_400_propagates_to_client(self):
        """400 ClientResponseError propagates to client layer for translation."""
        exc = _make_client_response_error(400, "Bad Request")
        mock = _make_mock_storage(download_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="file.txt")
        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_500_propagates_to_client(self):
        """500 ClientResponseError propagates to client layer for translation."""
        exc = _make_client_response_error(500, "Internal Server Error")
        mock = _make_mock_storage(download_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="file.txt")
        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_network_error_propagates_to_client(self):
        """ClientError (non-response) propagates to client layer."""
        exc = aiohttp.ClientConnectorError(
            connection_key=MagicMock(), os_error=OSError("Connection refused")
        )
        mock = _make_mock_storage(download_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="file.txt")
        with pytest.raises(aiohttp.ClientConnectorError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_generic_exception_propagates_to_client(self):
        """Generic exception propagates to client layer for wrapping."""
        exc = RuntimeError("unexpected failure")
        mock = _make_mock_storage(download_side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="file.txt")
        with pytest.raises(RuntimeError, match="unexpected failure"):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_metadata_404_returns_best_effort_metadata(self):
        """404 on download_metadata gracefully returns content with minimal metadata."""
        exc = _make_client_response_error(404, "Not Found")
        mock = _make_mock_storage(
            download_response=b"data",
            metadata_side_effect=exc,
        )
        # Override download to succeed but metadata to fail
        mock.download = AsyncMock(return_value=b"data")
        mock.download_metadata = AsyncMock(side_effect=exc)
        store = GcpAsyncBlobStore(bucket="b", _storage=mock)

        request = AsyncDownloadRequest(key="file.txt")
        response = await store.do_download(request)

        # Content was downloaded successfully
        assert response.content == b"data"
        assert response.key == "file.txt"
        # Metadata falls back to best-effort values
        assert response.metadata.content_length == 4  # len(b"data")
        assert response.metadata.e_tag is None
        assert response.metadata.version_id is None


class TestGcpAsyncDownloadLifecycle:
    """Client lifecycle during downloads."""

    @pytest.mark.asyncio
    async def test_download_initializes_storage(self):
        """Download lazily creates the Storage client."""
        mock = _make_mock_storage(download_response=b"data")
        store = GcpAsyncBlobStore(bucket="b")
        assert store._storage is None

        with patch.object(_gcp_store_mod, "Storage", return_value=mock) as storage_cls:
            request = AsyncDownloadRequest(key="file.txt")
            await store.do_download(request)

            storage_cls.assert_called_once()
            assert store._storage is not None

    @pytest.mark.asyncio
    async def test_download_reuses_existing_storage(self, mock_storage):
        """Multiple downloads share the same storage instance."""
        store = GcpAsyncBlobStore(bucket="b", _storage=mock_storage)

        request = AsyncDownloadRequest(key="file.txt")
        await store.do_download(request)
        await store.do_download(request)
        await store.do_download(request)

        # Storage was injected, so no new Storage instances created
        assert store._storage is mock_storage
