"""Unit tests for AWS async existence check implementation.

Tests validate AwsAsyncBlobStore.do_does_object_exist and do_does_bucket_exist:
- AWS head_object success → True
- AWS head_object 404 → False
- AWS head_object NoSuchKey → False
- AWS head_object with VersionId
- AWS head_object 403 → raises
- AWS head_object NoCredentialsError → raises
- AWS head_bucket success → True
- AWS head_bucket 404 → False
- AWS head_bucket NoSuchBucket → False
- AWS head_bucket 403 → raises
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore

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
    mock.head_bucket = AsyncMock()
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


# Tests for do_does_object_exist


@pytest.mark.asyncio
class TestAwsAsyncObjectExists:
    """Tests for AwsAsyncBlobStore.do_does_object_exist."""

    async def test_head_object_success_returns_true(self, store, mock_s3_client):
        """head_object success returns True."""
        mock_s3_client.head_object.return_value = {
            "ContentLength": 1024,
            "ETag": '"abc123"',
        }
        result = await store.do_does_object_exist("test-key.txt", version_id=None)
        assert result is True
        mock_s3_client.head_object.assert_awaited_once_with(
            Bucket="test-bucket", Key="test-key.txt"
        )

    async def test_head_object_404_returns_false(self, store, mock_s3_client):
        """head_object 404 returns False."""
        mock_s3_client.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "404", "Message": "Not Found"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        )
        result = await store.do_does_object_exist("missing-key.txt", version_id=None)
        assert result is False

    async def test_head_object_no_such_key_returns_false(self, store, mock_s3_client):
        """head_object NoSuchKey returns False."""
        mock_s3_client.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "NoSuchKey", "Message": "The key does not exist"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        )
        result = await store.do_does_object_exist("missing-key.txt", version_id=None)
        assert result is False

    async def test_head_object_with_version_id(self, store, mock_s3_client):
        """head_object with VersionId."""
        mock_s3_client.head_object.return_value = {
            "ContentLength": 1024,
            "VersionId": "v123",
        }
        result = await store.do_does_object_exist("test-key.txt", version_id="v123")
        assert result is True
        mock_s3_client.head_object.assert_awaited_once_with(
            Bucket="test-bucket", Key="test-key.txt", VersionId="v123"
        )

    async def test_head_object_403_raises(self, store, mock_s3_client):
        """head_object 403 raises ClientError (propagates raw to client layer)."""
        mock_s3_client.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "Forbidden"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "HeadObject",
        )
        with pytest.raises(ClientError):
            await store.do_does_object_exist("test-key.txt", version_id=None)

    async def test_head_object_no_credentials_raises(self, store, mock_s3_client):
        """head_object NoCredentialsError propagates raw to client layer."""
        mock_s3_client.head_object.side_effect = NoCredentialsError()
        with pytest.raises(NoCredentialsError):
            await store.do_does_object_exist("test-key.txt", version_id=None)

    async def test_head_object_botocore_error_raises(self, store, mock_s3_client):
        """head_object BotoCoreError propagates raw to client layer."""
        mock_s3_client.head_object.side_effect = BotoCoreError()
        with pytest.raises(BotoCoreError):
            await store.do_does_object_exist("test-key.txt", version_id=None)

    async def test_head_object_unknown_exception_raises(self, store, mock_s3_client):
        """head_object unknown exception propagates raw to client layer."""
        mock_s3_client.head_object.side_effect = RuntimeError("network error")
        with pytest.raises(RuntimeError, match="network error"):
            await store.do_does_object_exist("test-key.txt", version_id=None)

    async def test_head_object_500_raises(self, store, mock_s3_client):
        """head_object 500 raises ClientError (not False)."""
        mock_s3_client.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "InternalError", "Message": "Server Error"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "HeadObject",
        )
        with pytest.raises(ClientError):
            await store.do_does_object_exist("test-key.txt", version_id=None)


# Tests for do_does_bucket_exist


@pytest.mark.asyncio
class TestAwsAsyncBucketExists:
    """Tests for AwsAsyncBlobStore.do_does_bucket_exist."""

    async def test_head_bucket_success_returns_true(self, store, mock_s3_client):
        """head_bucket success returns True."""
        mock_s3_client.head_bucket.return_value = {}
        result = await store.do_does_bucket_exist()
        assert result is True
        mock_s3_client.head_bucket.assert_awaited_once_with(Bucket="test-bucket")

    async def test_head_bucket_404_returns_false(self, store, mock_s3_client):
        """head_bucket 404 returns False."""
        mock_s3_client.head_bucket.side_effect = ClientError(
            {
                "Error": {"Code": "404", "Message": "Not Found"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadBucket",
        )
        result = await store.do_does_bucket_exist()
        assert result is False

    async def test_head_bucket_no_such_bucket_returns_false(
        self, store, mock_s3_client
    ):
        """head_bucket NoSuchBucket returns False."""
        mock_s3_client.head_bucket.side_effect = ClientError(
            {
                "Error": {
                    "Code": "NoSuchBucket",
                    "Message": "The bucket does not exist",
                },
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadBucket",
        )
        result = await store.do_does_bucket_exist()
        assert result is False

    async def test_head_bucket_403_raises(self, store, mock_s3_client):
        """head_bucket 403 raises ClientError (propagates raw to client layer)."""
        mock_s3_client.head_bucket.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "Forbidden"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "HeadBucket",
        )
        with pytest.raises(ClientError):
            await store.do_does_bucket_exist()

    async def test_head_bucket_no_credentials_raises(self, store, mock_s3_client):
        """head_bucket NoCredentialsError propagates raw to client layer."""
        mock_s3_client.head_bucket.side_effect = NoCredentialsError()
        with pytest.raises(NoCredentialsError):
            await store.do_does_bucket_exist()

    async def test_head_bucket_botocore_error_raises(self, store, mock_s3_client):
        """head_bucket BotoCoreError propagates raw to client layer."""
        mock_s3_client.head_bucket.side_effect = BotoCoreError()
        with pytest.raises(BotoCoreError):
            await store.do_does_bucket_exist()

    async def test_head_bucket_unknown_exception_raises(self, store, mock_s3_client):
        """head_bucket unknown exception propagates raw to client layer."""
        mock_s3_client.head_bucket.side_effect = RuntimeError("network error")
        with pytest.raises(RuntimeError, match="network error"):
            await store.do_does_bucket_exist()

    async def test_head_bucket_500_raises(self, store, mock_s3_client):
        """head_bucket 500 raises ClientError (not False)."""
        mock_s3_client.head_bucket.side_effect = ClientError(
            {
                "Error": {"Code": "InternalError", "Message": "Server Error"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "HeadBucket",
        )
        with pytest.raises(ClientError):
            await store.do_does_bucket_exist()
