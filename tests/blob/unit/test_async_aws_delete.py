"""Unit tests for AwsAsyncBlobStore delete operations.

Tests validate the AWS S3 provider correctly:
- Calls delete_object for single deletes
- Calls delete_objects for batch deletes with proper batching
- Handles partial failures from S3 (Deleted + Errors lists)
- Passes version_id as VersionId parameter
- Translates S3 ClientError into SDK exceptions
- Is idempotent (S3 delete_object succeeds even for non-existent keys)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore
from multicloud.blob.blob_client.async_blob.async_driver import (
    BlobAsyncError,
    BlobIdentifier,
    BulkDeleteResponse,
    DeletedBlob,
    DeleteError,
)
from multicloud.common.exceptions import (
    ResourceNotFoundException,
    UnAuthorizedException,
    UnknownException,
)


def _make_client_error(code: str = "AccessDenied", status: int = 403) -> Any:
    """Create a botocore ClientError for testing."""
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": code, "Message": f"Error: {code}"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "DeleteObject",
    )


def _make_store(mock_client: AsyncMock) -> AwsAsyncBlobStore:
    """Create an AwsAsyncBlobStore with a mocked S3 client."""
    mock_session = MagicMock()
    store = AwsAsyncBlobStore(
        bucket="test-bucket",
        region="us-east-1",
        _session=mock_session,
    )
    # Bypass lazy init — inject mock client directly
    store._client = mock_client
    store._client_cm = MagicMock()
    return store


class TestAwsAsyncBlobStoreDelete:
    """Single object delete tests."""

    @pytest.mark.asyncio
    async def test_delete_calls_s3_delete_object(self):
        mock_client = AsyncMock()
        mock_client.delete_object = AsyncMock(return_value={})
        store = _make_store(mock_client)

        await store.do_delete("path/to/file.txt")

        mock_client.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key="path/to/file.txt"
        )

    @pytest.mark.asyncio
    async def test_delete_with_version_id(self):
        mock_client = AsyncMock()
        mock_client.delete_object = AsyncMock(return_value={})
        store = _make_store(mock_client)

        await store.do_delete("file.txt", version_id="abc123")

        mock_client.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key="file.txt", VersionId="abc123"
        )

    @pytest.mark.asyncio
    async def test_delete_without_version_id_omits_param(self):
        mock_client = AsyncMock()
        mock_client.delete_object = AsyncMock(return_value={})
        store = _make_store(mock_client)

        await store.do_delete("file.txt", version_id=None)

        call_kwargs = mock_client.delete_object.call_args[1]
        assert "VersionId" not in call_kwargs

    @pytest.mark.asyncio
    async def test_delete_idempotent_no_error_for_missing_key(self):
        """S3 delete_object returns success even for non-existent keys."""
        mock_client = AsyncMock()
        mock_client.delete_object = AsyncMock(return_value={})
        store = _make_store(mock_client)

        # Should not raise
        await store.do_delete("nonexistent-key.txt")

    @pytest.mark.asyncio
    async def test_delete_client_error_propagates_to_client_layer(self):
        """ClientError propagates raw — client layer calls translate_error."""
        mock_client = AsyncMock()
        mock_client.delete_object = AsyncMock(
            side_effect=_make_client_error("AccessDenied", 403)
        )
        store = _make_store(mock_client)

        with pytest.raises(ClientError):
            await store.do_delete("secret-file.txt")

    @pytest.mark.asyncio
    async def test_delete_bucket_not_found_propagates_to_client_layer(self):
        """NoSuchBucket ClientError propagates raw."""
        mock_client = AsyncMock()
        mock_client.delete_object = AsyncMock(
            side_effect=_make_client_error("NoSuchBucket", 404)
        )
        store = _make_store(mock_client)

        with pytest.raises(ClientError):
            await store.do_delete("file.txt")

    @pytest.mark.asyncio
    async def test_delete_unknown_error_propagates_to_client_layer(self):
        """Non-boto exceptions propagate raw to the client layer."""
        mock_client = AsyncMock()
        mock_client.delete_object = AsyncMock(side_effect=RuntimeError("unexpected"))
        store = _make_store(mock_client)

        with pytest.raises(RuntimeError, match="unexpected"):
            await store.do_delete("file.txt")


class TestAwsAsyncBlobStoreDeleteObjects:
    """Batch object delete tests."""

    @pytest.mark.asyncio
    async def test_delete_objects_all_success(self):
        mock_client = AsyncMock()
        mock_client.delete_objects = AsyncMock(
            return_value={
                "Deleted": [
                    {"Key": "a.txt"},
                    {"Key": "b.txt", "VersionId": "v1"},
                ],
                "Errors": [],
            }
        )
        store = _make_store(mock_client)

        identifiers = [
            BlobIdentifier(key="a.txt"),
            BlobIdentifier(key="b.txt", version_id="v1"),
        ]
        result = await store.do_delete_objects(identifiers)

        assert isinstance(result, BulkDeleteResponse)
        assert result.has_errors is False
        assert len(result.deleted) == 2
        assert result.deleted[0].key == "a.txt"
        assert result.deleted[1].version_id == "v1"

    @pytest.mark.asyncio
    async def test_delete_objects_partial_failure(self):
        mock_client = AsyncMock()
        mock_client.delete_objects = AsyncMock(
            return_value={
                "Deleted": [{"Key": "a.txt"}],
                "Errors": [
                    {
                        "Key": "b.txt",
                        "Code": "AccessDenied",
                        "Message": "No permission",
                    }
                ],
            }
        )
        store = _make_store(mock_client)

        identifiers = [
            BlobIdentifier(key="a.txt"),
            BlobIdentifier(key="b.txt"),
        ]
        result = await store.do_delete_objects(identifiers)

        assert result.has_errors is True
        assert len(result.deleted) == 1
        assert len(result.errors) == 1
        assert result.errors[0].key == "b.txt"
        assert result.errors[0].code == "AccessDenied"
        assert result.errors[0].message == "No permission"

    @pytest.mark.asyncio
    async def test_delete_objects_all_fail(self):
        mock_client = AsyncMock()
        mock_client.delete_objects = AsyncMock(
            return_value={
                "Deleted": [],
                "Errors": [
                    {"Key": "x.txt", "Code": "InternalError", "Message": "fail"},
                    {"Key": "y.txt", "Code": "InternalError", "Message": "fail"},
                ],
            }
        )
        store = _make_store(mock_client)

        result = await store.do_delete_objects(
            [BlobIdentifier(key="x.txt"), BlobIdentifier(key="y.txt")]
        )

        assert result.has_errors is True
        assert len(result.deleted) == 0
        assert len(result.errors) == 2

    @pytest.mark.asyncio
    async def test_delete_objects_passes_version_ids(self):
        mock_client = AsyncMock()
        mock_client.delete_objects = AsyncMock(
            return_value={
                "Deleted": [{"Key": "a.txt", "VersionId": "v1"}],
                "Errors": [],
            }
        )
        store = _make_store(mock_client)

        await store.do_delete_objects([BlobIdentifier(key="a.txt", version_id="v1")])

        call_kwargs = mock_client.delete_objects.call_args[1]
        objects = call_kwargs["Delete"]["Objects"]
        assert objects[0] == {"Key": "a.txt", "VersionId": "v1"}

    @pytest.mark.asyncio
    async def test_delete_objects_omits_version_id_when_none(self):
        mock_client = AsyncMock()
        mock_client.delete_objects = AsyncMock(
            return_value={"Deleted": [{"Key": "a.txt"}], "Errors": []}
        )
        store = _make_store(mock_client)

        await store.do_delete_objects([BlobIdentifier(key="a.txt")])

        call_kwargs = mock_client.delete_objects.call_args[1]
        objects = call_kwargs["Delete"]["Objects"]
        assert objects[0] == {"Key": "a.txt"}
        assert "VersionId" not in objects[0]

    @pytest.mark.asyncio
    async def test_delete_objects_batch_client_error_all_items_become_errors(self):
        """If the entire delete_objects call fails, all items are reported as errors."""
        mock_client = AsyncMock()
        mock_client.delete_objects = AsyncMock(
            side_effect=_make_client_error("InternalError", 500)
        )
        store = _make_store(mock_client)

        identifiers = [
            BlobIdentifier(key="a.txt"),
            BlobIdentifier(key="b.txt"),
            BlobIdentifier(key="c.txt"),
        ]
        result = await store.do_delete_objects(identifiers)

        assert result.has_errors is True
        assert len(result.deleted) == 0
        assert len(result.errors) == 3
        for err in result.errors:
            assert err.code == "BatchFailed"

    @pytest.mark.asyncio
    async def test_delete_objects_batches_over_1000(self):
        """Items exceeding 1000 should be split into multiple API calls."""
        mock_client = AsyncMock()
        mock_client.delete_objects = AsyncMock(
            side_effect=lambda **kwargs: {
                "Deleted": [{"Key": obj["Key"]} for obj in kwargs["Delete"]["Objects"]],
                "Errors": [],
            }
        )
        store = _make_store(mock_client)

        # Create 1500 identifiers
        identifiers = [BlobIdentifier(key=f"file-{i}.txt") for i in range(1500)]
        result = await store.do_delete_objects(identifiers)

        # Should be called twice: once with 1000, once with 500
        assert mock_client.delete_objects.call_count == 2
        first_call = mock_client.delete_objects.call_args_list[0][1]
        second_call = mock_client.delete_objects.call_args_list[1][1]
        assert len(first_call["Delete"]["Objects"]) == 1000
        assert len(second_call["Delete"]["Objects"]) == 500
        assert result.has_errors is False
        assert len(result.deleted) == 1500

    @pytest.mark.asyncio
    async def test_delete_objects_nonexistent_reported_as_deleted(self):
        """S3 reports non-existent keys as Deleted (idempotent behavior)."""
        mock_client = AsyncMock()
        mock_client.delete_objects = AsyncMock(
            return_value={
                "Deleted": [
                    {"Key": "exists.txt"},
                    {"Key": "ghost.txt"},  # didn't exist but still "Deleted"
                ],
                "Errors": [],
            }
        )
        store = _make_store(mock_client)

        result = await store.do_delete_objects(
            [BlobIdentifier(key="exists.txt"), BlobIdentifier(key="ghost.txt")]
        )

        assert result.has_errors is False
        assert len(result.deleted) == 2
