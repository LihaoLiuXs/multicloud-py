"""Unit tests for BaseAsyncBlobStore.copy() validation logic.

Tests validate the base driver correctly:
- Validates all required CopyRequest fields
- Generates correlation_id for each copy operation
- Delegates to do_copy after validation
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from multicloud.blob.blob_client.async_blob.async_driver import (
    BaseAsyncBlobStore,
    CopyRequest,
    CopyResponse,
)
from multicloud.common.exceptions import InvalidArgumentException


class FakeAsyncBlobStore(BaseAsyncBlobStore):
    """Minimal concrete implementation for testing base class logic."""

    def __init__(self, bucket: str = "test-bucket"):
        super().__init__(bucket=bucket)
        self.do_copy_called_with = None

    async def do_upload(self, key, payload, *, content_type, content_length, metadata):
        pass

    async def do_download(self, request):
        pass

    async def do_copy(self, request, *, correlation_id):
        self.do_copy_called_with = (request, correlation_id)
        return CopyResponse(
            key=request.destination_key,
            version_id="ver-001",
            e_tag="abc123",
            correlation_id=correlation_id,
        )

    async def do_delete(self, key, *, version_id=None):
        pass

    async def do_delete_objects(self, identifiers):
        from multicloud.blob.blob_client.async_blob.async_driver import (
            BulkDeleteResponse,
        )

        return BulkDeleteResponse()

    async def do_get_tags(self, key):
        return {}

    async def do_set_tags(self, key, tags):
        pass

    async def do_upload_directory(self, local_source_directory, **kwargs):
        raise NotImplementedError

    async def do_download_directory(
        self, prefix, local_destination_directory, **kwargs
    ):
        raise NotImplementedError

    async def do_delete_directory(self, prefix, **kwargs):
        raise NotImplementedError

    async def do_does_object_exist(self, key, *, version_id=None):
        return True

    async def do_get_metadata(self, key, *, version_id=None):
        from multicloud.blob.blob_client.async_blob.async_driver import BlobMetadata

        return BlobMetadata(key=key)

    async def do_does_bucket_exist(self):
        return True

    async def do_list_page(
        self, *, prefix=None, delimiter=None, max_results=1000, pagination_token=None
    ):
        from multicloud.blob.blob_client.async_blob.async_driver import (
            ListBlobsPageResponse,
        )

        return ListBlobsPageResponse()

    async def close(self):
        pass

    @classmethod
    def get_provider_id(cls) -> str:
        return "fake"

    def translate_error(self, exc):
        from multicloud.common.exceptions import UnknownException

        return UnknownException(str(exc))


@pytest.fixture
def store():
    return FakeAsyncBlobStore()


@pytest.fixture
def valid_request():
    return CopyRequest(
        source_bucket="src-bucket",
        source_key="src/key.txt",
        destination_bucket="dest-bucket",
        destination_key="dest/key.txt",
    )


class TestCopyRequestModel:
    """CopyRequest dataclass behavior."""

    def test_create_minimal_request(self):
        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        assert req.source_bucket == "src"
        assert req.source_key == "key"
        assert req.destination_bucket == "dst"
        assert req.destination_key == "dkey"
        assert req.source_version_id is None
        assert req.metadata is None

    def test_create_full_request(self):
        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
            source_version_id="v1",
            metadata={"env": "prod"},
        )
        assert req.source_version_id == "v1"
        assert req.metadata == {"env": "prod"}

    def test_frozen_immutable(self):
        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(Exception):
            req.source_bucket = "other"  # type: ignore

    def test_equality(self):
        r1 = CopyRequest(
            source_bucket="s",
            source_key="k",
            destination_bucket="d",
            destination_key="dk",
        )
        r2 = CopyRequest(
            source_bucket="s",
            source_key="k",
            destination_bucket="d",
            destination_key="dk",
        )
        assert r1 == r2

    def test_inequality_different_fields(self):
        r1 = CopyRequest(
            source_bucket="s",
            source_key="k",
            destination_bucket="d",
            destination_key="dk",
        )
        r2 = CopyRequest(
            source_bucket="s",
            source_key="k2",
            destination_bucket="d",
            destination_key="dk",
        )
        assert r1 != r2


class TestCopyResponseModel:
    """CopyResponse dataclass behavior."""

    def test_create_minimal_response(self):
        resp = CopyResponse(key="dest/key.txt")
        assert resp.key == "dest/key.txt"
        assert resp.version_id is None
        assert resp.e_tag is None
        assert resp.correlation_id is None

    def test_create_full_response(self):
        resp = CopyResponse(
            key="dest/key.txt",
            version_id="ver-001",
            e_tag="abc123",
            correlation_id="uuid-here",
        )
        assert resp.version_id == "ver-001"
        assert resp.e_tag == "abc123"
        assert resp.correlation_id == "uuid-here"

    def test_frozen_immutable(self):
        resp = CopyResponse(key="k")
        with pytest.raises(Exception):
            resp.key = "other"  # type: ignore


class TestCopyValidation:
    """Input validation before delegation to do_copy."""

    @pytest.mark.asyncio
    async def test_empty_source_bucket_raises(self, store):
        req = CopyRequest(
            source_bucket="",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(InvalidArgumentException, match="source_bucket"):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_empty_source_key_raises(self, store):
        req = CopyRequest(
            source_bucket="src",
            source_key="",
            destination_bucket="dst",
            destination_key="dkey",
        )
        with pytest.raises(InvalidArgumentException, match="source_key"):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_empty_destination_bucket_raises(self, store):
        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="",
            destination_key="dkey",
        )
        with pytest.raises(InvalidArgumentException, match="destination_bucket"):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_empty_destination_key_raises(self, store):
        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="",
        )
        with pytest.raises(InvalidArgumentException, match="destination_key"):
            await store.copy(req)

    @pytest.mark.asyncio
    async def test_valid_request_delegates_to_do_copy(self, store, valid_request):
        result = await store.copy(valid_request)

        assert store.do_copy_called_with is not None
        called_request, called_corr_id = store.do_copy_called_with
        assert called_request is valid_request
        assert called_corr_id  # non-empty string

    @pytest.mark.asyncio
    async def test_correlation_id_is_uuid_format(self, store, valid_request):
        result = await store.copy(valid_request)

        _, corr_id = store.do_copy_called_with
        # UUID format: 8-4-4-4-12 hex chars
        import re

        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            corr_id,
        )

    @pytest.mark.asyncio
    async def test_correlation_id_unique_per_call(self, store, valid_request):
        await store.copy(valid_request)
        corr1 = store.do_copy_called_with[1]

        await store.copy(valid_request)
        corr2 = store.do_copy_called_with[1]

        assert corr1 != corr2

    @pytest.mark.asyncio
    async def test_response_contains_correlation_id(self, store, valid_request):
        result = await store.copy(valid_request)
        assert result.correlation_id is not None
        assert result.correlation_id == store.do_copy_called_with[1]


class TestCopyScenarios:
    """Verify various copy scenarios pass validation."""

    @pytest.mark.asyncio
    async def test_same_bucket_copy(self, store):
        """Same-bucket copy (rename/move pattern)."""
        req = CopyRequest(
            source_bucket="my-bucket",
            source_key="old-path/file.txt",
            destination_bucket="my-bucket",
            destination_key="new-path/file.txt",
        )
        result = await store.copy(req)
        assert result.key == "new-path/file.txt"

    @pytest.mark.asyncio
    async def test_cross_bucket_copy(self, store):
        """Cross-bucket copy."""
        req = CopyRequest(
            source_bucket="bucket-a",
            source_key="path/file.txt",
            destination_bucket="bucket-b",
            destination_key="path/file.txt",
        )
        result = await store.copy(req)
        assert result.key == "path/file.txt"

    @pytest.mark.asyncio
    async def test_copy_with_version_id(self, store):
        """Copy specific version."""
        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
            source_version_id="ver-123",
        )
        result = await store.copy(req)
        assert result.key == "dkey"

    @pytest.mark.asyncio
    async def test_copy_with_metadata_override(self, store):
        """Copy with metadata override."""
        req = CopyRequest(
            source_bucket="src",
            source_key="key",
            destination_bucket="dst",
            destination_key="dkey",
            metadata={"env": "staging", "team": "multicloud"},
        )
        result = await store.copy(req)
        assert result.key == "dkey"
        # Verify request with metadata was passed through
        called_request, _ = store.do_copy_called_with
        assert called_request.metadata == {"env": "staging", "team": "multicloud"}
