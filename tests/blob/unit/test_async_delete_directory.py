"""Unit tests for the driver layer of async delete_directory.

Covers:
- BaseAsyncBlobStore.delete_directory validation and delegation
- BaseAsyncBlobStore.run_delete_tasks orchestration: concurrency bounding,
  lazy consumption, drain-to-exhaustion, failure aggregation and capping
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, List, Optional

import pytest
from multicloud.blob.blob_client.async_blob.async_driver import (
    MAX_REPORTED_DELETE_FAILURES,
    BaseAsyncBlobStore,
    BlobAsyncError,
    BlobMetadata,
    ListBlobsPageResponse,
)
from multicloud.common.exceptions import InvalidArgumentException


class FakeDeleteDirectoryStore(BaseAsyncBlobStore):
    """Concrete store recording delete_directory delegation."""

    def __init__(self, **kwargs):
        super().__init__(kwargs.pop("bucket", "test-bucket"), **kwargs)
        self.delete_directory_calls: List[dict] = []

    def get_provider_id(self) -> str:
        return "fake"

    def translate_error(self, exc: Exception):
        return BlobAsyncError(str(exc), cause=exc)

    async def do_upload(
        self, key, payload, *, content_type=None, content_length=None, metadata=None
    ):
        raise NotImplementedError

    async def do_download(self, request):
        raise NotImplementedError

    async def do_copy(self, request):
        raise NotImplementedError

    async def do_delete(self, key, *, version_id=None):
        raise NotImplementedError

    async def do_delete_objects(self, identifiers):
        raise NotImplementedError

    async def do_get_tags(self, key):
        return {}

    async def do_set_tags(self, key, tags):
        raise NotImplementedError

    async def do_upload_directory(self, local_source_directory, **kwargs):
        raise NotImplementedError

    async def do_download_directory(
        self, prefix, local_destination_directory, **kwargs
    ):
        raise NotImplementedError

    async def do_delete_directory(
        self, prefix: Optional[str], *, max_concurrency: int = 10
    ) -> None:
        self.delete_directory_calls.append(
            {"prefix": prefix, "max_concurrency": max_concurrency}
        )

    async def do_does_object_exist(self, key, *, version_id=None):
        return True

    async def do_does_bucket_exist(self):
        return True

    async def do_get_metadata(self, key, *, version_id=None):
        return BlobMetadata(key=key)

    async def do_list_page(
        self, *, prefix=None, delimiter=None, max_results=1000, pagination_token=None
    ):
        return ListBlobsPageResponse()

    async def close(self):
        pass


async def aiter_from(items: List) -> AsyncIterator:
    """Yield items from a list as an async iterator."""
    for item in items:
        yield item


@pytest.fixture
def store():
    return FakeDeleteDirectoryStore(bucket="test-bucket")


class TestDeleteDirectoryValidation:
    """Driver-level validation for delete_directory."""

    @pytest.mark.asyncio
    async def test_delegates_prefix_and_concurrency(self, store):
        await store.delete_directory("logs/", max_concurrency=4)

        assert store.delete_directory_calls == [
            {"prefix": "logs/", "max_concurrency": 4}
        ]

    @pytest.mark.asyncio
    async def test_defaults_to_shared_concurrency_constant(self, store):
        await store.delete_directory("logs/")

        assert store.delete_directory_calls[0]["max_concurrency"] == 10

    @pytest.mark.asyncio
    async def test_empty_prefix_is_allowed_and_passed_through(self, store):
        """An empty prefix targets the whole bucket and must not be rejected."""
        await store.delete_directory("")

        assert store.delete_directory_calls == [{"prefix": "", "max_concurrency": 10}]

    @pytest.mark.asyncio
    async def test_none_prefix_is_allowed_and_passed_through(self, store):
        await store.delete_directory(None)

        assert store.delete_directory_calls == [{"prefix": None, "max_concurrency": 10}]

    @pytest.mark.asyncio
    async def test_prefix_is_not_normalized(self, store):
        """Unlike download_directory, no trailing slash is appended."""
        await store.delete_directory("logs")

        assert store.delete_directory_calls[0]["prefix"] == "logs"

    @pytest.mark.asyncio
    async def test_zero_concurrency_rejected(self, store):
        with pytest.raises(InvalidArgumentException, match="max_concurrency must be"):
            await store.delete_directory("logs/", max_concurrency=0)

        assert store.delete_directory_calls == []

    @pytest.mark.asyncio
    async def test_negative_concurrency_rejected(self, store):
        with pytest.raises(InvalidArgumentException, match="max_concurrency must be"):
            await store.delete_directory("logs/", max_concurrency=-1)

        assert store.delete_directory_calls == []


class TestRunDeleteTasks:
    """Shared orchestration helper used by every provider."""

    @pytest.mark.asyncio
    async def test_every_unit_is_processed(self):
        processed: List[str] = []

        async def delete_fn(unit: str) -> List[str]:
            processed.append(unit)
            return []

        await BaseAsyncBlobStore.run_delete_tasks(
            aiter_from(["a", "b", "c"]), 2, delete_fn
        )

        assert sorted(processed) == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_empty_iterator_is_a_no_op(self):
        async def delete_fn(unit: str) -> List[str]:
            raise AssertionError("delete_fn must not be called")

        await BaseAsyncBlobStore.run_delete_tasks(aiter_from([]), 4, delete_fn)

    @pytest.mark.asyncio
    async def test_concurrency_is_bounded_and_saturated(self):
        """Peak concurrency must reach the limit exactly -- never exceed it,
        and never fall short (which a sequential implementation would)."""
        in_flight = 0
        peak = 0

        async def delete_fn(unit: int) -> List[str]:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            return []

        await BaseAsyncBlobStore.run_delete_tasks(
            aiter_from(list(range(20))), 3, delete_fn
        )

        assert peak == 3

    @pytest.mark.asyncio
    async def test_a_slow_unit_does_not_block_later_units(self):
        """Workers must keep pulling while a peer is still in flight.

        The first unit only completes once the last unit has run, so this
        deadlocks under any implementation that waits for a whole group of
        in-flight units before pulling the next one.
        """
        released = asyncio.Event()

        async def delete_fn(unit: int) -> List[str]:
            if unit == 0:
                await released.wait()
            elif unit == 3:
                released.set()
            return []

        await asyncio.wait_for(
            BaseAsyncBlobStore.run_delete_tasks(aiter_from([0, 1, 2, 3]), 2, delete_fn),
            timeout=5,
        )

    @pytest.mark.asyncio
    async def test_units_are_consumed_lazily(self):
        """The iterator must not be drained before work starts.

        Guards the memory property: a directory of any size is processed
        without materializing all of its keys first.
        """
        produced: List[int] = []
        observed_at_first_delete: List[int] = []

        async def units() -> AsyncIterator[int]:
            for i in range(10):
                produced.append(i)
                yield i

        async def delete_fn(unit: int) -> List[str]:
            if not observed_at_first_delete:
                observed_at_first_delete.append(len(produced))
            return []

        await BaseAsyncBlobStore.run_delete_tasks(units(), 2, delete_fn)

        # Work starts long before the listing is exhausted, and no more than
        # max_concurrency units are ever outstanding.
        assert observed_at_first_delete[0] <= 2
        assert len(produced) == 10

    @pytest.mark.asyncio
    async def test_listing_is_drained_before_failure_is_raised(self):
        """A failure in an early group must not abandon later units.

        This is the regression guard for reporting an error while pagination
        is still in progress.
        """
        processed: List[int] = []

        async def delete_fn(unit: int) -> List[str]:
            processed.append(unit)
            return ["boom"] if unit == 0 else []

        with pytest.raises(BlobAsyncError):
            await BaseAsyncBlobStore.run_delete_tasks(
                aiter_from(list(range(6))), 2, delete_fn
            )

        assert sorted(processed) == [0, 1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_failures_are_aggregated_into_one_error(self):
        async def delete_fn(unit: str) -> List[str]:
            return [f"{unit}: AccessDenied"]

        with pytest.raises(BlobAsyncError) as exc_info:
            await BaseAsyncBlobStore.run_delete_tasks(
                aiter_from(["x", "y"]), 4, delete_fn
            )

        message = str(exc_info.value)
        assert "failed to delete 2 object(s)" in message
        assert "x: AccessDenied" in message
        assert "y: AccessDenied" in message

    @pytest.mark.asyncio
    async def test_exception_from_delete_fn_becomes_a_failure(self):
        async def delete_fn(unit: str) -> List[str]:
            raise RuntimeError("network down")

        with pytest.raises(BlobAsyncError) as exc_info:
            await BaseAsyncBlobStore.run_delete_tasks(
                aiter_from(["only"]), 4, delete_fn
            )

        assert "network down" in str(exc_info.value)
        assert isinstance(exc_info.value.cause, RuntimeError)

    @pytest.mark.asyncio
    async def test_only_the_first_exception_is_kept_as_cause(self):
        """Later exceptions still appear in the message but do not replace cause."""

        async def delete_fn(unit: str) -> List[str]:
            raise RuntimeError(f"failure on {unit}")

        with pytest.raises(BlobAsyncError) as exc_info:
            await BaseAsyncBlobStore.run_delete_tasks(
                aiter_from(["first", "second"]), 4, delete_fn
            )

        message = str(exc_info.value)
        assert "failed to delete 2 object(s)" in message
        assert "failure on first" in message
        assert "failure on second" in message
        assert str(exc_info.value.cause) == "failure on first"

    @pytest.mark.asyncio
    async def test_reported_failures_are_capped(self):
        total = MAX_REPORTED_DELETE_FAILURES + 5

        async def delete_fn(unit: int) -> List[str]:
            return [f"key-{unit}: AccessDenied"]

        with pytest.raises(BlobAsyncError) as exc_info:
            await BaseAsyncBlobStore.run_delete_tasks(
                aiter_from(list(range(total))), 8, delete_fn
            )

        message = str(exc_info.value)
        assert f"failed to delete {total} object(s)" in message
        assert "and 5 more" in message

    @pytest.mark.asyncio
    async def test_total_is_exact_while_quoted_details_stay_bounded(self):
        """A wholly-undeletable prefix must not grow detail storage per key.

        The reported total stays exact, but the number of quoted descriptions
        is capped, so the message cannot grow with object count.
        """
        total = MAX_REPORTED_DELETE_FAILURES * 20

        async def delete_fn(unit: int) -> List[str]:
            return [f"key-{unit}: AccessDenied"]

        with pytest.raises(BlobAsyncError) as exc_info:
            await BaseAsyncBlobStore.run_delete_tasks(
                aiter_from(list(range(total))), 16, delete_fn
            )

        message = str(exc_info.value)
        assert f"failed to delete {total} object(s)" in message
        assert f"and {total - MAX_REPORTED_DELETE_FAILURES} more" in message
        assert message.count("AccessDenied") == MAX_REPORTED_DELETE_FAILURES

    @pytest.mark.asyncio
    async def test_listing_failure_propagates_untouched(self):
        """A listing error is a whole-operation failure, not a per-key one.

        It must surface as itself so the client layer can translate it, rather
        than being folded into the per-object failure aggregate.
        """
        attempted: List[int] = []

        async def units() -> AsyncIterator[int]:
            yield 0
            yield 1
            raise RuntimeError("listing blew up on page 2")

        async def delete_fn(unit: int) -> List[str]:
            attempted.append(unit)
            return []

        with pytest.raises(RuntimeError, match="listing blew up"):
            await BaseAsyncBlobStore.run_delete_tasks(units(), 2, delete_fn)

        # Units yielded before the failure were still attempted.
        assert sorted(attempted) == [0, 1]

    @pytest.mark.asyncio
    async def test_listing_failure_takes_precedence_over_delete_failures(self):
        async def units() -> AsyncIterator[int]:
            yield 0
            raise RuntimeError("listing died")

        async def delete_fn(unit: int) -> List[str]:
            return ["some key: AccessDenied"]

        with pytest.raises(RuntimeError, match="listing died"):
            await BaseAsyncBlobStore.run_delete_tasks(units(), 2, delete_fn)

    @pytest.mark.asyncio
    async def test_operation_name_prefixes_the_error(self):
        async def delete_fn(unit: str) -> List[str]:
            return ["nope"]

        with pytest.raises(BlobAsyncError, match="custom_op: failed to delete"):
            await BaseAsyncBlobStore.run_delete_tasks(
                aiter_from(["a"]), 2, delete_fn, operation="custom_op"
            )
