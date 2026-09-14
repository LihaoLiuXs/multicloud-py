"""
Unit tests for driver data structures (Message and MessageValidator).
"""

import pytest
from multicloud.pubsub.pubsub_client.driver import Message


class TestMessageAsType:
    """Test Message.as_type method edge cases."""

    def test_as_type_with_no_as_func_returns_false(self):
        """Test Message.as_type returns False when as_func is None."""
        msg = Message.builder().with_body(b"test").build()
        # as_func is None by default
        result = msg.as_type("some-target")
        assert result is False

    def test_as_type_with_as_func_calls_function(self):
        """Test Message.as_type calls as_func when provided."""
        call_count = []

        def mock_as_func(target):
            call_count.append(target)
            return True

        msg = Message(body=b"test", as_func=mock_as_func)
        result = msg.as_type("my-target")

        assert result is True
        assert call_count == ["my-target"]


class TestMessageByteSize:
    """Test Message byte-size calculation used by batching."""

    def test_get_byte_size_includes_body_and_metadata(self):
        """Message byte size should include body and metadata key/value bytes."""
        msg = (
            Message.builder()
            .with_body("abc")
            .with_metadata({"k1": "v1", "k2": "value2"})
            .build()
        )

        expected = len(b"abc") + len("k1v1k2value2".encode("utf-8"))
        assert msg.get_byte_size() == expected

    def test_get_byte_size_treats_missing_metadata_as_empty(self):
        """metadata=None is valid and must not crash size calculation."""
        msg = Message(body=b"abc", metadata=None)  # type: ignore[arg-type]
        assert msg.get_byte_size() == 3


class TestMessageBuilderEdgeCases:
    """Test Message.Builder edge cases."""

    def test_builder_with_empty_body_string(self):
        """Test building message with empty string body."""
        msg = Message.builder().with_body("").build()
        assert msg.body == b""

    def test_builder_with_invalid_metadata_type_raises(self):
        """Test with_metadata raises TypeError for invalid type."""
        with pytest.raises(TypeError, match="metadata must be dict or key string"):
            Message.builder().with_body(b"test").with_metadata(123).build()

    def test_builder_metadata_dict_overwrites_previous(self):
        """Test with_metadata(dict) overwrites previous metadata."""
        msg = (
            Message.builder()
            .with_body(b"test")
            .with_metadata("k1", "v1")
            .with_metadata({"k2": "v2"})  # This overwrites k1
            .build()
        )
        assert msg.metadata == {"k2": "v2"}
        assert "k1" not in msg.metadata

    def test_builder_with_string_body_converts_to_bytes(self):
        """Test builder converts string body to bytes."""
        msg = Message.builder().with_body("hello").build()
        assert msg.body == b"hello"
        assert isinstance(msg.body, bytes)

    def test_builder_metadata_with_none_value_uses_empty_string(self):
        """Test with_metadata(key, None) sets empty string value."""
        msg = Message.builder().with_body(b"test").with_metadata("key1", None).build()
        assert msg.metadata == {"key1": ""}

    def test_builder_multiple_string_metadata_accumulates(self):
        """Test multiple with_metadata(key, val) calls accumulate."""
        msg = (
            Message.builder()
            .with_body(b"test")
            .with_metadata("k1", "v1")
            .with_metadata("k2", "v2")
            .with_metadata("k3", "v3")
            .build()
        )
        assert msg.metadata == {"k1": "v1", "k2": "v2", "k3": "v3"}

    def test_builder_returns_same_message_instance(self):
        """Test build() returns the configured mutable message instance."""
        builder = Message.builder().with_body(b"test").with_metadata("k1", "v1")
        msg1 = builder.build()

        # Mutate the built message's metadata
        msg1.metadata["k2"] = "v2"

        # Build again - should reflect the same mutable message instance
        msg2 = builder.build()
        assert msg2 is msg1
        assert msg2.metadata == {"k1": "v1", "k2": "v2"}


"""
Additional Message.Builder tests for coverage.
"""
import pytest
from multicloud.pubsub.pubsub_client.driver import Message


class TestMessageBuilderCoverage:
    """Additional builder tests for coverage."""

    def test_builder_with_invalid_body_type_raises(self):
        """Test builder rejects invalid body type."""
        with pytest.raises(TypeError, match="body must be bytes or str"):
            Message.builder().with_body(123).build()

    def test_builder_with_loggable_id(self):
        """Test builder with loggable_id."""
        msg = Message.builder().with_body(b"test").with_loggable_id("log-123").build()
        assert msg.loggable_id == "log-123"
        assert msg.body == b"test"

    def test_builder_with_ack_id(self):
        """Test builder with ack_id."""

        class TestAckID:
            pass

        ack = TestAckID()
        msg = Message.builder().with_body(b"test").with_ack_id(ack).build()
        assert msg.ack_id is ack

    def test_builder_with_as_func(self):
        """Test builder with as_func."""

        def test_func(target):
            return True

        msg = Message.builder().with_body(b"test").with_as_func(test_func).build()
        assert msg.as_func is test_func
        assert msg.as_type("anything") is True

    def test_builder_with_before_send(self):
        """Test builder with before_send hook."""

        def test_hook(as_func):
            return None

        msg = Message.builder().with_body(b"test").with_before_send(test_hook).build()
        assert msg.before_send is test_hook

    def test_builder_with_after_send(self):
        """Test builder with after_send hook."""

        def test_hook(as_func):
            return None

        msg = Message.builder().with_body(b"test").with_after_send(test_hook).build()
        assert msg.after_send is test_hook


class TestMessageBuilderAllMethods:
    """Test all Message.Builder methods."""

    def test_builder_with_loggable_id(self):
        """Test builder with_loggable_id method."""
        msg = Message.builder().with_body(b"test").with_loggable_id("log-123").build()
        assert msg.loggable_id == "log-123"

    def test_builder_with_ack_id(self):
        """Test builder with_ack_id method."""

        class TestAckID:
            def __str__(self):
                return "ack-123"

        ack_id = TestAckID()
        msg = Message.builder().with_body(b"test").with_ack_id(ack_id).build()
        assert msg.ack_id == ack_id

    def test_builder_with_as_func(self):
        """Test builder with_as_func method."""

        def my_func(obj):
            return True

        msg = Message.builder().with_body(b"test").with_as_func(my_func).build()
        assert msg.as_func == my_func

    def test_builder_with_before_send(self):
        """Test builder with_before_send method."""

        def before_hook(obj):
            return None

        msg = Message.builder().with_body(b"test").with_before_send(before_hook).build()
        assert msg.before_send == before_hook

    def test_builder_with_after_send(self):
        """Test builder with_after_send method."""

        def after_hook(obj):
            return None

        msg = Message.builder().with_body(b"test").with_after_send(after_hook).build()
        assert msg.after_send == after_hook

    def test_builder_chain_all_methods(self):
        """Test chaining all builder methods."""

        def my_as_func(obj):
            return True

        def my_before(obj):
            return None

        def my_after(obj):
            return None

        class TestAckID:
            def __str__(self):
                return "ack-123"

        msg = (
            Message.builder()
            .with_body(b"test")
            .with_metadata({"key1": "value1"})
            .with_loggable_id("log-123")
            .with_ack_id(TestAckID())
            .with_as_func(my_as_func)
            .with_before_send(my_before)
            .with_after_send(my_after)
            .build()
        )

        assert msg.body == b"test"
        assert msg.metadata == {"key1": "value1"}
        assert msg.loggable_id == "log-123"
        assert msg.as_func == my_as_func
        assert msg.before_send == my_before
        assert msg.after_send == my_after

    def test_builder_with_metadata_string_key_initializes_dict(self):
        """Test with_metadata string key initializes dict when None."""
        builder = Message.builder()
        builder.with_metadata("key1", "value1")
        msg = builder.build()

        assert msg.metadata == {"key1": "value1"}

    def test_builder_with_metadata_string_key_none_value(self):
        """Test with_metadata string key with None value sets empty string."""
        msg = Message.builder().with_metadata("key1", None).build()
        assert msg.metadata == {"key1": ""}

    def test_builder_with_metadata_invalid_type_raises(self):
        """Test with_metadata with invalid type raises TypeError."""
        builder = Message.builder()
        with pytest.raises(TypeError, match="metadata must be dict or key string"):
            builder.with_metadata(123)


from typing import Dict, List

import pytest
from multicloud.common.exceptions import InvalidArgumentException
from multicloud.pubsub.pubsub_client.driver import Message, MessageValidator


class TestMessageValidator:
    """Test suite for MessageValidator validation and utility methods."""

    def test_validate_message_valid_message(self):
        """Test validation with a valid message."""
        message = (
            Message.builder()
            .with_body("test content")
            .with_metadata("key", "value")
            .build()
        )

        # Should not raise
        MessageValidator.validate_message(message)

    def test_validate_message_null_message(self):
        """Test validation with null message."""
        with pytest.raises(InvalidArgumentException, match="Message cannot be null"):
            MessageValidator.validate_message(None)

    def test_validate_message_empty_body_allowed(self):
        """Test that empty body is allowed."""
        message = (
            Message.builder().with_body(b"").build()
        )  # Empty body should be allowed

        # Should not raise
        MessageValidator.validate_message(message)

    def test_validate_message_loggable_id_not_allowed(self):
        """Test that loggableID is not allowed on outgoing messages."""
        message = (
            Message.builder()
            .with_body("test content")
            .with_loggable_id("some-logging-id")  # This should not be allowed
            .build()
        )

        with pytest.raises(InvalidArgumentException) as exc_info:
            MessageValidator.validate_message(message)
        assert "LoggableID cannot be set on outgoing messages" in str(exc_info.value)
        assert "reserved for received messages" in str(exc_info.value)

    def test_validate_message_valid_metadata(self):
        """Test validation with valid metadata."""
        metadata: Dict[str, str] = {
            "validKey": "validValue",
            "anotherKey": "anotherValue",
        }

        message = Message.builder().with_body("test").with_metadata(metadata).build()

        # Should not raise
        MessageValidator.validate_message(message)

    def test_validate_message_invalid_metadata_key(self):
        """Test validation with invalid metadata key."""
        metadata: Dict[str, str] = {"": "value"}  # Empty key should be invalid

        message = Message.builder().with_body("test").with_metadata(metadata).build()

        with pytest.raises(
            InvalidArgumentException, match="Metadata keys cannot be null or empty"
        ):
            MessageValidator.validate_message(message)

    def test_validate_message_valid_metadata_with_control_characters(self):
        """Test that control characters are allowed in metadata."""
        metadata: Dict[str, str] = {
            "key\twith\ttabs": "value\nwith\nnewlines\rand\rcarriagereturns",
        }

        message = Message.builder().with_body("test").with_metadata(metadata).build()

        # Should not raise since control characters are allowed in UTF-8 validation
        MessageValidator.validate_message(message)

    def test_validate_message_batch_valid_batch(self):
        """Test batch validation with valid batch."""
        messages: List[Message] = [
            Message.builder().with_body("message1").build(),
            Message.builder().with_body("message2").build(),
        ]

        # Should not raise
        MessageValidator.validate_message_batch(messages)

    def test_validate_message_batch_null_list(self):
        """Test batch validation with null list."""
        with pytest.raises(
            InvalidArgumentException, match="Messages list cannot be null"
        ):
            MessageValidator.validate_message_batch(None)

    def test_validate_message_batch_null_message_in_batch(self):
        """Test batch validation with null message in batch."""
        messages: List[Message] = [
            Message.builder().with_body("message1").build(),
            None,  # Null message in the list
            Message.builder().with_body("message3").build(),
        ]

        with pytest.raises(
            InvalidArgumentException, match="Message at index 1 cannot be null"
        ):
            MessageValidator.validate_message_batch(messages)

    def test_validate_message_batch_invalid_message_in_batch(self):
        """Test batch validation with invalid message in batch."""
        invalid_message = (
            Message.builder()
            .with_body("valid body")
            .with_loggable_id("invalid-for-outgoing")  # This makes the message invalid
            .build()
        )

        messages: List[Message] = [
            Message.builder().with_body("message1").build(),
            invalid_message,
        ]

        with pytest.raises(
            InvalidArgumentException,
            match="LoggableID cannot be set on outgoing messages",
        ):
            MessageValidator.validate_message_batch(messages)

    def test_calculate_byte_size_null_message(self):
        """Test byte size calculation with null message."""
        size = MessageValidator.calculate_byte_size(None)

        assert size == 0

    def test_calculate_byte_size_valid_message(self):
        """Test byte size calculation with valid message."""
        body_content = "test message"
        metadata: Dict[str, str] = {
            "key1": "value1",
            "key2": "value2",
        }

        message = (
            Message.builder().with_body(body_content).with_metadata(metadata).build()
        )

        size = MessageValidator.calculate_byte_size(message)

        # Size should only include body size (metadata and protocol overhead not included)
        assert size == len(body_content.encode("utf-8"))

    def test_calculate_byte_size_body_only_message(self):
        """Test byte size calculation with body-only message."""
        body_content = "simple message"
        message = Message.builder().with_body(body_content).build()

        size = MessageValidator.calculate_byte_size(message)

        assert size == len(body_content.encode("utf-8"))

    def test_calculate_byte_size_empty_body(self):
        """Test byte size calculation with empty body."""
        message = Message.builder().with_body(b"").build()

        size = MessageValidator.calculate_byte_size(message)

        assert size == 0


class TestMessageValidatorEdgeCases:
    """Test MessageValidator edge cases."""

    def test_validate_metadata_with_none_key_raises(self):
        """Test metadata validation rejects None keys."""
        with pytest.raises(
            InvalidArgumentException, match="Metadata keys cannot be null or empty"
        ):
            MessageValidator._validate_metadata({None: "value"})

    def test_validate_metadata_with_empty_key_raises(self):
        """Test metadata validation rejects empty keys."""
        with pytest.raises(
            InvalidArgumentException, match="Metadata keys cannot be null or empty"
        ):
            MessageValidator._validate_metadata({"": "value"})

    def test_validate_metadata_with_whitespace_only_key_raises(self):
        """Test metadata validation rejects whitespace-only keys."""
        with pytest.raises(
            InvalidArgumentException, match="Metadata keys cannot be null or empty"
        ):
            MessageValidator._validate_metadata({"   ": "value"})

    def test_validate_metadata_with_invalid_utf8_key_raises(self):
        """Test metadata validation rejects invalid UTF-8 keys."""
        invalid_key = "test\udcff"
        with pytest.raises(
            InvalidArgumentException, match="Metadata key is not valid UTF-8"
        ):
            MessageValidator._validate_metadata({invalid_key: "value"})

    def test_validate_metadata_with_invalid_utf8_value_raises(self):
        """Test metadata validation rejects invalid UTF-8 values."""
        invalid_value = "test\udcff"
        with pytest.raises(
            InvalidArgumentException, match="Metadata value is not valid UTF-8"
        ):
            MessageValidator._validate_metadata({"key": invalid_value})

    def test_validate_metadata_with_none_value_allowed(self):
        """Test metadata validation allows None values."""
        MessageValidator._validate_metadata({"key": None})

    def test_is_valid_utf8_string_with_none_returns_true(self):
        """Test None is considered valid UTF-8."""
        assert MessageValidator._is_valid_utf8_string(None) is True

    def test_validate_message_batch_with_none_list_raises(self):
        """Test batch validation rejects None list."""
        with pytest.raises(InvalidArgumentException, match="list cannot be null"):
            MessageValidator.validate_message_batch(None)

    def test_validate_message_batch_with_none_element_raises(self):
        """Test batch validation rejects None elements."""
        msg1 = Message(body=b"test1")
        msg2 = None
        msg3 = Message(body=b"test3")
        with pytest.raises(InvalidArgumentException, match="index 1 cannot be null"):
            MessageValidator.validate_message_batch([msg1, msg2, msg3])


class TestMessageValidatorRemainingPaths:
    """Test remaining MessageValidator code paths."""

    def test_calculate_byte_size_with_none_body_returns_zero(self):
        """Test calculate_byte_size with None body returns 0 (line 350)."""
        msg = Message()  # No body
        assert MessageValidator.calculate_byte_size(msg) == 0
