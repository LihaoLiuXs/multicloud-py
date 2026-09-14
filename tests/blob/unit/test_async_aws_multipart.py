"""Unit tests for AwsAsyncBlobStore multipart, with aiobotocore mocked.

Validates the parameters sent to S3 and the mapping of its responses back to
the agnostic models.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip(
    "aiobotocore", reason="aiobotocore not installed (optional async-aws dependency)"
)

from botocore.exceptions import ClientError
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore
from multicloud.blob.blob_client.async_blob.async_driver import (
    MultipartPart,
    MultipartUpload,
    UploadPartResponse,
)
from multicloud.common.exceptions import InvalidArgumentException

BUCKET = "test-bucket"


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {}}, operation
    )


def _make_store() -> tuple[AwsAsyncBlobStore, Any]:
    client = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.create_client.return_value = cm
    store = AwsAsyncBlobStore(bucket=BUCKET, region="us-west-2", _session=session)
    return store, client


@pytest.fixture
def store_and_client():
    return _make_store()


@pytest.fixture
def mpu() -> MultipartUpload:
    return MultipartUpload(bucket=BUCKET, key="obj.bin", upload_id="upload-1")


class TestInitiate:
    @pytest.mark.asyncio
    async def test_sends_bucket_and_key_only_when_nothing_optional_is_set(
        self, store_and_client
    ):
        store, client = store_and_client
        client.create_multipart_upload.return_value = {"UploadId": "u-1"}

        result = await store.do_initiate_multipart_upload(
            "obj.bin", metadata={}, content_type=None, tags={}, correlation_id="cid"
        )

        client.create_multipart_upload.assert_awaited_once_with(
            Bucket=BUCKET, Key="obj.bin"
        )
        assert result.upload_id == "u-1"
        assert result.bucket == BUCKET
        assert result.correlation_id == "cid"

    @pytest.mark.asyncio
    async def test_sends_metadata_and_content_type(self, store_and_client):
        store, client = store_and_client
        client.create_multipart_upload.return_value = {"UploadId": "u-1"}

        await store.do_initiate_multipart_upload(
            "obj.bin",
            metadata={"owner": "team"},
            content_type="text/plain",
            tags={},
            correlation_id="cid",
        )

        kwargs = client.create_multipart_upload.await_args.kwargs
        assert kwargs["Metadata"] == {"owner": "team"}
        assert kwargs["ContentType"] == "text/plain"

    @pytest.mark.asyncio
    async def test_tags_are_url_encoded(self, store_and_client):
        store, client = store_and_client
        client.create_multipart_upload.return_value = {"UploadId": "u-1"}

        await store.do_initiate_multipart_upload(
            "obj.bin",
            metadata={},
            content_type=None,
            tags={"a b": "c&d"},
            correlation_id="cid",
        )

        assert client.create_multipart_upload.await_args.kwargs["Tagging"] == (
            "a%20b=c%26d"
        )


class TestUploadPart:
    @pytest.mark.asyncio
    async def test_sends_part_and_returns_unquoted_etag(self, store_and_client, mpu):
        store, client = store_and_client
        client.upload_part.return_value = {"ETag": '"abc123"'}

        result = await store.do_upload_part(
            mpu, MultipartPart(part_number=2, content=b"hello"), correlation_id="cid"
        )

        client.upload_part.assert_awaited_once_with(
            Bucket=BUCKET,
            Key="obj.bin",
            UploadId="upload-1",
            PartNumber=2,
            Body=b"hello",
            ContentLength=5,
        )
        assert result == UploadPartResponse(
            part_number=2, e_tag="abc123", size_in_bytes=5, correlation_id="cid"
        )

    @pytest.mark.asyncio
    async def test_missing_etag_becomes_empty_string(self, store_and_client, mpu):
        store, client = store_and_client
        client.upload_part.return_value = {}

        result = await store.do_upload_part(
            mpu, MultipartPart(part_number=1, content=b"x"), correlation_id="cid"
        )

        assert result.e_tag == ""

    @pytest.mark.asyncio
    async def test_s3_errors_propagate(self, store_and_client, mpu):
        store, client = store_and_client
        client.upload_part.side_effect = _client_error("NoSuchUpload", "UploadPart")

        with pytest.raises(ClientError):
            await store.do_upload_part(
                mpu, MultipartPart(part_number=1, content=b"x"), correlation_id="cid"
            )


class TestComplete:
    @pytest.mark.asyncio
    async def test_parts_are_sorted_into_ascending_order(self, store_and_client, mpu):
        store, client = store_and_client
        client.complete_multipart_upload.return_value = {"ETag": '"final"'}

        await store.do_complete_multipart_upload(
            mpu,
            [
                UploadPartResponse(part_number=3, e_tag="c", size_in_bytes=1),
                UploadPartResponse(part_number=1, e_tag="a", size_in_bytes=1),
                UploadPartResponse(part_number=2, e_tag="b", size_in_bytes=1),
            ],
            correlation_id="cid",
        )

        sent = client.complete_multipart_upload.await_args.kwargs["MultipartUpload"]
        assert sent["Parts"] == [
            {"PartNumber": 1, "ETag": "a"},
            {"PartNumber": 2, "ETag": "b"},
            {"PartNumber": 3, "ETag": "c"},
        ]

    @pytest.mark.asyncio
    async def test_maps_response_fields(self, store_and_client, mpu):
        store, client = store_and_client
        client.complete_multipart_upload.return_value = {
            "ETag": '"final"',
            "Key": "obj.bin",
            "VersionId": "v-9",
        }

        result = await store.do_complete_multipart_upload(
            mpu,
            [UploadPartResponse(part_number=1, e_tag="a", size_in_bytes=1)],
            correlation_id="cid",
        )

        assert result.e_tag == "final"
        assert result.key == "obj.bin"
        assert result.version_id == "v-9"
        assert result.correlation_id == "cid"


class TestAbort:
    @pytest.mark.asyncio
    async def test_sends_abort(self, store_and_client, mpu):
        store, client = store_and_client

        await store.do_abort_multipart_upload(mpu, correlation_id="cid")

        client.abort_multipart_upload.assert_awaited_once_with(
            Bucket=BUCKET, Key="obj.bin", UploadId="upload-1"
        )

    @pytest.mark.asyncio
    async def test_already_gone_upload_is_not_an_error(self, store_and_client, mpu):
        store, client = store_and_client
        client.abort_multipart_upload.side_effect = _client_error(
            "NoSuchUpload", "AbortMultipartUpload"
        )

        await store.do_abort_multipart_upload(mpu, correlation_id="cid")

    @pytest.mark.asyncio
    async def test_other_errors_still_propagate(self, store_and_client, mpu):
        store, client = store_and_client
        client.abort_multipart_upload.side_effect = _client_error(
            "AccessDenied", "AbortMultipartUpload"
        )

        with pytest.raises(ClientError):
            await store.do_abort_multipart_upload(mpu, correlation_id="cid")


class TestUploadPartStreams:
    """A stream part is read off the event loop, and short reads are refused."""

    @pytest.mark.asyncio
    async def test_stream_is_read_without_blocking_the_loop(
        self, store_and_client, mpu
    ):
        import asyncio

        store, client = store_and_client
        client.upload_part.return_value = {"ETag": '"abc"'}
        calls = []
        real = asyncio.to_thread

        async def spy(fn, *a, **kw):
            calls.append(fn)
            return await real(fn, *a, **kw)

        with patch("asyncio.to_thread", spy):
            await store.do_upload_part(
                mpu,
                MultipartPart(
                    part_number=1, content=io.BytesIO(b"hello"), content_length=5
                ),
                correlation_id="cid",
            )
        assert calls, "stream read was not offloaded"
        assert client.upload_part.await_args.kwargs["Body"] == b"hello"

    @pytest.mark.asyncio
    async def test_an_exhausted_stream_is_refused_rather_than_truncating(
        self, store_and_client, mpu
    ):
        """A retry re-reading a consumed stream would otherwise upload short
        bytes while reporting the declared size."""
        store, client = store_and_client
        spent = io.BytesIO(b"hello")
        spent.read()

        with pytest.raises(InvalidArgumentException, match="cannot be"):
            await store.do_upload_part(
                mpu,
                MultipartPart(part_number=1, content=spent, content_length=5),
                correlation_id="cid",
            )
        client.upload_part.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_size_reported_is_what_was_sent(self, store_and_client, mpu):
        store, client = store_and_client
        client.upload_part.return_value = {"ETag": '"abc"'}

        result = await store.do_upload_part(
            mpu, MultipartPart(part_number=1, content=b"1234567"), correlation_id="cid"
        )

        assert result.size_in_bytes == 7
        assert client.upload_part.await_args.kwargs["ContentLength"] == 7


class TestPartNumberCeiling:
    """S3's own ceiling is left to S3.

    It is a service-side limit, so duplicating it here would couple the SDK to
    a number AWS controls and reject requests that had become valid. S3 answers
    an out-of-range part number with InvalidArgument, which translates to the
    same exception a local check would have raised.
    """

    @pytest.mark.asyncio
    async def test_a_high_part_number_is_forwarded_rather_than_pre_empted(
        self, store_and_client, mpu
    ):
        store, client = store_and_client
        client.upload_part.return_value = {"ETag": '"abc"'}

        await store.do_upload_part(
            mpu, MultipartPart(part_number=10001, content=b"x"), correlation_id="cid"
        )

        assert client.upload_part.await_args.kwargs["PartNumber"] == 10001

    @pytest.mark.asyncio
    async def test_the_service_rejection_translates_to_invalid_argument(
        self, store_and_client, mpu
    ):
        store, client = store_and_client
        client.upload_part.side_effect = _client_error("InvalidArgument", "UploadPart")

        with pytest.raises(ClientError):
            await store.do_upload_part(
                mpu,
                MultipartPart(part_number=10001, content=b"x"),
                correlation_id="cid",
            )
        assert isinstance(
            store.translate_error(_client_error("InvalidArgument", "UploadPart")),
            InvalidArgumentException,
        )
