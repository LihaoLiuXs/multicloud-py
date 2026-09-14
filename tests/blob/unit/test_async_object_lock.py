"""Unit tests for get_object_lock at the driver and client layers.

Tests validate:
- BaseAsyncBlobStore.get_object_lock validates the key before delegating
- the version_id reaches the provider hook unchanged
- a provider that does not implement the hook reports it as unsupported
- AsyncBucketClient forwards the result, including None, unchanged
- AsyncBucketClient translates a native provider error
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from unittest.mock import patch

import pytest
from multicloud.blob.blob_client.async_blob.async_client import AsyncBucketClient
from multicloud.blob.blob_client.async_blob.async_driver import (
    ASYNC_BLOB_REGISTRY,
    BaseAsyncBlobStore,
    BlobNotFoundError,
    ObjectLockInfo,
    ObjectRetentionConfig,
    RetentionMode,
)
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
    ResourceNotFoundException,
    SubstrateSdkException,
    UnknownException,
    UnsupportedOperationException,
)

RETAIN_UNTIL = datetime(2032, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class RecordingLockStore(BaseAsyncBlobStore):
    """Records the arguments the driver forwards to the provider hook."""

    provider_id = "recording-lock"

    def __init__(self, **kwargs):
        super().__init__(bucket=kwargs.get("bucket", "b"))
        self.calls: list[dict] = []
        self.retention_calls: list[dict] = []
        self.hold_calls: list[dict] = []
        self.result: Optional[ObjectLockInfo] = ObjectLockInfo(
            mode=RetentionMode.COMPLIANCE,
            retain_until_date=RETAIN_UNTIL,
            legal_hold=True,
            event_based_hold=False,
        )
        self.raise_native: Optional[Exception] = None

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        if isinstance(exc, SubstrateSdkException):
            return exc
        return UnknownException(str(exc))

    async def do_get_object_lock(
        self, key: str, *, version_id: Optional[str] = None
    ) -> Optional[ObjectLockInfo]:
        self.calls.append({"key": key, "version_id": version_id})
        if self.raise_native is not None:
            raise self.raise_native
        return self.result

    async def do_update_object_retention(
        self,
        key: str,
        retention: ObjectRetentionConfig,
        *,
        version_id: Optional[str] = None,
    ) -> None:
        self.retention_calls.append(
            {"key": key, "retention": retention, "version_id": version_id}
        )
        if self.raise_native is not None:
            raise self.raise_native

    async def do_update_legal_hold(
        self, key: str, enabled: bool, *, version_id: Optional[str] = None
    ) -> None:
        self.hold_calls.append(
            {"key": key, "enabled": enabled, "version_id": version_id}
        )
        if self.raise_native is not None:
            raise self.raise_native

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

    async def do_download_directory(
        self, prefix, local_destination_directory, **kwargs
    ):
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


class UnsupportedLockStore(RecordingLockStore):
    """Falls back to the driver default, which reports the hook as unsupported."""

    provider_id = "unsupported-lock"

    do_get_object_lock = BaseAsyncBlobStore.do_get_object_lock  # type: ignore[assignment]


class UnsupportedRetentionStore(RecordingLockStore):
    """Falls back to the driver default for the retention hook."""

    provider_id = "unsupported-retention"

    do_update_object_retention = (
        BaseAsyncBlobStore.do_update_object_retention  # type: ignore[assignment]
    )


class UnsupportedLegalHoldStore(RecordingLockStore):
    """Falls back to the driver default for the legal-hold hook."""

    provider_id = "unsupported-legal-hold"

    do_update_legal_hold = (
        BaseAsyncBlobStore.do_update_legal_hold  # type: ignore[assignment]
    )


class FlatteningLockStore(RecordingLockStore):
    """A store whose translate_error flattens every exception to one type.

    The client must not route an already-translated exception back through
    translation, so with this store a specific SDK exception still arrives
    intact at the caller.
    """

    provider_id = "flattening-lock"

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        return UnknownException(str(exc))


@pytest.mark.asyncio
class TestDriverGetObjectLock:
    """Tests for BaseAsyncBlobStore.get_object_lock."""

    async def test_empty_key_is_rejected_before_the_hook(self):
        """Validation runs in the template, so the provider is never called."""
        store = RecordingLockStore(bucket="b")

        with pytest.raises(InvalidArgumentException):
            await store.get_object_lock("")

        assert store.calls == []

    async def test_version_id_reaches_the_hook(self):
        """The driver forwards key and version_id verbatim."""
        store = RecordingLockStore(bucket="b")

        await store.get_object_lock("k.txt", version_id="v-9")

        assert store.calls == [{"key": "k.txt", "version_id": "v-9"}]

    async def test_omitted_version_id_forwards_none(self):
        """Omitting version_id forwards None rather than a sentinel."""
        store = RecordingLockStore(bucket="b")

        await store.get_object_lock("k.txt")

        assert store.calls == [{"key": "k.txt", "version_id": None}]

    async def test_hook_result_is_returned_unchanged(self):
        """The driver does not reshape the provider's ObjectLockInfo."""
        store = RecordingLockStore(bucket="b")

        result = await store.get_object_lock("k.txt")

        assert result is store.result
        assert result is not None
        assert result.mode is RetentionMode.COMPLIANCE
        assert result.retain_until_date == RETAIN_UNTIL

    async def test_none_result_is_preserved(self):
        """An unlocked object stays None instead of becoming an empty lock."""
        store = RecordingLockStore(bucket="b")
        store.result = None

        assert await store.get_object_lock("k.txt") is None

    async def test_provider_without_the_hook_reports_unsupported(self):
        """The default hook refuses rather than silently returning None."""
        store = UnsupportedLockStore(bucket="b")

        with pytest.raises(UnsupportedOperationException):
            await store.get_object_lock("k.txt")


@pytest.mark.asyncio
class TestClientGetObjectLock:
    """Tests for AsyncBucketClient.get_object_lock."""

    async def _client(self, store: RecordingLockStore) -> AsyncBucketClient:
        with patch.object(ASYNC_BLOB_REGISTRY, "get", return_value=lambda **kw: store):
            return AsyncBucketClient(provider="recording-lock", bucket="b")

    async def test_forwards_key_and_version_id(self):
        """The client passes both arguments through to the store."""
        store = RecordingLockStore(bucket="b")
        client = await self._client(store)

        result = await client.get_object_lock("k.txt", version_id="v-3")

        assert store.calls == [{"key": "k.txt", "version_id": "v-3"}]
        assert result is not None
        assert result.legal_hold is True
        assert result.event_based_hold is False

    async def test_returns_none_for_unlocked_object(self):
        """None survives the client layer instead of being coerced."""
        store = RecordingLockStore(bucket="b")
        store.result = None
        client = await self._client(store)

        assert await client.get_object_lock("k.txt") is None

    async def test_native_error_is_translated(self):
        """A non-SDK exception is translated, not leaked to the caller."""
        store = RecordingLockStore(bucket="b")
        store.raise_native = RuntimeError("native boom")
        client = await self._client(store)

        with pytest.raises(UnknownException):
            await client.get_object_lock("k.txt")

    async def test_sdk_exception_is_not_rewrapped(self):
        """An already-translated exception keeps its specific type.

        The store used here flattens everything it translates, so the specific
        type only survives if the client skips translation for exceptions that
        are already part of the SDK hierarchy.
        """
        store = FlatteningLockStore(bucket="b")
        store.raise_native = InvalidArgumentException("bad key")
        client = await self._client(store)

        with pytest.raises(InvalidArgumentException):
            await client.get_object_lock("k.txt")


@pytest.mark.asyncio
class TestDriverUpdateObjectRetention:
    """Tests for BaseAsyncBlobStore.update_object_retention."""

    async def test_empty_key_is_rejected_before_the_hook(self):
        store = RecordingLockStore(bucket="b")

        with pytest.raises(InvalidArgumentException):
            await store.update_object_retention(
                "", ObjectRetentionConfig(retain_until_date=RETAIN_UNTIL)
            )

        assert store.retention_calls == []

    async def test_missing_config_is_rejected(self):
        """A null config is refused rather than reaching the provider."""
        store = RecordingLockStore(bucket="b")

        with pytest.raises(InvalidArgumentException):
            await store.update_object_retention("k.txt", None)  # type: ignore[arg-type]

        assert store.retention_calls == []

    async def test_naive_retain_until_is_rejected(self):
        """A timezone-naive instant is ambiguous, so it never reaches a provider."""
        store = RecordingLockStore(bucket="b")
        naive = datetime(2032, 1, 2, 3, 4, 5)

        with pytest.raises(InvalidArgumentException):
            await store.update_object_retention(
                "k.txt", ObjectRetentionConfig(retain_until_date=naive)
            )

        assert store.retention_calls == []

    async def test_config_and_version_id_reach_the_hook(self):
        """The driver forwards the config object and version verbatim."""
        store = RecordingLockStore(bucket="b")
        config = ObjectRetentionConfig(
            retain_until_date=RETAIN_UNTIL,
            mode=RetentionMode.COMPLIANCE,
            bypass_governance=True,
        )

        await store.update_object_retention("k.txt", config, version_id="v-9")

        assert len(store.retention_calls) == 1
        call = store.retention_calls[0]
        assert call["key"] == "k.txt"
        assert call["version_id"] == "v-9"
        assert call["retention"] is config

    async def test_provider_without_the_hook_reports_unsupported(self):
        store = UnsupportedRetentionStore(bucket="b")

        with pytest.raises(UnsupportedOperationException):
            await store.update_object_retention(
                "k.txt", ObjectRetentionConfig(retain_until_date=RETAIN_UNTIL)
            )


@pytest.mark.asyncio
class TestClientUpdateObjectRetention:
    """Tests for AsyncBucketClient.update_object_retention."""

    async def _client(self, store: RecordingLockStore) -> AsyncBucketClient:
        with patch.object(ASYNC_BLOB_REGISTRY, "get", return_value=lambda **kw: store):
            return AsyncBucketClient(provider="recording-lock", bucket="b")

    async def test_forwards_to_the_store(self):
        store = RecordingLockStore(bucket="b")
        client = await self._client(store)
        config = ObjectRetentionConfig(retain_until_date=RETAIN_UNTIL)

        await client.update_object_retention("k.txt", config, version_id="v-4")

        assert store.retention_calls == [
            {"key": "k.txt", "retention": config, "version_id": "v-4"}
        ]

    async def test_native_error_is_translated(self):
        store = RecordingLockStore(bucket="b")
        store.raise_native = RuntimeError("native boom")
        client = await self._client(store)

        with pytest.raises(UnknownException):
            await client.update_object_retention(
                "k.txt", ObjectRetentionConfig(retain_until_date=RETAIN_UNTIL)
            )

    async def test_precondition_failure_keeps_its_type(self):
        """A rules-table refusal must not be flattened by translation."""
        store = FlatteningLockStore(bucket="b")
        store.raise_native = FailedPreconditionException("no current retention")
        client = await self._client(store)

        with pytest.raises(FailedPreconditionException):
            await client.update_object_retention(
                "k.txt", ObjectRetentionConfig(retain_until_date=RETAIN_UNTIL)
            )


@pytest.mark.asyncio
class TestDriverUpdateLegalHold:
    """Tests for BaseAsyncBlobStore.update_legal_hold."""

    async def test_empty_key_is_rejected_before_the_hook(self):
        store = RecordingLockStore(bucket="b")

        with pytest.raises(InvalidArgumentException):
            await store.update_legal_hold("", True)

        assert store.hold_calls == []

    async def test_enable_and_version_id_reach_the_hook(self):
        store = RecordingLockStore(bucket="b")

        await store.update_legal_hold("k.txt", True, version_id="v-2")

        assert store.hold_calls == [
            {"key": "k.txt", "enabled": True, "version_id": "v-2"}
        ]

    async def test_disable_is_forwarded_as_false_not_dropped(self):
        """Releasing a hold is a real request, not an omission."""
        store = RecordingLockStore(bucket="b")

        await store.update_legal_hold("k.txt", False)

        assert store.hold_calls == [
            {"key": "k.txt", "enabled": False, "version_id": None}
        ]

    async def test_provider_without_the_hook_reports_unsupported(self):
        store = UnsupportedLegalHoldStore(bucket="b")

        with pytest.raises(UnsupportedOperationException):
            await store.update_legal_hold("k.txt", True)


@pytest.mark.asyncio
class TestClientUpdateLegalHold:
    """Tests for AsyncBucketClient.update_legal_hold."""

    async def _client(self, store: RecordingLockStore) -> AsyncBucketClient:
        with patch.object(ASYNC_BLOB_REGISTRY, "get", return_value=lambda **kw: store):
            return AsyncBucketClient(provider="recording-lock", bucket="b")

    async def test_forwards_to_the_store(self):
        store = RecordingLockStore(bucket="b")
        client = await self._client(store)

        await client.update_legal_hold("k.txt", True, version_id="v-7")

        assert store.hold_calls == [
            {"key": "k.txt", "enabled": True, "version_id": "v-7"}
        ]

    async def test_native_error_is_translated(self):
        store = RecordingLockStore(bucket="b")
        store.raise_native = RuntimeError("native boom")
        client = await self._client(store)

        with pytest.raises(UnknownException):
            await client.update_legal_hold("k.txt", True)


@pytest.mark.asyncio
class TestMissingBlobIsUniformlyCatchable:
    """The missing-blob error must be catchable the same way on every provider."""

    async def test_blob_not_found_satisfies_resource_not_found(self):
        """A caller's single ResourceNotFoundException handler must catch it.

        Providers differ in whether they surface a missing object as the
        blob-specific error or the generic one, so the blob-specific error is
        a ResourceNotFoundException to keep one handler sufficient.
        """
        with pytest.raises(ResourceNotFoundException):
            raise BlobNotFoundError("gone", key="k.txt")

    async def test_blob_not_found_keeps_its_own_identity(self):
        """Widening the base must not cost callers the specific type."""
        with pytest.raises(BlobNotFoundError) as exc_info:
            raise BlobNotFoundError("gone", key="k.txt")

        assert exc_info.value.key == "k.txt"
