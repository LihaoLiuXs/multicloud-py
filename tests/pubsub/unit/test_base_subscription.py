"""
Unit tests for BaseSubscription.
"""

import inspect
import threading
import time
from collections import deque
from concurrent.futures import Future
from datetime import timedelta
from typing import Deque, List, Optional

import pytest
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
    SubstrateSdkException,
    UnknownException,
)
from multicloud.pubsub.pubsub_client.driver import (
    AckID,
    AckInfo,
    BaseSubscription,
    Batcher,
    GetAttributeResult,
    Message,
)


class TestAckID:
    """Test implementation of AckID."""

    def __init__(self, id: str) -> None:
        self.id = id

    def __str__(self) -> str:
        return self.id

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if not isinstance(other, TestAckID):
            return False
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id) if self.id else 0


class MockMessageSource:
    """Mock external message source that simulates a real pub/sub service."""

    def __init__(self):
        self.available_messages: Deque[Message] = deque()
        self.should_throw = False
        self.exception_to_throw: Optional[Exception] = None

    def add_messages(self, messages: List[Message]) -> None:
        self.available_messages.extend(messages)

    def set_throw_exception(self, exc: Exception) -> None:
        self.should_throw = True
        self.exception_to_throw = exc

    def fetch_batch(self, batch_size: int) -> List[Message]:
        if self.should_throw and self.exception_to_throw:
            raise self.exception_to_throw

        batch = []
        for _ in range(batch_size):
            if self.available_messages:
                msg = self.available_messages.popleft()
                if msg is not None:
                    batch.append(msg)
            else:
                break
        return batch


class TestSubscription(BaseSubscription):
    """Test implementation of BaseSubscription."""

    def __init__(self, message_source: Optional[MockMessageSource] = None, **kwargs):
        if "subscription_name" not in kwargs:
            kwargs["subscription_name"] = "sub"
        if "region" not in kwargs:
            kwargs["region"] = "region"

        super().__init__(**kwargs)
        self.message_source = message_source or MockMessageSource()
        self.acks_sent: List[List[AckID]] = []
        self.nacks_sent: List[List[AckInfo]] = []

    def get_provider_id(self) -> str:
        return "test"

    def do_receive_batch(self, batch_size: int) -> List[Message]:
        return self.message_source.fetch_batch(batch_size)

    def do_send_acks(self, ack_ids: List[AckID]) -> None:
        self.acks_sent.append(list(ack_ids))

    def do_send_nacks(self, nacks: List[AckInfo]) -> None:
        self.nacks_sent.append(list(nacks))

    def can_nack(self) -> bool:
        return False

    def get_attributes(self) -> GetAttributeResult:
        return GetAttributeResult(approximate_number_of_messages=0)

    def is_retryable(self, error: BaseException) -> bool:
        # Only retry specific transient exceptions
        if isinstance(error, (TimeoutError, ConnectionError)):
            return True
        if isinstance(error, OSError) and "timeout" in str(error):
            return True
        return False

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        return UnknownException(str(exc))

    def create_ack_batcher_options(self) -> Batcher.Options:
        return Batcher.Options(
            max_handlers=2,
            min_batch_size=1,
            max_batch_size=1000,
            max_batch_byte_size=0,
        )


class TestBaseSubscription:
    """Test suite for BaseSubscription."""

    def test_receive_method_signature(self):
        """Test receive() method signature contract."""
        sig = inspect.signature(BaseSubscription.receive)
        params = list(sig.parameters.values())

        # receive(self) -> Message
        assert len(params) == 1
        assert params[0].name == "self"
        assert sig.return_annotation in (Message, "Message")

    def test_receive_with_provider_exception(self):
        """Test receive with provider exception."""
        source = MockMessageSource()
        source.set_throw_exception(RuntimeError("Provider error"))
        subscription = TestSubscription(message_source=source)

        # Driver now propagates the original exception (no generic wrapping).
        with pytest.raises(RuntimeError, match="Provider error"):
            subscription.receive()

        # Subscription should now be in permanent error state with the original exception
        assert subscription._permanent_error is not None
        assert "Provider error" in str(subscription._permanent_error)

        subscription.close()

    def test_receive_with_checked_provider_exception(self):
        """Test receive with checked provider exception."""
        source = MockMessageSource()
        source.set_throw_exception(RuntimeError("Checked error"))
        subscription = TestSubscription(message_source=source)

        # Driver now propagates the original exception (no generic wrapping).
        with pytest.raises(RuntimeError, match="Checked error"):
            subscription.receive()

        subscription.close()

    def test_constructor_with_default_parameters(self):
        """Test constructor with default parameters."""
        subscription = TestSubscription()

        assert subscription.subscription_name == "sub"
        assert subscription.region == "region"
        assert not subscription.is_shutdown

    def test_close_method(self):
        """Test close method."""
        subscription = TestSubscription()

        subscription.close()

        assert subscription.is_shutdown

    def test_abstract_methods_are_implemented(self):
        """Test that abstract methods return expected values."""
        subscription = TestSubscription()

        # Test concrete return values, not just non-null
        assert subscription.can_nack() is False
        result = subscription.get_attributes()
        assert result.approximate_number_of_messages == 0
        assert subscription.is_retryable(TimeoutError()) is True
        assert subscription.is_retryable(RuntimeError()) is False

        subscription.close()

    def test_send_ack_with_null_ack_id(self):
        """Test send_ack with null ack ID."""
        subscription = TestSubscription()

        with pytest.raises(InvalidArgumentException):
            subscription.send_ack(None)

        subscription.close()

    def test_send_acks_with_null_ack_id(self):
        """Test send_acks with null ack ID."""
        subscription = TestSubscription()

        with pytest.raises(InvalidArgumentException):
            subscription.send_acks(None)

        subscription.close()

    def test_send_acks_with_empty_list(self):
        """Test send_acks with empty list."""
        subscription = TestSubscription()

        future = subscription.send_acks([])
        future.result()

        # Should not have sent anything
        assert len(subscription.acks_sent) == 0
        subscription.close()

    def test_send_ack_with_valid_ack_id(self):
        """Test send_ack with valid ack ID."""
        subscription = TestSubscription()
        ack_id = TestAckID("test-ack-1")

        subscription.send_ack(ack_id)
        subscription.close()

        # Should have batched and sent
        assert len(subscription.acks_sent) >= 1
        all_acks = [ack for batch in subscription.acks_sent for ack in batch]
        assert ack_id in all_acks

    def test_send_acks_with_valid_ack_ids(self):
        """Test send_acks with valid ack IDs."""
        subscription = TestSubscription()
        ack_ids = [TestAckID("ack-1"), TestAckID("ack-2"), TestAckID("ack-3")]

        future = subscription.send_acks(ack_ids)
        assert future.done()
        future.result()
        subscription.close()

        # All should have been sent
        all_acks = [ack for batch in subscription.acks_sent for ack in batch]
        for ack_id in ack_ids:
            assert ack_id in all_acks

    def test_permanent_error_state_handling(self):
        """Test permanent error state handling."""
        source = MockMessageSource()
        source.set_throw_exception(RuntimeError("Permanent error"))
        subscription = TestSubscription(message_source=source)

        # First receive should fail with the original exception (driver propagates it).
        with pytest.raises(RuntimeError, match="Permanent error"):
            subscription.receive()

        # Subscription should now be in permanent error state with the error stored
        assert subscription._permanent_error is not None
        assert isinstance(subscription._permanent_error, RuntimeError)

        # Subsequent receives should continue surfacing the original exception.
        with pytest.raises(RuntimeError, match="Permanent error"):
            subscription.receive()

        subscription.close()

    def test_nack_visibility_timeout_defaults_to_zero(self):
        """Test nack visibility timeout defaults to zero."""
        subscription = TestSubscription()
        assert subscription.nack_visibility_timeout == timedelta(seconds=0)
        subscription.close()

    def test_nack_visibility_timeout_null_on_builder_treated_as_zero(self):
        """Test nack visibility timeout null on builder treated as zero."""
        subscription = TestSubscription(nack_visibility_timeout=None)
        assert subscription.nack_visibility_timeout == timedelta(seconds=0)
        subscription.close()

    def test_nack_visibility_timeout_positive_value_on_builder(self):
        """Test nack visibility timeout positive value on builder."""
        timeout = timedelta(seconds=30)
        subscription = TestSubscription(nack_visibility_timeout=timeout)
        assert subscription.nack_visibility_timeout == timeout
        subscription.close()

    def test_prefetch_error_handling(self):
        """Test prefetch error handling."""
        source = MockMessageSource()
        # Add one message so receive works
        msg = (
            Message.builder().with_body(b"test").with_ack_id(TestAckID("ack1")).build()
        )
        source.add_messages([msg])

        subscription = TestSubscription(message_source=source)

        # Receive should work and return the message
        received = subscription.receive()
        assert received.body == b"test"

        # Set error for next fetch
        source.set_throw_exception(RuntimeError("Prefetch error"))

        # This shouldn't crash, error should be handled
        subscription.close()

    def test_advanced_shutdown_logic(self):
        """Test advanced shutdown logic."""
        subscription = TestSubscription()

        # Close should be idempotent
        subscription.close()
        subscription.close()

        assert subscription.is_shutdown

    def test_send_nack_with_valid_ack_id(self):
        """Test send_nack with valid ack ID."""
        subscription = TestSubscription()
        ack_id = TestAckID("nack-1")

        subscription.send_nack(ack_id)
        subscription.close()

        # Should have batched and sent
        assert len(subscription.nacks_sent) >= 1
        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        assert ack_id in all_nacks

    def test_send_nack_with_null_ack_id(self):
        """Test send_nack with null ack ID."""
        subscription = TestSubscription()

        with pytest.raises(InvalidArgumentException):
            subscription.send_nack(None)

        subscription.close()

    def test_send_nacks_with_valid_ack_ids(self):
        """Test send_nacks with valid ack IDs."""
        subscription = TestSubscription()
        ack_ids = [TestAckID("nack-1"), TestAckID("nack-2")]

        future = subscription.send_nacks(ack_ids)
        assert future.done()
        future.result()
        subscription.close()

        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        for ack_id in ack_ids:
            assert ack_id in all_nacks

    def test_send_nacks_with_null_list(self):
        """Test send_nacks with null list."""
        subscription = TestSubscription()

        with pytest.raises(InvalidArgumentException):
            subscription.send_nacks(None)

        subscription.close()

    def test_send_nacks_with_empty_list(self):
        """Test send_nacks with empty list."""
        subscription = TestSubscription()

        future = subscription.send_nacks([])
        future.result()

        assert len(subscription.nacks_sent) == 0
        subscription.close()

    def test_send_nacks_with_null_ack_id_in_list(self):
        """Test send_nacks with null ack ID in list."""
        subscription = TestSubscription()

        with pytest.raises(InvalidArgumentException):
            subscription.send_nacks([TestAckID("valid"), None])

        subscription.close()

    def test_send_nack_throws_on_negative_visibility_timeout(self):
        """Test send_nack throws on negative visibility timeout."""
        subscription = TestSubscription()
        ack_id = TestAckID("test")

        with pytest.raises(InvalidArgumentException):
            subscription.send_nack(ack_id, visibility_timeout=timedelta(seconds=-1))

        subscription.close()

    def test_builder_rejects_negative_nack_visibility_timeout(self):
        """Test builder rejects negative nack visibility timeout."""
        with pytest.raises(InvalidArgumentException):
            TestSubscription(nack_visibility_timeout=timedelta(seconds=-5))

    def test_send_nacks_with_single_ack_id(self):
        """Test send_nacks with single ack ID."""
        subscription = TestSubscription()
        ack_ids = [TestAckID("single")]

        future = subscription.send_nacks(ack_ids)
        future.result()
        subscription.close()

        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        assert TestAckID("single") in all_nacks

    def test_send_nacks_with_large_list(self):
        """Test send_nacks with large list."""
        subscription = TestSubscription()
        ack_ids = [TestAckID(f"nack-{i}") for i in range(100)]

        future = subscription.send_nacks(ack_ids)
        future.result()
        subscription.close()

        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        assert len(all_nacks) == 100

    def test_send_nack_and_send_nacks_consistency(self):
        """Test send_nack and send_nacks consistency."""
        subscription = TestSubscription()

        # Send via send_nack
        subscription.send_nack(TestAckID("single"))

        # Send via send_nacks
        future = subscription.send_nacks([TestAckID("batch-1"), TestAckID("batch-2")])
        future.result()

        subscription.close()

        # All should have been sent
        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        assert len(all_nacks) == 3

    def test_send_nack_after_subscription_close(self):
        """Test send_nack after subscription close."""
        subscription = TestSubscription()
        subscription.close()

        with pytest.raises(FailedPreconditionException):
            subscription.send_nack(TestAckID("test"))

    def test_send_nacks_after_subscription_close(self):
        """Test send_nacks after subscription close."""
        subscription = TestSubscription()
        subscription.close()

        with pytest.raises(FailedPreconditionException):
            subscription.send_nacks([TestAckID("test")])

    def test_send_nack_with_different_ack_id_types(self):
        """Test send_nack with different ack ID types."""
        subscription = TestSubscription()

        # Send different types (all TestAckID but different values)
        subscription.send_nack(TestAckID("type-a"))
        subscription.send_nack(TestAckID("type-b"))

        subscription.close()

        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        assert len(all_nacks) == 2

    def test_send_nacks_with_mixed_ack_id_types(self):
        """Test send_nacks with mixed ack ID types."""
        subscription = TestSubscription()

        ack_ids = [TestAckID("mixed-1"), TestAckID("mixed-2"), TestAckID("mixed-3")]
        future = subscription.send_nacks(ack_ids)
        future.result()

        subscription.close()

        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        assert len(all_nacks) == 3

    def test_send_nacks_return_value(self):
        """Test send_nacks return value."""
        subscription = TestSubscription()

        future = subscription.send_nacks([TestAckID("test")])

        assert isinstance(future, Future)
        future.result()  # Should complete without error

        subscription.close()

    def test_send_nacks_with_empty_list_return_value(self):
        """Test send_nacks with empty list return value."""
        subscription = TestSubscription()

        future = subscription.send_nacks([])

        assert isinstance(future, Future)
        future.result()  # Should complete immediately

        subscription.close()

    def test_send_nack_and_send_ack_consistency(self):
        """Test send_nack and send_ack consistency."""
        subscription = TestSubscription()

        # Mix acks and nacks
        subscription.send_ack(TestAckID("ack-1"))
        subscription.send_nack(TestAckID("nack-1"))
        subscription.send_ack(TestAckID("ack-2"))

        subscription.close()

        # Both should have been sent to their respective batchers
        all_acks = [ack for batch in subscription.acks_sent for ack in batch]
        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]

        assert len(all_acks) == 2
        assert len(all_nacks) == 1

    def test_send_nacks_and_send_acks_consistency(self):
        """Test send_nacks and send_acks consistency."""
        subscription = TestSubscription()

        # Send batches of both
        future_acks = subscription.send_acks([TestAckID("ack-1"), TestAckID("ack-2")])
        future_nacks = subscription.send_nacks(
            [TestAckID("nack-1"), TestAckID("nack-2")]
        )

        future_acks.result()
        future_nacks.result()

        subscription.close()

        all_acks = [ack for batch in subscription.acks_sent for ack in batch]
        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]

        assert len(all_acks) == 2
        assert len(all_nacks) == 2

    def test_send_nack_with_same_ack_id_multiple_times(self):
        """Test send_nack with same ack ID multiple times."""
        subscription = TestSubscription()
        ack_id = TestAckID("duplicate")

        subscription.send_nack(ack_id)
        subscription.send_nack(ack_id)
        subscription.send_nack(ack_id)

        subscription.close()

        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        # Should have sent all three (even if duplicate)
        assert len(all_nacks) == 3

    def test_send_nacks_with_same_ack_id_multiple_times(self):
        """Test send_nacks with same ack ID multiple times."""
        subscription = TestSubscription()
        ack_id = TestAckID("duplicate")

        future = subscription.send_nacks([ack_id, ack_id, ack_id])
        future.result()

        subscription.close()

        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        assert len(all_nacks) == 3

    def test_send_nack_with_null_ack_id_after_valid_ack_id(self):
        """Test send_nack with null ack ID after valid ack ID."""
        subscription = TestSubscription()

        subscription.send_nack(TestAckID("valid"))

        with pytest.raises(InvalidArgumentException):
            subscription.send_nack(None)

        subscription.close()

    def test_send_nacks_with_null_ack_id_after_valid_ack_ids(self):
        """Test send_nacks with null ack ID after valid ack IDs."""
        subscription = TestSubscription()

        future = subscription.send_nacks([TestAckID("valid1"), TestAckID("valid2")])
        future.result()

        with pytest.raises(InvalidArgumentException):
            subscription.send_nacks([TestAckID("valid3"), None])

        subscription.close()

    def test_send_nack_and_send_nacks_mixed_usage(self):
        """Test send_nack and send_nacks mixed usage."""
        subscription = TestSubscription()

        # Mix single and batch
        subscription.send_nack(TestAckID("single-1"))
        future1 = subscription.send_nacks([TestAckID("batch-1"), TestAckID("batch-2")])
        subscription.send_nack(TestAckID("single-2"))
        future2 = subscription.send_nacks([TestAckID("batch-3")])

        future1.result()
        future2.result()

        subscription.close()

        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        assert len(all_nacks) == 5

    def test_builder_provider_id(self):
        """Test builder provider ID."""
        subscription = TestSubscription()
        assert subscription.get_provider_id() == "test"
        subscription.close()

    def test_batch_size_never_zero(self):
        """Test batch size never zero."""
        subscription = TestSubscription()

        # Internal batch size tracking should never be zero
        assert subscription._running_batch_size > 0

        subscription.close()

    def test_builder_methods(self):
        """Test builder-equivalent constructor options with all fields."""
        timeout = timedelta(seconds=45)
        subscription = TestSubscription(
            subscription_name="test-subscription",
            region="us-west-2",
            endpoint="https://test.example.com",
            nack_visibility_timeout=timeout,
        )

        assert subscription.subscription_name == "test-subscription"
        assert subscription.region == "us-west-2"
        assert subscription.endpoint == "https://test.example.com"
        assert subscription.nack_visibility_timeout == timeout

        subscription.close()

    def test_send_nacks_concurrent_access(self):
        """Test concurrent access to send_nacks from multiple threads."""
        subscription = TestSubscription()
        num_threads = 10
        nacks_per_thread = 20
        start_latch = threading.Event()
        done_counter = 0
        done_counter_lock = threading.Lock()
        errors: List[Exception] = []
        errors_lock = threading.Lock()
        all_futures: List[Future[None]] = []
        futures_lock = threading.Lock()

        def worker(thread_id: int) -> None:
            nonlocal done_counter
            start_latch.wait()
            try:
                local_ids = [
                    TestAckID(f"t{thread_id}-n{i}") for i in range(nacks_per_thread)
                ]
                future = subscription.send_nacks(local_ids)
                with futures_lock:
                    all_futures.append(future)
            except Exception as exc:
                with errors_lock:
                    errors.append(exc)
            finally:
                with done_counter_lock:
                    done_counter += 1

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True)
            for i in range(num_threads)
        ]
        for thread in threads:
            thread.start()

        start_latch.set()
        for thread in threads:
            thread.join(timeout=10)

        assert done_counter == num_threads
        assert not errors

        for future in all_futures:
            future.result(timeout=10)

        subscription.close()

        all_nacks = [nack.ack_id for batch in subscription.nacks_sent for nack in batch]
        assert len(all_nacks) == num_threads * nacks_per_thread


class TestBaseSubscriptionAdaptiveBatching:
    """Test adaptive batch sizing."""

    def test_update_batch_size_with_throughput_history(self):
        """Test _update_batch_size calculates based on throughput."""
        sub = TestSubscription(subscription_name="test")

        sub._throughput_start_ns = time.monotonic_ns() - 1_000_000_000
        sub._throughput_count = 100

        batch_size = sub._update_batch_size()

        assert 1 <= batch_size <= 3000
        assert sub._throughput_start_ns != 0
        assert sub._throughput_count == 0

        sub.close()

    def test_update_batch_size_growth_capped(self):
        """Test batch size growth is capped at 2x."""
        sub = TestSubscription(subscription_name="test")
        sub._running_batch_size = 10.0
        sub._throughput_start_ns = time.monotonic_ns() - 1_000_000_000
        sub._throughput_count = 1000

        batch_size = sub._update_batch_size()

        assert batch_size <= 20

        sub.close()

    def test_update_batch_size_shrink_capped(self):
        """Test batch size shrink is capped at 0.75x."""
        sub = TestSubscription(subscription_name="test")
        sub._running_batch_size = 100.0
        sub._throughput_start_ns = time.monotonic_ns() - 1_000_000_000
        sub._throughput_count = 1

        batch_size = sub._update_batch_size()

        assert batch_size >= 75

        sub.close()

    def test_get_next_batch_with_empty_split(self):
        """Test _get_next_batch when split returns empty."""
        sub = TestSubscription(subscription_name="test")

        messages = sub._get_next_batch(0)

        assert messages == []

        sub.close()

    def test_aggregate_futures_with_empty_list(self):
        """Test _aggregate_futures handles empty list."""
        sub = TestSubscription(subscription_name="test")

        future = sub._aggregate_futures([])
        result = future.result()

        assert result is None

        sub.close()


class TestBaseSubscriptionRemainingPaths:
    """Test remaining BaseSubscription code paths."""

    def test_send_acks_after_shutdown_raises(self):
        """Test send_acks checks shutdown state (line 850)."""
        sub = TestSubscription(subscription_name="test")
        sub.close()

        with pytest.raises(FailedPreconditionException, match="shut down"):
            sub.send_acks([TestAckID("test")])

    def test_send_acks_with_null_batch_raises(self):
        """Test send_acks validates null batch (lines 865-870)."""
        sub = TestSubscription(subscription_name="test")

        from multicloud.common.exceptions import InvalidArgumentException

        # Create batch with None in it
        ack_ids = [TestAckID("test1"), None, TestAckID("test2")]

        # This should trigger validation error with permanent error
        with pytest.raises(InvalidArgumentException, match="cannot be null"):
            future = sub.send_acks(ack_ids)
            future.result()

        sub.close()

    def test_send_nacks_with_negative_timeout_raises(self):
        """Test send_nacks validates negative timeout (lines 915-920)."""
        from datetime import timedelta

        from multicloud.common.exceptions import InvalidArgumentException

        sub = TestSubscription(subscription_name="test")

        # This should set permanent error
        with pytest.raises(InvalidArgumentException, match="cannot be negative"):
            future = sub.send_nacks(
                [TestAckID("test")], visibility_timeout=timedelta(seconds=-1)
            )
            future.result()

        sub.close()

    def test_handle_ack_and_nack_batch_non_retryable_error(self):
        """Test _handle_ack_and_nack_batch with non-retryable error (lines 962-967)."""

        class NonRetryableSub(TestSubscription):
            def do_send_acks(self, ack_ids):
                raise RuntimeError("Non-retryable error")

            def is_retryable(self, error):
                return False  # Non-retryable

        sub = NonRetryableSub(subscription_name="test")

        from multicloud.pubsub.pubsub_client.driver import AckInfo

        ack_info = AckInfo(
            ack_id=TestAckID("test"), is_ack=True, visibility_timeout=None
        )

        # Should set permanent error and propagate the original exception
        with pytest.raises(RuntimeError, match="Non-retryable error"):
            sub._handle_ack_and_nack_batch([ack_info])

        # Check permanent error was set to the RuntimeError we injected
        assert sub._permanent_error is not None
        assert isinstance(sub._permanent_error, RuntimeError)

        # Close will raise unreported error
        try:
            sub.close()
        except:
            pass

    def test_handle_ack_and_nack_batch_nack_non_retryable_error(self):
        """Test _handle_ack_and_nack_batch nack with non-retryable error (lines 971-977)."""
        from datetime import timedelta

        class NonRetryableSub(TestSubscription):
            def do_send_nacks(self, nacks):
                raise RuntimeError("Non-retryable nack error")

            def is_retryable(self, error):
                return False

        sub = NonRetryableSub(subscription_name="test")

        from multicloud.pubsub.pubsub_client.driver import AckInfo

        ack_info = AckInfo(
            ack_id=TestAckID("test"),
            is_ack=False,
            visibility_timeout=timedelta(seconds=30),
        )

        # Driver now propagates the original exception (no generic wrapping).
        with pytest.raises(RuntimeError, match="Non-retryable nack error"):
            sub._handle_ack_and_nack_batch([ack_info])

        assert sub._permanent_error is not None
        assert isinstance(sub._permanent_error, RuntimeError)

        try:
            sub.close()
        except:
            pass

    def test_update_batch_size_normal_adjustment(self):
        """Test _update_batch_size normal adjustment within bounds (line 1046)."""
        sub = TestSubscription(subscription_name="test")

        sub._running_batch_size = 50.0
        sub._throughput_start_ns = time.monotonic_ns() - 1_000_000_000
        sub._throughput_count = 30  # 30 msgs/sec

        batch_size = sub._update_batch_size()

        # Should be smoothed: 50 * 0.5 + 60 * 0.5 = 55
        # And within bounds [37.5, 100]
        assert 37.5 <= batch_size <= 100

        sub.close()

    def test_aggregate_futures_with_first_exception(self):
        """Test _aggregate_futures captures first exception (lines 1005, 1011)."""
        from concurrent.futures import Future

        sub = TestSubscription(subscription_name="test")

        f1 = Future()
        f2 = Future()

        f1.set_exception(ValueError("Error 1"))
        f2.set_result(None)

        composite = sub._aggregate_futures([f1, f2])

        with pytest.raises(ValueError, match="Error 1"):
            composite.result(timeout=1)

        sub.close()

    def test_update_batch_size_single_handler_optimization(self):
        """Test _update_batch_size with single handler/batch optimization (line 1023)."""

        class SingleBatchSub(TestSubscription):
            def create_receive_batcher_options(self):
                from multicloud.pubsub.pubsub_client.driver import Batcher

                return Batcher.Options(max_handlers=1, max_batch_size=1)

        sub = SingleBatchSub(subscription_name="test")

        batch_size = sub._update_batch_size()

        # Should return 1 for single handler/batch optimization
        assert batch_size == 1

        sub.close()

    def test_get_next_batch_parallel_fetch(self):
        """Test _get_next_batch with multiple batches parallel fetch (lines 1076-1083)."""

        class ParallelSub(TestSubscription):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.fetch_counts = []

            def create_receive_batcher_options(self):
                from multicloud.pubsub.pubsub_client.driver import Batcher

                return Batcher.Options(max_handlers=3, max_batch_size=5)

            def do_receive_batch(self, batch_size):
                self.fetch_counts.append(batch_size)
                time.sleep(0.01)  # Simulate network delay
                return []

        sub = ParallelSub(subscription_name="test")

        # Request 15 messages - should split into 3 batches of 5
        messages = sub._get_next_batch(15)

        # Should have made multiple parallel fetches
        assert len(sub.fetch_counts) >= 2

        sub.close()

    def test_close_with_unreported_ack_error_raises(self):
        """Test close raises unreported ack error (lines 1096-1098)."""

        class ErrorSub(TestSubscription):
            def do_send_acks(self, ack_ids):
                raise RuntimeError("Ack failed")

            def is_retryable(self, error):
                return False

        sub = ErrorSub(subscription_name="test")

        from multicloud.pubsub.pubsub_client.driver import AckInfo

        ack_info = AckInfo(
            ack_id=TestAckID("test"), is_ack=True, visibility_timeout=None
        )

        # Trigger error
        try:
            sub._handle_ack_and_nack_batch([ack_info])
        except:
            pass

        # Close should raise the original unreported error (driver propagates it).
        with pytest.raises(RuntimeError, match="Ack failed"):
            sub.close()

    def test_receive_checks_permanent_error(self):
        """Test receive checks for permanent error (line 820)."""
        sub = TestSubscription(subscription_name="test")

        # Set a permanent error
        sub._permanent_error = RuntimeError("Permanent error")

        # Driver now propagates the original exception (no generic wrapping).
        with pytest.raises(RuntimeError, match="Permanent error"):
            sub.receive()

        sub.close()
