"""Unit tests for :meth:`GcpBlobStore.do_get_metadata` — HEAD-style lookup.

The sync provider translates native google-api-core errors inside
``do_get_metadata`` (mirroring ``do_download``), so these store-level tests
assert the translated :class:`SubstrateSdkException` subtypes directly.

Unlike the S3 provider, GCS exposes a real creation timestamp
(``blob.time_created``) distinct from ``blob.updated``; both are already
tz-aware datetimes, so no RFC3339 string parsing is needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from google.api_core import exceptions as gcs_exceptions
from multicloud.blob.blob_gcp.gcp_blob_store import GcpBlobStore
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceNotFoundException,
    UnAuthorizedException,
)


@pytest.fixture
def mock_credentials():
    """Mock google.auth.default() credentials."""
    creds = MagicMock()
    creds.valid = True
    creds.token = "fake-access-token"
    creds.service_account_email = "test@project.iam.gserviceaccount.com"
    return creds


@pytest.fixture
def mock_storage_client(mock_credentials):
    """A mock google.cloud.storage.Client with google.auth.default patched."""
    with (
        patch("google.auth.default") as mock_auth,
        patch("multicloud.blob.blob_gcp.gcp_blob_store.storage.Client") as mock_cls,
    ):
        mock_auth.return_value = (mock_credentials, "test-project")
        client_instance = MagicMock()
        client_instance._credentials = mock_credentials
        mock_cls.return_value = client_instance
        yield client_instance


@pytest.fixture
def store(mock_storage_client):
    """A GcpBlobStore with a mocked storage client."""
    return GcpBlobStore(bucket="test-bucket", region="us-central1")


def _make_blob(**attrs):
    """Build a blob handle mock with sensible metadata defaults."""
    blob = MagicMock()
    blob.size = attrs.get("size")
    blob.content_type = attrs.get("content_type")
    blob.etag = attrs.get("etag")
    blob.generation = attrs.get("generation")
    blob.metadata = attrs.get("metadata")
    blob.updated = attrs.get("updated")
    blob.time_created = attrs.get("time_created")
    return blob


class TestGcpGetMetadata:
    """Tests for do_get_metadata (GCS blob.reload)."""

    def test_maps_all_fields(self, store, mock_storage_client):
        """A fully-populated blob maps onto every BlobMetadata field."""
        updated = datetime(2025, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
        created = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        blob = _make_blob(
            size=42,
            content_type="application/json",
            etag="gcs-etag",
            generation=999,
            metadata={"k": "v"},
            updated=updated,
            time_created=created,
        )
        mock_storage_client.bucket.return_value.blob.return_value = blob

        result = store.do_get_metadata("file.json", version_id=None)

        assert result.key == "file.json"
        assert result.content_length == 42
        assert result.content_type == "application/json"
        assert result.e_tag == "gcs-etag"
        assert result.version_id == "999"
        assert result.metadata == {"k": "v"}
        # last_modified maps to blob.updated; created_time to blob.time_created.
        assert result.last_modified == updated
        assert result.created_time == created
        blob.reload.assert_called_once()

    def test_does_not_download_body(self, store, mock_storage_client):
        """get_metadata must reload metadata only, never download bytes."""
        blob = _make_blob(size=10, generation=1)
        mock_storage_client.bucket.return_value.blob.return_value = blob

        store.do_get_metadata("k", version_id=None)

        blob.reload.assert_called_once()
        blob.download_as_bytes.assert_not_called()

    def test_missing_optional_fields_default(self, store, mock_storage_client):
        """A sparse blob still yields a valid BlobMetadata with safe defaults."""
        blob = _make_blob(size=0)
        mock_storage_client.bucket.return_value.blob.return_value = blob

        result = store.do_get_metadata("empty", version_id=None)

        assert result.key == "empty"
        assert result.content_length == 0
        assert result.content_type is None
        assert result.e_tag is None
        assert result.version_id is None
        # None metadata coerces to an empty dict, never None.
        assert result.metadata == {}
        assert result.last_modified is None
        assert result.created_time is None

    def test_version_id_threaded_as_generation(self, store, mock_storage_client):
        """version_id='42' resolves to an int generation on the blob handle."""
        blob = _make_blob(size=0, generation=42)
        mock_storage_client.bucket.return_value.blob.return_value = blob

        result = store.do_get_metadata("k", version_id="42")

        assert result.version_id == "42"
        mock_storage_client.bucket.return_value.blob.assert_called_with(
            "k", generation=42
        )

    def test_invalid_version_id_raises_invalid_argument(
        self, store, mock_storage_client
    ):
        """A non-integer version_id fails fast before any network call."""
        with pytest.raises(InvalidArgumentException, match="integer generation"):
            store.do_get_metadata("k", version_id="not-a-number")

    def test_not_found_raises_resource_not_found(self, store, mock_storage_client):
        """A NotFound on reload translates to ResourceNotFoundException."""
        blob = _make_blob()
        blob.reload.side_effect = gcs_exceptions.NotFound("gone")
        mock_storage_client.bucket.return_value.blob.return_value = blob

        with pytest.raises(ResourceNotFoundException):
            store.do_get_metadata("missing", version_id=None)

    def test_forbidden_raises_unauthorized(self, store, mock_storage_client):
        """A Forbidden on reload translates to UnAuthorizedException."""
        blob = _make_blob()
        blob.reload.side_effect = gcs_exceptions.Forbidden("denied")
        mock_storage_client.bucket.return_value.blob.return_value = blob

        with pytest.raises(UnAuthorizedException):
            store.do_get_metadata("protected", version_id=None)

    def test_gcp_tag_entries_not_stripped(self, store, mock_storage_client):
        """Raw custom metadata is returned verbatim, including gcp-tag-* keys.

        This documents parity with do_download: get_metadata does NOT filter
        the internal tag-emulation prefix; only get_tags strips it.
        """
        blob = _make_blob(
            size=1,
            metadata={"gcp-tag-env": "prod", "user-key": "user-val"},
        )
        mock_storage_client.bucket.return_value.blob.return_value = blob

        result = store.do_get_metadata("k", version_id=None)

        assert result.metadata == {"gcp-tag-env": "prod", "user-key": "user-val"}

    def test_public_get_metadata_happy_path(self, store, mock_storage_client):
        """The public get_metadata delegates to do_get_metadata and returns it."""
        blob = _make_blob(size=3, etag="e", generation=7)
        mock_storage_client.bucket.return_value.blob.return_value = blob

        result = store.get_metadata("k")

        assert result.key == "k"
        assert result.content_length == 3
        assert result.version_id == "7"

    def test_empty_key_raises_invalid_argument(self, store):
        """The base class validates the key before any network call."""
        with pytest.raises(
            InvalidArgumentException, match="Object name cannot be null or empty"
        ):
            store.get_metadata("")
