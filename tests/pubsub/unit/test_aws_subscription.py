"""Unit tests for AWS SQS subscription implementation."""

import json
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
    StsCredentials,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceNotFoundException,
    SubstrateSdkException,
    UnAuthorizedException,
)
from multicloud.pubsub.pubsub_aws.aws_subscription import (
    AwsAckID,
    AwsSubscription,
    AwsSubscriptionOptions,
    create_aws_subscription,
)
from multicloud.pubsub.pubsub_client.driver import AckInfo


@pytest.fixture
def mock_sqs_client():
    """Create a mock boto3 SQS client."""
    client = Mock()
    client.get_queue_url.return_value = {
        "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
    }
    return client


@pytest.fixture
def aws_subscription(mock_sqs_client):
    """Create an AwsSubscription instance with mocked SQS client."""
    subscription = AwsSubscription(
        subscription_name="test-queue",
        region="us-east-1",
        nack_visibility_timeout=timedelta(seconds=30),
    )
    subscription._client = mock_sqs_client
    subscription._queue_url = (
        "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
    )
    return subscription


class TestAwsAckID:
    """Test AwsAckID wrapper class."""

    def test_create_with_valid_handle(self):
        """Test creating AwsAckID with valid receipt handle."""
        ack_id = AwsAckID("valid-receipt-handle")
        assert str(ack_id) == "valid-receipt-handle"

    def test_create_with_empty_handle(self):
        """Test that empty receipt handle raises error."""
        with pytest.raises(InvalidArgumentException, match="cannot be null or empty"):
            AwsAckID("")

    def test_create_with_whitespace_handle(self):
        """Test that whitespace-only receipt handle raises error."""
        with pytest.raises(InvalidArgumentException, match="cannot be null or empty"):
            AwsAckID("   ")

    def test_repr(self):
        """Test string representation."""
        ack_id = AwsAckID("test-handle")
        assert "AwsAckID" in repr(ack_id)
        assert "test-handle" in repr(ack_id)


class TestAwsSubscriptionBasics:
    """Test basic AWS subscription functionality."""

    def test_provider_id(self, aws_subscription):
        """Test that provider ID is correct."""
        assert aws_subscription.get_provider_id() == "aws"

    def test_can_nack(self, aws_subscription):
        """Test that AWS supports nacking."""
        assert aws_subscription.can_nack() is True

    def test_is_retryable(self, aws_subscription):
        """Test that errors are not retryable by default."""
        error = RuntimeError("test")
        assert aws_subscription.is_retryable(error) is False

    def test_receive_batcher_options(self, aws_subscription):
        """Test receive batcher options (SQS max 10 per request)."""
        options = aws_subscription.create_receive_batcher_options()
        assert options.max_handlers == 100
        assert options.min_batch_size == 1
        assert options.max_batch_size == 10

    def test_ack_batcher_options(self, aws_subscription):
        """Test ack batcher options (SQS max 10 per request)."""
        options = aws_subscription.create_ack_batcher_options()
        assert options.max_handlers == 100
        assert options.min_batch_size == 1
        assert options.max_batch_size == 10

    def test_constructor_wait_time_passthrough(self, mock_sqs_client):
        """Test that wait_time_seconds is passed through unchanged."""
        sub = AwsSubscription(
            subscription_name="test-queue",
            region="us-east-1",
            wait_time_seconds=25,
        )
        sub._client = mock_sqs_client
        sub._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        mock_sqs_client.receive_message.return_value = {"Messages": []}
        sub.do_receive_batch(1)

        call_args = mock_sqs_client.receive_message.call_args
        assert call_args[1]["WaitTimeSeconds"] == 25

    def test_close_calls_client_close(self, mock_sqs_client):
        """Test that close invokes underlying SQS client close."""
        sub = AwsSubscription(subscription_name="test-queue", region="us-east-1")
        sub._client = mock_sqs_client
        sub._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        sub.close()

        assert mock_sqs_client.close.called

    def test_create_aws_subscription_with_options(self, mock_sqs_client):
        """Test provider-specific factory restores AWS-only option ergonomics."""
        sub = create_aws_subscription(
            subscription_name="test-queue",
            region="us-east-1",
            options=AwsSubscriptionOptions(
                wait_time_seconds=10,
                nack_lazy=True,
                raw_message_delivery=True,
            ),
        )
        sub._client = mock_sqs_client
        sub._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        mock_sqs_client.receive_message.return_value = {"Messages": []}
        sub.do_receive_batch(1)

        call_args = mock_sqs_client.receive_message.call_args
        assert call_args[1]["WaitTimeSeconds"] == 10
        assert sub._nack_lazy is True
        assert sub._raw is True


class TestAwsSubscriptionReceive:
    """Test message receiving functionality."""

    def test_receive_empty_queue(self, aws_subscription, mock_sqs_client):
        """Test receiving from empty queue."""
        mock_sqs_client.receive_message.return_value = {"Messages": []}

        messages = aws_subscription.do_receive_batch(5)

        assert messages == []
        assert mock_sqs_client.receive_message.called

    def test_receive_simple_message(self, aws_subscription, mock_sqs_client):
        """Test receiving a simple message."""
        mock_sqs_client.receive_message.return_value = {
            "Messages": [
                {
                    "MessageId": "msg-123",
                    "ReceiptHandle": "receipt-123",
                    "Body": "test message",
                    "MessageAttributes": {},
                }
            ]
        }

        messages = aws_subscription.do_receive_batch(1)

        assert len(messages) == 1
        assert messages[0].body == b"test message"
        assert messages[0].loggable_id == "msg-123"
        assert str(messages[0].ack_id) == "receipt-123"

    def test_receive_with_attributes(self, aws_subscription, mock_sqs_client):
        """Test receiving message with attributes."""
        mock_sqs_client.receive_message.return_value = {
            "Messages": [
                {
                    "MessageId": "msg-123",
                    "ReceiptHandle": "receipt-123",
                    "Body": "test",
                    "MessageAttributes": {
                        "key1": {"StringValue": "value1"},
                        "key2": {"StringValue": "value2"},
                    },
                }
            ]
        }

        messages = aws_subscription.do_receive_batch(1)

        assert len(messages) == 1
        assert messages[0].metadata["key1"] == "value1"
        assert messages[0].metadata["key2"] == "value2"

    def test_receive_base64_encoded(self, aws_subscription, mock_sqs_client):
        """Test receiving base64-encoded message."""
        import base64

        original_data = b"\x80\x81\x82\x83"
        encoded = base64.b64encode(original_data).decode("ascii")

        mock_sqs_client.receive_message.return_value = {
            "Messages": [
                {
                    "MessageId": "msg-123",
                    "ReceiptHandle": "receipt-123",
                    "Body": encoded,
                    "MessageAttributes": {
                        "base64encoded": {"StringValue": "true"},
                    },
                }
            ]
        }

        messages = aws_subscription.do_receive_batch(1)

        assert len(messages) == 1
        assert messages[0].body == original_data

    def test_receive_sns_wrapped_message(self, aws_subscription, mock_sqs_client):
        """Test receiving SNS-wrapped message."""
        sns_body = {
            "Type": "Notification",
            "TopicArn": "arn:aws:sns:us-west-2:123456789012:test-topic",
            "Message": "actual message",
            "MessageAttributes": {
                "attr1": {"Value": "value1"},
            },
        }

        mock_sqs_client.receive_message.return_value = {
            "Messages": [
                {
                    "MessageId": "msg-123",
                    "ReceiptHandle": "receipt-123",
                    "Body": json.dumps(sns_body),
                    "MessageAttributes": {},
                }
            ]
        }

        messages = aws_subscription.do_receive_batch(1)

        assert len(messages) == 1
        assert messages[0].body == b"actual message"
        assert messages[0].metadata["attr1"] == "value1"

    def test_receive_batch_size_clamped(self, aws_subscription, mock_sqs_client):
        """Test that batch size is clamped to 10 (SQS max)."""
        mock_sqs_client.receive_message.return_value = {"Messages": []}

        aws_subscription.do_receive_batch(20)

        call_args = mock_sqs_client.receive_message.call_args
        assert call_args[1]["MaxNumberOfMessages"] == 10

    def test_receive_with_wait_time(self, mock_sqs_client):
        """Test that wait_time_seconds is passed to SQS."""
        sub = AwsSubscription(
            subscription_name="test-queue",
            region="us-east-1",
            wait_time_seconds=10,
        )
        sub._client = mock_sqs_client
        sub._queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"

        mock_sqs_client.receive_message.return_value = {"Messages": []}
        sub.do_receive_batch(1)

        call_args = mock_sqs_client.receive_message.call_args
        assert call_args[1]["WaitTimeSeconds"] == 10


class TestAwsSubscriptionAck:
    """Test message acknowledgment functionality."""

    def test_ack_single_message(self, aws_subscription, mock_sqs_client):
        """Test acknowledging a single message."""
        mock_sqs_client.delete_message_batch.return_value = {
            "Successful": [{"Id": "0"}],
            "Failed": [],
        }

        ack_ids = [AwsAckID("receipt-123")]
        aws_subscription.do_send_acks(ack_ids)

        assert mock_sqs_client.delete_message_batch.called
        call_args = mock_sqs_client.delete_message_batch.call_args
        assert len(call_args[1]["Entries"]) == 1
        assert call_args[1]["Entries"][0]["ReceiptHandle"] == "receipt-123"

    def test_ack_batch(self, aws_subscription, mock_sqs_client):
        """Test acknowledging multiple messages."""
        mock_sqs_client.delete_message_batch.return_value = {
            "Successful": [{"Id": "0"}, {"Id": "1"}],
            "Failed": [],
        }

        ack_ids = [AwsAckID(f"receipt-{i}") for i in range(5)]
        aws_subscription.do_send_acks(ack_ids)

        assert mock_sqs_client.delete_message_batch.called

    def test_ack_empty_list(self, aws_subscription, mock_sqs_client):
        """Test that acking empty list is a no-op."""
        aws_subscription.do_send_acks([])
        assert not mock_sqs_client.delete_message_batch.called

    def test_ack_failure(self, aws_subscription, mock_sqs_client):
        """Test handling ack failures."""
        mock_sqs_client.delete_message_batch.return_value = {
            "Successful": [],
            "Failed": [
                {
                    "Id": "0",
                    "Code": "InvalidParameterValue",
                    "Message": "Invalid receipt handle",
                }
            ],
        }

        ack_ids = [AwsAckID("receipt-123")]

        with pytest.raises(SubstrateSdkException, match="DeleteMessageBatch failed"):
            aws_subscription.do_send_acks(ack_ids)


class TestAwsSubscriptionNack:
    """Test message negative acknowledgment functionality."""

    def test_nack_lazy_mode(self, mock_sqs_client):
        """Test that nack_lazy mode bypasses ChangeMessageVisibility."""
        sub = AwsSubscription(
            subscription_name="test-queue",
            region="us-east-1",
            nack_lazy=True,
        )
        sub._client = mock_sqs_client

        nacks = [AckInfo(AwsAckID("receipt-123"), False, None)]
        sub.do_send_nacks(nacks)

        # Should not call change_message_visibility_batch
        assert not mock_sqs_client.change_message_visibility_batch.called

    def test_nack_with_visibility_timeout(self, aws_subscription, mock_sqs_client):
        """Test nacking with visibility timeout."""
        mock_sqs_client.change_message_visibility_batch.return_value = {
            "Successful": [{"Id": "0"}],
            "Failed": [],
        }

        nacks = [AckInfo(AwsAckID("receipt-123"), False, timedelta(seconds=10))]
        aws_subscription.do_send_nacks(nacks)

        assert mock_sqs_client.change_message_visibility_batch.called
        call_args = mock_sqs_client.change_message_visibility_batch.call_args
        assert call_args[1]["Entries"][0]["VisibilityTimeout"] == 10

    def test_nack_uses_subscription_default(self, aws_subscription, mock_sqs_client):
        """Test that nack uses subscription default timeout."""
        mock_sqs_client.change_message_visibility_batch.return_value = {
            "Successful": [{"Id": "0"}],
            "Failed": [],
        }

        # nack without per-message timeout
        nacks = [AckInfo(AwsAckID("receipt-123"), False, None)]
        aws_subscription.do_send_nacks(nacks)

        call_args = mock_sqs_client.change_message_visibility_batch.call_args
        # Should use subscription default (30 seconds from fixture)
        assert call_args[1]["Entries"][0]["VisibilityTimeout"] == 30

    def test_nack_timeout_clamped_to_max(self, aws_subscription, mock_sqs_client):
        """Test that nack timeout is clamped to 12 hours."""
        mock_sqs_client.change_message_visibility_batch.return_value = {
            "Successful": [{"Id": "0"}],
            "Failed": [],
        }

        # Try to set 24 hours (above max)
        nacks = [AckInfo(AwsAckID("receipt-123"), False, timedelta(hours=24))]
        aws_subscription.do_send_nacks(nacks)

        call_args = mock_sqs_client.change_message_visibility_batch.call_args
        # Should be clamped to 43200 seconds (12 hours)
        assert call_args[1]["Entries"][0]["VisibilityTimeout"] == 43200

    def test_nack_empty_list(self, aws_subscription, mock_sqs_client):
        """Test that nacking empty list is a no-op."""
        aws_subscription.do_send_nacks([])
        assert not mock_sqs_client.change_message_visibility_batch.called

    def test_nack_ignores_invalid_receipt_handle(
        self, aws_subscription, mock_sqs_client
    ):
        """Test that ReceiptHandleIsInvalid errors are ignored."""
        mock_sqs_client.change_message_visibility_batch.return_value = {
            "Successful": [],
            "Failed": [
                {
                    "Id": "0",
                    "Code": "ReceiptHandleIsInvalid",
                    "Message": "Receipt handle is invalid",
                }
            ],
        }

        nacks = [AckInfo(AwsAckID("receipt-123"), False, None)]
        # Should not raise
        aws_subscription.do_send_nacks(nacks)


class TestAwsSubscriptionGetAttributes:
    """Test subscription attribute retrieval."""

    def test_get_attributes_success(self, aws_subscription, mock_sqs_client):
        """Test retrieving subscription attributes (queue URL and ARN)."""
        mock_sqs_client.get_queue_attributes.return_value = {
            "Attributes": {
                "QueueArn": "arn:aws:sqs:us-west-2:123456789012:test-queue",
            }
        }

        result = aws_subscription.get_attributes()

        assert (
            result.name == "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
        )
        assert result.topic == "arn:aws:sqs:us-west-2:123456789012:test-queue"

    def test_get_attributes_none(self, aws_subscription, mock_sqs_client):
        """Test retrieving attributes when ARN is missing."""
        mock_sqs_client.get_queue_attributes.return_value = {"Attributes": {}}

        result = aws_subscription.get_attributes()

        assert (
            result.name == "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
        )
        assert result.topic is None


class TestAwsSubscriptionMetadataDecoding:
    """Test metadata key decoding."""

    def test_decode_metadata_key_plain(self):
        """Test decoding plain alphanumeric key."""
        decoded = AwsSubscription._decode_metadata_key("abc123")
        assert decoded == "abc123"

    def test_decode_metadata_key_hex_encoded(self):
        """Test decoding hex-encoded special characters."""
        encoded = "key__0x3A__value"  # Colon
        decoded = AwsSubscription._decode_metadata_key(encoded)
        assert decoded == "key:value"

    def test_decode_metadata_key_space(self):
        """Test decoding hex-encoded space."""
        encoded = "my__0x20__key"
        decoded = AwsSubscription._decode_metadata_key(encoded)
        assert decoded == "my key"

    def test_decode_metadata_key_empty(self):
        """Test decoding empty string."""
        decoded = AwsSubscription._decode_metadata_key("")
        assert decoded == ""

    def test_decode_metadata_key_invalid_hex(self):
        """Test decoding with invalid hex format (should handle gracefully)."""
        # Invalid hex chars (GG is not valid hex)
        encoded = "key__0xGG__value"
        decoded = AwsSubscription._decode_metadata_key(encoded)
        # Should not crash; invalid pattern stays as-is
        assert "__0xGG__" in decoded

    def test_decode_metadata_key_incomplete_pattern(self):
        """Test decoding with incomplete hex pattern (no closing __)."""
        encoded = "key__0x2F"  # Missing closing __
        decoded = AwsSubscription._decode_metadata_key(encoded)
        # Incomplete pattern should stay as-is (not crash)
        assert "__0x2F" in decoded

    def test_decode_metadata_key_incomplete_pattern_at_end(self):
        """Test decoding with incomplete pattern at string end."""
        encoded = "test__0x"
        decoded = AwsSubscription._decode_metadata_key(encoded)
        # Should handle gracefully
        assert "__0x" in decoded


class TestAwsSubscriptionErrorTranslation:
    """Test exception translation for AWS errors."""

    def test_translate_queue_does_not_exist(self, aws_subscription):
        """Test translation of QueueDoesNotExist error."""
        error = ClientError(
            {"Error": {"Code": "QueueDoesNotExist", "Message": "Queue not found"}},
            "ReceiveMessage",
        )
        translated = aws_subscription.translate_error(error)
        assert isinstance(translated, ResourceNotFoundException)

    def test_translate_invalid_credentials(self, aws_subscription):
        """Test translation of credential errors."""
        error = ClientError(
            {"Error": {"Code": "InvalidClientTokenId", "Message": "Invalid token"}},
            "ReceiveMessage",
        )
        translated = aws_subscription.translate_error(error)
        assert isinstance(translated, UnAuthorizedException)

    def test_translate_no_credentials_error(self, aws_subscription):
        """Test translation of NoCredentialsError."""
        error = NoCredentialsError()
        translated = aws_subscription.translate_error(error)
        assert isinstance(translated, UnAuthorizedException)

    def test_translate_already_substrate_exception(self, aws_subscription):
        """Test that SubstrateSdkException is returned unchanged."""
        original = InvalidArgumentException("test")
        translated = aws_subscription.translate_error(original)
        assert translated is original


class TestAwsSubscriptionSNSUnwrapping:
    """Test SNS JSON unwrapping edge cases."""

    def test_convert_to_message_with_non_sns_json(self, aws_subscription):
        """Test that regular JSON (no TopicArn) is not parsed as SNS."""
        regular_json = json.dumps(
            {"message": "Hello World", "timestamp": "2023-01-01T00:00:00Z"}
        )

        sqs_message = {
            "Body": regular_json,
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        assert result.body.decode("utf-8") == regular_json

    def test_convert_to_message_with_plain_text(self, aws_subscription):
        """Test that plain text (non-JSON) is treated as raw message."""
        plain_text = "Hello World - plain text message"

        sqs_message = {
            "Body": plain_text,
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        assert result.body.decode("utf-8") == plain_text

    def test_convert_to_message_sns_json_missing_topic_arn(self, aws_subscription):
        """Test that SNS-like JSON without TopicArn is not parsed."""
        json_body = json.dumps({"Type": "Notification", "Message": "Hello from SNS"})

        sqs_message = {
            "Body": json_body,
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        assert result.body.decode("utf-8") == json_body

    def test_convert_to_message_sns_json_null_topic_arn(self, aws_subscription):
        """Test that SNS JSON with null TopicArn is not parsed."""
        json_body = json.dumps(
            {"Type": "Notification", "TopicArn": None, "Message": "Hello from SNS"}
        )

        sqs_message = {
            "Body": json_body,
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        assert result.body.decode("utf-8") == json_body

    def test_convert_to_message_sns_json_no_message_field(self, aws_subscription):
        """Test that SNS JSON without Message field defaults to empty string."""
        json_body = json.dumps(
            {
                "Type": "Notification",
                "TopicArn": "arn:aws:sns:us-east-1:123456789012:test-topic",
            }
        )

        sqs_message = {
            "Body": json_body,
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        assert result.body.decode("utf-8") == ""

    def test_convert_to_message_invalid_json(self, aws_subscription):
        """Test that invalid JSON is treated as raw message."""
        invalid_json = "{ invalid json }"

        sqs_message = {
            "Body": invalid_json,
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        assert result.body.decode("utf-8") == invalid_json

    def test_convert_to_message_empty_body(self, aws_subscription):
        """Test that empty body is treated as raw message."""
        sqs_message = {
            "Body": "",
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        assert result.body.decode("utf-8") == ""

    def test_convert_to_message_whitespace_body(self, aws_subscription):
        """Test that whitespace-only body is treated as raw message."""
        whitespace_body = "   \n\t  "

        sqs_message = {
            "Body": whitespace_body,
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        assert result.body.decode("utf-8") == whitespace_body

    def test_convert_to_message_sns_attributes_without_value(self, aws_subscription):
        """Test SNS MessageAttributes without Value field are skipped."""
        sns_json = {
            "Type": "Notification",
            "TopicArn": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "Message": "Hello from SNS",
            "MessageAttributes": {
                "attr1": {"Type": "String"},  # No Value field
                "attr2": {"Type": "String", "Value": "value2"},
            },
        }

        sqs_message = {
            "Body": json.dumps(sns_json),
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        assert result.body.decode("utf-8") == "Hello from SNS"
        assert "attr1" not in result.metadata
        assert result.metadata.get("attr2") == "value2"

    def test_convert_to_message_sns_json_with_whitespace(self, aws_subscription):
        """Test that SNS JSON with surrounding whitespace is parsed correctly."""
        sns_json = {
            "Type": "Notification",
            "TopicArn": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "Message": "Hello from SNS",
        }
        json_with_whitespace = f"  \n  {json.dumps(sns_json)}  \n  "

        sqs_message = {
            "Body": json_with_whitespace,
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        assert result.body.decode("utf-8") == "Hello from SNS"

    def test_with_raw_true_sns_not_parsed(self, mock_sqs_client):
        """Test that raw_message_delivery=True prevents SNS JSON parsing."""
        subscription = AwsSubscription(
            subscription_name="test-queue",
            region="us-east-1",
            raw_message_delivery=True,
        )
        subscription._client = mock_sqs_client
        subscription._queue_url = (
            "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
        )

        sns_json = {
            "Type": "Notification",
            "TopicArn": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "Message": "Hello from SNS",
            "MessageAttributes": {"attr1": {"Type": "String", "Value": "value1"}},
        }
        sns_json_str = json.dumps(sns_json)

        sqs_message = {
            "Body": sns_json_str,
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = subscription._convert_to_message(sqs_message)
        # With raw=True, SNS JSON should NOT be parsed
        assert result.body.decode("utf-8") == sns_json_str
        # SNS MessageAttributes should NOT be extracted
        assert result.metadata.get("attr1") is None

    def test_with_raw_default_false(self, aws_subscription):
        """Test that default value of raw is False (SNS parsing enabled)."""
        sns_json = {
            "Type": "Notification",
            "TopicArn": "arn:aws:sns:us-east-1:123456789012:test-topic",
            "Message": "Hello from SNS",
        }

        sqs_message = {
            "Body": json.dumps(sns_json),
            "MessageId": "test-message-id",
            "ReceiptHandle": "test-receipt-handle",
            "MessageAttributes": {},
        }

        result = aws_subscription._convert_to_message(sqs_message)
        # Default behavior: SNS should be unwrapped
        assert result.body.decode("utf-8") == "Hello from SNS"


class TestAwsSubscriptionBatchOperations:
    """Test batch splitting for acks/nacks >10."""

    def test_do_send_acks_large_batch(self, mock_sqs_client):
        """Test that 15 acks split into batches of 10+5 (SQS limit is 10)."""
        subscription = AwsSubscription(
            subscription_name="test-queue",
            region="us-east-1",
        )
        subscription._client = mock_sqs_client
        subscription._queue_url = (
            "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
        )

        # Create 15 ackIDs
        ack_ids = [AwsAckID(f"receipt-{i}") for i in range(15)]

        mock_sqs_client.delete_message_batch.return_value = {
            "Successful": [],
            "Failed": [],
        }

        subscription.do_send_acks(ack_ids)

        # Should be called twice: once for 10, once for 5
        assert mock_sqs_client.delete_message_batch.call_count == 2

    def test_do_send_nacks_large_batch(self, mock_sqs_client):
        """Test that 25 nacks split into batches of 10+10+5."""
        subscription = AwsSubscription(
            subscription_name="test-queue",
            region="us-east-1",
            nack_visibility_timeout=timedelta(seconds=30),
        )
        subscription._client = mock_sqs_client
        subscription._queue_url = (
            "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
        )

        # Create 25 nack AckInfo objects
        nacks = [
            AckInfo(ack_id=AwsAckID(f"receipt-{i}"), is_ack=False) for i in range(25)
        ]

        mock_sqs_client.change_message_visibility_batch.return_value = {
            "Successful": [],
            "Failed": [],
        }

        subscription.do_send_nacks(nacks)

        # Should be called 3 times: 10+10+5
        assert mock_sqs_client.change_message_visibility_batch.call_count == 3

    def test_do_send_nacks_receipt_handle_invalid_ignored(self, mock_sqs_client):
        """Test that ReceiptHandleIsInvalid errors are filtered (expected)."""
        subscription = AwsSubscription(
            subscription_name="test-queue",
            region="us-east-1",
            nack_visibility_timeout=timedelta(seconds=30),
        )
        subscription._client = mock_sqs_client
        subscription._queue_url = (
            "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
        )

        nacks = [AckInfo(ack_id=AwsAckID("receipt-1"), is_ack=False)]

        # Mock response with ReceiptHandleIsInvalid error (should be ignored)
        mock_sqs_client.change_message_visibility_batch.return_value = {
            "Successful": [],
            "Failed": [
                {
                    "Id": "0",
                    "Code": "ReceiptHandleIsInvalid",
                    "Message": "The receipt handle is invalid",
                }
            ],
        }

        # Should NOT raise (ReceiptHandleIsInvalid is expected/filtered)
        subscription.do_send_nacks(nacks)

    def test_do_send_nacks_mixed_failures_filters_receipt_handle(self, mock_sqs_client):
        """Test mixed failures: keep real errors, filter ReceiptHandleIsInvalid."""
        subscription = AwsSubscription(
            subscription_name="test-queue",
            region="us-east-1",
            nack_visibility_timeout=timedelta(seconds=30),
        )
        subscription._client = mock_sqs_client
        subscription._queue_url = (
            "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
        )

        nacks = [
            AckInfo(ack_id=AwsAckID("receipt-1"), is_ack=False),
            AckInfo(ack_id=AwsAckID("receipt-2"), is_ack=False),
        ]

        # Mock mixed failures: one ReceiptHandleIsInvalid (ignore), one real error (throw)
        mock_sqs_client.change_message_visibility_batch.return_value = {
            "Successful": [],
            "Failed": [
                {
                    "Id": "0",
                    "Code": "ReceiptHandleIsInvalid",
                    "Message": "The receipt handle is invalid",
                },
                {
                    "Id": "1",
                    "Code": "InternalError",
                    "Message": "Internal server error",
                },
            ],
        }

        # Should raise because of the InternalError (real error, not filtered)
        with pytest.raises(SubstrateSdkException, match="InternalError"):
            subscription.do_send_nacks(nacks)

    def test_do_send_nacks_per_message_visibility_timeout(self, mock_sqs_client):
        """Test per-message visibility timeout overrides."""
        subscription = AwsSubscription(
            subscription_name="test-queue",
            region="us-east-1",
            nack_visibility_timeout=timedelta(seconds=30),  # default
        )
        subscription._client = mock_sqs_client
        subscription._queue_url = (
            "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
        )

        # Three nacks with different timeouts
        nacks = [
            AckInfo(
                ack_id=AwsAckID("receipt-1"),
                is_ack=False,
                visibility_timeout=timedelta(seconds=0),
            ),  # immediate
            AckInfo(
                ack_id=AwsAckID("receipt-2"),
                is_ack=False,
                visibility_timeout=timedelta(seconds=60),
            ),  # override
            AckInfo(
                ack_id=AwsAckID("receipt-3"), is_ack=False
            ),  # None → falls back to subscription default (30s)
        ]

        mock_sqs_client.change_message_visibility_batch.return_value = {
            "Successful": [],
            "Failed": [],
        }

        subscription.do_send_nacks(nacks)

        # Should be called 3 times (one per unique timeout: 0s, 60s, 30s)
        assert mock_sqs_client.change_message_visibility_batch.call_count == 3


class TestAwsSubscriptionExceptionTranslation:
    """Test missing exception translation cases."""

    def test_translate_throttling_exception(self, aws_subscription):
        """Test ThrottlingException maps to ResourceExhaustedException."""
        from multicloud.common.exceptions import ResourceExhaustedException

        error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "ReceiveMessage",
        )
        translated = aws_subscription.translate_error(error)
        assert isinstance(translated, ResourceExhaustedException)

    def test_translate_no_error_details(self, aws_subscription):
        """Test exception with null/missing error details."""
        from multicloud.common.exceptions import UnknownException

        # ClientError with missing Error dict
        error = ClientError({"Error": {}}, "ReceiveMessage")
        translated = aws_subscription.translate_error(error)
        assert isinstance(translated, UnknownException)

    def test_translate_illegal_argument_exception(self, aws_subscription):
        """Test that ValueError maps to UnknownException."""
        error = ValueError("Invalid argument")
        translated = aws_subscription.translate_error(error)
        # ValueError is not specifically handled, so it becomes UnknownException
        from multicloud.common.exceptions import UnknownException

        assert isinstance(translated, UnknownException)


class TestAwsSubscriptionCredentialsOverrider:
    """Test that credentials_overrider is resolved and used to build the client.

    The resolver itself (``get_session``) is unit-tested in
    ``tests/aws_common/test_aws_credentials.py``; these tests verify the
    subscription forwards its stored overrider to that resolver and builds the
    SQS client from the resolved session.
    """

    @pytest.mark.parametrize(
        "overrider",
        [
            pytest.param(
                CredentialsOverrider(
                    credentials_type=CredentialsType.SESSION,
                    session_credentials=StsCredentials(
                        access_key_id="AKIAEXAMPLE",
                        access_key_secret="secret",
                        security_token="token",
                    ),
                ),
                id="session",
            ),
            pytest.param(
                CredentialsOverrider(
                    credentials_type=CredentialsType.ASSUME_ROLE,
                    role="arn:aws:iam::123456789012:role/my-role",
                ),
                id="assume_role",
            ),
            pytest.param(
                CredentialsOverrider(
                    credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
                    role="arn:aws:iam::123456789012:role/web-role",
                    web_identity_token_supplier=lambda: "oidc-token",
                ),
                id="web_identity",
            ),
        ],
    )
    def test_overrider_forwarded_to_get_session(self, overrider):
        """Each override type reaches get_session, and the client is built from it."""
        with patch(
            "multicloud.pubsub.pubsub_aws.aws_subscription.get_session"
        ) as mock_get_session:
            mock_session = Mock()
            mock_client = Mock()
            mock_session.client.return_value = mock_client
            mock_get_session.return_value = mock_session

            subscription = AwsSubscription(
                subscription_name="test-queue",
                region="us-east-1",
                credentials_overrider=overrider,
            )
            client = subscription._get_or_create_client()

            # Resolver received the exact overrider + region this subscription holds.
            mock_get_session.assert_called_once_with(overrider, "us-east-1")
            # Client was built from the resolved session, not created directly.
            mock_session.client.assert_called_once_with("sqs", region_name="us-east-1")
            assert client is mock_client

    def test_no_overrider_passes_none_to_get_session(self):
        """Without an overrider, None is forwarded so defaults are used."""
        with patch(
            "multicloud.pubsub.pubsub_aws.aws_subscription.get_session"
        ) as mock_get_session:
            mock_session = Mock()
            mock_client = Mock()
            mock_session.client.return_value = mock_client
            mock_get_session.return_value = mock_session

            subscription = AwsSubscription(
                subscription_name="test-queue",
                region="us-east-1",
            )
            subscription._get_or_create_client()

            mock_get_session.assert_called_once_with(None, "us-east-1")
