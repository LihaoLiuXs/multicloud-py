"""Unit tests for async list_blob_versions at the driver and client layers.

Tests validate:
- the iterator is obtained without awaiting, and the key is validated then
- a provider that does not support it refuses immediately, not on first use
- nothing is requested until the iterator is consumed
- the client forwards the key and translates a failure to obtain the iterator
"""

from __future__ import annotations

import inspect
from typing import AsyncIterator, Optional
from unittest.mock import patch

import pytest
from multicloud.blob.blob_client.async_blob.async_client import AsyncBucketClient
from multicloud.blob.blob_client.async_blob.async_driver import (
    ASYNC_BLOB_REGISTRY,
    BaseAsyncBlobStore,
    BlobInfo,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
    UnknownException,
    UnsupportedOperationException,
)

VERSIONS = [
    BlobInfo(key="k.txt", version_id="v-1", size=11, e_tag="etag-one"),
    BlobInfo(key="k.txt", version_id="v-2", size=22, e_tag="etag-two"),
    BlobInfo(key="k.txt", version_id="v-3", size=33, e_tag="etag-three"),
]


class RecordingVersionStore(BaseAsyncBlobStore):
    """Records the key, and tracks when the iterator is actually consumed."""

    provider_id = "recording-versions"

    def __init__(self, **kwargs):
        super().__init__(bucket=kwargs.get("bucket", "b"))
        self.calls: list[str] = []
        self.yielded = 0
        self.raise_on_call: Optional[Exception] = None

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        if isinstance(exc, SubstrateSdkException):
            return exc
        return UnknownException(str(exc))

    def do_list_blob_versions(self, key: str) -> AsyncIterator[BlobInfo]:
        self.calls.append(key)
        if self.raise_on_call is not None:
            raise self.raise_on_call

        async def _iter() -> AsyncIterator[BlobInfo]:
            for info in VERSIONS:
                self.yielded += 1
                yield info

        return _iter()

    async def do_upload(self, key, payload, **kwargs):
        raise NotImplementedError

    async def do_download(self, request):
        raise NotImplementedError

    async def do_copy(self, request, *, correlation_id):
        raise NotImplementedError

    async def do_delete(self, key, *, version_id=None):
        raise NotImplementedError

    async def do_delete_objects(self, identifiers):
        raise NotImplementedError

    async def do_get_tags(self, key):
        raise NotImplementedError

    async def do_set_tags(self, key, tags):
        raise NotImplementedError

    async def do_upload_directory(self, local_source_directory, **kwargs):
        raise NotImplementedError

    async def do_download_directory(self, prefix, local_destination, **kwargs):
        raise NotImplementedError

    async def do_delete_directory(self, prefix, **kwargs):
        raise NotImplementedError

    async def do_get_metadata(self, key, *, version_id=None):
        raise NotImplementedError

    async def do_list_page(self, *, prefix, delimiter, max_results, pagination_token):
        raise NotImplementedError

    async def do_does_object_exist(self, key, *, version_id):
        raise NotImplementedError

    async def do_does_bucket_exist(self):
        raise NotImplementedError

    async def close(self) -> None:
        return None


class UnsupportedVersionStore(RecordingVersionStore):
    """Falls back to the driver default for the version-listing hook."""

    provider_id = "unsupported-versions"

    do_list_blob_versions = (
        BaseAsyncBlobStore.do_list_blob_versions  # type: ignore[assignment]
    )


@pytest.mark.asyncio
class TestDriverListBlobVersions:
    """Tests for BaseAsyncBlobStore.list_blob_versions."""

    async def test_is_not_a_coroutine_so_no_await_is_needed(self):
        """The iterator is returned directly, matching the sync shape."""
        assert not inspect.iscoroutinefunction(BaseAsyncBlobStore.list_blob_versions)

        store = RecordingVersionStore(bucket="b")
        listed = [info async for info in store.list_blob_versions("k.txt")]

        assert [i.version_id for i in listed] == ["v-1", "v-2", "v-3"]

    async def test_empty_key_is_rejected_without_iterating(self):
        store = RecordingVersionStore(bucket="b")

        with pytest.raises(InvalidArgumentException):
            store.list_blob_versions("")

        assert store.calls == []

    async def test_the_key_reaches_the_hook(self):
        store = RecordingVersionStore(bucket="b")

        store.list_blob_versions("some/deep/key.txt")

        assert store.calls == ["some/deep/key.txt"]

    async def test_nothing_is_yielded_until_the_iterator_is_consumed(self):
        """Obtaining the iterator must not fetch anything."""
        store = RecordingVersionStore(bucket="b")

        iterator = store.list_blob_versions("k.txt")
        assert store.yielded == 0

        first = await iterator.__anext__()
        assert first.version_id == "v-1"
        assert store.yielded == 1, "the whole listing was materialised up front"

        await iterator.aclose()

    async def test_unsupported_provider_refuses_immediately(self):
        """The refusal must not be deferred to the first iteration.

        A hook written as an async generator would return an iterator and only
        raise once the caller started looping, which is the wrong place.
        """
        store = UnsupportedVersionStore(bucket="b")

        with pytest.raises(UnsupportedOperationException):
            store.list_blob_versions("k.txt")

    async def test_every_field_survives_the_driver(self):
        """Values are not transposed between versions on the way through."""
        store = RecordingVersionStore(bucket="b")

        listed = [info async for info in store.list_blob_versions("k.txt")]

        assert [(i.version_id, i.size, i.e_tag) for i in listed] == [
            ("v-1", 11, "etag-one"),
            ("v-2", 22, "etag-two"),
            ("v-3", 33, "etag-three"),
        ]


@pytest.mark.asyncio
class TestClientListBlobVersions:
    """Tests for AsyncBucketClient.list_blob_versions."""

    async def _client(self, store: RecordingVersionStore) -> AsyncBucketClient:
        with patch.object(ASYNC_BLOB_REGISTRY, "get", return_value=lambda **kw: store):
            return AsyncBucketClient(provider="recording-versions", bucket="b")

    async def test_is_not_a_coroutine_on_the_client_either(self):
        assert not inspect.iscoroutinefunction(AsyncBucketClient.list_blob_versions)

    async def test_forwards_the_key_and_yields_the_versions(self):
        store = RecordingVersionStore(bucket="b")
        client = await self._client(store)

        listed = [info async for info in client.list_blob_versions("k.txt")]

        assert store.calls == ["k.txt"]
        assert [i.version_id for i in listed] == ["v-1", "v-2", "v-3"]
        assert [i.size for i in listed] == [11, 22, 33]

    async def test_empty_key_is_rejected(self):
        store = RecordingVersionStore(bucket="b")
        client = await self._client(store)

        with pytest.raises(InvalidArgumentException):
            client.list_blob_versions("")

    async def test_native_error_obtaining_the_iterator_is_translated(self):
        store = RecordingVersionStore(bucket="b")
        store.raise_on_call = RuntimeError("native boom")
        client = await self._client(store)

        with pytest.raises(UnknownException):
            client.list_blob_versions("k.txt")

    async def test_unsupported_keeps_its_type(self):
        store = UnsupportedVersionStore(bucket="b")
        client = await self._client(store)

        with pytest.raises(UnsupportedOperationException):
            client.list_blob_versions("k.txt")
