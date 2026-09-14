"""
Unit tests for BaseTopic.
"""

import threading
import time
from typing import List

import pytest
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
    UnknownException,
)
from multicloud.pubsub.pubsub_client.driver import BaseTopic, Message


class TestablBaseTopic(BaseTopic):
    """Test implementation of BaseTopic for testing purposes."""

    # Test control flags
    should_throw_runtime_exception: bool = False
    should_throw_checked_exception: bool = False
    should_interrupt: bool = False

    # Track method calls
    sent_batches: List[List[Message]]

    def __init__(
        self,
        *,
        topic_name: str,
        region: str = None,
        endpoint: str = None,
    ) -> None:
        super().__init__(
            topic_name=topic_name,
            region=region,
            endpoint=endpoint,
        )
        self.sent_batches = []

    def get_provider_id(self) -> str:
        return "test"

    def do_send_batch(self, messages: List[Message]) -> None:
        if self.should_interrupt:
            raise RuntimeError("Interrupted")

        if self.should_throw_runtime_exception:
            raise RuntimeError("Provider error")

        if self.should_throw_checked_exception:
            raise Exception("Checked provider error")

        self.sent_batches.append(list(messages))

    def translate_error(self, exc: Exception):
        """Translate provider-specific exceptions to common hierarchy."""
        from multicloud.common.exceptions import UnknownException

        return UnknownException(str(exc))

    def get_max_message_size_bytes(self) -> int:
        """Return message size limit for testing."""
        return 256 * 1024  # 256 KB


class TestBaseTopic:
    """Test suite for BaseTopic class."""

    TOPIC_NAME = "test-topic"
    REGION = "test-region"

    @pytest.fixture
    def topic(self) -> TestablBaseTopic:
        """Create a fresh TestablBaseTopic for each test."""
        return TestablBaseTopic(topic_name=self.TOPIC_NAME, region=self.REGION)

    def test_constructor_sets_fields_correctly(self, topic: TestablBaseTopic):
        """Test constructor sets fields correctly."""
        assert topic.topic_name == self.TOPIC_NAME
        assert topic.region == self.REGION

    def test_constructor_with_all_params(self):
        """Test constructor with all parameters."""
        topic = TestablBaseTopic(
            topic_name=self.TOPIC_NAME,
            region=self.REGION,
            endpoint="https://custom-endpoint.example.com",
        )

        assert topic.topic_name == self.TOPIC_NAME
        assert topic.region == self.REGION
        assert topic.endpoint == "https://custom-endpoint.example.com"

    def test_send_valid_message(self, topic: TestablBaseTopic):
        """Test sending a valid message."""
        message = Message.builder().with_body("test-data").build()
        topic.send(message)

        # Close to flush batcher
        topic.close()

        # Verify do_send_batch was called with the message
        assert len(topic.sent_batches) >= 1
        all_messages = [msg for batch in topic.sent_batches for msg in batch]
        assert message in all_messages

    def test_send_null_message(self, topic: TestablBaseTopic):
        """Test that sending None raises an exception."""
        with pytest.raises(InvalidArgumentException):
            topic.send(None)

    def test_send_empty_body_message(self, topic: TestablBaseTopic):
        """Test sending a message with empty body."""
        empty_message = Message.builder().with_body(b"").build()
        topic.send(empty_message)
        topic.close()
        assert len(topic.sent_batches) >= 1

    def test_send_oversized_message_raises(self, topic: TestablBaseTopic):
        """Test oversized message body raises InvalidArgumentException."""
        oversized_body = b"x" * (topic.get_max_message_size_bytes() + 1)
        oversized_message = Message.builder().with_body(oversized_body).build()

        with pytest.raises(InvalidArgumentException, match="exceeds"):
            topic.send(oversized_message)

    def test_send_body_at_limit_with_metadata_raises(self, topic: TestablBaseTopic):
        """Per-message limit includes metadata, not just body."""
        max_size = topic.get_max_message_size_bytes()
        message = Message(body=b"x" * max_size, metadata={"k": "v"})

        with pytest.raises(
            InvalidArgumentException,
            match=r"Message size \(\d+ bytes\) exceeds maximum allowed size",
        ):
            topic.send(message)

    def test_send_with_provider_exception(self, topic: TestablBaseTopic):
        """Test that provider exceptions propagate unchanged at driver layer."""
        topic.should_throw_runtime_exception = True
        message = Message.builder().with_body("test-data").build()

        # Driver layer lets exceptions propagate - client layer does translation
        with pytest.raises(RuntimeError, match="Provider error"):
            topic.send(message)
            topic.close()  # Flush to trigger the error

    def test_send_with_checked_provider_exception(self, topic: TestablBaseTopic):
        """Test that checked provider exceptions propagate unchanged at driver layer."""
        topic.should_throw_checked_exception = True
        message = Message.builder().with_body("test-data").build()

        # Driver layer lets exceptions propagate - client layer does translation
        with pytest.raises(Exception, match="Checked provider error"):
            topic.send(message)
            topic.close()  # Flush to trigger the error

    def test_send_with_interruption(self, topic: TestablBaseTopic):
        """Test that interruption propagates unchanged at driver layer."""
        topic.should_interrupt = True
        message = Message.builder().with_body("test-data").build()

        # Driver layer lets exceptions propagate - client layer does translation
        with pytest.raises(RuntimeError, match="Interrupted"):
            topic.send(message)
            topic.close()  # Flush to trigger the error

    def test_concurrent_sends(self, topic: TestablBaseTopic):
        """Test concurrent send operations from multiple threads."""
        num_threads = 10
        messages_per_thread = 100
        start_event = threading.Event()
        threads = []
        success_count = [0]
        error_ref = [None]
        lock = threading.Lock()

        def worker(thread_id: int):
            try:
                start_event.wait()
                for j in range(messages_per_thread):
                    message = (
                        Message.builder()
                        .with_body(f"thread-{thread_id}-msg-{j}")
                        .build()
                    )
                    topic.send(message)
                    with lock:
                        success_count[0] += 1
            except Exception as e:
                error_ref[0] = e

        for i in range(num_threads):
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()

        start_event.set()  # Start all threads

        for thread in threads:
            thread.join(timeout=8.0)

        topic.close()

        assert error_ref[0] is None, "Should not have any errors"
        assert success_count[0] == num_threads * messages_per_thread
        assert len(topic.sent_batches) > 0, "Should have sent some batches"

    def test_close_method(self, topic: TestablBaseTopic):
        """Test that close does not raise an exception."""
        topic.close()  # Should not raise
        assert topic.is_shutdown is True

    def test_send_after_close_raises(self, topic: TestablBaseTopic):
        """Test that sending after close raises FailedPreconditionException."""
        topic.close()

        message = Message.builder().with_body("test-data").build()

        with pytest.raises(FailedPreconditionException):
            topic.send(message)

    def test_double_close_is_safe(self, topic: TestablBaseTopic):
        """Test that calling close twice is safe."""
        topic.close()
        topic.close()  # Should not raise
        assert topic.is_shutdown is True


class TestBaseTopicInternalPaths:
    """Test BaseTopic internal shutdown and error paths."""

    def test_send_batch_with_empty_list_returns_early(self):
        """Test _send_batch with empty list returns without processing."""
        topic = TestablBaseTopic(topic_name="test")

        topic._send_batch([])

        assert len(topic.sent_batches) == 0

        topic.close()

    def test_send_batch_after_shutdown_raises(self):
        """Test _send_batch after shutdown raises."""
        topic = TestablBaseTopic(topic_name="test")
        topic.close()

        msg = Message.builder().with_body(b"test").build()

        with pytest.raises(FailedPreconditionException, match="shut down"):
            topic._send_batch([msg])

    def test_handle_batch_after_shutdown_raises(self):
        """Test _handle_batch after shutdown raises."""
        topic = TestablBaseTopic(topic_name="test")
        topic.close()

        msg = Message.builder().with_body(b"test").build()

        with pytest.raises(FailedPreconditionException, match="shut down"):
            topic._handle_batch([msg])

    def test_handle_batch_propagates_substrate_exception(self):
        """Test _handle_batch propagates SubstrateSdkException without translation."""

        class FailingTopic(TestablBaseTopic):
            def do_send_batch(self, messages):
                raise UnknownException("Provider exception")

        topic = FailingTopic(topic_name="test")
        msg = Message.builder().with_body(b"test").build()

        with pytest.raises(UnknownException, match="Provider exception"):
            topic._handle_batch([msg])

        topic.close()

    def test_handle_batch_propagates_provider_exception(self):
        """Test _handle_batch propagates provider exceptions unchanged."""

        class FailingTopic(TestablBaseTopic):
            def do_send_batch(self, messages):
                raise RuntimeError("Runtime error")

        topic = FailingTopic(topic_name="test")
        msg = Message.builder().with_body(b"test").build()

        # Driver layer lets exceptions propagate - client layer does translation
        with pytest.raises(RuntimeError, match="Runtime error"):
            topic._handle_batch([msg])

        topic.close()


class TestBaseTopicRemainingPaths:
    """Test remaining BaseTopic code paths."""

    def test_send_batch_propagates_provider_exception(self):
        """Test _send_batch propagates provider exceptions to client layer for translation."""

        class FailingBatcherTopic(TestablBaseTopic):
            def do_send_batch(self, messages):
                raise ValueError("Batcher error")

        topic = FailingBatcherTopic(topic_name="test")
        msg = Message.builder().with_body(b"test").build()

        # Provider exceptions propagate unchanged - client layer handles translation
        with pytest.raises(ValueError, match="Batcher error"):
            topic.send(msg)

        topic.close()


class TestTopicForThreadSafety(BaseTopic):
    """Test topic implementation for thread safety tests."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent_messages = []

    def get_provider_id(self) -> str:
        return "test-thread"

    def do_send_batch(self, messages):
        time.sleep(0.01)
        self.sent_messages.extend(messages)

    def translate_error(self, exc):
        from multicloud.common.exceptions import UnknownException

        return UnknownException(str(exc))

    def get_max_message_size_bytes(self) -> int:
        """Return message size limit for testing."""
        return 256 * 1024  # 256 KB


class TestThreadSafeClose:
    """Test thread-safe close() behavior."""

    def test_concurrent_send_and_close(self):
        """Test that close() blocks concurrent sends safely."""
        topic = TestTopicForThreadSafety(topic_name="test-topic")
        results = {"send_succeeded": 0, "send_failed": 0}

        def send_messages():
            for _ in range(5):
                try:
                    msg = Message.builder().with_body(b"test").build()
                    topic.send(msg)
                    results["send_succeeded"] += 1
                except FailedPreconditionException:
                    results["send_failed"] += 1
                time.sleep(0.005)

        sender = threading.Thread(target=send_messages)
        sender.start()

        time.sleep(0.02)
        topic.close()

        sender.join()

        assert results["send_succeeded"] + results["send_failed"] == 5
        assert results["send_failed"] > 0

    def test_double_close_is_idempotent(self):
        """Test that close() can be called multiple times safely."""
        topic = TestTopicForThreadSafety(topic_name="test-topic")

        topic.close()
        assert topic.is_shutdown

        topic.close()
        assert topic.is_shutdown


class TestMetadataValidation:
    """Test metadata key validation in BaseTopic."""

    def test_validate_metadata_no_reserved_keys(self):
        """Test that messages without reserved keys pass validation."""
        topic = TestablBaseTopic(topic_name="test-topic")
        message = (
            Message.builder().with_body(b"test").with_metadata({"key": "value"}).build()
        )

        # Should not raise
        topic._validate_metadata_keys(message)

    def test_validate_metadata_reserved_keys_rejected(self):
        """Test that messages with reserved keys are rejected."""

        class TopicWithReservedKeys(TestablBaseTopic):
            def _get_reserved_metadata_keys(self) -> set:
                return {"reserved1", "reserved2"}

        topic = TopicWithReservedKeys(topic_name="test-topic")
        message = (
            Message.builder()
            .with_body(b"test")
            .with_metadata({"reserved1": "x"})
            .build()
        )

        with pytest.raises(InvalidArgumentException, match="reserved keys"):
            topic._validate_metadata_keys(message)

    def test_validate_metadata_multiple_collisions(self):
        """Test error message lists all colliding keys."""

        class TopicWithReservedKeys(TestablBaseTopic):
            def _get_reserved_metadata_keys(self) -> set:
                return {"a", "b", "c"}

        topic = TopicWithReservedKeys(topic_name="test-topic")
        message = (
            Message.builder()
            .with_body(b"test")
            .with_metadata({"a": "1", "b": "2", "ok": "3"})
            .build()
        )

        with pytest.raises(InvalidArgumentException, match="a, b"):
            topic._validate_metadata_keys(message)

    def test_validate_metadata_no_metadata(self):
        """Test that messages without metadata pass validation."""

        class TopicWithReservedKeys(TestablBaseTopic):
            def _get_reserved_metadata_keys(self) -> set:
                return {"reserved"}

        topic = TopicWithReservedKeys(topic_name="test-topic")
        message = Message.builder().with_body(b"test").build()

        # Should not raise
        topic._validate_metadata_keys(message)

    def test_get_reserved_metadata_keys_default_empty(self):
        """Test that default implementation returns empty set."""
        topic = TestablBaseTopic(topic_name="test-topic")
        assert topic._get_reserved_metadata_keys() == set()

    def test_send_validates_metadata_keys(self):
        """Test that send() validates metadata keys."""

        class TopicWithReservedKeys(TestablBaseTopic):
            def _get_reserved_metadata_keys(self) -> set:
                return {"forbidden"}

        topic = TopicWithReservedKeys(topic_name="test-topic")
        message = (
            Message.builder()
            .with_body(b"test")
            .with_metadata({"forbidden": "x"})
            .build()
        )

        with pytest.raises(InvalidArgumentException, match="reserved keys"):
            topic.send(message)
