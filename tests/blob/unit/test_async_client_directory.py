"""Unit tests for AsyncBucketClient directory operations (upload_directory / download_directory).

Tests the public client layer:
- Correct delegation to the underlying store
- Error translation (non-SubstrateSdkException → BlobAsyncError)
- SubstrateSdkException pass-through
- Input validation at the store layer
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from multicloud.blob.blob_client.async_blob.async_driver import (
    AsyncDirectoryDownloadResponse,
    AsyncDirectoryUploadResponse,
    BaseAsyncBlobStore,
    BlobAsyncError,
    FailedAsyncBlobDownload,
    FailedAsyncBlobUpload,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
)


class MockAsyncBlobStore(BaseAsyncBlobStore):
    """Minimal mock of BaseAsyncBlobStore for testing AsyncBucketClient delegation."""

    def __init__(self, **kwargs):
        self._bucket = kwargs.get("bucket", "test-bucket")
        self._region = kwargs.get("region", "us-east-1")
        self._transfer_config = None
        self.delete_directory_calls: list[dict] = []
        self.delete_directory_error: Exception | None = None

    def get_provider_id(self) -> str:
        return "mock"

    def translate_error(self, exc: Exception):
        return BlobAsyncError(str(exc), cause=exc)

    async def do_upload(
        self, key, payload, *, content_type=None, content_length=None, metadata=None
    ):
        pass

    async def do_download(self, request):
        pass

    async def do_copy(self, request):
        pass

    async def do_delete(self, key, *, version_id=None):
        pass

    async def do_delete_objects(self, identifiers):
        pass

    async def do_get_tags(self, key):
        return {}

    async def do_set_tags(self, key, tags):
        pass

    async def do_upload_directory(
        self,
        local_source_directory,
        *,
        prefix="",
        include_sub_folders=True,
        follow_symbolic_links=False,
        max_concurrency=10,
        tags=None,
    ):
        return AsyncDirectoryUploadResponse(
            total_files=5,
            successful_uploads=5,
            failed_transfers=[],
            total_bytes_transferred=1024,
        )

    async def do_download_directory(
        self,
        prefix,
        local_destination_directory,
        *,
        max_concurrency=10,
        prefixes_to_exclude=None,
    ):
        return AsyncDirectoryDownloadResponse(
            total_files=3,
            successful_downloads=3,
            failed_transfers=[],
            total_bytes_transferred=512,
        )

    async def do_delete_directory(self, prefix, *, max_concurrency=10):
        self.delete_directory_calls.append(
            {"prefix": prefix, "max_concurrency": max_concurrency}
        )
        if self.delete_directory_error is not None:
            raise self.delete_directory_error

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


@pytest.fixture
def mock_store():
    return MockAsyncBlobStore(bucket="test-bucket")


@pytest.fixture
def client(mock_store):
    """AsyncBucketClient backed by MockAsyncBlobStore."""
    from multicloud.blob.blob_client.async_blob.async_client import AsyncBucketClient

    c = AsyncBucketClient.__new__(AsyncBucketClient)
    c._store = mock_store
    return c


@pytest.fixture
def tmp_dir():
    """Temporary directory for upload tests."""
    tmp = tempfile.mkdtemp(prefix="test_client_dir_")
    d = Path(tmp)
    (d / "file1.txt").write_text("hello")
    yield d
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


class TestAsyncBucketClientUploadDirectory:
    """Tests for AsyncBucketClient.upload_directory()."""

    @pytest.mark.asyncio
    async def test_upload_directory_delegates_to_store(self, client, tmp_dir):
        """Client delegates to store.upload_directory and returns response."""
        result = await client.upload_directory(
            str(tmp_dir),
            prefix="test/",
        )

        assert isinstance(result, AsyncDirectoryUploadResponse)
        assert result.total_files == 5
        assert result.successful_uploads == 5

    @pytest.mark.asyncio
    async def test_upload_directory_translates_errors(
        self, client, mock_store, tmp_dir
    ):
        """Non-SubstrateSdkException is translated via translate_error."""
        original = RuntimeError("oops")

        async def _fail(local_source_directory, **kwargs):
            raise original

        mock_store.do_upload_directory = _fail

        with pytest.raises(BlobAsyncError) as exc_info:
            await client.upload_directory(str(tmp_dir), prefix="")
        assert exc_info.value.__cause__ is original

    @pytest.mark.asyncio
    async def test_upload_directory_passes_substrate_exceptions(
        self, client, mock_store, tmp_dir
    ):
        """SubstrateSdkException subclasses pass through untranslated."""
        with pytest.raises(InvalidArgumentException):
            await client.upload_directory("/nonexistent/path", prefix="")


class TestAsyncBucketClientDownloadDirectory:
    """Tests for AsyncBucketClient.download_directory()."""

    @pytest.mark.asyncio
    async def test_download_directory_delegates_to_store(self, client):
        """Client delegates to store.download_directory and returns response."""
        result = await client.download_directory("data/", "/tmp/test")

        assert isinstance(result, AsyncDirectoryDownloadResponse)
        assert result.total_files == 3
        assert result.successful_downloads == 3

    @pytest.mark.asyncio
    async def test_download_directory_translates_errors(self, client, mock_store):
        """Non-SubstrateSdkException is translated via translate_error."""
        original = RuntimeError("connection failed")

        async def _fail(prefix, local_destination_directory, **kwargs):
            raise original

        mock_store.do_download_directory = _fail

        with pytest.raises(BlobAsyncError) as exc_info:
            await client.download_directory("data/", "/tmp/test")
        assert exc_info.value.__cause__ is original

    @pytest.mark.asyncio
    async def test_download_directory_passes_substrate_exceptions(
        self, client, mock_store
    ):
        """SubstrateSdkException subclasses pass through untranslated."""
        with pytest.raises(InvalidArgumentException):
            await client.download_directory("", "/tmp/test")

    @pytest.mark.asyncio
    async def test_download_directory_validates_empty_destination(self, client):
        """Validates that local_destination_directory is not empty."""
        with pytest.raises(InvalidArgumentException):
            await client.download_directory("data/", "")

    @pytest.mark.asyncio
    async def test_download_directory_response_has_failures_property(
        self, client, mock_store
    ):
        """AsyncDirectoryDownloadResponse.has_failures works correctly."""

        async def _with_failures(prefix, local_destination_directory, **kwargs):
            return AsyncDirectoryDownloadResponse(
                total_files=2,
                successful_downloads=1,
                failed_transfers=[
                    FailedAsyncBlobDownload(
                        destination=Path("/tmp/bad.txt"),
                        exception=RuntimeError("fail"),
                    )
                ],
                total_bytes_transferred=100,
            )

        mock_store.do_download_directory = _with_failures

        result = await client.download_directory("data/", "/tmp/test")

        assert result.has_failures is True
        assert result.successful_downloads == 1
        assert len(result.failed_transfers) == 1


class TestAsyncBucketClientDeleteDirectory:
    """Tests for AsyncBucketClient.delete_directory()."""

    @pytest.mark.asyncio
    async def test_delete_directory_delegates_to_store(self, client, mock_store):
        """Client forwards prefix and concurrency, and returns nothing."""
        result = await client.delete_directory("data/", max_concurrency=4)

        assert result is None
        assert mock_store.delete_directory_calls == [
            {"prefix": "data/", "max_concurrency": 4}
        ]

    @pytest.mark.asyncio
    async def test_delete_directory_defaults_concurrency(self, client, mock_store):
        await client.delete_directory("data/")

        assert mock_store.delete_directory_calls[0]["max_concurrency"] == 10

    @pytest.mark.asyncio
    async def test_delete_directory_forwards_empty_prefix(self, client, mock_store):
        """An empty prefix is a legal whole-bucket target, not a validation error."""
        await client.delete_directory("")

        assert mock_store.delete_directory_calls == [
            {"prefix": "", "max_concurrency": 10}
        ]

    @pytest.mark.asyncio
    async def test_delete_directory_forwards_none_prefix(self, client, mock_store):
        await client.delete_directory(None)

        assert mock_store.delete_directory_calls == [
            {"prefix": None, "max_concurrency": 10}
        ]

    @pytest.mark.asyncio
    async def test_delete_directory_does_not_normalize_prefix(self, client, mock_store):
        """The prefix reaches the store verbatim, with no trailing slash added."""
        await client.delete_directory("logs")

        assert mock_store.delete_directory_calls[0]["prefix"] == "logs"

    @pytest.mark.asyncio
    async def test_delete_directory_translates_errors(self, client, mock_store):
        """Non-SubstrateSdkException is translated via translate_error."""
        original = RuntimeError("connection failed")
        mock_store.delete_directory_error = original

        with pytest.raises(BlobAsyncError) as exc_info:
            await client.delete_directory("data/")
        assert exc_info.value.__cause__ is original

    @pytest.mark.asyncio
    async def test_delete_directory_passes_substrate_exceptions(
        self, client, mock_store
    ):
        """SubstrateSdkException subclasses pass through untranslated."""
        original = InvalidArgumentException("bad prefix")
        mock_store.delete_directory_error = original

        with pytest.raises(InvalidArgumentException) as exc_info:
            await client.delete_directory("data/")
        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_delete_directory_validates_max_concurrency(self, client, mock_store):
        with pytest.raises(InvalidArgumentException, match="max_concurrency"):
            await client.delete_directory("data/", max_concurrency=0)

        assert mock_store.delete_directory_calls == []
