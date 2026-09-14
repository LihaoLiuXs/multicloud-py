"""Unit tests for GCP async get_metadata implementation.

Tests validate GcpAsyncBlobStore.do_get_metadata:
- download_metadata success → maps fields (size string→int, etag,
  generation→version_id, RFC3339 updated→last_modified & timeCreated→created_time)
- No body is downloaded (download is never awaited)
- versioned access uses _download(params={"alt":"json","generation":...})
- invalid (non-integer) version_id → InvalidArgumentException
- 404 → BlobNotFoundError (with key + cause)
- 403 / network / unknown errors → propagate raw to the client layer
"""

from __future__ import annotations

import json
from datetime import timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from multicloud.blob.blob_client.async_blob.async_driver import (
    BlobMetadata,
    BlobNotFoundError,
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
    mock.download_metadata = AsyncMock()
    mock._download = AsyncMock()
    mock.download = AsyncMock()
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


# Tests for do_get_metadata (non-versioned)


@pytest.mark.asyncio
class TestGcpAsyncGetMetadata:
    """Tests for GcpAsyncBlobStore.do_get_metadata."""

    async def test_success_maps_all_fields(self, store, mock_storage):
        """download_metadata success maps every BlobMetadata field."""
        mock_storage.download_metadata.return_value = {
            "name": "test-key.txt",
            "size": "4096",
            "contentType": "text/plain",
            "etag": "CNCJo5bv1PUCEAK=",
            "generation": "1234567890",
            "metadata": {"team": "multicloud"},
            "updated": "2026-07-20T12:30:45.123456Z",
            "timeCreated": "2026-07-19T08:00:00.000000Z",
        }

        result = await store.do_get_metadata("test-key.txt", version_id=None)

        assert isinstance(result, BlobMetadata)
        assert result.key == "test-key.txt"
        # GCS "size" is a decimal string; coerced to int.
        assert result.content_length == 4096
        assert result.content_type == "text/plain"
        assert result.e_tag == "CNCJo5bv1PUCEAK="
        assert result.version_id == "1234567890"
        assert result.metadata == {"team": "multicloud"}
        mock_storage.download_metadata.assert_awaited_once_with(
            "test-bucket", "test-key.txt"
        )

    async def test_success_parses_rfc3339_timestamps(self, store, mock_storage):
        """updated → last_modified and timeCreated → created_time (tz-aware)."""
        mock_storage.download_metadata.return_value = {
            "name": "k",
            "size": "1",
            "updated": "2026-07-20T12:30:45.123456Z",
            "timeCreated": "2026-07-19T08:00:00.000000Z",
        }

        result = await store.do_get_metadata("k", version_id=None)

        assert result.last_modified is not None
        assert result.last_modified.year == 2026
        assert result.last_modified.month == 7
        assert result.last_modified.day == 20
        assert result.last_modified.hour == 12
        assert result.last_modified.microsecond == 123456
        # Trailing "Z" normalized to a UTC tz-aware datetime.
        assert result.last_modified.tzinfo is not None
        assert result.last_modified.utcoffset() == timezone.utc.utcoffset(None)

        assert result.created_time is not None
        assert result.created_time.day == 19
        assert result.created_time.hour == 8
        assert result.created_time.tzinfo is not None
        # last_modified and created_time come from distinct GCS fields.
        assert result.created_time != result.last_modified

    async def test_does_not_download_body(self, store, mock_storage):
        """get_metadata must not download the object body."""
        mock_storage.download_metadata.return_value = {"name": "k", "size": "1"}

        await store.do_get_metadata("k", version_id=None)

        mock_storage.download.assert_not_awaited()

    async def test_missing_optional_fields_default(self, store, mock_storage):
        """A sparse metadata dict still yields a valid BlobMetadata."""
        mock_storage.download_metadata.return_value = {"name": "k"}

        result = await store.do_get_metadata("k", version_id=None)

        assert result.key == "k"
        assert result.content_length is None
        assert result.content_type is None
        assert result.e_tag is None
        assert result.version_id is None
        assert result.metadata == {}
        assert result.last_modified is None
        assert result.created_time is None

    async def test_non_numeric_size_coerces_to_none(self, store, mock_storage):
        """A non-numeric size is defensively coerced to None."""
        mock_storage.download_metadata.return_value = {
            "name": "k",
            "size": "not-a-number",
        }

        result = await store.do_get_metadata("k", version_id=None)

        assert result.content_length is None

    async def test_null_metadata_defaults_to_empty(self, store, mock_storage):
        """A null 'metadata' value defaults to an empty dict."""
        mock_storage.download_metadata.return_value = {"name": "k", "metadata": None}

        result = await store.do_get_metadata("k", version_id=None)

        assert result.metadata == {}

    async def test_404_raises_blob_not_found(self, store, mock_storage):
        """download_metadata 404 raises BlobNotFoundError carrying key + cause."""
        cause = _client_response_error(404)
        mock_storage.download_metadata.side_effect = cause

        with pytest.raises(BlobNotFoundError) as excinfo:
            await store.do_get_metadata("missing.txt", version_id=None)

        assert excinfo.value.key == "missing.txt"
        assert excinfo.value.cause is cause

    async def test_403_propagates(self, store, mock_storage):
        """download_metadata 403 propagates raw to the client layer."""
        mock_storage.download_metadata.side_effect = _client_response_error(403)

        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_get_metadata("k", version_id=None)

    async def test_500_propagates(self, store, mock_storage):
        """download_metadata 500 propagates raw (not BlobNotFound)."""
        mock_storage.download_metadata.side_effect = _client_response_error(500)

        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_get_metadata("k", version_id=None)

    async def test_network_error_propagates(self, store, mock_storage):
        """download_metadata network error propagates raw to the client layer."""
        mock_storage.download_metadata.side_effect = aiohttp.ClientError("boom")

        with pytest.raises(aiohttp.ClientError):
            await store.do_get_metadata("k", version_id=None)


# Tests for do_get_metadata (versioned)


@pytest.mark.asyncio
class TestGcpAsyncGetMetadataVersioned:
    """Versioned get_metadata uses _download with the generation param."""

    async def test_versioned_uses_download_with_generation(self, store, mock_storage):
        """Versioned reads pin the generation via _download and parse JSON bytes."""
        mock_storage._download.return_value = json.dumps(
            {
                "name": "test-key.txt",
                "size": "128",
                "contentType": "application/octet-stream",
                "etag": "abc=",
                "generation": "1234567890",
                "updated": "2026-07-20T12:30:45Z",
                "timeCreated": "2026-07-19T08:00:00Z",
            }
        ).encode()

        result = await store.do_get_metadata("test-key.txt", version_id="1234567890")

        assert result.content_length == 128
        assert result.version_id == "1234567890"
        assert result.last_modified is not None
        assert result.created_time is not None
        mock_storage._download.assert_awaited_once_with(
            "test-bucket",
            "test-key.txt",
            params={"alt": "json", "generation": "1234567890"},
        )
        # Non-versioned path must not be used.
        mock_storage.download_metadata.assert_not_awaited()

    async def test_versioned_zero_generation(self, store, mock_storage):
        """generation='0' is a valid integer and uses the _download path."""
        mock_storage._download.return_value = json.dumps(
            {"name": "k", "generation": "0"}
        ).encode()

        result = await store.do_get_metadata("k", version_id="0")

        assert result.key == "k"
        mock_storage._download.assert_awaited_once_with(
            "test-bucket", "k", params={"alt": "json", "generation": "0"}
        )

    async def test_versioned_invalid_version_id_raises(self, store, mock_storage):
        """Non-integer version_id raises InvalidArgumentException (no call made)."""
        with pytest.raises(
            InvalidArgumentException,
            match="GCP version_id must be an integer generation; got 'not-an-integer'",
        ):
            await store.do_get_metadata("k", version_id="not-an-integer")

        mock_storage._download.assert_not_awaited()
        mock_storage.download_metadata.assert_not_awaited()

    async def test_versioned_404_raises_blob_not_found(self, store, mock_storage):
        """Versioned _download 404 raises BlobNotFoundError carrying key + cause."""
        cause = _client_response_error(404)
        mock_storage._download.side_effect = cause

        with pytest.raises(BlobNotFoundError) as excinfo:
            await store.do_get_metadata("missing.txt", version_id="123")

        assert excinfo.value.key == "missing.txt"
        assert excinfo.value.cause is cause

    async def test_versioned_403_propagates(self, store, mock_storage):
        """Versioned _download 403 propagates raw to the client layer."""
        mock_storage._download.side_effect = _client_response_error(403)

        with pytest.raises(aiohttp.ClientResponseError):
            await store.do_get_metadata("k", version_id="123")

    async def test_versioned_does_not_download_body(self, store, mock_storage):
        """Versioned get_metadata must not download the object body."""
        mock_storage._download.return_value = json.dumps(
            {"name": "k", "generation": "123"}
        ).encode()

        await store.do_get_metadata("k", version_id="123")

        mock_storage.download.assert_not_awaited()


# Tests for parse_rfc3339_timestamp helper


class TestParseRfc3339Timestamp:
    """Tests for the parse_rfc3339_timestamp utility function."""

    def test_parse_none_returns_none(self):
        """None input returns None (best-effort parsing)."""
        from multicloud.blob.blob_client.async_blob.async_driver import (
            parse_rfc3339_timestamp,
        )

        assert parse_rfc3339_timestamp(None) is None

    def test_parse_empty_string_returns_none(self):
        """Empty string returns None (falsy check)."""
        from multicloud.blob.blob_client.async_blob.async_driver import (
            parse_rfc3339_timestamp,
        )

        assert parse_rfc3339_timestamp("") is None

    def test_parse_invalid_timestamp_returns_none(self):
        """Malformed timestamp returns None (ValueError caught)."""
        from multicloud.blob.blob_client.async_blob.async_driver import (
            parse_rfc3339_timestamp,
        )

        # This covers the except ValueError: return None branch at lines 390-391
        assert parse_rfc3339_timestamp("not-a-real-timestamp") is None

    def test_parse_valid_rfc3339_with_z_suffix(self):
        """Valid RFC 3339 timestamp with 'Z' suffix is parsed correctly."""
        from datetime import timedelta

        from multicloud.blob.blob_client.async_blob.async_driver import (
            parse_rfc3339_timestamp,
        )

        result = parse_rfc3339_timestamp("2026-07-20T12:30:45Z")

        assert result is not None
        assert result.year == 2026
        assert result.month == 7
        assert result.day == 20
        assert result.hour == 12
        assert result.minute == 30
        assert result.second == 45
        # Must be timezone-aware with UTC offset
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)

    def test_parse_valid_rfc3339_with_microseconds(self):
        """Valid RFC 3339 timestamp with microseconds is parsed correctly."""
        from multicloud.blob.blob_client.async_blob.async_driver import (
            parse_rfc3339_timestamp,
        )

        result = parse_rfc3339_timestamp("2026-07-20T12:30:45.123456Z")

        assert result is not None
        assert result.microsecond == 123456
