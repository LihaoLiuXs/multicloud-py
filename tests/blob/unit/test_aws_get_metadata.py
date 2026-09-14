"""Unit tests for :meth:`AwsBlobStore.do_get_metadata` — HEAD-style lookup.

The sync provider translates native boto errors inside ``do_get_metadata``
(mirroring ``do_download``), so these store-level tests assert the translated
:class:`SubstrateSdkException` subtypes directly rather than the raw boto
exceptions asserted by the async suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from multicloud.blob.blob_aws.aws_blob_store import AwsBlobStore
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceNotFoundException,
    UnAuthorizedException,
    UnknownException,
)


@pytest.fixture
def mock_s3():
    """A mock boto3 S3 client."""
    return MagicMock()


@pytest.fixture
def store(mock_s3):
    """An AwsBlobStore with an injected mock S3 client."""
    return AwsBlobStore(bucket="test-bucket", region="us-east-1", s3_client=mock_s3)


class TestAwsGetMetadata:
    """Tests for do_get_metadata (S3 head_object)."""

    def test_maps_all_fields(self, store, mock_s3):
        """A full head_object response maps onto every BlobMetadata field."""
        last_modified = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        mock_s3.head_object.return_value = {
            "ContentLength": 12,
            "ContentType": "text/plain",
            "ETag": '"etag"',
            "VersionId": "v2",
            "Metadata": {"x": "y"},
            "LastModified": last_modified,
        }

        result = store.do_get_metadata("path/file.txt", version_id=None)

        assert result.key == "path/file.txt"
        assert result.content_length == 12
        assert result.content_type == "text/plain"
        # ETag arrives quoted from S3; the provider strips the quotes.
        assert result.e_tag == "etag"
        assert result.version_id == "v2"
        assert result.metadata == {"x": "y"}
        assert result.last_modified == last_modified
        mock_s3.head_object.assert_called_once_with(
            Bucket="test-bucket", Key="path/file.txt"
        )

    def test_created_time_equals_last_modified(self, store, mock_s3):
        """S3 HEAD has no distinct creation field, so created_time mirrors it."""
        last_modified = datetime(2025, 6, 7, 8, 9, 10, tzinfo=timezone.utc)
        mock_s3.head_object.return_value = {
            "ContentLength": 1,
            "LastModified": last_modified,
        }

        result = store.do_get_metadata("k", version_id=None)

        assert result.created_time == last_modified
        assert result.created_time is result.last_modified

    def test_does_not_download_body(self, store, mock_s3):
        """get_metadata must not fetch the object body (HEAD only)."""
        mock_s3.head_object.return_value = {"ContentLength": 10, "ETag": '"e"'}

        store.do_get_metadata("k", version_id=None)

        mock_s3.get_object.assert_not_called()

    def test_missing_optional_fields_default(self, store, mock_s3):
        """A sparse head_object response still yields a valid BlobMetadata."""
        mock_s3.head_object.return_value = {"ContentLength": 0}

        result = store.do_get_metadata("empty.txt", version_id=None)

        assert result.key == "empty.txt"
        assert result.content_length == 0
        assert result.content_type is None
        assert result.e_tag is None
        assert result.version_id is None
        assert result.metadata == {}
        assert result.last_modified is None
        assert result.created_time is None

    def test_null_metadata_defaults_to_empty(self, store, mock_s3):
        """A ``None`` Metadata value coerces to an empty dict, never None."""
        mock_s3.head_object.return_value = {"ContentLength": 0, "Metadata": None}

        result = store.do_get_metadata("k", version_id=None)

        assert result.metadata == {}

    def test_version_id_threaded_into_head_object(self, store, mock_s3):
        """A supplied version_id is forwarded as VersionId to head_object."""
        mock_s3.head_object.return_value = {"ContentLength": 512, "VersionId": "v123"}

        result = store.do_get_metadata("path/file.txt", version_id="v123")

        assert result.version_id == "v123"
        mock_s3.head_object.assert_called_once_with(
            Bucket="test-bucket", Key="path/file.txt", VersionId="v123"
        )

    def test_404_raises_resource_not_found(self, store, mock_s3):
        """A bare 404 status translates to ResourceNotFoundException."""
        mock_s3.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "404", "Message": "Not Found"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        )
        with pytest.raises(ResourceNotFoundException):
            store.do_get_metadata("missing-key.txt", version_id=None)

    def test_no_such_key_raises_resource_not_found(self, store, mock_s3):
        """NoSuchKey translates to ResourceNotFoundException."""
        mock_s3.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "NoSuchKey", "Message": "The key does not exist"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        )
        with pytest.raises(ResourceNotFoundException):
            store.do_get_metadata("missing-key.txt", version_id=None)

    def test_no_such_bucket_raises_resource_not_found(self, store, mock_s3):
        """NoSuchBucket translates to ResourceNotFoundException."""
        mock_s3.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "NoSuchBucket", "Message": "No such bucket"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        )
        with pytest.raises(ResourceNotFoundException):
            store.do_get_metadata("test-key.txt", version_id=None)

    def test_403_raises_unauthorized(self, store, mock_s3):
        """AccessDenied / 403 translates to UnAuthorizedException."""
        mock_s3.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "Forbidden"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "HeadObject",
        )
        with pytest.raises(UnAuthorizedException):
            store.do_get_metadata("test-key.txt", version_id=None)

    def test_500_raises_unknown(self, store, mock_s3):
        """An unmapped 500 status translates to UnknownException."""
        mock_s3.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "InternalError", "Message": "Server Error"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "HeadObject",
        )
        with pytest.raises(UnknownException):
            store.do_get_metadata("test-key.txt", version_id=None)

    def test_no_credentials_raises_unauthorized(self, store, mock_s3):
        """NoCredentialsError translates to UnAuthorizedException."""
        mock_s3.head_object.side_effect = NoCredentialsError()
        with pytest.raises(UnAuthorizedException):
            store.do_get_metadata("test-key.txt", version_id=None)

    def test_botocore_error_raises_invalid_argument(self, store, mock_s3):
        """A generic BotoCoreError translates to InvalidArgumentException."""
        mock_s3.head_object.side_effect = BotoCoreError()
        with pytest.raises(InvalidArgumentException):
            store.do_get_metadata("test-key.txt", version_id=None)

    def test_public_get_metadata_happy_path(self, store, mock_s3):
        """The public get_metadata delegates to do_get_metadata and returns it."""
        mock_s3.head_object.return_value = {"ContentLength": 3, "ETag": '"abc"'}

        result = store.get_metadata("k")

        assert result.key == "k"
        assert result.content_length == 3
        assert result.e_tag == "abc"

    def test_empty_key_raises_invalid_argument(self, store):
        """The base class validates the key before any network call."""
        with pytest.raises(
            InvalidArgumentException, match="Object name cannot be null or empty"
        ):
            store.get_metadata("")
