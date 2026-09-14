"""Unit tests for AWS base topic shared functionality."""

from typing import List

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
    UnAuthorizedException,
    UnknownException,
)
from multicloud.pubsub.pubsub_aws.aws_base_topic import (
    AwsBaseTopic,
    Base64EncodingStrategy,
    TopicOptions,
)
from multicloud.pubsub.pubsub_client.driver import Message


class TestAwsBaseTopic(AwsBaseTopic):
    """Concrete test implementation of AwsBaseTopic for unit testing."""

    def do_send_batch(self, messages: List[Message]) -> None:
        """Stub implementation for testing."""
        pass

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Stub implementation for testing."""
        return UnknownException(str(exc))

    def get_provider_id(self) -> str:
        """Return test provider ID."""
        return "test"

    def get_max_message_size_bytes(self) -> int:
        """Return AWS message size limit for testing."""
        return 256 * 1024  # 256 KB


class TestAwsBaseTopicEncoding:
    """Test shared AWS base topic functionality (encoding, UTF-8 validation, exception mapping)."""

    def test_encode_metadata_key_valid_characters(self):
        """Test encoding of valid alphanumeric, underscore, hyphen, and period characters."""
        assert TestAwsBaseTopic._encode_metadata_key("simpleKey") == "simpleKey"
        assert (
            TestAwsBaseTopic._encode_metadata_key("key_with_underscore")
            == "key_with_underscore"
        )
        assert TestAwsBaseTopic._encode_metadata_key("key-with-dash") == "key-with-dash"
        assert TestAwsBaseTopic._encode_metadata_key("key.name") == "key.name"

    def test_encode_metadata_key_invalid_characters(self):
        """Test encoding of special characters to hex format."""
        assert TestAwsBaseTopic._encode_metadata_key("file/path") == "file__0x2F__path"
        assert TestAwsBaseTopic._encode_metadata_key("user name") == "user__0x20__name"
        assert (
            TestAwsBaseTopic._encode_metadata_key("type:message")
            == "type__0x3A__message"
        )

    def test_encode_metadata_key_period_at_start(self):
        """Test encoding of period at the start of key."""
        assert TestAwsBaseTopic._encode_metadata_key(".key") == "__0x2E__key"

    def test_encode_metadata_key_consecutive_periods(self):
        """Test encoding of consecutive periods."""
        assert (
            TestAwsBaseTopic._encode_metadata_key("key..name")
            == "key__0x2E____0x2E__name"
        )

    def test_encode_metadata_key_edge_cases(self):
        """Test encoding of null and empty keys."""
        assert TestAwsBaseTopic._encode_metadata_key("") == ""
        assert TestAwsBaseTopic._encode_metadata_key(None) == ""

    def test_encode_metadata_value(self):
        """Test URL encoding of metadata values."""
        assert TestAwsBaseTopic._encode_metadata_value("simpleValue") == "simpleValue"
        assert (
            TestAwsBaseTopic._encode_metadata_value("value with spaces")
            == "value%20with%20spaces"
        )
        # Note: Python quote() does not encode '/' by default (safe char)
        assert (
            TestAwsBaseTopic._encode_metadata_value("value/with/slash")
            == "value/with/slash"
        )

    def test_encode_metadata_value_edge_cases(self):
        """Test encoding of null and empty values."""
        assert TestAwsBaseTopic._encode_metadata_value("") == ""
        assert TestAwsBaseTopic._encode_metadata_value(None) == ""

    def test_is_valid_utf8_valid_cases(self):
        """Test UTF-8 validation for valid inputs."""
        assert TestAwsBaseTopic._is_valid_utf8(b"Hello World") is True
        assert TestAwsBaseTopic._is_valid_utf8("测试中文".encode("utf-8")) is True
        assert TestAwsBaseTopic._is_valid_utf8(b"") is True

    def test_is_valid_utf8_invalid_cases(self):
        """Test UTF-8 validation for invalid byte sequences."""
        invalid_utf8 = bytes([0xFF, 0xFE])
        assert TestAwsBaseTopic._is_valid_utf8(invalid_utf8) is False

    def test_should_encode_body_always_strategy(self):
        """Test ALWAYS encoding strategy encodes all bodies."""
        topic_options = TopicOptions(body_base64_encoding=Base64EncodingStrategy.ALWAYS)
        topic = TestAwsBaseTopic(topic_name="test", topic_options=topic_options)

        valid_utf8 = b"test"
        assert topic._should_encode_body(valid_utf8) is True

    def test_should_encode_body_never_strategy(self):
        """Test NEVER encoding strategy never encodes."""
        topic_options = TopicOptions(body_base64_encoding=Base64EncodingStrategy.NEVER)
        topic = TestAwsBaseTopic(topic_name="test", topic_options=topic_options)

        valid_utf8 = b"test"
        invalid_utf8 = bytes([0xFF, 0xFE])
        assert topic._should_encode_body(valid_utf8) is False
        assert topic._should_encode_body(invalid_utf8) is False

    def test_should_encode_body_auto_strategy(self):
        """Test AUTO encoding strategy encodes only invalid UTF-8."""
        topic_options = TopicOptions(body_base64_encoding=Base64EncodingStrategy.AUTO)
        topic = TestAwsBaseTopic(topic_name="test", topic_options=topic_options)

        valid_utf8 = b"test"
        invalid_utf8 = bytes([0xFF, 0xFE])
        assert topic._should_encode_body(valid_utf8) is False
        assert topic._should_encode_body(invalid_utf8) is True

    def test_should_encode_body_empty_or_none(self):
        """Test encoding strategy with empty or None body."""
        topic = TestAwsBaseTopic(topic_name="test")
        assert topic._should_encode_body(b"") is False
        assert topic._should_encode_body(None) is False
