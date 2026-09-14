"""Unit tests for AWS async list_page implementation.

Tests validate AwsAsyncBlobStore.do_list_page (and the public list_page
validation path):
- single-page listing → maps Contents[] to BlobInfo (etag quotes stripped),
  is_truncated False, next_pagination_token None
- multi-page manual iteration → NextContinuationToken surfaces as
  next_pagination_token and is echoed back as ContinuationToken on page 2
- prefix / delimiter / max_results forwarding to list_objects_v2 kwargs
  (omitted when None)
- CommonPrefixes[] → common_prefixes
- empty result → empty blobs, is_truncated False, token None
- max_results < 1 → InvalidArgumentException (public list_page validation)
- list_objects_v2 errors propagate raw to the client layer
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore
from multicloud.blob.blob_client.async_blob.async_driver import (
    BlobInfo,
    ListBlobsPageResponse,
)
from multicloud.common.exceptions import InvalidArgumentException

# Mock botocore exceptions
try:
    from botocore.exceptions import ClientError
except ImportError:
    pytest.skip("aiobotocore not installed", allow_module_level=True)


# Fixtures


@pytest.fixture
def mock_s3_client():
    """Create a mock S3 client."""
    mock = AsyncMock()
    mock.list_objects_v2 = AsyncMock()
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


@pytest.mark.asyncio
class TestAwsAsyncListPage:
    """Tests for AwsAsyncBlobStore.do_list_page."""

    async def test_single_page_maps_fields(self, store, mock_s3_client):
        """A single, non-truncated page maps every BlobInfo field."""
        last_modified = datetime(2026, 7, 20, 12, 30, 45, tzinfo=timezone.utc)
        mock_s3_client.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": "a.txt",
                    "Size": 100,
                    "ETag": '"etag-a"',
                    "LastModified": last_modified,
                    "StorageClass": "STANDARD",
                },
                {
                    "Key": "b.txt",
                    "Size": 200,
                    "ETag": '"etag-b"',
                    "LastModified": last_modified,
                    "StorageClass": "GLACIER",
                },
            ],
            "IsTruncated": False,
        }

        result = await store.do_list_page(
            prefix=None, delimiter=None, max_results=1000, pagination_token=None
        )

        assert isinstance(result, ListBlobsPageResponse)
        assert len(result.blobs) == 2
        first = result.blobs[0]
        assert isinstance(first, BlobInfo)
        assert first.key == "a.txt"
        assert first.size == 100
        # ETag quotes are stripped.
        assert first.e_tag == "etag-a"
        assert first.last_modified == last_modified
        assert first.storage_class == "STANDARD"
        assert result.blobs[1].key == "b.txt"
        assert result.blobs[1].storage_class == "GLACIER"
        assert result.common_prefixes == []
        assert result.is_truncated is False
        assert result.next_pagination_token is None

    async def test_multi_page_manual_iteration(self, store, mock_s3_client):
        """A truncated page surfaces the token; page 2 echoes it back."""
        mock_s3_client.list_objects_v2.side_effect = [
            {
                "Contents": [{"Key": "a.txt", "Size": 1, "ETag": '"e1"'}],
                "IsTruncated": True,
                "NextContinuationToken": "t1",
            },
            {
                "Contents": [{"Key": "b.txt", "Size": 2, "ETag": '"e2"'}],
                "IsTruncated": False,
            },
        ]

        page1 = await store.do_list_page(
            prefix=None, delimiter=None, max_results=1, pagination_token=None
        )
        assert page1.is_truncated is True
        assert page1.next_pagination_token == "t1"
        assert [b.key for b in page1.blobs] == ["a.txt"]

        # Caller feeds the token back to fetch the next page.
        page2 = await store.do_list_page(
            prefix=None,
            delimiter=None,
            max_results=1,
            pagination_token=page1.next_pagination_token,
        )
        assert page2.is_truncated is False
        assert page2.next_pagination_token is None
        assert [b.key for b in page2.blobs] == ["b.txt"]

        # Page 2 request carried the continuation token.
        second_call = mock_s3_client.list_objects_v2.await_args_list[1]
        assert second_call.kwargs["ContinuationToken"] == "t1"

    async def test_prefix_delimiter_max_results_forwarded(self, store, mock_s3_client):
        """prefix/delimiter/max_results map to list_objects_v2 kwargs."""
        mock_s3_client.list_objects_v2.return_value = {"IsTruncated": False}

        await store.do_list_page(
            prefix="photos/",
            delimiter="/",
            max_results=42,
            pagination_token=None,
        )

        call = mock_s3_client.list_objects_v2.await_args
        assert call.kwargs["Bucket"] == "test-bucket"
        assert call.kwargs["Prefix"] == "photos/"
        assert call.kwargs["Delimiter"] == "/"
        assert call.kwargs["MaxKeys"] == 42
        # ContinuationToken omitted when pagination_token is None.
        assert "ContinuationToken" not in call.kwargs

    async def test_optional_params_omitted_when_none(self, store, mock_s3_client):
        """Prefix/Delimiter/ContinuationToken are omitted when None."""
        mock_s3_client.list_objects_v2.return_value = {"IsTruncated": False}

        await store.do_list_page(
            prefix=None, delimiter=None, max_results=1000, pagination_token=None
        )

        call = mock_s3_client.list_objects_v2.await_args
        assert "Prefix" not in call.kwargs
        assert "Delimiter" not in call.kwargs
        assert "ContinuationToken" not in call.kwargs
        # MaxKeys is always sent.
        assert call.kwargs["MaxKeys"] == 1000

    async def test_common_prefixes_mapped(self, store, mock_s3_client):
        """CommonPrefixes[].Prefix maps to common_prefixes."""
        mock_s3_client.list_objects_v2.return_value = {
            "Contents": [],
            "CommonPrefixes": [{"Prefix": "photos/"}, {"Prefix": "videos/"}],
            "IsTruncated": False,
        }

        result = await store.do_list_page(
            prefix=None, delimiter="/", max_results=1000, pagination_token=None
        )

        assert result.blobs == []
        assert result.common_prefixes == ["photos/", "videos/"]

    async def test_empty_result(self, store, mock_s3_client):
        """No matching objects → empty page, not truncated, no token."""
        mock_s3_client.list_objects_v2.return_value = {"IsTruncated": False}

        result = await store.do_list_page(
            prefix="nomatch/", delimiter=None, max_results=1000, pagination_token=None
        )

        assert result.blobs == []
        assert result.common_prefixes == []
        assert result.is_truncated is False
        assert result.next_pagination_token is None

    async def test_missing_is_truncated_defaults_false(self, store, mock_s3_client):
        """A response without IsTruncated defaults to not-truncated."""
        mock_s3_client.list_objects_v2.return_value = {"Contents": [{"Key": "a.txt"}]}

        result = await store.do_list_page(
            prefix=None, delimiter=None, max_results=1000, pagination_token=None
        )

        assert result.is_truncated is False
        # A sparse entry (only Key) still yields a valid BlobInfo.
        assert result.blobs[0].key == "a.txt"
        assert result.blobs[0].size is None
        assert result.blobs[0].e_tag is None

    async def test_error_propagates(self, store, mock_s3_client):
        """list_objects_v2 errors propagate raw to the client layer."""
        mock_s3_client.list_objects_v2.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "Forbidden"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "ListObjectsV2",
        )

        with pytest.raises(ClientError):
            await store.do_list_page(
                prefix=None, delimiter=None, max_results=1000, pagination_token=None
            )

    async def test_public_list_page_rejects_non_positive_max_results(
        self, store, mock_s3_client
    ):
        """The public list_page validates max_results before any S3 call."""
        with pytest.raises(InvalidArgumentException):
            await store.list_page(max_results=0)

        mock_s3_client.list_objects_v2.assert_not_awaited()

    async def test_public_list_page_delegates(self, store, mock_s3_client):
        """The public list_page forwards params through to list_objects_v2."""
        mock_s3_client.list_objects_v2.return_value = {
            "Contents": [{"Key": "a.txt", "Size": 5, "ETag": '"e"'}],
            "IsTruncated": False,
        }

        result = await store.list_page(prefix="p/", delimiter="/", max_results=10)

        assert [b.key for b in result.blobs] == ["a.txt"]
        call = mock_s3_client.list_objects_v2.await_args
        assert call.kwargs["Prefix"] == "p/"
        assert call.kwargs["Delimiter"] == "/"
        assert call.kwargs["MaxKeys"] == 10
