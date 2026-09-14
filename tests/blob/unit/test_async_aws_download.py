"""Unit tests for AwsAsyncBlobStore.do_download() with mocked aiobotocore.

Tests validate the AWS async provider correctly:
- Passes correct parameters to S3 get_object
- Handles Range headers for partial downloads
- Translates AWS errors (NoSuchKey, AccessDenied, etc.) to BlobNotFoundError
- Extracts metadata (content_length, content_type, etag, version_id)
- Verifies build_range_header() helper
"""

from __future__ import annotations

from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

pytest.importorskip(
    "aiobotocore", reason="aiobotocore not installed (optional async-aws dependency)"
)

import multicloud.blob.blob_aws.async_blob.aws_async_blob_store as _aws_store_mod
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import (
    AwsAsyncBlobStore,
)
from multicloud.blob.blob_client.async_blob.async_driver import (
    AsyncDownloadRequest,
    AsyncDownloadResponse,
    BlobAsyncError,
    BlobMetadata,
    BlobNotFoundError,
    ReplicationStatus,
    build_range_header,
)
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
    ResourceNotFoundException,
    UnAuthorizedException,
    UnknownException,
)


def _make_client_error(code: str, status: int = 400, message: str = "error") -> Any:
    """Create a mock botocore ClientError."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "GetObject",
    )


def _make_get_object_response(
    body: bytes = b"file content",
    content_length: int = 12,
    content_type: str = "text/plain",
    etag: str = '"abc123"',
    version_id: str | None = "ver-001",
    metadata: dict | None = None,
) -> dict:
    """Create a mock S3 get_object response dict."""
    mock_body = AsyncMock()
    mock_body.read = AsyncMock(return_value=body)

    resp = {
        "Body": mock_body,
        "ContentLength": content_length,
        "ContentType": content_type,
        "ETag": etag,
        "Metadata": metadata or {},
    }
    if version_id is not None:
        resp["VersionId"] = version_id
    return resp


def _make_mock_session(
    get_response: dict | None = None,
    get_side_effect: Any = None,
):
    """Create a mock aiobotocore session with a mock S3 client."""
    mock_client = AsyncMock()
    if get_response is not None:
        mock_client.get_object.return_value = get_response
    if get_side_effect is not None:
        mock_client.get_object.side_effect = get_side_effect

    # Mock context manager for create_client
    mock_client_cm = AsyncMock()
    mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.create_client.return_value = mock_client_cm

    return mock_session, mock_client


@pytest.fixture
def mock_session_success():
    """Session that returns a successful get_object response."""
    return _make_mock_session(get_response=_make_get_object_response())


@pytest.fixture
def aws_store(mock_session_success):
    """AwsAsyncBlobStore with mocked session."""
    session, _ = mock_session_success
    return AwsAsyncBlobStore(
        bucket="test-bucket",
        region="us-east-1",
        _session=session,
    )


class TestBuildRangeHeader:
    """Tests for the build_range_header() helper function."""

    def test_both_none_returns_none(self):
        """No range requested returns None."""
        assert build_range_header(None, None) is None

    def test_start_and_end(self):
        """Both start and end produce 'bytes=start-end'."""
        assert build_range_header(0, 499) == "bytes=0-499"

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


class TestAwsAsyncDownloadSuccess:
    """Successful download scenarios."""

    @pytest.mark.asyncio
    async def test_download_returns_response(self, aws_store):
        """Basic download returns AsyncDownloadResponse."""
        request = AsyncDownloadRequest(key="my-file.txt")
        result = await aws_store.do_download(request)

        assert isinstance(result, AsyncDownloadResponse)
        assert result.key == "my-file.txt"
        assert result.content == b"file content"

    @pytest.mark.asyncio
    async def test_download_extracts_metadata(self, aws_store):
        """Download extracts full metadata from S3 response."""
        request = AsyncDownloadRequest(key="my-file.txt")
        result = await aws_store.do_download(request)

        assert result.metadata.key == "my-file.txt"
        assert result.metadata.content_length == 12
        assert result.metadata.content_type == "text/plain"
        assert result.metadata.e_tag == "abc123"  # quotes stripped
        assert result.metadata.version_id == "ver-001"

    @pytest.mark.asyncio
    async def test_download_passes_bucket_and_key(self, mock_session_success):
        """get_object is called with correct Bucket and Key."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="my-bucket", _session=session)

        request = AsyncDownloadRequest(key="path/to/file.bin")
        await store.do_download(request)

        call_kwargs = mock_client.get_object.call_args[1]
        assert call_kwargs["Bucket"] == "my-bucket"
        assert call_kwargs["Key"] == "path/to/file.bin"

    @pytest.mark.asyncio
    async def test_download_with_version_id(self, mock_session_success):
        """Version ID is forwarded to get_object."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt", version_id="ver-42")
        await store.do_download(request)

        call_kwargs = mock_client.get_object.call_args[1]
        assert call_kwargs["VersionId"] == "ver-42"

    @pytest.mark.asyncio
    async def test_download_without_version_id(self, mock_session_success):
        """No VersionId in get_object params when not requested."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt")
        await store.do_download(request)

        call_kwargs = mock_client.get_object.call_args[1]
        assert "VersionId" not in call_kwargs

    @pytest.mark.asyncio
    async def test_download_with_range_start_end(self, mock_session_success):
        """Range request with start/end passes Range header."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt", start=100, end=200)
        await store.do_download(request)

        call_kwargs = mock_client.get_object.call_args[1]
        assert call_kwargs["Range"] == "bytes=100-200"

    @pytest.mark.asyncio
    async def test_download_with_range_start_only(self, mock_session_success):
        """Range request with start only produces open-ended range."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt", start=500)
        await store.do_download(request)

        call_kwargs = mock_client.get_object.call_args[1]
        assert call_kwargs["Range"] == "bytes=500-"

    @pytest.mark.asyncio
    async def test_download_with_range_end_only(self, mock_session_success):
        """Range request with end only produces suffix range."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt", end=99)
        await store.do_download(request)

        call_kwargs = mock_client.get_object.call_args[1]
        assert call_kwargs["Range"] == "bytes=-99"

    @pytest.mark.asyncio
    async def test_download_no_range_omits_header(self, mock_session_success):
        """No Range header when start/end are both None."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt")
        await store.do_download(request)

        call_kwargs = mock_client.get_object.call_args[1]
        assert "Range" not in call_kwargs

    @pytest.mark.asyncio
    async def test_download_no_version_id_in_response(self):
        """Missing VersionId in response yields None in metadata."""
        resp = _make_get_object_response(version_id=None)
        session, _ = _make_mock_session(get_response=resp)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt")
        result = await store.do_download(request)

        assert result.metadata.version_id is None

    @pytest.mark.asyncio
    async def test_download_with_user_metadata(self):
        """User metadata from S3 response is preserved."""
        resp = _make_get_object_response(
            metadata={"env": "production", "team": "multicloud"}
        )
        session, _ = _make_mock_session(get_response=resp)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt")
        result = await store.do_download(request)

        assert result.metadata.metadata == {"env": "production", "team": "multicloud"}

    @pytest.mark.asyncio
    async def test_download_empty_body(self):
        """Empty object returns empty bytes."""
        resp = _make_get_object_response(body=b"", content_length=0)
        session, _ = _make_mock_session(get_response=resp)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="empty.txt")
        result = await store.do_download(request)

        assert result.content == b""
        assert result.metadata.content_length == 0

    @pytest.mark.asyncio
    async def test_download_large_body(self):
        """Large object content is returned intact."""
        large_body = b"x" * (1024 * 1024)
        resp = _make_get_object_response(
            body=large_body, content_length=len(large_body)
        )
        session, _ = _make_mock_session(get_response=resp)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="large.bin")
        result = await store.do_download(request)

        assert len(result.content) == 1024 * 1024


class TestAwsAsyncDownloadReplicationStatus:
    """replication_status is populated on download from the S3 header."""

    def _resp_with_status(self, replication_status):
        resp = _make_get_object_response(version_id=None)
        if replication_status is not None:
            resp["ReplicationStatus"] = replication_status
        return resp

    @pytest.mark.asyncio
    async def test_download_maps_completed_to_complete(self):
        # S3's terminal "COMPLETED" normalizes to the common COMPLETE.
        session, _ = _make_mock_session(
            get_response=self._resp_with_status("COMPLETED")
        )
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        result = await store.do_download(AsyncDownloadRequest(key="k"))
        assert result.metadata.replication_status == ReplicationStatus.COMPLETE

    @pytest.mark.asyncio
    async def test_download_maps_pending(self):
        session, _ = _make_mock_session(get_response=self._resp_with_status("PENDING"))
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        result = await store.do_download(AsyncDownloadRequest(key="k"))
        assert result.metadata.replication_status == ReplicationStatus.PENDING

    @pytest.mark.asyncio
    async def test_download_absent_header_is_none(self):
        session, _ = _make_mock_session(get_response=self._resp_with_status(None))
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        result = await store.do_download(AsyncDownloadRequest(key="k"))
        assert result.metadata.replication_status is None


class TestAwsAsyncGetMetadataReplicationStatus:
    """replication_status is populated on head_object (get_metadata)."""

    @pytest.mark.asyncio
    async def test_get_metadata_maps_replication_status(self):
        session, mock_client = _make_mock_session()
        mock_client.head_object.return_value = {
            "ContentLength": 5,
            "Metadata": {},
            "ReplicationStatus": "COMPLETED",
        }
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        meta = await store.do_get_metadata("k")
        assert meta.replication_status == ReplicationStatus.COMPLETE

    @pytest.mark.asyncio
    async def test_get_metadata_absent_header_is_none(self):
        session, mock_client = _make_mock_session()
        mock_client.head_object.return_value = {"ContentLength": 5, "Metadata": {}}
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        meta = await store.do_get_metadata("k")
        assert meta.replication_status is None


class TestAwsAsyncDownloadErrors:
    """Error handling and translation for downloads."""

    @pytest.mark.asyncio
    async def test_no_such_key_raises_blob_not_found(self):
        """NoSuchKey error raises BlobNotFoundError with key."""
        exc = _make_client_error("NoSuchKey", 404, "The specified key does not exist.")
        session, _ = _make_mock_session(get_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="missing.txt")
        with pytest.raises(BlobNotFoundError) as exc_info:
            await store.do_download(request)

        assert exc_info.value.key == "missing.txt"
        assert exc_info.value.cause is exc

    @pytest.mark.asyncio
    async def test_404_status_raises_blob_not_found(self):
        """Generic 404 status raises BlobNotFoundError."""
        exc = _make_client_error("404", 404, "Not Found")
        session, _ = _make_mock_session(get_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="gone.txt")
        with pytest.raises(BlobNotFoundError) as exc_info:
            await store.do_download(request)

        assert exc_info.value.key == "gone.txt"

    @pytest.mark.asyncio
    async def test_no_such_bucket_raises_blob_not_found(self):
        """NoSuchBucket error raises BlobNotFoundError."""
        exc = _make_client_error(
            "NoSuchBucket", 404, "The specified bucket does not exist."
        )
        session, _ = _make_mock_session(get_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt")
        with pytest.raises(BlobNotFoundError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_access_denied_propagates(self):
        """AccessDenied error propagates to client layer for translation."""
        exc = _make_client_error("AccessDenied", 403, "Access Denied")
        session, _ = _make_mock_session(get_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="secret.txt")
        with pytest.raises(ClientError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_invalid_argument_propagates(self):
        """InvalidArgument error propagates to client layer for translation."""
        exc = _make_client_error("InvalidArgument", 400, "Bad range")
        session, _ = _make_mock_session(get_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt", start=-1)
        with pytest.raises(ClientError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_unknown_client_error_propagates(self):
        """Unrecognized ClientError code propagates to client layer."""
        exc = _make_client_error("InternalError", 500, "Server error")
        session, _ = _make_mock_session(get_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt")
        with pytest.raises(ClientError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_botocore_error_propagates(self):
        """BotoCoreError propagates to client layer for translation."""
        session, mock_client = _make_mock_session()
        mock_client.get_object.side_effect = BotoCoreError()
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt")
        with pytest.raises(BotoCoreError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_no_credentials_propagates(self):
        """NoCredentialsError propagates to client layer for translation."""
        session, mock_client = _make_mock_session()
        mock_client.get_object.side_effect = NoCredentialsError()
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt")
        with pytest.raises(NoCredentialsError):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_generic_exception_propagates_to_client(self):
        """Unexpected exception propagates (client layer handles wrapping)."""
        session, mock_client = _make_mock_session()
        mock_client.get_object.side_effect = RuntimeError("network timeout")
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt")
        with pytest.raises(RuntimeError, match="network timeout"):
            await store.do_download(request)

    @pytest.mark.asyncio
    async def test_translate_error_returns_resource_not_found_for_404(self):
        """translate_error returns ResourceNotFoundException for NoSuchKey."""
        session, _ = _make_mock_session()
        store = AwsAsyncBlobStore(bucket="b", _session=session)
        exc = _make_client_error("NoSuchKey", 404)

        result = store.translate_error(exc)
        assert isinstance(result, ResourceNotFoundException)


class TestAwsAsyncDownloadIfMatch:
    """Conditional read (if_match) precondition handling."""

    @pytest.mark.asyncio
    async def test_if_match_applied_as_quoted_header(self, mock_session_success):
        """A bare if_match is re-quoted and sent as the IfMatch header."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt", if_match="abc123")
        await store.do_download(request)

        call_kwargs = mock_client.get_object.call_args[1]
        assert call_kwargs["IfMatch"] == '"abc123"'

    @pytest.mark.asyncio
    async def test_already_quoted_if_match_not_double_quoted(
        self, mock_session_success
    ):
        """An already-quoted if_match is passed through unchanged."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt", if_match='"abc123"')
        await store.do_download(request)

        call_kwargs = mock_client.get_object.call_args[1]
        assert call_kwargs["IfMatch"] == '"abc123"'

    @pytest.mark.asyncio
    async def test_no_if_match_omits_header(self, mock_session_success):
        """No if_match means no IfMatch key in the get_object kwargs."""
        session, mock_client = mock_session_success
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        await store.do_download(AsyncDownloadRequest(key="file.txt"))

        assert "IfMatch" not in mock_client.get_object.call_args[1]

    @pytest.mark.asyncio
    async def test_if_match_mismatch_maps_to_failed_precondition(self):
        """A 412 PreconditionFailed on read translates to FailedPreconditionException."""
        exc = _make_client_error("PreconditionFailed", 412, "At least one precondition")
        session, _ = _make_mock_session(get_side_effect=exc)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        # do_download re-raises non-404 ClientErrors; the client layer
        # translates. Assert translate_error produces the cloud-agnostic type.
        result = store.translate_error(exc)
        assert isinstance(result, FailedPreconditionException)


class TestAwsAsyncDownloadLifecycle:
    """Client lifecycle during downloads."""

    @pytest.mark.asyncio
    async def test_download_initializes_client(self):
        """Download lazily creates the S3 client."""
        resp = _make_get_object_response()
        session, _ = _make_mock_session(get_response=resp)
        store = AwsAsyncBlobStore(bucket="b", region="eu-west-1", _session=session)

        assert store._client is None

        request = AsyncDownloadRequest(key="file.txt")
        await store.do_download(request)

        assert store._client is not None
        session.create_client.assert_called_once_with(
            "s3", region_name="eu-west-1", config=ANY
        )

    @pytest.mark.asyncio
    async def test_download_reuses_existing_client(self):
        """Multiple downloads share the same client instance."""
        resp = _make_get_object_response()
        session, _ = _make_mock_session(get_response=resp)
        store = AwsAsyncBlobStore(bucket="b", _session=session)

        request = AsyncDownloadRequest(key="file.txt")
        await store.do_download(request)
        await store.do_download(request)
        await store.do_download(request)

        session.create_client.assert_called_once()
