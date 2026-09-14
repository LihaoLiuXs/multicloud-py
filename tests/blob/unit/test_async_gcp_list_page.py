"""Unit tests for GCP async list_page implementation.

Tests validate GcpAsyncBlobStore.do_list_page (and the public list_page
validation path):
- single-page listing → maps items[] to BlobInfo (size string→int, etag,
  RFC3339 updated→last_modified datetime, storageClass; version_id stays
  None to match sync GCP + async AWS)
- multi-page manual iteration → nextPageToken surfaces as
  next_pagination_token and is echoed back as params["pageToken"] on page 2
- prefix / delimiter / maxResults forwarding to list_objects params
  (omitted when None; maxResults always a string)
- prefixes[] → common_prefixes
- empty result ({}) → empty blobs, is_truncated False, token None
- trailing 'Z' timestamp regression → tz-aware datetime
- max_results < 1 → InvalidArgumentException (public list_page validation)
- list_objects errors propagate raw to the client layer
"""

from __future__ import annotations

from datetime import timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from multicloud.blob.blob_client.async_blob.async_driver import (
    BlobInfo,
    ListBlobsPageResponse,
)
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
    mock.list_objects = AsyncMock()
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


def _client_response_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=status,
        message="error",
    )


@pytest.mark.asyncio
class TestGcpAsyncListPage:
    """Tests for GcpAsyncBlobStore.do_list_page."""

    async def test_single_page_maps_fields(self, store, mock_storage):
        """A single, non-truncated page maps every BlobInfo field."""
        mock_storage.list_objects.return_value = {
            "items": [
                {
                    "name": "a.txt",
                    "size": "100",
                    "etag": "etag-a",
                    "updated": "2026-07-20T12:30:45.123456Z",
                    "storageClass": "STANDARD",
                    "generation": "1234567890",
                },
                {
                    "name": "b.txt",
                    "size": "200",
                    "etag": "etag-b",
                    "updated": "2026-07-20T12:30:46Z",
                    "storageClass": "NEARLINE",
                    "generation": "1234567891",
                },
            ],
        }

        result = await store.do_list_page(
            prefix=None, delimiter=None, max_results=1000, pagination_token=None
        )

        assert isinstance(result, ListBlobsPageResponse)
        assert len(result.blobs) == 2
        first = result.blobs[0]
        assert isinstance(first, BlobInfo)
        assert first.key == "a.txt"
        # GCS "size" is a decimal string; coerced to int.
        assert first.size == 100
        assert first.e_tag == "etag-a"
        # updated (RFC3339) parsed to a tz-aware datetime.
        assert first.last_modified is not None
        assert first.last_modified.year == 2026
        assert first.last_modified.microsecond == 123456
        assert first.storage_class == "STANDARD"
        # version_id is intentionally NOT populated from GCS "generation":
        # sync GCP and async AWS both leave it None, so async GCP matches for
        # uniform cross-provider list_page results (even though "generation"
        # is present in the payload above).
        assert first.version_id is None
        assert result.blobs[1].key == "b.txt"
        assert result.blobs[1].storage_class == "NEARLINE"
        assert result.common_prefixes == []
        assert result.is_truncated is False
        assert result.next_pagination_token is None

    async def test_multi_page_manual_iteration(self, store, mock_storage):
        """A page with nextPageToken surfaces it; page 2 echoes it back."""
        mock_storage.list_objects.side_effect = [
            {
                "items": [{"name": "a.txt", "size": "1"}],
                "nextPageToken": "t1",
            },
            {
                "items": [{"name": "b.txt", "size": "2"}],
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

        # Page 2 request carried the page token.
        second_call = mock_storage.list_objects.await_args_list[1]
        assert second_call.kwargs["params"]["pageToken"] == "t1"

    async def test_prefix_delimiter_max_results_forwarded(self, store, mock_storage):
        """prefix/delimiter/max_results map to list_objects params."""
        mock_storage.list_objects.return_value = {}

        await store.do_list_page(
            prefix="photos/",
            delimiter="/",
            max_results=42,
            pagination_token=None,
        )

        call = mock_storage.list_objects.await_args
        # Positional bucket, keyword params.
        assert call.args[0] == "test-bucket"
        params = call.kwargs["params"]
        assert params["prefix"] == "photos/"
        assert params["delimiter"] == "/"
        # maxResults is always sent as a string.
        assert params["maxResults"] == "42"
        # pageToken omitted when pagination_token is None.
        assert "pageToken" not in params

    async def test_optional_params_omitted_when_none(self, store, mock_storage):
        """prefix/delimiter/pageToken are omitted when None."""
        mock_storage.list_objects.return_value = {}

        await store.do_list_page(
            prefix=None, delimiter=None, max_results=1000, pagination_token=None
        )

        params = mock_storage.list_objects.await_args.kwargs["params"]
        assert "prefix" not in params
        assert "delimiter" not in params
        assert "pageToken" not in params
        # maxResults is always sent.
        assert params["maxResults"] == "1000"

    async def test_common_prefixes_mapped(self, store, mock_storage):
        """prefixes[] maps to common_prefixes."""
        mock_storage.list_objects.return_value = {
            "items": [],
            "prefixes": ["photos/", "videos/"],
        }

        result = await store.do_list_page(
            prefix=None, delimiter="/", max_results=1000, pagination_token=None
        )

        assert result.blobs == []
        assert result.common_prefixes == ["photos/", "videos/"]

    async def test_empty_result(self, store, mock_storage):
        """An empty GCS JSON payload → empty page, not truncated, no token."""
        mock_storage.list_objects.return_value = {}

        result = await store.do_list_page(
            prefix="nomatch/", delimiter=None, max_results=1000, pagination_token=None
        )

        assert result.blobs == []
        assert result.common_prefixes == []
        assert result.is_truncated is False
        assert result.next_pagination_token is None

    async def test_null_prefixes_defaults_to_empty(self, store, mock_storage):
        """A null 'prefixes' value defaults to an empty list."""
        mock_storage.list_objects.return_value = {"items": [], "prefixes": None}

        result = await store.do_list_page(
            prefix=None, delimiter="/", max_results=1000, pagination_token=None
        )

        assert result.common_prefixes == []

    async def test_sparse_item_defaults(self, store, mock_storage):
        """An item with only a name still yields a valid BlobInfo."""
        mock_storage.list_objects.return_value = {"items": [{"name": "a.txt"}]}

        result = await store.do_list_page(
            prefix=None, delimiter=None, max_results=1000, pagination_token=None
        )

        blob = result.blobs[0]
        assert blob.key == "a.txt"
        assert blob.size is None
        assert blob.e_tag is None
        assert blob.last_modified is None
        assert blob.storage_class is None
        assert blob.version_id is None

    async def test_non_numeric_size_coerces_to_none(self, store, mock_storage):
        """A non-numeric size is defensively coerced to None."""
        mock_storage.list_objects.return_value = {
            "items": [{"name": "a.txt", "size": "not-a-number"}]
        }

        result = await store.do_list_page(
            prefix=None, delimiter=None, max_results=1000, pagination_token=None
        )

        assert result.blobs[0].size is None

    async def test_timestamp_z_suffix_regression(self, store, mock_storage):
        """A trailing 'Z' in 'updated' yields a tz-aware datetime (py3.10 floor)."""
        mock_storage.list_objects.return_value = {
            "items": [{"name": "a.txt", "updated": "2026-07-20T12:30:45Z"}]
        }

        result = await store.do_list_page(
            prefix=None, delimiter=None, max_results=1000, pagination_token=None
        )

        last_modified = result.blobs[0].last_modified
        assert last_modified is not None
        assert last_modified.tzinfo is not None
        assert last_modified.utcoffset() == timezone.utc.utcoffset(None)

    async def test_error_propagates(self, store, mock_storage):
        """list_objects errors propagate raw to the client layer."""
        mock_storage.list_objects.side_effect = _client_response_error(403)

        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_list_page(
                prefix=None, delimiter=None, max_results=1000, pagination_token=None
            )

    async def test_uninitialized_storage_raises_runtime_error(self, store):
        """If the client can't be initialized, the None-storage guard fires.

        Mirrors the guard in every other GCP op: ``_ensure_client`` returning
        without a live ``_storage`` (e.g. a failed lazy init) raises rather
        than dereferencing None.
        """
        from unittest.mock import AsyncMock

        store._storage = None
        # _ensure_client is a no-op so _storage stays None through the guard.
        store._ensure_client = AsyncMock()

        with pytest.raises(RuntimeError, match="not initialized"):
            await store.do_list_page(
                prefix=None, delimiter=None, max_results=1000, pagination_token=None
            )

    async def test_public_list_page_rejects_non_positive_max_results(
        self, store, mock_storage
    ):
        """The public list_page validates max_results before any GCS call."""
        with pytest.raises(InvalidArgumentException):
            await store.list_page(max_results=0)

        mock_storage.list_objects.assert_not_awaited()

    async def test_public_list_page_delegates(self, store, mock_storage):
        """The public list_page forwards params through to list_objects."""
        mock_storage.list_objects.return_value = {
            "items": [{"name": "a.txt", "size": "5"}],
        }

        result = await store.list_page(prefix="p/", delimiter="/", max_results=10)

        assert [b.key for b in result.blobs] == ["a.txt"]
        params = mock_storage.list_objects.await_args.kwargs["params"]
        assert params["prefix"] == "p/"
        assert params["delimiter"] == "/"
        assert params["maxResults"] == "10"
