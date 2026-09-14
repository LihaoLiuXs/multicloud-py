"""Unit tests for AWS async get_metadata implementation.

Tests validate AwsAsyncBlobStore.do_get_metadata:
- head_object success → maps all BlobMetadata fields (etag quotes stripped,
  content_length from ContentLength, last_modified passthrough,
  created_time == last_modified)
- No body is downloaded (get_object is never awaited)
- head_object with VersionId (versioned access)
- head_object 404 / NoSuchKey / NoSuchBucket → BlobNotFoundError (with key + cause)
- head_object 403 → propagates raw to the client layer
- head_object network / unknown errors → propagate raw
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore
from multicloud.blob.blob_client.async_blob.async_driver import (
    BlobMetadata,
    BlobNotFoundError,
)

# Mock botocore exceptions
try:
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:
    pytest.skip("aiobotocore not installed", allow_module_level=True)


# Fixtures


@pytest.fixture
def mock_s3_client():
    """Create a mock S3 client."""
    mock = AsyncMock()
    mock.head_object = AsyncMock()
    mock.get_object = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def store(mock_s3_client):
    """Create an AwsAsyncBlobStore with a mocked S3 client."""
    with patch(
        "multicloud.blob.blob_aws.async_blob.aws_async_blob_store.get_aio_session"
    ):
        store = AwsAsyncBlobStore(bucket="test-bucket", region="us-west-2")
        # Inject the mock client to bypass lazy initialization
        store._client = mock_s3_client
        store._client_cm = MagicMock()
        yield store
        await store.close()


# Tests for do_get_metadata


@pytest.mark.asyncio
class TestAwsAsyncGetMetadata:
    """Tests for AwsAsyncBlobStore.do_get_metadata."""

    async def test_success_maps_all_fields(self, store, mock_s3_client):
        """head_object success maps every BlobMetadata field."""
        last_modified = datetime(2026, 7, 20, 12, 30, 45, tzinfo=timezone.utc)
        mock_s3_client.head_object.return_value = {
            "ContentLength": 2048,
            "ContentType": "application/json",
            "ETag": '"abc123def456"',
            "VersionId": "v-42",
            "Metadata": {"team": "multicloud", "env": "prod"},
            "LastModified": last_modified,
        }

        result = await store.do_get_metadata("test-key.txt", version_id=None)

        assert isinstance(result, BlobMetadata)
        assert result.key == "test-key.txt"
        assert result.content_length == 2048
        assert result.content_type == "application/json"
        # ETag quotes are stripped.
        assert result.e_tag == "abc123def456"
        assert result.version_id == "v-42"
        assert result.metadata == {"team": "multicloud", "env": "prod"}
        assert result.last_modified == last_modified
        # S3 HEAD has no distinct creation timestamp; created_time mirrors it.
        assert result.created_time == last_modified
        mock_s3_client.head_object.assert_awaited_once_with(
            Bucket="test-bucket", Key="test-key.txt"
        )

    async def test_does_not_download_body(self, store, mock_s3_client):
        """get_metadata must not download the object body (HEAD only)."""
        mock_s3_client.head_object.return_value = {
            "ContentLength": 10,
            "ETag": '"etag"',
        }

        await store.do_get_metadata("test-key.txt", version_id=None)

        mock_s3_client.get_object.assert_not_awaited()

    async def test_created_time_equals_last_modified(self, store, mock_s3_client):
        """created_time is set to last_modified for S3 (no distinct field)."""
        last_modified = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        mock_s3_client.head_object.return_value = {
            "ContentLength": 1,
            "LastModified": last_modified,
        }

        result = await store.do_get_metadata("k", version_id=None)

        assert result.created_time is result.last_modified

    async def test_missing_optional_fields_default(self, store, mock_s3_client):
        """A sparse head_object response still yields a valid BlobMetadata."""
        mock_s3_client.head_object.return_value = {"ContentLength": 0}

        result = await store.do_get_metadata("empty.txt", version_id=None)

        assert result.key == "empty.txt"
        assert result.content_length == 0
        assert result.content_type is None
        assert result.e_tag is None
        assert result.version_id is None
        assert result.metadata == {}
        assert result.last_modified is None
        assert result.created_time is None

    async def test_with_version_id(self, store, mock_s3_client):
        """Versioned access passes VersionId to head_object."""
        mock_s3_client.head_object.return_value = {
            "ContentLength": 512,
            "VersionId": "v123",
        }

        result = await store.do_get_metadata("test-key.txt", version_id="v123")

        assert result.version_id == "v123"
        mock_s3_client.head_object.assert_awaited_once_with(
            Bucket="test-bucket", Key="test-key.txt", VersionId="v123"
        )

    async def test_404_raises_blob_not_found(self, store, mock_s3_client):
        """head_object 404 raises BlobNotFoundError carrying key + cause."""
        cause = ClientError(
            {
                "Error": {"Code": "404", "Message": "Not Found"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        )
        mock_s3_client.head_object.side_effect = cause

        with pytest.raises(BlobNotFoundError) as excinfo:
            await store.do_get_metadata("missing-key.txt", version_id=None)

        assert excinfo.value.key == "missing-key.txt"
        assert excinfo.value.cause is cause

    async def test_no_such_key_raises_blob_not_found(self, store, mock_s3_client):
        """head_object NoSuchKey raises BlobNotFoundError."""
        mock_s3_client.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "NoSuchKey", "Message": "The key does not exist"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        )

        with pytest.raises(BlobNotFoundError):
            await store.do_get_metadata("missing-key.txt", version_id=None)

    async def test_no_such_bucket_raises_blob_not_found(self, store, mock_s3_client):
        """head_object NoSuchBucket raises BlobNotFoundError."""
        mock_s3_client.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "NoSuchBucket", "Message": "No such bucket"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        )

        with pytest.raises(BlobNotFoundError):
            await store.do_get_metadata("test-key.txt", version_id=None)

    async def test_403_propagates(self, store, mock_s3_client):
        """head_object 403 propagates raw to the client layer (not BlobNotFound)."""
        mock_s3_client.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "Forbidden"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "HeadObject",
        )

        with pytest.raises(ClientError):
            await store.do_get_metadata("test-key.txt", version_id=None)

    async def test_500_propagates(self, store, mock_s3_client):
        """head_object 500 propagates raw (not BlobNotFound)."""
        mock_s3_client.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "InternalError", "Message": "Server Error"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "HeadObject",
        )

        with pytest.raises(ClientError):
            await store.do_get_metadata("test-key.txt", version_id=None)

    async def test_no_credentials_propagates(self, store, mock_s3_client):
        """head_object NoCredentialsError propagates raw to the client layer."""
        mock_s3_client.head_object.side_effect = NoCredentialsError()

        with pytest.raises(NoCredentialsError):
            await store.do_get_metadata("test-key.txt", version_id=None)

    async def test_botocore_error_propagates(self, store, mock_s3_client):
        """head_object BotoCoreError propagates raw to the client layer."""
        mock_s3_client.head_object.side_effect = BotoCoreError()

        with pytest.raises(BotoCoreError):
            await store.do_get_metadata("test-key.txt", version_id=None)
