"""Unit tests for AwsAsyncBlobStore.do_copy() with mocked aiobotocore.

Tests validate the AWS async provider correctly:
- Passes correct parameters to S3 copy_object
- Handles MetadataDirective for metadata override
- Translates AWS errors (NoSuchKey, AccessDenied, etc.)
- Supports same-bucket and cross-bucket copy
- Handles version_id in CopySource
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip(
    "aiobotocore", reason="aiobotocore not installed (optional async-aws dependency)"
)

from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore
from multicloud.blob.blob_client.async_blob.async_driver import (
    CopyRequest,
    CopyResponse,
)
from multicloud.common.exceptions import (
    ResourceNotFoundException,
)


def _make_client_error(code: str, status: int = 400, message: str = "error") -> Any:
    """Create a mock botocore ClientError."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "CopyObject",
    )


def _make_mock_session(copy_response: dict | None = None, copy_side_effect: Any = None):
    """Create a mock aiobotocore session with a mock S3 client."""
    mock_client = AsyncMock()
    if copy_response is not None:
        mock_client.copy_object.return_value = copy_response
    if copy_side_effect is not None:
        mock_client.copy_object.side_effect = copy_side_effect

    # Mock context manager for create_client
    mock_client_cm = AsyncMock()
    mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.create_client.return_value = mock_client_cm

    return mock_session, mock_client


@pytest.fixture
def copy_response_success():
    """Standard successful copy_object response."""
    return {
        "CopyObjectResult": {
            "ETag": '"abc123def456"',
            "LastModified": "2025-01-01T00:00:00Z",
        },
        "VersionId": "ver-new-001",
    }


@pytest.fixture
def mock_session_success(copy_response_success):
    return _make_mock_session(copy_response=copy_response_success)


@pytest.fixture
def aws_store(mock_session_success):
    session, _ = mock_session_success
    return AwsAsyncBlobStore(
        bucket="test-bucket",
        region="us-east-1",
        _session=session,
    )


@pytest.fixture
def same_bucket_request():
    return CopyRequest(
        source_bucket="test-bucket",
        source_key="src/file.txt",
        destination_bucket="test-bucket",
        destination_key="dest/file.txt",
    )


@pytest.fixture
def cross_bucket_request():
    return CopyRequest(
        source_bucket="source-bucket",
        source_key="data/report.pdf",
        destination_bucket="archive-bucket",
        destination_key="archived/report.pdf",
    )


class TestAwsAsyncCopySuccess:
    """Successful copy scenarios."""

    @pytest.mark.asyncio
    async def test_same_bucket_copy_returns_response(
        self, aws_store, same_bucket_request
    ):
        result = await aws_store.copy(same_bucket_request)

        assert isinstance(result, CopyResponse)
        assert result.key == "dest/file.txt"
        assert result.e_tag == "abc123def456"  # quotes stripped
        assert result.version_id == "ver-new-001"
        assert result.last_modified == "2025-01-01T00:00:00Z"
        assert result.correlation_id is not None

    @pytest.mark.asyncio
    async def test_cross_bucket_copy_returns_response(
        self, aws_store, cross_bucket_request
    ):
        result = await aws_store.copy(cross_bucket_request)

        assert isinstance(result, CopyResponse)
        assert result.key == "archived/report.pdf"
        assert result.version_id == "ver-new-001"

    @pytest.mark.asyncio
    async def test_copy_passes_correct_copy_source(
        self, mock_session_success, same_bucket_request
    ):
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="test-bucket", _session=session)

        await store.copy(same_bucket_request)

        call_kwargs = mock_client.copy_object.call_args[1]
        assert call_kwargs["CopySource"] == {
            "Bucket": "test-bucket",
            "Key": "src/file.txt",
        }
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"] == "dest/file.txt"

    @pytest.mark.asyncio
    async def test_copy_with_version_id(self, mock_session_success):
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src-bucket",
            source_key="key",
            destination_bucket="dst-bucket",
            destination_key="dkey",
            source_version_id="ver-abc-123",
        )
        await store.copy(req)

        call_kwargs = mock_client.copy_object.call_args[1]
        assert call_kwargs["CopySource"]["VersionId"] == "ver-abc-123"

    @pytest.mark.asyncio
    async def test_copy_without_version_id_omits_field(self, mock_session_success):
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        await store.copy(req)

        call_kwargs = mock_client.copy_object.call_args[1]
        assert "VersionId" not in call_kwargs["CopySource"]

    @pytest.mark.asyncio
    async def test_copy_with_metadata_override(self, mock_session_success):
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
            metadata={"env": "prod", "team": "multicloud"},
        )
        await store.copy(req)

        call_kwargs = mock_client.copy_object.call_args[1]
        assert call_kwargs["Metadata"] == {"env": "prod", "team": "multicloud"}
        assert call_kwargs["MetadataDirective"] == "REPLACE"

    @pytest.mark.asyncio
    async def test_copy_without_metadata_no_directive(self, mock_session_success):
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        await store.copy(req)

        call_kwargs = mock_client.copy_object.call_args[1]
        assert "Metadata" not in call_kwargs
        assert "MetadataDirective" not in call_kwargs

    @pytest.mark.asyncio
    async def test_copy_no_version_id_in_response(self):
        session, _ = _make_mock_session(
            copy_response={
                "CopyObjectResult": {"ETag": '"etag"'},
            }
        )
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        result = await store.copy(req)
        assert result.version_id is None

    @pytest.mark.asyncio
    async def test_copy_etag_quotes_stripped(self):
        session, _ = _make_mock_session(
            copy_response={
                "CopyObjectResult": {"ETag": '"abcdef123456"'},
                "VersionId": "v1",
            }
        )
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        result = await store.copy(req)
        assert result.e_tag == "abcdef123456"


class TestAwsAsyncCopyErrors:
    """Error handling and translation for copy."""

    @pytest.mark.asyncio
    async def test_source_not_found_raises_resource_not_found(self):
        exc = _make_client_error("NoSuchKey", 404)
        session, _ = _make_mock_session(copy_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="missing-key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(ResourceNotFoundException, match="Source object not found"):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_source_bucket_not_found_raises_resource_not_found(self):
        exc = _make_client_error("NoSuchBucket", 404)
        session, _ = _make_mock_session(copy_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="nonexistent-bucket",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(ResourceNotFoundException):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_access_denied_propagates_as_client_error(self):
        """Non-404 ClientError propagates raw for client-layer translation."""
        exc = _make_client_error("AccessDenied", 403)
        session, _ = _make_mock_session(copy_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(ClientError):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_no_credentials_propagates(self):
        """NoCredentialsError propagates raw for client-layer translation."""
        session, mock_client = _make_mock_session()
        mock_client.copy_object.side_effect = NoCredentialsError()
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(NoCredentialsError):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_botocore_error_propagates(self):
        """BotoCoreError propagates raw for client-layer translation."""
        session, mock_client = _make_mock_session()
        mock_client.copy_object.side_effect = BotoCoreError()
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(BotoCoreError):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_unexpected_error_propagates(self):
        """Unexpected exceptions propagate raw for client-layer translation."""
        session, mock_client = _make_mock_session()
        mock_client.copy_object.side_effect = RuntimeError("network failure")
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(RuntimeError, match="network failure"):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_404_status_code_raises_resource_not_found(self):
        """404 via HTTPStatusCode (not error code) still raises ResourceNotFound."""
        exc = _make_client_error("", 404, "Not Found")
        session, _ = _make_mock_session(copy_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(ResourceNotFoundException):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_non_404_client_error_propagates(self):
        """Non-404 ClientError propagates raw (not translated at provider layer)."""
        exc = _make_client_error("InternalError", 500)
        session, _ = _make_mock_session(copy_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(ClientError):
            await store.copy(req)


class TestAwsAsyncCopyEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_copy_with_empty_metadata_dict(self, mock_session_success):
        """Empty metadata dict should trigger REPLACE directive."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
            metadata={},
        )
        await store.copy(req)

        call_kwargs = mock_client.copy_object.call_args[1]
        # Empty dict is still not None, so REPLACE is used
        assert call_kwargs["Metadata"] == {}
        assert call_kwargs["MetadataDirective"] == "REPLACE"

    @pytest.mark.asyncio
    async def test_copy_with_special_characters_in_key(self, mock_session_success):
        """Keys with special characters are passed through correctly."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        req = CopyRequest(
            source_bucket="src",
            source_key="path/with spaces/file (1).txt",
            destination_bucket="dst",
            destination_key="new path/file (1).txt",
        )
        await store.copy(req)

        call_kwargs = mock_client.copy_object.call_args[1]
        assert call_kwargs["CopySource"]["Key"] == "path/with spaces/file (1).txt"
        assert call_kwargs["Key"] == "new path/file (1).txt"

    @pytest.mark.asyncio
    async def test_copy_large_metadata(self, mock_session_success):
        """Copy with many metadata entries."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        metadata = {f"key-{i}": f"value-{i}" for i in range(20)}
        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
            metadata=metadata,
        )
        await store.copy(req)

        call_kwargs = mock_client.copy_object.call_args[1]
        assert len(call_kwargs["Metadata"]) == 20
