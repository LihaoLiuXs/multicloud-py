"""Unit tests for AwsAsyncBlobStore presigned URL generation.

These tests drive the real aiobotocore SigV4 signer (with throwaway
credentials and no network I/O) so they exercise the actual signed URL,
not a mock of it. They assert the two properties that matter for a
usable presigned URL:

- The returned url is a signed string, not an un-awaited coroutine.
- Every header folded into X-Amz-SignedHeaders is surfaced in
  required_headers, so the caller can reproduce the request. A signed
  header the caller is not told about causes a 403 SignatureDoesNotMatch
  at upload time.
"""

from __future__ import annotations

import inspect
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip(
    "aiobotocore", reason="aiobotocore not installed (optional async-aws dependency)"
)

from aiobotocore.session import get_session
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore
from multicloud.blob.blob_client.async_blob.async_driver import PresignedUrlResult

# us-west-2 is a region where botocore defaults to SigV2 unless the client
# pins SigV4. Using it here means these tests fail if the store ever stops
# pinning SigV4 -- the exact regression that broke KMS-bucket uploads. The
# store must supply the SigV4 config itself; the test does not rig it.
_REGION = "us-west-2"


def _make_store() -> AwsAsyncBlobStore:
    """Build a store backed by a real session with throwaway creds."""
    session = get_session()
    session.set_credentials("AKIAFAKEFAKEFAKE", "fakesecretfakesecretfakesecret", None)
    return AwsAsyncBlobStore(bucket="test-bucket", region=_REGION, _session=session)


def _signed_headers(url: str) -> set[str]:
    query = parse_qs(urlparse(url).query)
    raw = query.get("X-Amz-SignedHeaders", [""])[0]
    return {h for h in raw.split(";") if h}


class TestAwsAsyncPresignedReturnsUsableUrl:
    """The url field must be a signed string, never a coroutine."""

    @pytest.mark.asyncio
    async def test_get_url_is_signed_string(self):
        store = _make_store()
        try:
            result = await store.generate_presigned_url("k.txt", "GET")
        finally:
            await store.close()

        assert isinstance(result, PresignedUrlResult)
        assert isinstance(result.url, str)
        assert not inspect.iscoroutine(result.url)
        assert result.url.startswith("https://")
        assert "X-Amz-Signature=" in result.url

    @pytest.mark.asyncio
    async def test_put_url_is_signed_string(self):
        store = _make_store()
        try:
            result = await store.generate_presigned_url("k.txt", "PUT")
        finally:
            await store.close()

        assert isinstance(result.url, str)
        assert result.url.startswith("https://")
        assert result.http_method == "PUT"


class TestAwsAsyncPresignedRequiredHeaders:
    """Every SigV4-signed header must appear in required_headers."""

    @pytest.mark.asyncio
    async def test_put_surfaces_all_signed_headers(self):
        store = _make_store()
        try:
            result = await store.generate_presigned_url(
                "k.txt",
                "PUT",
                content_type="application/json",
                metadata={"team": "blob", "env": "prod"},
                tags={"project": "x"},
                kms_key_id="arn:aws:kms:us-west-2:1:key/abc",
                content_disposition="attachment; filename=x.json",
            )
        finally:
            await store.close()

        # The contract: caller must send every signed header except host.
        signed = _signed_headers(result.url)
        assert signed, "expected SigV4 signed headers on the URL"
        surfaced = {h.lower() for h in result.required_headers}
        missing = {h for h in signed if h != "host"} - surfaced
        assert not missing, f"signed headers not surfaced to caller: {missing}"

        # Spot-check the specific values the caller must replay.
        assert result.required_headers["Content-Type"] == "application/json"
        assert result.required_headers["x-amz-meta-team"] == "blob"
        assert result.required_headers["x-amz-meta-env"] == "prod"
        assert result.required_headers["x-amz-tagging"] == "project=x"
        assert result.required_headers["x-amz-server-side-encryption"] == "aws:kms"
        assert (
            result.required_headers["x-amz-server-side-encryption-aws-kms-key-id"]
            == "arn:aws:kms:us-west-2:1:key/abc"
        )

    @pytest.mark.asyncio
    async def test_get_needs_no_extra_headers(self):
        store = _make_store()
        try:
            result = await store.generate_presigned_url(
                "k.txt", "GET", content_disposition="attachment; filename=x.txt"
            )
        finally:
            await store.close()

        # Response overrides ride as query params on GET, so no signed
        # request headers beyond host -> caller needs no extra headers.
        assert dict(result.required_headers) == {}
        query = parse_qs(urlparse(result.url).query)
        assert "response-content-disposition" in query


class TestAwsAsyncPresignedVersionId:
    """version_id is valid for GET but must not be sent to put_object."""

    @pytest.mark.asyncio
    async def test_put_with_version_id_does_not_raise(self):
        store = _make_store()
        try:
            # put_object has no VersionId param; forwarding it would raise
            # ParamValidationError. It must be ignored for PUT.
            result = await store.generate_presigned_url(
                "k.txt", "PUT", version_id="v-123"
            )
        finally:
            await store.close()

        assert result.url.startswith("https://")
        assert "v-123" not in result.url

    @pytest.mark.asyncio
    async def test_get_with_version_id_is_in_query(self):
        store = _make_store()
        try:
            result = await store.generate_presigned_url(
                "k.txt", "GET", version_id="v-123"
            )
        finally:
            await store.close()

        query = parse_qs(urlparse(result.url).query)
        assert query.get("versionId") == ["v-123"]


class TestAwsAsyncPresignedUsesSigV4:
    """In us-west-2 the store must still emit SigV4, not the SigV2 default.

    This is the direct regression guard: KMS-encrypted buckets reject SigV2
    (``SignatureDoesNotMatch`` / ``InvalidArgument``). If the store stops
    pinning SigV4, these URLs revert to SigV2 in this region and the asserts
    below fail.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["GET", "PUT"])
    async def test_url_is_sigv4(self, operation):
        store = _make_store()
        try:
            result = await store.generate_presigned_url("k.txt", operation)
        finally:
            await store.close()

        query = parse_qs(urlparse(result.url).query)
        # SigV4 markers present ...
        assert query.get("X-Amz-Algorithm") == ["AWS4-HMAC-SHA256"]
        assert "X-Amz-Credential" in query
        assert "X-Amz-Signature" in query
        # ... and no SigV2 markers.
        assert "AWSAccessKeyId" not in query
        assert "Signature" not in query
        assert "Expires" not in query
