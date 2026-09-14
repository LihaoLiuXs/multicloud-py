"""Unit tests for streaming and file-based download APIs.

Tests cover:
- download_to_path: writes directly to a file path
- download_to_file: writes to a file-like object
- download_stream: returns a lazy streaming response

Each method is tested at the driver level (validation + delegation) and
at the provider level (AWS and GCP implementations with mocked SDKs).
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from multicloud.blob.blob_aws.aws_blob_store import AwsBlobStore
from multicloud.blob.blob_client.driver import (
    BaseBlobStore,
    BlobMetadata,
    DownloadResponse,
    StreamingDownloadResponse,
)
from multicloud.blob.blob_gcp.gcp_blob_store import GcpBlobStore
from multicloud.common.exceptions import InvalidArgumentException


class TestStreamingDownloadResponse:
    """Tests for the StreamingDownloadResponse class itself."""

    def _make_response(self, chunks: list[bytes]) -> StreamingDownloadResponse:
        meta = BlobMetadata(key="test.bin", content_length=10)
        return StreamingDownloadResponse(
            key="test.bin",
            metadata=meta,
            stream=iter(chunks),
        )

    def test_iterate_yields_all_chunks(self):
        resp = self._make_response([b"aaa", b"bbb", b"ccc"])
        collected = list(resp)
        assert collected == [b"aaa", b"bbb", b"ccc"]

    def test_read_all_returns_full_content(self):
        resp = self._make_response([b"hello", b" ", b"world"])
        assert resp.read() == b"hello world"

    def test_read_with_size_returns_partial(self):
        resp = self._make_response([b"hello world"])
        assert resp.read(5) == b"hello"
        assert resp.read(6) == b" world"
        assert resp.read(10) == b""

    def test_read_across_chunk_boundaries(self):
        resp = self._make_response([b"ab", b"cd", b"ef"])
        assert resp.read(3) == b"abc"
        assert resp.read(3) == b"def"
        assert resp.read(1) == b""

    def test_context_manager_closes(self):
        close_called = []
        meta = BlobMetadata(key="k")
        resp = StreamingDownloadResponse(
            key="k",
            metadata=meta,
            stream=iter([b"data"]),
            close_func=lambda: close_called.append(True),
        )
        with resp as stream:
            assert stream.read() == b"data"
        assert close_called == [True]
        assert resp.closed is True

    def test_read_after_close_raises(self):
        resp = self._make_response([b"data"])
        resp.close()
        with pytest.raises(ValueError, match="closed stream"):
            resp.read()

    def test_iterate_after_close_raises(self):
        resp = self._make_response([b"data"])
        resp.close()
        with pytest.raises(ValueError, match="closed stream"):
            list(resp)

    def test_properties(self):
        meta = BlobMetadata(key="doc.pdf", content_length=1024)
        resp = StreamingDownloadResponse(key="doc.pdf", metadata=meta, stream=iter([]))
        assert resp.key == "doc.pdf"
        assert resp.metadata.content_length == 1024
        assert resp.closed is False

    def test_iterate_with_prefilled_buffer(self):
        """If read(size) partially consumed the stream, __iter__ yields the buffer first."""
        resp = self._make_response([b"hello", b"world"])
        # Partially read — leaves "llo" in the buffer from the first chunk
        first = resp.read(2)
        assert first == b"he"
        # Now iterate the rest — should get buffer remainder + remaining chunks
        remaining = list(resp)
        assert b"".join(remaining) == b"lloworld"

    def test_close_without_close_func(self):
        """Closing a response with no close_func does not raise."""
        meta = BlobMetadata(key="k")
        resp = StreamingDownloadResponse(key="k", metadata=meta, stream=iter([b"data"]))
        resp.close()
        assert resp.closed is True

    def test_close_is_idempotent(self):
        """Calling close() multiple times does not call close_func again."""
        call_count = []
        meta = BlobMetadata(key="k")
        resp = StreamingDownloadResponse(
            key="k",
            metadata=meta,
            stream=iter([b"data"]),
            close_func=lambda: call_count.append(1),
        )
        resp.close()
        resp.close()
        resp.close()
        assert len(call_count) == 1

    def test_read_size_zero_returns_empty(self):
        """read(0) returns empty bytes without consuming."""
        resp = self._make_response([b"hello"])
        assert resp.read(0) == b""
        # Stream is still intact
        assert resp.read() == b"hello"

    def test_read_empty_stream(self):
        """read() on an empty stream returns empty bytes."""
        resp = self._make_response([])
        assert resp.read() == b""
        assert resp.read(10) == b""

    def test_iterate_empty_stream(self):
        """Iterating an empty stream yields nothing."""
        resp = self._make_response([])
        assert list(resp) == []


class TestAwsDownloadToPath:
    """Tests for AwsBlobStore.do_download_to_path."""

    @pytest.fixture
    def mock_s3(self):
        return MagicMock()

    @pytest.fixture
    def store(self, mock_s3):
        return AwsBlobStore(bucket="test-bucket", region="us-east-1", s3_client=mock_s3)

    def _setup_get_object(self, mock_s3, chunks, **overrides):
        """Helper to configure mock_s3.get_object return value."""
        body_mock = MagicMock()
        body_mock.iter_chunks.return_value = iter(chunks)
        defaults = {
            "Body": body_mock,
            "ContentLength": sum(len(c) for c in chunks),
            "ContentType": "application/octet-stream",
            "ETag": '"abc123"',
            "VersionId": None,
            "Metadata": {},
        }
        defaults.update(overrides)
        mock_s3.get_object.return_value = defaults
        return body_mock

    def test_writes_content_to_file(self, store, mock_s3, tmp_path):
        """Content is streamed to the destination file without full memory load."""
        body_mock = self._setup_get_object(mock_s3, [b"chunk1", b"chunk2"])
        dest = tmp_path / "output.bin"
        resp = store.download_to_path("my/object.bin", dest)

        assert dest.read_bytes() == b"chunk1chunk2"
        assert resp.content == b""
        assert resp.metadata.content_length == 12
        body_mock.close.assert_called_once()

    def test_creates_parent_dirs(self, store, mock_s3, tmp_path):
        """create_parent_dirs=True creates intermediate directories."""
        self._setup_get_object(mock_s3, [b"data"], ContentType="text/plain")
        dest = tmp_path / "deep" / "nested" / "file.txt"
        store.download_to_path("k", dest, create_parent_dirs=True)

        assert dest.read_bytes() == b"data"

    def test_empty_key_raises(self, store, tmp_path):
        dest = tmp_path / "out.bin"
        with pytest.raises(
            InvalidArgumentException, match="Object name cannot be null or empty"
        ):
            store.download_to_path("", dest)

    def test_range_download(self, store, mock_s3, tmp_path):
        """Range parameters are passed through to the provider."""
        self._setup_get_object(mock_s3, [b"partial"])
        dest = tmp_path / "out.bin"
        store.download_to_path("k", dest, start=10, end=16)

        call_kwargs = mock_s3.get_object.call_args[1]
        assert call_kwargs["Range"] == "bytes=10-16"

    def test_version_id_passed_to_s3(self, store, mock_s3, tmp_path):
        """version_id is forwarded as VersionId to S3."""
        self._setup_get_object(mock_s3, [b"versioned"])
        dest = tmp_path / "out.bin"
        store.download_to_path("k", dest, version_id="abc-123")

        call_kwargs = mock_s3.get_object.call_args[1]
        assert call_kwargs["VersionId"] == "abc-123"

    def test_s3_error_raises_translated_exception(self, store, mock_s3, tmp_path):
        """S3 ClientError is translated to SubstrateSdkException."""
        from botocore.exceptions import ClientError as BotoClientError

        error_resp = {
            "Error": {"Code": "NoSuchKey", "Message": "Not found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_s3.get_object.side_effect = BotoClientError(error_resp, "GetObject")

        from multicloud.common.exceptions import ResourceNotFoundException

        dest = tmp_path / "out.bin"
        with pytest.raises(ResourceNotFoundException):
            store.download_to_path("missing-key", dest)


class TestAwsDownloadToFile:
    """Tests for AwsBlobStore.do_download_to_file."""

    @pytest.fixture
    def mock_s3(self):
        return MagicMock()

    @pytest.fixture
    def store(self, mock_s3):
        return AwsBlobStore(bucket="test-bucket", region="us-east-1", s3_client=mock_s3)

    def _setup_get_object(self, mock_s3, chunks, **overrides):
        body_mock = MagicMock()
        body_mock.iter_chunks.return_value = iter(chunks)
        defaults = {
            "Body": body_mock,
            "ContentLength": sum(len(c) for c in chunks),
            "ContentType": "text/plain",
            "ETag": '"etag"',
            "VersionId": None,
            "Metadata": {},
        }
        defaults.update(overrides)
        mock_s3.get_object.return_value = defaults
        return body_mock

    def test_writes_content_to_file_object(self, store, mock_s3):
        """Content is streamed to the file-like object."""
        body_mock = self._setup_get_object(mock_s3, [b"hello", b" world"])
        buf = io.BytesIO()
        resp = store.download_to_file("greeting.txt", buf)

        assert buf.getvalue() == b"hello world"
        assert resp.content == b""
        assert resp.metadata.content_length == 11
        body_mock.close.assert_called_once()

    def test_empty_key_raises(self, store):
        buf = io.BytesIO()
        with pytest.raises(
            InvalidArgumentException, match="Object name cannot be null or empty"
        ):
            store.download_to_file("", buf)

    def test_version_id_passed_to_s3(self, store, mock_s3):
        """version_id is forwarded as VersionId to S3."""
        self._setup_get_object(mock_s3, [b"v2-content"])
        buf = io.BytesIO()
        store.download_to_file("k", buf, version_id="ver-42")

        call_kwargs = mock_s3.get_object.call_args[1]
        assert call_kwargs["VersionId"] == "ver-42"

    def test_range_passed_to_s3(self, store, mock_s3):
        """start/end are forwarded as Range header."""
        self._setup_get_object(mock_s3, [b"range"])
        buf = io.BytesIO()
        store.download_to_file("k", buf, start=5, end=9)

        call_kwargs = mock_s3.get_object.call_args[1]
        assert call_kwargs["Range"] == "bytes=5-9"

    def test_s3_error_raises_translated_exception(self, store, mock_s3):
        """S3 ClientError is translated to SubstrateSdkException."""
        from botocore.exceptions import ClientError as BotoClientError

        error_resp = {
            "Error": {"Code": "NoSuchKey", "Message": "Not found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_s3.get_object.side_effect = BotoClientError(error_resp, "GetObject")

        from multicloud.common.exceptions import ResourceNotFoundException

        buf = io.BytesIO()
        with pytest.raises(ResourceNotFoundException):
            store.download_to_file("missing", buf)


class TestAwsDownloadStream:
    """Tests for AwsBlobStore.do_download_stream."""

    @pytest.fixture
    def mock_s3(self):
        return MagicMock()

    @pytest.fixture
    def store(self, mock_s3):
        return AwsBlobStore(bucket="test-bucket", region="us-east-1", s3_client=mock_s3)

    def _setup_get_object(self, mock_s3, chunks, **overrides):
        body_mock = MagicMock()
        body_mock.iter_chunks.return_value = iter(chunks)
        defaults = {
            "Body": body_mock,
            "ContentLength": sum(len(c) for c in chunks),
            "ContentType": "application/octet-stream",
            "ETag": '"etag"',
            "VersionId": None,
            "Metadata": {},
        }
        defaults.update(overrides)
        mock_s3.get_object.return_value = defaults
        return body_mock

    def test_returns_streaming_response(self, store, mock_s3):
        """download_stream returns a StreamingDownloadResponse."""
        self._setup_get_object(mock_s3, [b"chunk1", b"chunk2"])
        resp = store.download_stream("large-file.bin")

        assert isinstance(resp, StreamingDownloadResponse)
        assert resp.key == "large-file.bin"
        assert resp.metadata.content_length == 12

    def test_stream_yields_chunks(self, store, mock_s3):
        """Iterating the streaming response yields the expected chunks."""
        body_mock = self._setup_get_object(
            mock_s3, [b"aaa", b"bbb"], ContentType="text/plain"
        )
        with store.download_stream("k") as stream:
            chunks = list(stream)
        assert chunks == [b"aaa", b"bbb"]
        body_mock.close.assert_called_once()

    def test_empty_key_raises(self, store):
        with pytest.raises(
            InvalidArgumentException, match="Object name cannot be null or empty"
        ):
            store.download_stream("")

    def test_version_id_passed_to_s3(self, store, mock_s3):
        """version_id is forwarded as VersionId to S3."""
        self._setup_get_object(mock_s3, [b"data"])
        store.download_stream("k", version_id="v99")

        call_kwargs = mock_s3.get_object.call_args[1]
        assert call_kwargs["VersionId"] == "v99"

    def test_range_passed_to_s3(self, store, mock_s3):
        """start/end are forwarded as Range header."""
        self._setup_get_object(mock_s3, [b"range"])
        store.download_stream("k", start=100, end=199)

        call_kwargs = mock_s3.get_object.call_args[1]
        assert call_kwargs["Range"] == "bytes=100-199"

    def test_s3_error_raises_translated_exception(self, store, mock_s3):
        """S3 ClientError is translated to SubstrateSdkException."""
        from botocore.exceptions import ClientError as BotoClientError

        error_resp = {
            "Error": {"Code": "NoSuchKey", "Message": "Not found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_s3.get_object.side_effect = BotoClientError(error_resp, "GetObject")

        from multicloud.common.exceptions import ResourceNotFoundException

        with pytest.raises(ResourceNotFoundException):
            store.download_stream("missing-key")

    def test_metadata_version_id_in_response(self, store, mock_s3):
        """VersionId from S3 response is included in metadata."""
        self._setup_get_object(mock_s3, [b"x"], VersionId="vid-abc")
        resp = store.download_stream("k")
        assert resp.metadata.version_id == "vid-abc"


class TestGcpDownloadToPath:
    """Tests for GcpBlobStore.do_download_to_path."""

    @pytest.fixture
    def store(self):
        with (
            patch(
                "multicloud.blob.blob_gcp.gcp_blob_store.google.auth.default",
                return_value=(MagicMock(), "test-project"),
            ),
            patch("multicloud.blob.blob_gcp.gcp_blob_store.storage.Client") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_bucket = MagicMock()
            mock_client.bucket.return_value = mock_bucket
            s = GcpBlobStore(bucket="test-bucket")
            s._mock_bucket = mock_bucket
            return s

    def _setup_blob(self, store, **overrides):
        mock_blob = MagicMock()
        mock_blob.size = overrides.get("size", 1024)
        mock_blob.content_type = overrides.get("content_type", "application/pdf")
        mock_blob.etag = overrides.get("etag", "etag1")
        mock_blob.generation = overrides.get("generation", 12345)
        mock_blob.metadata = overrides.get("metadata", {})
        store._mock_bucket.blob.return_value = mock_blob
        return mock_blob

    def test_calls_download_to_filename(self, store, tmp_path):
        """Delegates to blob.download_to_filename, writing to a temp file in dest.parent."""
        self._setup_blob(store)

        dest = tmp_path / "out.pdf"
        resp = store.download_to_path("docs/report.pdf", dest)

        mock_dl = store._mock_bucket.blob.return_value.download_to_filename
        assert mock_dl.call_count == 1
        called_path, called_kwargs = mock_dl.call_args.args[0], mock_dl.call_args.kwargs
        # Atomic-write pattern: GCS writes to a temp file under dest.parent,
        # then the implementation renames it to dest on success.
        assert Path(called_path).parent == tmp_path
        assert Path(called_path) != dest
        assert called_kwargs == {"start": None, "end": None}
        assert resp.content == b""
        assert resp.metadata.content_type == "application/pdf"

    def test_empty_key_raises(self, store, tmp_path):
        with pytest.raises(
            InvalidArgumentException, match="Object name cannot be null or empty"
        ):
            store.download_to_path("", tmp_path / "out.bin")

    def test_version_id_passes_generation(self, store, tmp_path):
        """version_id is converted to an integer generation."""
        self._setup_blob(store)
        dest = tmp_path / "out.bin"
        store.download_to_path("k", dest, version_id="9999")

        store._mock_bucket.blob.assert_called_with("k", generation=9999)

    def test_invalid_version_id_raises(self, store, tmp_path):
        """Non-integer version_id raises InvalidArgumentException."""
        dest = tmp_path / "out.bin"
        with pytest.raises(InvalidArgumentException, match="integer generation"):
            store.download_to_path("k", dest, version_id="not-a-number")

    def test_range_params_forwarded(self, store, tmp_path):
        """start/end are forwarded to download_to_filename (called against the temp file)."""
        mock_blob = self._setup_blob(store)
        dest = tmp_path / "out.bin"
        store.download_to_path("k", dest, start=10, end=50)

        assert mock_blob.download_to_filename.call_count == 1
        called_path = mock_blob.download_to_filename.call_args.args[0]
        called_kwargs = mock_blob.download_to_filename.call_args.kwargs
        assert Path(called_path).parent == tmp_path
        assert called_kwargs == {"start": 10, "end": 50}

    def test_gcs_error_raises_translated_exception(self, store, tmp_path):
        """GCS NotFound is translated to ResourceNotFoundException."""
        from google.api_core.exceptions import NotFound

        mock_blob = self._setup_blob(store)
        mock_blob.download_to_filename.side_effect = NotFound("not found")

        from multicloud.common.exceptions import ResourceNotFoundException

        dest = tmp_path / "out.bin"
        with pytest.raises(ResourceNotFoundException):
            store.download_to_path("missing", dest)


class TestGcpDownloadToFile:
    """Tests for GcpBlobStore.do_download_to_file."""

    @pytest.fixture
    def store(self):
        with (
            patch(
                "multicloud.blob.blob_gcp.gcp_blob_store.google.auth.default",
                return_value=(MagicMock(), "test-project"),
            ),
            patch("multicloud.blob.blob_gcp.gcp_blob_store.storage.Client") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_bucket = MagicMock()
            mock_client.bucket.return_value = mock_bucket
            s = GcpBlobStore(bucket="test-bucket")
            s._mock_bucket = mock_bucket
            return s

    def _setup_blob(self, store, **overrides):
        mock_blob = MagicMock()
        mock_blob.size = overrides.get("size", 500)
        mock_blob.content_type = overrides.get("content_type", "text/plain")
        mock_blob.etag = overrides.get("etag", "etag2")
        mock_blob.generation = overrides.get("generation", 999)
        mock_blob.metadata = overrides.get("metadata", {"author": "test"})
        store._mock_bucket.blob.return_value = mock_blob
        return mock_blob

    def test_calls_download_to_file(self, store):
        """Delegates to blob.download_to_file with the file object."""
        self._setup_blob(store)

        buf = io.BytesIO()
        resp = store.download_to_file("notes.txt", buf)

        store._mock_bucket.blob.return_value.download_to_file.assert_called_once_with(
            buf, start=None, end=None
        )
        assert resp.content == b""
        assert resp.metadata.metadata == {"author": "test"}

    def test_version_id_passes_generation(self, store):
        """version_id is converted to an integer generation."""
        self._setup_blob(store)
        buf = io.BytesIO()
        store.download_to_file("k", buf, version_id="555")

        store._mock_bucket.blob.assert_called_with("k", generation=555)

    def test_invalid_version_id_raises(self, store):
        """Non-integer version_id raises InvalidArgumentException."""
        buf = io.BytesIO()
        with pytest.raises(InvalidArgumentException, match="integer generation"):
            store.download_to_file("k", buf, version_id="bad")

    def test_range_params_forwarded(self, store):
        """start/end are forwarded to download_to_file."""
        mock_blob = self._setup_blob(store)
        buf = io.BytesIO()
        store.download_to_file("k", buf, start=20, end=80)

        mock_blob.download_to_file.assert_called_once_with(buf, start=20, end=80)

    def test_gcs_error_raises_translated_exception(self, store):
        """GCS NotFound is translated to ResourceNotFoundException."""
        from google.api_core.exceptions import NotFound

        mock_blob = self._setup_blob(store)
        mock_blob.download_to_file.side_effect = NotFound("not found")

        from multicloud.common.exceptions import ResourceNotFoundException

        buf = io.BytesIO()
        with pytest.raises(ResourceNotFoundException):
            store.download_to_file("missing", buf)


class TestGcpDownloadStream:
    """Tests for GcpBlobStore.do_download_stream."""

    @pytest.fixture
    def store(self):
        with (
            patch(
                "multicloud.blob.blob_gcp.gcp_blob_store.google.auth.default",
                return_value=(MagicMock(), "test-project"),
            ),
            patch("multicloud.blob.blob_gcp.gcp_blob_store.storage.Client") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_bucket = MagicMock()
            mock_client.bucket.return_value = mock_bucket
            s = GcpBlobStore(bucket="test-bucket")
            s._mock_bucket = mock_bucket
            return s

    def _setup_blob(self, store, reader_chunks, **overrides):
        mock_blob = MagicMock()
        mock_blob.size = overrides.get("size", 2048)
        mock_blob.content_type = overrides.get(
            "content_type", "application/octet-stream"
        )
        mock_blob.etag = overrides.get("etag", "etag3")
        mock_blob.generation = overrides.get("generation", 777)
        mock_blob.metadata = overrides.get("metadata", {})
        store._mock_bucket.blob.return_value = mock_blob

        mock_reader = MagicMock()
        mock_reader.read.side_effect = reader_chunks + [b""]
        mock_blob.open.return_value = mock_reader
        return mock_blob, mock_reader

    def test_returns_streaming_response(self, store):
        """download_stream returns a StreamingDownloadResponse backed by blob.open()."""
        self._setup_blob(store, [b"part1", b"part2"])

        resp = store.download_stream("big-file.dat")

        assert isinstance(resp, StreamingDownloadResponse)
        assert resp.key == "big-file.dat"
        assert resp.metadata.content_length == 2048

    def test_stream_yields_chunks_from_reader(self, store):
        """Iterating the response yields chunks from the underlying reader."""
        _, mock_reader = self._setup_blob(store, [b"aaa", b"bbb"], size=10)

        with store.download_stream("k") as stream:
            chunks = list(stream)

        assert chunks == [b"aaa", b"bbb"]
        mock_reader.close.assert_called_once()

    def test_empty_key_raises(self, store):
        with pytest.raises(
            InvalidArgumentException, match="Object name cannot be null or empty"
        ):
            store.download_stream("")

    def test_version_id_passes_generation(self, store):
        """version_id is converted to an integer generation."""
        self._setup_blob(store, [b"data"])
        store.download_stream("k", version_id="888")

        store._mock_bucket.blob.assert_called_with("k", generation=888)

    def test_invalid_version_id_raises(self, store):
        """Non-integer version_id raises InvalidArgumentException."""
        with pytest.raises(InvalidArgumentException, match="integer generation"):
            store.download_stream("k", version_id="invalid")

    def test_gcs_reload_error_raises_translated(self, store):
        """GCS error on blob.reload() is translated."""
        from google.api_core.exceptions import NotFound

        mock_blob = MagicMock()
        mock_blob.reload.side_effect = NotFound("not found")
        store._mock_bucket.blob.return_value = mock_blob

        from multicloud.common.exceptions import ResourceNotFoundException

        with pytest.raises(ResourceNotFoundException):
            store.download_stream("missing")

    def test_gcs_open_error_raises_translated(self, store):
        """GCS error on blob.open() is translated."""
        from google.api_core.exceptions import Forbidden

        mock_blob = MagicMock()
        mock_blob.size = 100
        mock_blob.content_type = "text/plain"
        mock_blob.etag = "e"
        mock_blob.generation = 1
        mock_blob.metadata = {}
        mock_blob.open.side_effect = Forbidden("access denied")
        store._mock_bucket.blob.return_value = mock_blob

        from multicloud.common.exceptions import UnAuthorizedException

        with pytest.raises(UnAuthorizedException):
            store.download_stream("forbidden-file")


class TestAwsMidStreamFailure:
    """Mid-stream failure tests for the AWS implementation.

    These tests verify the two fixes:
      1. Atomic writes: a mid-stream failure leaves no file at the destination.
      2. Stream cleanup: body_stream.close() runs in try/finally so the S3
         connection is always released, even when iter_chunks or
         file_obj.write raises.
    """

    @pytest.fixture
    def mock_s3(self):
        return MagicMock()

    @pytest.fixture
    def store(self, mock_s3):
        return AwsBlobStore(bucket="test-bucket", region="us-east-1", s3_client=mock_s3)

    def _setup_get_object(self, mock_s3, body_mock, content_length=8):
        mock_s3.get_object.return_value = {
            "Body": body_mock,
            "ContentLength": content_length,
            "ContentType": "application/octet-stream",
            "ETag": '"etag"',
            "VersionId": None,
            "Metadata": {},
        }

    def _chunks_then_raise(self, chunks, exc):
        """Generator that yields the given chunks and then raises exc."""

        def gen():
            for c in chunks:
                yield c
            raise exc

        return gen()

    def test_path_mid_stream_botocore_error_cleans_up_and_translates(
        self, store, mock_s3, tmp_path
    ):
        """iter_chunks yields one chunk then raises BotoCoreError.

        Verifies: body_stream.close() called, no file at destination,
        exception translated to SubstrateSdkException.
        """
        from botocore.exceptions import BotoCoreError
        from multicloud.common.exceptions import SubstrateSdkException

        body_mock = MagicMock()
        body_mock.iter_chunks.return_value = self._chunks_then_raise(
            [b"first-chunk"], BotoCoreError()
        )
        self._setup_get_object(mock_s3, body_mock, content_length=20)

        dest = tmp_path / "out.bin"

        with pytest.raises(SubstrateSdkException):
            store.download_to_path("k", dest)

        body_mock.close.assert_called_once()
        assert not dest.exists(), "no file should remain at destination on failure"
        # Temp file must also be cleaned up — only the original tmp_path dir
        # contents remain unchanged.
        leftover = [p for p in tmp_path.iterdir() if p.name != "out.bin"]
        assert leftover == [], f"temp files should be cleaned up, found: {leftover}"

    def test_path_no_partial_file_when_get_object_succeeds_then_stream_fails(
        self, store, mock_s3, tmp_path
    ):
        """If a destination file already exists, a mid-stream failure must
        not overwrite it with a partial result.
        """
        from botocore.exceptions import BotoCoreError
        from multicloud.common.exceptions import SubstrateSdkException

        body_mock = MagicMock()
        body_mock.iter_chunks.return_value = self._chunks_then_raise(
            [b"partial"], BotoCoreError()
        )
        self._setup_get_object(mock_s3, body_mock, content_length=999)

        dest = tmp_path / "preexisting.bin"
        dest.write_bytes(b"ORIGINAL-CONTENTS")

        with pytest.raises(SubstrateSdkException):
            store.download_to_path("k", dest)

        # Atomic rename never happened, so the original content survives.
        assert dest.read_bytes() == b"ORIGINAL-CONTENTS"
        body_mock.close.assert_called_once()

    def test_file_write_failure_still_closes_stream(self, store, mock_s3):
        """If file_obj.write() raises, body_stream.close() must still be called."""
        body_mock = MagicMock()
        body_mock.iter_chunks.return_value = iter([b"first", b"second"])
        self._setup_get_object(mock_s3, body_mock, content_length=11)

        bad_file = MagicMock()
        bad_file.write.side_effect = OSError("disk full")

        with pytest.raises(OSError, match="disk full"):
            store.download_to_file("k", bad_file)

        body_mock.close.assert_called_once()

    def test_file_iter_chunks_failure_still_closes_stream(self, store, mock_s3):
        """If iter_chunks raises mid-stream, body_stream.close() must still be
        called and the exception must be translated to SubstrateSdkException.
        """
        from botocore.exceptions import BotoCoreError
        from multicloud.common.exceptions import SubstrateSdkException

        body_mock = MagicMock()
        body_mock.iter_chunks.return_value = self._chunks_then_raise(
            [b"chunk1"], BotoCoreError()
        )
        self._setup_get_object(mock_s3, body_mock, content_length=20)

        buf = io.BytesIO()
        with pytest.raises(SubstrateSdkException):
            store.download_to_file("k", buf)

        body_mock.close.assert_called_once()


class TestGcpMidStreamFailure:
    """Mid-stream failure tests for the GCP implementation.

    Verifies:
      1. download_to_filename failures leave no file at the destination
         (temp file cleaned up).
      2. Range stream reader failures close the reader and translate.
    """

    @pytest.fixture
    def store(self):
        with (
            patch(
                "multicloud.blob.blob_gcp.gcp_blob_store.google.auth.default",
                return_value=(MagicMock(), "test-project"),
            ),
            patch("multicloud.blob.blob_gcp.gcp_blob_store.storage.Client") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_bucket = MagicMock()
            mock_client.bucket.return_value = mock_bucket
            s = GcpBlobStore(bucket="test-bucket")
            s._mock_bucket = mock_bucket
            return s

    def test_path_download_to_filename_mid_stream_failure_cleans_up(
        self, store, tmp_path
    ):
        """blob.download_to_filename raises mid-stream — verify no file at
        destination and exception translated.
        """
        from google.api_core.exceptions import GoogleAPIError
        from multicloud.common.exceptions import SubstrateSdkException

        mock_blob = MagicMock()
        mock_blob.size = 100
        mock_blob.content_type = "application/octet-stream"
        mock_blob.etag = "e"
        mock_blob.generation = 1
        mock_blob.metadata = {}

        def fake_download(path, **kwargs):
            # Simulate partial write to the temp file before failing.
            with open(path, "wb") as f:
                f.write(b"partial-bytes")
            raise GoogleAPIError("network error mid-stream")

        mock_blob.download_to_filename.side_effect = fake_download
        store._mock_bucket.blob.return_value = mock_blob

        dest = tmp_path / "out.bin"

        with pytest.raises(SubstrateSdkException):
            store.download_to_path("k", dest)

        assert not dest.exists(), "no file should remain at destination on failure"
        # Temp file must also be cleaned up — directory should be empty.
        leftover = list(tmp_path.iterdir())
        assert leftover == [], f"temp files should be cleaned up, found: {leftover}"

    def test_path_download_to_filename_failure_preserves_existing_file(
        self, store, tmp_path
    ):
        """If destination already exists, a mid-stream failure must leave it
        untouched (atomic rename never happens).
        """
        from google.api_core.exceptions import GoogleAPIError
        from multicloud.common.exceptions import SubstrateSdkException

        mock_blob = MagicMock()
        mock_blob.size = 100
        mock_blob.content_type = "application/octet-stream"
        mock_blob.etag = "e"
        mock_blob.generation = 1
        mock_blob.metadata = {}
        mock_blob.download_to_filename.side_effect = GoogleAPIError("boom")
        store._mock_bucket.blob.return_value = mock_blob

        dest = tmp_path / "preexisting.bin"
        dest.write_bytes(b"ORIGINAL")

        with pytest.raises(SubstrateSdkException):
            store.download_to_path("k", dest)

        assert dest.read_bytes() == b"ORIGINAL"

    def test_range_stream_reader_read_raises_closes_reader(self, store):
        """When iterating a range stream and reader.read() raises, the reader
        must be closed (no leaked read channel) and the error must propagate.
        """
        mock_blob = MagicMock()
        mock_blob.size = 1024
        mock_blob.content_type = "application/octet-stream"
        mock_blob.etag = "e"
        mock_blob.generation = 1
        mock_blob.metadata = {}

        mock_reader = MagicMock()
        # First read returns a chunk; second raises mid-stream.
        mock_reader.read.side_effect = [b"first-chunk", IOError("connection reset")]
        mock_blob.open.return_value = mock_reader
        store._mock_bucket.blob.return_value = mock_blob

        # Range download — exercises the lazy chunk iterator path.
        resp = store.download_stream("k", start=0, end=99)

        with resp as stream:
            with pytest.raises(IOError, match="connection reset"):
                list(stream)

        # Context manager close must still release the reader.
        mock_reader.close.assert_called_once()

    def test_full_stream_reader_read_raises_closes_reader(self, store):
        """Full-object stream: reader.read() raising mid-stream still closes
        the reader on context-manager exit.
        """
        mock_blob = MagicMock()
        mock_blob.size = 1024
        mock_blob.content_type = "application/octet-stream"
        mock_blob.etag = "e"
        mock_blob.generation = 1
        mock_blob.metadata = {}

        mock_reader = MagicMock()
        mock_reader.read.side_effect = [b"chunk1", IOError("network blip")]
        mock_blob.open.return_value = mock_reader
        store._mock_bucket.blob.return_value = mock_blob

        resp = store.download_stream("k")

        with resp as stream:
            with pytest.raises(IOError, match="network blip"):
                list(stream)

        mock_reader.close.assert_called_once()
