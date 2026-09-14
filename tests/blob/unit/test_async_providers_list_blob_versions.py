"""Unit tests for the AWS and GCP async list_blob_versions implementations.

Tests validate, for both providers:
- entries sharing the key's prefix are filtered out
- pagination is followed, and only as the caller consumes
- every field is mapped to the right version, not transposed between them
- a key with no versions yields nothing

AWS additionally: delete markers are excluded.
GCP additionally: is_latest comes from the deletion timestamp.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import GcpAsyncBlobStore
from multicloud.common.exceptions import UnAuthorizedException

try:
    import aiohttp
    from botocore.exceptions import ClientError
except ImportError:
    pytest.skip("async extras not installed", allow_module_level=True)


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "ListObjectVersions",
    )


def _client_response_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        request_info=MagicMock(), history=(), status=status, message="error"
    )


KEY = "docs/report.txt"
SIBLING = f"{KEY}.backup"

MODIFIED_ONE = datetime(2026, 1, 1, tzinfo=timezone.utc)
MODIFIED_TWO = datetime(2026, 2, 2, tzinfo=timezone.utc)
MODIFIED_THREE = datetime(2026, 3, 3, tzinfo=timezone.utc)


@pytest.fixture
def mock_s3_client():
    mock = AsyncMock()
    mock.list_object_versions = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def aws_store(mock_s3_client):
    with patch(
        "multicloud.blob.blob_aws.async_blob.aws_async_blob_store.get_aio_session"
    ):
        store = AwsAsyncBlobStore(bucket="versioned", region="us-west-2")
        store._client = mock_s3_client
        store._client_cm = MagicMock()
        yield store
        await store.close()


@pytest.fixture
def mock_storage():
    mock = AsyncMock()
    mock.list_objects = AsyncMock()
    mock.close = AsyncMock()
    return mock


@pytest_asyncio.fixture
async def gcp_store(mock_storage):
    store = GcpAsyncBlobStore(bucket="versioned", _storage=mock_storage)
    yield store
    await store.close()


@pytest.mark.asyncio
class TestAwsAsyncListBlobVersions:
    """Tests for AwsAsyncBlobStore.do_list_blob_versions."""

    async def test_maps_each_version_to_its_own_values(self, aws_store, mock_s3_client):
        """Distinct sizes and etags, so a transposition between versions fails."""
        mock_s3_client.list_object_versions.return_value = {
            "Versions": [
                {
                    "Key": KEY,
                    "VersionId": "v-3",
                    "Size": 33,
                    "ETag": '"etag-three"',
                    "LastModified": MODIFIED_THREE,
                    "StorageClass": "STANDARD",
                    "IsLatest": True,
                },
                {
                    "Key": KEY,
                    "VersionId": "v-1",
                    "Size": 11,
                    "ETag": '"etag-one"',
                    "LastModified": MODIFIED_ONE,
                    "StorageClass": "GLACIER",
                    "IsLatest": False,
                },
            ],
            "IsTruncated": False,
        }

        listed = [info async for info in aws_store.do_list_blob_versions(KEY)]

        assert [
            (
                i.version_id,
                i.size,
                i.e_tag,
                i.last_modified,
                i.storage_class,
                i.is_latest,
            )
            for i in listed
        ] == [
            ("v-3", 33, "etag-three", MODIFIED_THREE, "STANDARD", True),
            ("v-1", 11, "etag-one", MODIFIED_ONE, "GLACIER", False),
        ]

    async def test_prefix_siblings_are_filtered_out(self, aws_store, mock_s3_client):
        """The API is prefix-based, so a longer key must not be reported."""
        mock_s3_client.list_object_versions.return_value = {
            "Versions": [
                {"Key": SIBLING, "VersionId": "s-1", "Size": 5},
                {"Key": KEY, "VersionId": "v-1", "Size": 11},
            ],
            "IsTruncated": False,
        }

        listed = [info async for info in aws_store.do_list_blob_versions(KEY)]

        assert [i.version_id for i in listed] == ["v-1"]

    async def test_delete_markers_are_excluded(self, aws_store, mock_s3_client):
        """A delete marker is not a version of the content."""
        mock_s3_client.list_object_versions.return_value = {
            "Versions": [{"Key": KEY, "VersionId": "v-1", "Size": 11}],
            "DeleteMarkers": [{"Key": KEY, "VersionId": "dm-1", "IsLatest": True}],
            "IsTruncated": False,
        }

        listed = [info async for info in aws_store.do_list_blob_versions(KEY)]

        assert [i.version_id for i in listed] == ["v-1"]

    async def test_pagination_is_followed_with_both_markers(
        self, aws_store, mock_s3_client
    ):
        mock_s3_client.list_object_versions.side_effect = [
            {
                "Versions": [{"Key": KEY, "VersionId": "v-1", "Size": 11}],
                "IsTruncated": True,
                "NextKeyMarker": "km-1",
                "NextVersionIdMarker": "vm-1",
            },
            {
                "Versions": [{"Key": KEY, "VersionId": "v-2", "Size": 22}],
                "IsTruncated": False,
            },
        ]

        listed = [info async for info in aws_store.do_list_blob_versions(KEY)]

        assert [i.version_id for i in listed] == ["v-1", "v-2"]
        second_call = mock_s3_client.list_object_versions.call_args_list[1].kwargs
        assert second_call["KeyMarker"] == "km-1"
        assert second_call["VersionIdMarker"] == "vm-1"

    async def test_later_pages_are_not_fetched_until_needed(
        self, aws_store, mock_s3_client
    ):
        """Pagination must follow consumption, not run ahead of it."""
        mock_s3_client.list_object_versions.side_effect = [
            {
                "Versions": [{"Key": KEY, "VersionId": "v-1", "Size": 11}],
                "IsTruncated": True,
                "NextKeyMarker": "km-1",
                "NextVersionIdMarker": "vm-1",
            },
            {
                "Versions": [{"Key": KEY, "VersionId": "v-2", "Size": 22}],
                "IsTruncated": False,
            },
        ]

        iterator = aws_store.do_list_blob_versions(KEY)
        first = await iterator.__anext__()

        assert first.version_id == "v-1"
        assert mock_s3_client.list_object_versions.await_count == 1

        await iterator.aclose()

    async def test_request_targets_the_key_as_a_prefix(self, aws_store, mock_s3_client):
        mock_s3_client.list_object_versions.return_value = {"IsTruncated": False}

        [info async for info in aws_store.do_list_blob_versions(KEY)]

        sent = mock_s3_client.list_object_versions.call_args.kwargs
        assert sent == {"Bucket": "versioned", "Prefix": KEY}

    async def test_a_key_with_no_versions_yields_nothing(
        self, aws_store, mock_s3_client
    ):
        mock_s3_client.list_object_versions.return_value = {"IsTruncated": False}

        assert [info async for info in aws_store.do_list_blob_versions(KEY)] == []

    async def test_pagination_terminates_and_does_not_spin(
        self, aws_store, mock_s3_client
    ):
        """A finite side_effect makes a runaway loop fail instead of hang.

        With a single queued response, an implementation that ignores the
        termination condition asks for a second page and raises StopIteration
        here, rather than spinning forever and taking CI down with it --
        pytest-timeout is not installed.
        """
        mock_s3_client.list_object_versions.side_effect = [
            {
                "Versions": [{"Key": KEY, "VersionId": "v-1", "Size": 11}],
                "IsTruncated": False,
            }
        ]

        listed = [info async for info in aws_store.do_list_blob_versions(KEY)]

        assert [i.version_id for i in listed] == ["v-1"]
        assert mock_s3_client.list_object_versions.await_count == 1

    async def test_a_response_without_is_truncated_terminates(
        self, aws_store, mock_s3_client
    ):
        """Every field of this response is optional, so absence must not throw."""
        mock_s3_client.list_object_versions.return_value = {
            "Versions": [{"Key": KEY, "VersionId": "v-1", "Size": 11}]
        }

        listed = [info async for info in aws_store.do_list_blob_versions(KEY)]

        assert [i.version_id for i in listed] == ["v-1"]

    async def test_a_first_page_failure_is_translated(self, aws_store, mock_s3_client):
        """Calling an async generator runs no body, so the client cannot catch this.

        The client's try/except has already returned by the time the first page
        is fetched, so translation has to happen here or a native exception
        reaches the caller.
        """
        mock_s3_client.list_object_versions.side_effect = _client_error(
            "AccessDenied", 403
        )

        with pytest.raises(UnAuthorizedException):
            [info async for info in aws_store.do_list_blob_versions(KEY)]

    async def test_a_later_page_failure_is_translated(self, aws_store, mock_s3_client):
        """A failure part-way through must not truncate the listing silently."""
        mock_s3_client.list_object_versions.side_effect = [
            {
                "Versions": [{"Key": KEY, "VersionId": "v-1", "Size": 11}],
                "IsTruncated": True,
                "NextKeyMarker": "km-1",
                "NextVersionIdMarker": "vm-1",
            },
            _client_error("AccessDenied", 403),
        ]

        seen = []
        with pytest.raises(UnAuthorizedException):
            async for info in aws_store.do_list_blob_versions(KEY):
                seen.append(info.version_id)

        assert seen == ["v-1"], "the first page should have been delivered"


@pytest.mark.asyncio
class TestGcpAsyncListBlobVersions:
    """Tests for GcpAsyncBlobStore.do_list_blob_versions."""

    async def test_maps_each_version_to_its_own_values(self, gcp_store, mock_storage):
        """Distinct sizes and etags, so a transposition between versions fails."""
        mock_storage.list_objects.return_value = {
            "items": [
                {
                    "name": KEY,
                    "generation": 3003,
                    "size": "33",
                    "etag": "etag-three",
                    "updated": "2026-03-03T00:00:00.000Z",
                    "storageClass": "STANDARD",
                },
                {
                    "name": KEY,
                    "generation": 1001,
                    "size": "11",
                    "etag": "etag-one",
                    "updated": "2026-01-01T00:00:00.000Z",
                    "storageClass": "NEARLINE",
                    "timeDeleted": "2026-02-02T00:00:00.000Z",
                },
            ]
        }

        listed = [info async for info in gcp_store.do_list_blob_versions(KEY)]

        assert [
            (i.version_id, i.size, i.e_tag, i.storage_class, i.is_latest)
            for i in listed
        ] == [
            ("3003", 33, "etag-three", "STANDARD", True),
            ("1001", 11, "etag-one", "NEARLINE", False),
        ]
        assert listed[0].last_modified == MODIFIED_THREE
        assert listed[1].last_modified == MODIFIED_ONE

    async def test_is_latest_comes_from_the_deletion_timestamp(
        self, gcp_store, mock_storage
    ):
        """A generation with a deletion time is no longer the live one."""
        mock_storage.list_objects.return_value = {
            "items": [
                {
                    "name": KEY,
                    "generation": 1,
                    "timeDeleted": "2026-02-02T00:00:00.000Z",
                },
                {"name": KEY, "generation": 2},
            ]
        }

        listed = [info async for info in gcp_store.do_list_blob_versions(KEY)]

        assert [i.is_latest for i in listed] == [False, True]

    async def test_prefix_siblings_are_filtered_out(self, gcp_store, mock_storage):
        mock_storage.list_objects.return_value = {
            "items": [
                {"name": SIBLING, "generation": 9},
                {"name": KEY, "generation": 1},
            ]
        }

        listed = [info async for info in gcp_store.do_list_blob_versions(KEY)]

        assert [i.version_id for i in listed] == ["1"]

    async def test_the_whole_request_is_as_intended(self, gcp_store, mock_storage):
        """Asserted as a whole, not field by field.

        A spot check would miss maxResults dropping to 1, which would turn one
        round trip into one per version.
        """
        mock_storage.list_objects.return_value = {"items": []}

        [info async for info in gcp_store.do_list_blob_versions(KEY)]

        call = mock_storage.list_objects.call_args
        assert call.args == ("versioned",)
        assert call.kwargs["params"] == {
            "prefix": KEY,
            "versions": "true",
            "maxResults": "1000",
        }

    async def test_a_first_page_failure_is_translated(self, gcp_store, mock_storage):
        """Calling an async generator runs no body, so the client cannot catch this."""
        mock_storage.list_objects.side_effect = _client_response_error(403)

        with pytest.raises(UnAuthorizedException):
            [info async for info in gcp_store.do_list_blob_versions(KEY)]

    async def test_a_later_page_failure_is_translated(self, gcp_store, mock_storage):
        """A failure part-way through must not truncate the listing silently."""
        mock_storage.list_objects.side_effect = [
            {"items": [{"name": KEY, "generation": 1}], "nextPageToken": "tok-1"},
            _client_response_error(403),
        ]

        seen = []
        with pytest.raises(UnAuthorizedException):
            async for info in gcp_store.do_list_blob_versions(KEY):
                seen.append(info.version_id)

        assert seen == ["1"], "the first page should have been delivered"

    async def test_pagination_is_followed_with_the_page_token(
        self, gcp_store, mock_storage
    ):
        mock_storage.list_objects.side_effect = [
            {"items": [{"name": KEY, "generation": 1}], "nextPageToken": "tok-1"},
            {"items": [{"name": KEY, "generation": 2}]},
        ]

        listed = [info async for info in gcp_store.do_list_blob_versions(KEY)]

        assert [i.version_id for i in listed] == ["1", "2"]
        second = mock_storage.list_objects.call_args_list[1].kwargs["params"]
        assert second["pageToken"] == "tok-1"

    async def test_later_pages_are_not_fetched_until_needed(
        self, gcp_store, mock_storage
    ):
        mock_storage.list_objects.side_effect = [
            {"items": [{"name": KEY, "generation": 1}], "nextPageToken": "tok-1"},
            {"items": [{"name": KEY, "generation": 2}]},
        ]

        iterator = gcp_store.do_list_blob_versions(KEY)
        first = await iterator.__anext__()

        assert first.version_id == "1"
        assert mock_storage.list_objects.await_count == 1

        await iterator.aclose()

    async def test_a_key_with_no_versions_yields_nothing(self, gcp_store, mock_storage):
        mock_storage.list_objects.return_value = {"items": []}

        assert [info async for info in gcp_store.do_list_blob_versions(KEY)] == []

    async def test_pagination_terminates_and_does_not_spin(
        self, gcp_store, mock_storage
    ):
        """A finite side_effect makes a runaway loop fail instead of hang."""
        mock_storage.list_objects.side_effect = [
            {"items": [{"name": KEY, "generation": 1}]}
        ]

        listed = [info async for info in gcp_store.do_list_blob_versions(KEY)]

        assert [i.version_id for i in listed] == ["1"]
        assert mock_storage.list_objects.await_count == 1

    async def test_a_zero_generation_is_still_reported(self, gcp_store, mock_storage):
        """Guarded on None, not truthiness, matching the sync provider.

        Real generations are microsecond timestamps so zero is unreachable, but
        the two stacks should not disagree on the same input.
        """
        mock_storage.list_objects.return_value = {
            "items": [{"name": KEY, "generation": 0}]
        }

        listed = [info async for info in gcp_store.do_list_blob_versions(KEY)]

        assert [i.version_id for i in listed] == ["0"]

    async def test_a_missing_items_key_yields_nothing(self, gcp_store, mock_storage):
        """An empty listing omits items entirely rather than sending []."""
        mock_storage.list_objects.return_value = {}

        assert [info async for info in gcp_store.do_list_blob_versions(KEY)] == []
