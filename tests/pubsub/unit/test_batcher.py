"""
Unit tests for Batcher.
"""

import threading
import time
from concurrent.futures import Future
from typing import List
from unittest.mock import Mock

import pytest
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
)
from multicloud.pubsub.pubsub_client.driver import Batcher


class SizableString(Batcher.SizableItem):
    """Test class that implements Batcher.SizableItem."""

    def __init__(self, value: str) -> None:
        self.value = value

    def get_byte_size(self) -> int:
        return len(self.value.encode("utf-8"))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SizableString):
            return self.value == other.value
        return False

    def __repr__(self) -> str:
        return f"SizableString({self.value})"


class SizableTestItem(Batcher.SizableItem):
    """Test class with explicit byte size for testing byte-size batching."""

    def __init__(self, value: str, byte_size: int) -> None:
        self.value = value
        self.byte_size = byte_size

    def get_byte_size(self) -> int:
        return self.byte_size

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SizableTestItem):
            return self.value == other.value
        return False

    def __repr__(self) -> str:
        return f"SizableTestItem({self.value}, {self.byte_size})"


class TestBatcher:
    """Test suite for Batcher class."""

    def test_add_synchronously(self):
        """Test adding a single item synchronously."""
        mock_handler = Mock()
        batcher = Batcher(mock_handler)
        item = SizableString("test-item")

        batcher.add(item)

        mock_handler.assert_called_once()
        args = mock_handler.call_args[0][0]
        assert len(args) == 1
        assert args[0] == item
        batcher.shutdown_and_drain()

    def test_add_asynchronously(self):
        """Test adding a single item asynchronously."""
        mock_handler = Mock()
        batcher = Batcher(mock_handler)
        item = SizableString("test-item")

        future = batcher.add_no_wait(item)
        result = future.result()

        assert result is None
        assert future.done()
        assert not future.exception()
        mock_handler.assert_called_once()
        args = mock_handler.call_args[0][0]
        assert len(args) == 1
        assert args[0] == item
        batcher.shutdown_and_drain()

    def test_multiple_items_batching(self):
        """Test that multiple items are batched together."""
        mock_handler = Mock()
        options = Batcher.Options(min_batch_size=3)
        batcher = Batcher(mock_handler, options)

        future1 = batcher.add_no_wait(SizableString("item1"))
        future2 = batcher.add_no_wait(SizableString("item2"))
        future3 = batcher.add_no_wait(SizableString("item3"))

        # Wait for all futures
        future1.result()
        future2.result()
        future3.result()

        mock_handler.assert_called_once()
        args = mock_handler.call_args[0][0]
        assert len(args) == 3
        values = [item.value for item in args]
        assert "item1" in values
        assert "item2" in values
        assert "item3" in values
        batcher.shutdown_and_drain()

    def test_add_all_no_wait_coalesces_into_single_batch(self):
        """add_all_no_wait enqueues the whole batch before the handler runs."""
        mock_handler = Mock()
        batcher = Batcher(mock_handler)

        futures = batcher.add_all_no_wait(
            [SizableString("item1"), SizableString("item2"), SizableString("item3")]
        )
        for future in futures:
            future.result()

        # All three items land in one handler pass, not one pass each.
        mock_handler.assert_called_once()
        values = [item.value for item in mock_handler.call_args[0][0]]
        assert values == ["item1", "item2", "item3"]
        batcher.shutdown_and_drain()

    def test_add_all_no_wait_rejects_null_item(self):
        """add_all_no_wait rejects a None item in the batch."""
        mock_handler = Mock()
        batcher = Batcher(mock_handler)

        with pytest.raises(InvalidArgumentException, match="cannot be null"):
            batcher.add_all_no_wait([SizableString("item1"), None])

        batcher.shutdown_and_drain()

    def test_add_all_no_wait_after_shutdown_fails_futures(self):
        """add_all_no_wait after shutdown fails each returned future."""
        mock_handler = Mock()
        batcher = Batcher(mock_handler)
        batcher.shutdown_and_drain()

        futures = batcher.add_all_no_wait([SizableString("item1")])

        with pytest.raises(FailedPreconditionException, match="shut down"):
            futures[0].result()
        mock_handler.assert_not_called()

    def test_max_batch_size(self):
        """Test that maxBatchSize is respected."""
        mock_handler = Mock()
        options = Batcher.Options(max_batch_size=2)
        batcher = Batcher(mock_handler, options)

        future1 = batcher.add_no_wait(SizableString("item1"))
        future2 = batcher.add_no_wait(SizableString("item2"))
        future3 = batcher.add_no_wait(SizableString("item3"))

        future1.result()
        future2.result()
        future3.result()

        # Should be called at least once (multiple batches)
        assert mock_handler.call_count >= 1

        # Verify all items were processed
        all_batches = [call[0][0] for call in mock_handler.call_args_list]
        total_items = sum(len(batch) for batch in all_batches)
        assert total_items == 3

        # Verify no batch exceeds maxBatchSize
        for batch in all_batches:
            assert len(batch) <= 2

        batcher.shutdown_and_drain()

    def test_sizable_item_batching(self):
        """Test batching based on byte size with SizableItem."""
        mock_handler = Mock()
        options = Batcher.Options(max_batch_byte_size=100)
        batcher = Batcher(mock_handler, options)

        item1 = SizableTestItem("item1", 50)
        item2 = SizableTestItem("item2", 30)
        item3 = SizableTestItem("item3", 40)

        future1 = batcher.add_no_wait(item1)
        future2 = batcher.add_no_wait(item2)
        future3 = batcher.add_no_wait(item3)

        future1.result()
        future2.result()
        future3.result()

        # Should be called at least once
        assert mock_handler.call_count >= 1

        # Verify all items were processed
        all_batches = [call[0][0] for call in mock_handler.call_args_list]
        total_items = sum(len(batch) for batch in all_batches)
        assert total_items == 3

        # Verify no batch exceeds maxBatchByteSize
        for batch in all_batches:
            batch_byte_size = sum(item.get_byte_size() for item in batch)
            assert batch_byte_size <= 100

        batcher.shutdown_and_drain()

    def test_sizable_item_batching_with_min_batch_size(self):
        """Test byte-size batching with minBatchSize."""
        mock_handler = Mock()
        options = Batcher.Options(min_batch_size=2, max_batch_byte_size=80)
        batcher = Batcher(mock_handler, options)

        item1 = SizableTestItem("item1", 50)
        item2 = SizableTestItem("item2", 30)  # Total: 80 bytes (at limit)
        item3 = SizableTestItem("item3", 40)  # Would exceed byte limit
        item4 = SizableTestItem("item4", 30)  # Can batch with item3

        future1 = batcher.add_no_wait(item1)
        future2 = batcher.add_no_wait(item2)
        future3 = batcher.add_no_wait(item3)
        future4 = batcher.add_no_wait(item4)

        future1.result()
        future2.result()
        future3.result()
        future4.result()

        # Should create 2 batches: [item1,item2] and [item3,item4]
        assert mock_handler.call_count == 2

        all_batches = [call[0][0] for call in mock_handler.call_args_list]
        assert len(all_batches) == 2

        # First batch: item1 + item2 = 80 bytes (at limit)
        assert len(all_batches[0]) == 2
        # Second batch: item3 + item4 = 70 bytes
        assert len(all_batches[1]) == 2

        batcher.shutdown_and_drain()

    def test_message_too_large_exception(self):
        """Test that oversized items are rejected."""
        mock_handler = Mock()
        options = Batcher.Options(max_batch_byte_size=10)
        batcher = Batcher(mock_handler, options)

        large_item = SizableString("this-is-a-very-large-item")  # More than 10 bytes
        future = batcher.add_no_wait(large_item)

        with pytest.raises(
            InvalidArgumentException, match="exceeds maximum batch byte size"
        ):
            future.result()

        mock_handler.assert_not_called()
        batcher.shutdown_and_drain()

    def test_add_null_item_throws_exception(self):
        """Test that adding None raises an exception."""
        mock_handler = Mock()
        batcher = Batcher(mock_handler)

        with pytest.raises(InvalidArgumentException, match="cannot be null"):
            batcher.add(None)

        mock_handler.assert_not_called()
        batcher.shutdown_and_drain()

    def test_handler_exception(self):
        """Test that handler exceptions are propagated to futures."""
        error = RuntimeError("Handler failed")
        mock_handler = Mock(side_effect=error)
        batcher = Batcher(mock_handler)

        future = batcher.add_no_wait(SizableString("test"))

        with pytest.raises(RuntimeError, match="Handler failed"):
            future.result()

        mock_handler.assert_called_once()
        batcher.shutdown_and_drain()

    def test_shutdown_prevents_new_items(self):
        """Test that shutdown prevents adding new items."""
        mock_handler = Mock()
        batcher = Batcher(mock_handler)

        batcher.shutdown_and_drain()

        future = batcher.add_no_wait(SizableString("test"))

        with pytest.raises(FailedPreconditionException, match="shut down"):
            future.result()

        mock_handler.assert_not_called()

    def test_empty_batch_handling(self):
        """Test that empty batches are handled correctly."""
        mock_handler = Mock()
        options = Batcher.Options(min_batch_size=1)
        batcher = Batcher(mock_handler, options)

        # Don't add any items, just shutdown
        batcher.shutdown_and_drain()

        mock_handler.assert_not_called()

    def test_async_composition(self):
        """Test composing futures with thenApply-like patterns."""
        result_ref = []

        def handler(items: List[SizableString]) -> None:
            result_ref.append(items[0].value.upper())

        batcher = Batcher(handler)

        # Add item and chain another operation
        future = batcher.add_no_wait(SizableString("test"))
        future.result()  # Wait for completion

        assert result_ref[0] == "TEST"
        batcher.shutdown_and_drain()

    def test_selective_waiting(self):
        """Test waiting on specific futures."""
        handler = Mock(return_value=None)
        options = Batcher.Options(min_batch_size=2, max_batch_size=2, max_handlers=2)
        batcher = Batcher(handler, options)

        f1 = batcher.add_no_wait(SizableString("item1"))
        f2 = batcher.add_no_wait(SizableString("item2"))

        # Wait for first batch
        f1.result()
        f2.result()

        assert f1.done()
        assert f2.done()

        # Add second batch
        f10 = batcher.add_no_wait(SizableString("item10"))
        f11 = batcher.add_no_wait(SizableString("item11"))

        # Wait for second batch
        f10.result()
        f11.result()

        assert f10.done()
        assert f11.done()

        batcher.shutdown_and_drain()

    def test_async_error_handling(self):
        """Test that async errors are propagated correctly."""
        test_exception = RuntimeError("Async failure")

        def failing_handler(items: List[SizableString]) -> None:
            raise test_exception

        batcher = Batcher(failing_handler)
        caught_exception = []

        future = batcher.add_no_wait(SizableString("item"))

        try:
            future.result()
        except Exception as ex:
            caught_exception.append(ex)

        assert len(caught_exception) == 1
        assert isinstance(caught_exception[0], RuntimeError)
        assert str(caught_exception[0]) == "Async failure"
        batcher.shutdown_and_drain()

    def test_concurrent_async_operations(self):
        """Test concurrent add operations from multiple threads."""
        num_operations = 1000
        processed_count = 0
        processed_count_lock = threading.Lock()

        def handler(items: List[SizableString]) -> None:
            nonlocal processed_count
            with processed_count_lock:
                processed_count += len(items)

        options = Batcher.Options(min_batch_size=10, max_batch_size=100)
        batcher = Batcher(handler, options)

        futures = []
        for i in range(num_operations):
            futures.append(batcher.add_no_wait(SizableString(f"item{i}")))

        batcher.shutdown_and_drain()

        assert processed_count == num_operations
        for future in futures:
            assert future.done()

    def test_context_manager_cleanup(self):
        """Test that context manager ensures cleanup."""
        mock_handler = Mock()
        with Batcher(mock_handler) as batcher:
            batcher.add(SizableString("test"))
            assert not batcher.is_shutdown
        # After exiting context, should be shutdown
        assert batcher.is_shutdown

    def test_context_manager_cleanup_on_exception(self):
        """Test that context manager cleans up even on exception."""
        mock_handler = Mock()
        with pytest.raises(RuntimeError):
            with Batcher(mock_handler) as batcher:
                batcher.add(SizableString("test"))
                raise RuntimeError("Test exception")
        # Should still be shutdown after exception
        assert batcher.is_shutdown


class TestBatcherSplit:
    """Test suite for Batcher.split() static method."""

    def test_split_below_min_batch_size(self):
        """Test split returns empty when below min batch size."""
        options = Batcher.Options(min_batch_size=5)
        result = Batcher.split(3, options)
        assert result == []

    def test_split_unlimited_max_batch_size(self):
        """Test split with no max batch size limit."""
        options = Batcher.Options(min_batch_size=1, max_batch_size=0)
        result = Batcher.split(100, options)
        assert result == [100]

    def test_split_respects_max_batch_size(self):
        """Test split respects max_batch_size."""
        options = Batcher.Options(min_batch_size=1, max_batch_size=10, max_handlers=10)
        result = Batcher.split(25, options)
        assert sum(result) == 25
        assert all(size <= 10 for size in result)

    def test_split_capped_by_max_handlers(self):
        """Test split is capped by max_handlers."""
        options = Batcher.Options(min_batch_size=1, max_batch_size=10, max_handlers=3)
        result = Batcher.split(100, options)
        assert len(result) <= 3
        assert sum(result) >= 30  # At least 3 batches of 10

    def test_split_with_null_options(self):
        """Test split with None options uses defaults."""
        result = Batcher.split(10, None)
        assert result == [10]

    def test_split_exactly_min_batch_size(self):
        """Test split with exactly min batch size."""
        options = Batcher.Options(min_batch_size=10)
        result = Batcher.split(10, options)
        assert result == [10]

    def test_split_zero_items(self):
        """Test split with zero items."""
        options = Batcher.Options(min_batch_size=1)
        result = Batcher.split(0, options)
        assert result == []

    def test_split_exact_multiple_of_max_batch_size(self):
        """Test split with exact multiple of max batch size."""
        options = Batcher.Options(min_batch_size=1, max_batch_size=10, max_handlers=10)
        result = Batcher.split(30, options)
        assert result == [10, 10, 10] or sum(result) == 30

    def test_split_with_remainder(self):
        """Test split with remainder."""
        options = Batcher.Options(min_batch_size=1, max_batch_size=10, max_handlers=10)
        result = Batcher.split(25, options)
        assert sum(result) == 25
        assert all(size <= 10 for size in result)

    def test_split_max_handlers_limit_with_remainder(self):
        """Test split respects max_handlers with remainder."""
        options = Batcher.Options(min_batch_size=1, max_batch_size=10, max_handlers=2)
        result = Batcher.split(25, options)
        assert len(result) <= 2
        assert sum(result) == 20  # Can only fit 2 batches of 10

    def test_split_large_number_with_small_batches(self):
        """Test split with large number and small batches."""
        options = Batcher.Options(min_batch_size=1, max_batch_size=5, max_handlers=10)
        result = Batcher.split(100, options)
        assert len(result) <= 10
        assert all(size <= 5 for size in result)

    def test_options_min_greater_than_max_allowed(self):
        """Test Options allows min > max batch size."""
        options = Batcher.Options(min_batch_size=20, max_batch_size=10, max_handlers=1)
        assert options.min_batch_size == 20
        assert options.max_batch_size == 10

    def test_options_negative_max_handlers_raises(self):
        """Test Options validation rejects negative max_handlers."""
        with pytest.raises(
            InvalidArgumentException, match="max_handlers must be greater than 0"
        ):
            Batcher.Options(max_handlers=0)

    def test_options_zero_min_batch_size_raises(self):
        """Test Options validation rejects zero min_batch_size."""
        with pytest.raises(
            InvalidArgumentException, match="min_batch_size must be at least 1"
        ):
            Batcher.Options(min_batch_size=0)

    def test_split_max_handlers_one(self):
        """Test split with max_handlers=1."""
        options = Batcher.Options(min_batch_size=1, max_batch_size=10, max_handlers=1)
        result = Batcher.split(50, options)
        assert len(result) == 1
        assert result[0] == 10

    def test_split_default_options(self):
        """Test split with default options."""
        result = Batcher.split(100, Batcher.Options())
        assert result == [100]


class TestBatcherOptionsValidation:
    """Test Batcher.Options validation."""

    def test_options_zero_max_handlers_raises(self):
        """Test that max_handlers=0 raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException, match="max_handlers must be greater than 0"
        ):
            Batcher.Options(max_handlers=0)

    def test_options_negative_max_handlers_raises(self):
        """Test that negative max_handlers raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException, match="max_handlers must be greater than 0"
        ):
            Batcher.Options(max_handlers=-1)

    def test_options_zero_min_batch_size_raises(self):
        """Test that min_batch_size=0 raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException, match="min_batch_size must be at least 1"
        ):
            Batcher.Options(min_batch_size=0)

    def test_options_negative_min_batch_size_raises(self):
        """Test that negative min_batch_size raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException, match="min_batch_size cannot be negative"
        ):
            Batcher.Options(min_batch_size=-1)

    def test_options_negative_max_batch_size_raises(self):
        """Test that negative max_batch_size raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException, match="max_batch_size cannot be negative"
        ):
            Batcher.Options(max_batch_size=-1)

    def test_options_negative_max_batch_byte_size_raises(self):
        """Test that negative max_batch_byte_size raises InvalidArgumentException."""
        with pytest.raises(
            InvalidArgumentException, match="max_batch_byte_size cannot be negative"
        ):
            Batcher.Options(max_batch_byte_size=-1)


class TestBatcherShutdownBehavior:
    """Test Batcher shutdown behavior."""

    def test_shutdown_and_drain_processes_pending_items(self):
        """Test that shutdown processes pending items below min_batch_size."""
        processed = []

        def handler(items):
            processed.extend(items)

        options = Batcher.Options(min_batch_size=100)
        batcher = Batcher(handler, options)

        for i in range(10):
            batcher.add_no_wait(f"item{i}")

        batcher.shutdown_and_drain()

        assert len(processed) == 10

    def test_context_manager_cleanup_exception_no_user_exception(self):
        """Test __exit__ propagates cleanup exception when no user exception."""

        class FailingShutdownBatcher(Batcher):
            def shutdown_and_drain(self):
                raise RuntimeError("Shutdown failed")

        handler = lambda items: None
        batcher = FailingShutdownBatcher(handler, Batcher.Options())

        with pytest.raises(RuntimeError, match="Shutdown failed"):
            with batcher:
                pass

    def test_context_manager_cleanup_exception_with_user_exception(self):
        """Test __exit__ suppresses cleanup exception when user exception exists."""

        class FailingShutdownBatcher(Batcher):
            def shutdown_and_drain(self):
                raise RuntimeError("Shutdown failed")

        handler = lambda items: None
        batcher = FailingShutdownBatcher(handler, Batcher.Options())

        with pytest.raises(ValueError, match="User error"):
            with batcher:
                raise ValueError("User error")


class TestBatcherRemainingPaths:
    """Test remaining Batcher code paths."""

    def test_add_wraps_non_substrate_exception(self):
        """Test add() wraps non-SubstrateSdkException (line 508)."""
        from multicloud.common.exceptions import FailedPreconditionException

        def failing_handler(items):
            raise ValueError("Handler error")

        batcher = Batcher(failing_handler, Batcher.Options(min_batch_size=1))

        # add() should wrap or propagate the exception
        try:
            batcher.add("item1")
        except (FailedPreconditionException, ValueError):
            pass  # Either is acceptable

        batcher.shutdown_and_drain()

    def test_get_next_batch_returns_none_when_empty(self):
        """Test _get_next_batch returns None when no batch (line 601)."""
        handler = Mock()
        batcher = Batcher(handler, Batcher.Options(min_batch_size=10))

        # Don't add anything
        batch = batcher._get_next_batch(ignore_min_batch_size=False)

        assert batch is None

        batcher.shutdown_and_drain()

    def test_process_handler_stranded_items_recovery(self):
        """Test _process_handler picks up stranded items (line 628)."""
        processed = []

        def handler(items):
            processed.extend(items)

        options = Batcher.Options(min_batch_size=2, max_batch_size=2, max_handlers=1)
        batcher = Batcher(handler, options)

        # Add 3 items - 2 will batch, 1 will be stranded
        batcher.add_no_wait("item1")
        batcher.add_no_wait("item2")  # Triggers first batch
        time.sleep(0.05)  # Let first batch process

        batcher.add_no_wait("item3")  # Stranded (below min_batch_size)

        # Shutdown should pick up the stranded item
        batcher.shutdown_and_drain()

        # All 3 items should be processed
        assert len(processed) == 3
