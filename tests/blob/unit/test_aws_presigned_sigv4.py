"""Unit tests proving AwsBlobStore presigns with SigV4, not legacy SigV2.

These drive the real botocore signer with throwaway credentials and no
network I/O, so they assert on the actual signed URL rather than a mock of
it. Presigning is pure local computation -- nothing here talks to AWS.

The AWS SDK defaults presigned URL generation to the deprecated SigV2
algorithm in every region whose published endpoint metadata still lists
SigV2 as supported (us-west-2 among them). SigV2 is rejected outright by
buckets with KMS default encryption, which answer a SigV2 presigned
request with SignatureDoesNotMatch or InvalidArgument. The store must
therefore pin SigV4 explicitly.

Assertions deliberately target the URL's query parameters rather than
``client.meta.config.signature_version``: that attribute reads "s3v4"
even when the emitted URL is SigV2, because the SigV2 downgrade is
applied by a ``choose-signer.s3`` event handler rather than by client
config.
"""

from __future__ import annotations

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from multicloud.blob.blob_aws.aws_blob_store import AwsBlobStore
from multicloud.blob.blob_client.driver import PresignedOperation

# Regions whose published endpoint metadata still lists SigV2, so the SDK
# default would emit a SigV2 URL for them. us-west-2 is the region the
# conformance suite defaults to, and the one the bug was reported against.
_SIGV2_LEGACY_REGIONS = ["us-west-2", "us-east-1", "eu-west-1"]

_SIGV4_ALGORITHM = "AWS4-HMAC-SHA256"
_BUCKET = "test-bucket"


def _make_store(session, region, **kwargs) -> AwsBlobStore:
    """Build a store over a real boto3 Session, bypassing credential discovery."""
    with patch(
        "multicloud.blob.blob_aws.aws_blob_store.get_session",
        return_value=session,
    ):
        return AwsBlobStore(bucket=_BUCKET, region=region, **kwargs)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


@pytest.fixture
def session():
    """A boto3 Session with throwaway credentials -- signs, never sends."""
    return boto3.Session(
        aws_access_key_id="AKIAFAKEFAKEFAKE",
        aws_secret_access_key="fakesecretfakesecretfakesecret",
    )


class TestAwsPresignedUsesSigV4:
    @pytest.mark.parametrize("region", _SIGV2_LEGACY_REGIONS)
    @pytest.mark.parametrize(
        "operation", [PresignedOperation.UPLOAD, PresignedOperation.DOWNLOAD]
    )
    def test_url_is_sigv4(self, session, region, operation):
        store = _make_store(session, region)

        response = store.generate_presigned_url(
            "k.txt", operation, duration_seconds=3600
        )

        query = _query(response.url)
        assert query["X-Amz-Algorithm"] == [_SIGV4_ALGORITHM]
        assert "X-Amz-Credential" in query
        assert "X-Amz-Date" in query
        assert "X-Amz-Expires" in query
        assert "X-Amz-Signature" in query
        assert "X-Amz-SignedHeaders" in query

    @pytest.mark.parametrize("region", _SIGV2_LEGACY_REGIONS)
    @pytest.mark.parametrize(
        "operation", [PresignedOperation.UPLOAD, PresignedOperation.DOWNLOAD]
    )
    def test_sigv2_parameters_are_absent(self, session, region, operation):
        store = _make_store(session, region)

        response = store.generate_presigned_url(
            "k.txt", operation, duration_seconds=3600
        )

        query = _query(response.url)
        assert "AWSAccessKeyId" not in query
        assert "Signature" not in query
        assert "Expires" not in query

    def test_credential_scope_matches_requested_region(self, session):
        store = _make_store(session, "us-west-2")

        response = store.generate_presigned_url(
            "k.txt", PresignedOperation.DOWNLOAD, duration_seconds=3600
        )

        # Scope is <key>/<date>/<region>/s3/aws4_request. A scope naming a
        # region other than the bucket's own makes S3 reject the URL with
        # AuthorizationQueryParametersError.
        credential = _query(response.url)["X-Amz-Credential"][0]
        assert credential.split("/")[2] == "us-west-2"
        assert credential.endswith("/s3/aws4_request")

    def test_max_duration_is_accepted(self, session):
        # 7 days is the SigV4 ceiling and the SDK's documented maximum. The
        # cap is a SigV4-only concept -- SigV2's Expires has no such limit.
        store = _make_store(session, "us-west-2")

        response = store.generate_presigned_url(
            "k.txt", PresignedOperation.DOWNLOAD, duration_seconds=604800
        )

        assert _query(response.url)["X-Amz-Expires"] == ["604800"]

    def test_session_token_rides_in_query(self):
        session = boto3.Session(
            aws_access_key_id="AKIAFAKEFAKEFAKE",
            aws_secret_access_key="fakesecretfakesecretfakesecret",
            aws_session_token="FAKETOKEN123",
        )
        store = _make_store(session, "us-west-2")

        response = store.generate_presigned_url(
            "k.txt", PresignedOperation.DOWNLOAD, duration_seconds=3600
        )

        assert "X-Amz-Security-Token" in _query(response.url)


class TestAwsPresignedKmsHeaderContract:
    """KMS is the case SigV2 could not serve at all."""

    def test_kms_values_are_signed_headers_and_all_surfaced(self, session):
        store = _make_store(session, "us-west-2")

        response = store.generate_presigned_url(
            "k.txt",
            PresignedOperation.UPLOAD,
            duration_seconds=3600,
            content_type="application/json",
            metadata={"team": "blob"},
            tags={"project": "x"},
            kms_key_id="arn:aws:kms:us-west-2:1:key/abc",
            content_disposition="attachment; filename=x.json",
        )

        query = _query(response.url)
        assert query["X-Amz-Algorithm"] == [_SIGV4_ALGORITHM]

        # Every signed header except host must be surfaced to the caller,
        # or replaying the request yields 403 SignatureDoesNotMatch.
        signed = {h for h in query["X-Amz-SignedHeaders"][0].split(";") if h}
        surfaced = {h.lower() for h in response.required_headers}
        assert signed - {"host"} == surfaced

        # The KMS values must be signed headers, not query parameters.
        assert response.required_headers["x-amz-server-side-encryption"] == "aws:kms"
        assert (
            response.required_headers["x-amz-server-side-encryption-aws-kms-key-id"]
            == "arn:aws:kms:us-west-2:1:key/abc"
        )


class TestAwsPresignedCustomEndpoint:
    """A custom endpoint must keep path-style addressing.

    Virtual-host addressing would rewrite the host to
    ``test-bucket.localhost:9000``, which no local S3 emulator resolves.
    This test fails if anyone adds ``s3={"addressing_style": "virtual"}``
    to the client config.
    """

    def test_custom_endpoint_keeps_bucket_in_path(self, session):
        store = _make_store(session, "us-west-2", endpoint_url="http://localhost:9000")

        response = store.generate_presigned_url(
            "k.txt", PresignedOperation.DOWNLOAD, duration_seconds=3600
        )

        parsed = urlparse(response.url)
        assert parsed.netloc == "localhost:9000"
        assert parsed.path.startswith(f"/{_BUCKET}/")
        # Still SigV4 against the emulator.
        assert _query(response.url)["X-Amz-Algorithm"] == [_SIGV4_ALGORITHM]
