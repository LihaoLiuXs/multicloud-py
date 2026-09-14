"""Additional exception handling tests for AWS providers to improve coverage."""

from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from multicloud.common.exceptions import SubstrateSdkException
from multicloud.pubsub.pubsub_aws.aws_sqs_topic import AwsSqsTopic
from multicloud.pubsub.pubsub_aws.aws_subscription import AwsSubscription
from multicloud.pubsub.pubsub_client.driver import Message


@pytest.fixture
def mock_sqs_client():
    """Create a mock boto3 SQS client."""
    client = Mock()
    client.get_queue_url.return_value = {
        "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
    }
    return client


class TestAwsTopicExceptionPaths:
    """Test exception handling paths in AWS topic."""

    def test_send_generic_exception(self, mock_sqs_client):
        """Test that generic exceptions propagate (translation happens in driver layer)."""
        topic = AwsSqsTopic(topic_name="test-queue", region="us-east-1")
        topic._client = mock_sqs_client
        topic._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        # Mock to raise generic exception
        mock_sqs_client.send_message_batch.side_effect = RuntimeError("Network error")

        message = Message.builder().with_body(b"test").build()

        # Provider lets native exceptions propagate; driver translates them
        with pytest.raises(RuntimeError, match="Network error"):
            topic.do_send_batch([message])

    def test_queue_url_resolution_failure(self):
        """Test handling queue URL resolution failure.

        Provider layer lets native boto3 exceptions propagate up.
        Translation happens at the client layer.
        """
        mock_client = Mock()
        mock_client.get_queue_url.side_effect = ClientError(
            {"Error": {"Code": "QueueDoesNotExist", "Message": "Queue not found"}},
            "GetQueueUrl",
        )

        topic = AwsSqsTopic(topic_name="nonexistent-queue", region="us-east-1")
        topic._client = mock_client

        with pytest.raises(ClientError, match="QueueDoesNotExist"):
            topic._get_queue_url()

    def test_empty_body_encoding(self):
        """Test encoding message with empty body."""
        topic = AwsSqsTopic(topic_name="test-queue", region="us-east-1")
        message = Message.builder().with_body(b"").build()

        body, is_base64 = topic._encode_message_body(message)
        assert body == ""
        assert is_base64 is False

    def test_empty_key_encoding(self):
        """Test encoding empty metadata key."""
        encoded = AwsSqsTopic._encode_metadata_key("")
        assert encoded == ""

    def test_max_attributes_limit(self, mock_sqs_client):
        """Test that attributes are limited to 10 (SQS max)."""
        topic = AwsSqsTopic(topic_name="test-queue", region="us-east-1")
        topic._client = mock_sqs_client
        topic._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        mock_sqs_client.send_message_batch.return_value = {
            "Successful": [],
            "Failed": [],
        }

        # Create metadata with more than 10 attributes
        metadata = {f"key{i}": f"value{i}" for i in range(15)}
        message = Message.builder().with_body(b"test").with_metadata(metadata).build()

        topic.do_send_batch([message])

        call_args = mock_sqs_client.send_message_batch.call_args
        entry = call_args[1]["Entries"][0]
        # Should have at most 10 attributes (excluding base64encoded)
        assert len(entry["MessageAttributes"]) <= 10

    def test_close_method(self):
        """Test that close() method sets _client to None."""
        topic = AwsSqsTopic(topic_name="test-queue", region="us-east-1")
        topic._client = Mock()

        topic.close()

        assert topic._client is None


class TestAwsSubscriptionExceptionPaths:
    """Test exception handling paths in AWS subscription."""

    def test_receive_client_error(self, mock_sqs_client):
        """Test handling ClientError during receive - provider layer lets it propagate."""
        sub = AwsSubscription(subscription_name="test-queue", region="us-east-1")
        sub._client = mock_sqs_client
        sub._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        mock_sqs_client.receive_message.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access denied"}},
            "ReceiveMessage",
        )

        # Provider layer (do_*) lets native exceptions propagate
        # Translation happens at client layer
        with pytest.raises(ClientError):
            sub.do_receive_batch(5)

    def test_receive_generic_exception(self, mock_sqs_client):
        """Test handling generic exception during receive - provider layer lets it propagate."""
        sub = AwsSubscription(subscription_name="test-queue", region="us-east-1")
        sub._client = mock_sqs_client
        sub._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        mock_sqs_client.receive_message.side_effect = RuntimeError("Network error")

        # Provider layer (do_*) lets native exceptions propagate
        # Translation happens at client layer
        with pytest.raises(RuntimeError):
            sub.do_receive_batch(5)

    def test_ack_client_error(self, mock_sqs_client):
        """Test ClientError propagates from provider ack path."""
        sub = AwsSubscription(subscription_name="test-queue", region="us-east-1")
        sub._client = mock_sqs_client
        sub._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        from multicloud.pubsub.pubsub_aws.aws_subscription import AwsAckID

        mock_sqs_client.delete_message_batch.side_effect = ClientError(
            {"Error": {"Code": "InvalidParameterValue", "Message": "Invalid"}},
            "DeleteMessageBatch",
        )

        with pytest.raises(ClientError):
            sub.do_send_acks([AwsAckID("receipt-123")])

    def test_get_attributes_exception(self, mock_sqs_client):
        """Test ClientError propagates from provider get_attributes path."""
        sub = AwsSubscription(subscription_name="test-queue", region="us-east-1")
        sub._client = mock_sqs_client
        sub._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        mock_sqs_client.get_queue_attributes.side_effect = ClientError(
            {"Error": {"Code": "QueueDoesNotExist", "Message": "Queue not found"}},
            "GetQueueAttributes",
        )

        with pytest.raises(ClientError):
            sub.get_attributes()

    def test_base64_decode_fallback(self, mock_sqs_client):
        """Test fallback when base64 decode fails."""
        sub = AwsSubscription(subscription_name="test-queue", region="us-east-1")
        sub._client = mock_sqs_client
        sub._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        # Message marked as base64 but with invalid base64 content
        mock_sqs_client.receive_message.return_value = {
            "Messages": [
                {
                    "MessageId": "msg-123",
                    "ReceiptHandle": "receipt-123",
                    "Body": "not-valid-base64!!!",
                    "MessageAttributes": {
                        "base64encoded": {"StringValue": "true"},
                    },
                }
            ]
        }

        messages = sub.do_receive_batch(1)

        # Should fall back to UTF-8 encoding
        assert len(messages) == 1
        assert messages[0].body == b"not-valid-base64!!!"

    def test_sns_extraction_none_return(self, mock_sqs_client):
        """Test SNS message extraction returning None for non-SNS messages."""
        sub = AwsSubscription(subscription_name="test-queue", region="us-east-1")
        sub._client = mock_sqs_client
        sub._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        # Plain SQS message (not SNS-wrapped)
        mock_sqs_client.receive_message.return_value = {
            "Messages": [
                {
                    "MessageId": "msg-123",
                    "ReceiptHandle": "receipt-123",
                    "Body": "plain message",
                    "MessageAttributes": {},
                }
            ]
        }

        messages = sub.do_receive_batch(1)

        assert len(messages) == 1
        assert messages[0].body == b"plain message"

    def test_close_method(self):
        """Test that close() method sets _client to None."""
        sub = AwsSubscription(subscription_name="test-queue", region="us-east-1")
        sub._client = Mock()

        sub.close()

        assert sub._client is None
