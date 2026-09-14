"""Unit tests for AWS SQS topic implementation."""

from unittest.mock import ANY, Mock, patch

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceNotFoundException,
    SubstrateSdkException,
    UnAuthorizedException,
)
from multicloud.pubsub.pubsub_aws.aws_base_topic import (
    Base64EncodingStrategy,
    TopicOptions,
)
from multicloud.pubsub.pubsub_aws.aws_sqs_topic import AwsSqsTopic
from multicloud.pubsub.pubsub_client.driver import Message


@pytest.fixture
def mock_sqs_client():
    """Create a mock boto3 SQS client."""
    client = Mock()
    # Mock get_queue_url response
    client.get_queue_url.return_value = {
        "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
    }
    return client


@pytest.fixture
def aws_sqs_topic(mock_sqs_client):
    """Create an AwsSqsTopic instance with mocked SQS client."""
    topic = AwsSqsTopic(
        topic_name="test-queue",
        region="us-east-1",
    )
    # Inject the mock client
    topic._client = mock_sqs_client
    topic._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
    return topic


class TestAwsSqsTopicBasics:
    """Test basic AWS topic functionality."""

    def test_provider_id(self, aws_sqs_topic):
        """Test that provider ID is correct."""
        assert aws_sqs_topic.get_provider_id() == "awssqs"

    def test_batcher_options(self, aws_sqs_topic):
        """Test that batcher options match AWS SQS limits."""
        options = aws_sqs_topic.create_batcher_options()
        assert options.max_handlers == 100
        assert options.min_batch_size == 1
        assert options.max_batch_size == 10  # AWS SQS limit
        assert options.max_batch_byte_size == 256 * 1024  # 256 KB

    def test_constructor_requires_topic_name(self):
        """Test that topic_name is required."""
        with pytest.raises(InvalidArgumentException, match="topic_name is required"):
            AwsSqsTopic(topic_name="", region="us-east-1")


class TestAwsSqsTopicSend:
    """Test message sending functionality."""

    def test_send_rejects_body_at_limit_when_metadata_exceeds_cap(
        self, aws_sqs_topic, mock_sqs_client
    ):
        """Body at the 256KB cap plus metadata fails as a message, not a batch item."""
        max_size = aws_sqs_topic.get_max_message_size_bytes()
        options = aws_sqs_topic.create_batcher_options()
        assert max_size == options.max_batch_byte_size

        message = Message(body=b"x" * max_size, metadata={"k": "v"})
        assert message.get_byte_size() > max_size

        try:
            with pytest.raises(
                InvalidArgumentException,
                match=r"Message size \(\d+ bytes\) exceeds maximum allowed size",
            ) as exc_info:
                aws_sqs_topic.send(message)
            assert "batch" not in str(exc_info.value).lower()
            assert not mock_sqs_client.send_message_batch.called
        finally:
            aws_sqs_topic.close()

    def test_send_simple_message(self, aws_sqs_topic, mock_sqs_client):
        """Test sending a simple UTF-8 message."""
        mock_sqs_client.send_message_batch.return_value = {
            "Successful": [],
            "Failed": [],
        }

        message = Message.builder().with_body(b"test message").build()
        messages = [message]

        # Should not raise
        aws_sqs_topic.do_send_batch(messages)

        # Verify send_message_batch was called
        assert mock_sqs_client.send_message_batch.called
        call_args = mock_sqs_client.send_message_batch.call_args
        assert call_args[1]["QueueUrl"] == aws_sqs_topic._queue_url
        assert len(call_args[1]["Entries"]) == 1
        assert call_args[1]["Entries"][0]["MessageBody"] == "test message"

    def test_send_with_metadata(self, aws_sqs_topic, mock_sqs_client):
        """Test sending message with metadata attributes."""
        mock_sqs_client.send_message_batch.return_value = {
            "Successful": [],
            "Failed": [],
        }

        metadata = {
            "key1": "value1",
            "key2": "value2",
            "DeduplicationId": "dedup-123",
            "MessageGroupId": "group-456",
        }
        message = Message.builder().with_body(b"test").with_metadata(metadata).build()

        aws_sqs_topic.do_send_batch([message])

        call_args = mock_sqs_client.send_message_batch.call_args
        entry = call_args[1]["Entries"][0]
        assert entry["MessageDeduplicationId"] == "dedup-123"
        assert entry["MessageGroupId"] == "group-456"
        assert "MessageAttributes" in entry

    def test_send_binary_message_base64_encoding(self, aws_sqs_topic, mock_sqs_client):
        """Test that binary content is base64-encoded."""
        mock_sqs_client.send_message_batch.return_value = {
            "Successful": [],
            "Failed": [],
        }

        # Non-UTF-8 binary data
        binary_data = b"\x80\x81\x82\x83"
        message = Message.builder().with_body(binary_data).build()

        aws_sqs_topic.do_send_batch([message])

        call_args = mock_sqs_client.send_message_batch.call_args
        entry = call_args[1]["Entries"][0]
        # Body should be base64-encoded
        assert "MessageAttributes" in entry
        assert "base64encoded" in entry["MessageAttributes"]

    def test_send_utf8_message_base64_with_always_strategy(self, mock_sqs_client):
        """TopicOptions ALWAYS strategy should force base64 encoding."""
        mock_sqs_client.send_message_batch.return_value = {
            "Successful": [],
            "Failed": [],
        }
        topic = AwsSqsTopic(
            topic_name="test-queue",
            region="us-east-1",
            topic_options=TopicOptions(
                body_base64_encoding=Base64EncodingStrategy.ALWAYS
            ),
        )
        topic._client = mock_sqs_client
        topic._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        message = Message.builder().with_body(b"test message").build()
        topic.do_send_batch([message])

        call_args = mock_sqs_client.send_message_batch.call_args
        entry = call_args[1]["Entries"][0]
        assert "base64encoded" in entry["MessageAttributes"]

    def test_send_batch_failure(self, aws_sqs_topic, mock_sqs_client):
        """Test handling of batch send failures."""
        mock_sqs_client.send_message_batch.return_value = {
            "Successful": [],
            "Failed": [
                {
                    "Id": "0",
                    "Code": "InvalidMessageContents",
                    "Message": "Message content is invalid",
                }
            ],
        }

        message = Message.builder().with_body(b"test").build()

        with pytest.raises(SubstrateSdkException, match="SQS SendMessageBatch failed"):
            aws_sqs_topic.do_send_batch([message])

    def test_send_empty_batch(self, aws_sqs_topic, mock_sqs_client):
        """Test that sending empty batch is a no-op."""
        aws_sqs_topic.do_send_batch([])
        assert not mock_sqs_client.send_message_batch.called


class TestAwsSqsTopicErrorTranslation:
    """Test exception translation for AWS errors."""

    def test_translate_queue_does_not_exist(self, aws_sqs_topic):
        """Test translation of QueueDoesNotExist error."""
        error = ClientError(
            {"Error": {"Code": "QueueDoesNotExist", "Message": "Queue not found"}},
            "SendMessage",
        )
        translated = aws_sqs_topic.translate_error(error)
        assert isinstance(translated, ResourceNotFoundException)

    def test_translate_invalid_credentials(self, aws_sqs_topic):
        """Test translation of credential errors."""
        error = ClientError(
            {"Error": {"Code": "InvalidClientTokenId", "Message": "Invalid token"}},
            "SendMessage",
        )
        translated = aws_sqs_topic.translate_error(error)
        assert isinstance(translated, UnAuthorizedException)

    def test_translate_no_credentials_error(self, aws_sqs_topic):
        """Test translation of NoCredentialsError."""
        error = NoCredentialsError()
        translated = aws_sqs_topic.translate_error(error)
        assert isinstance(translated, UnAuthorizedException)

    def test_translate_already_substrate_exception(self, aws_sqs_topic):
        """Test that SubstrateSdkException is returned unchanged."""
        original = InvalidArgumentException("test")
        translated = aws_sqs_topic.translate_error(original)
        assert translated is original

    def test_translate_generic_exception(self, aws_sqs_topic):
        """Test translation of unknown exceptions."""
        error = RuntimeError("Unknown error")
        translated = aws_sqs_topic.translate_error(error)
        assert isinstance(translated, SubstrateSdkException)


class TestAwsSqsTopicMetadataEncoding:
    """Test metadata key encoding for SQS compliance."""

    def test_encode_metadata_key_alphanumeric(self):
        """Test that alphanumeric keys pass through."""
        encoded = AwsSqsTopic._encode_metadata_key("abc123")
        assert encoded == "abc123"

    def test_encode_metadata_key_underscore_hyphen(self):
        """Test that underscore and hyphen are allowed."""
        encoded = AwsSqsTopic._encode_metadata_key("my_key-name")
        assert encoded == "my_key-name"

    def test_encode_metadata_key_period_middle(self):
        """Test that period in middle is allowed."""
        encoded = AwsSqsTopic._encode_metadata_key("my.key")
        assert encoded == "my.key"

    def test_encode_metadata_key_period_start(self):
        """Test that period at start is encoded."""
        encoded = AwsSqsTopic._encode_metadata_key(".key")
        assert "__0x2E__" in encoded

    def test_encode_metadata_key_special_chars(self):
        """Test that special characters are hex-encoded."""
        encoded = AwsSqsTopic._encode_metadata_key("key:value")
        assert "__0x3A__" in encoded  # Colon

    def test_encode_metadata_key_space(self):
        """Test that spaces are hex-encoded."""
        encoded = AwsSqsTopic._encode_metadata_key("my key")
        assert "__0x20__" in encoded


class TestAwsSqsTopicQueueUrl:
    """Test queue URL resolution."""

    def test_queue_url_from_name(self):
        """Test resolving queue name to URL."""
        with patch(
            "multicloud.pubsub.pubsub_aws.aws_sqs_topic.get_session"
        ) as mock_get_session:
            mock_session = Mock()
            mock_client = Mock()
            mock_client.get_queue_url.return_value = {
                "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue"
            }
            mock_session.client.return_value = mock_client
            mock_get_session.return_value = mock_session

            topic = AwsSqsTopic(topic_name="my-queue", region="us-east-1")
            url = topic._get_queue_url()

            assert url == "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue"

    def test_queue_url_direct_https(self):
        """Test that HTTPS URLs are used directly."""
        topic = AwsSqsTopic(
            topic_name="https://sqs.us-east-1.amazonaws.com/123456789012/my-queue",
            region="us-east-1",
        )
        topic._client = Mock()  # Prevent lazy init

        url = topic._get_queue_url()
        assert url == "https://sqs.us-east-1.amazonaws.com/123456789012/my-queue"
        # Should not call get_queue_url
        assert not topic._client.get_queue_url.called
