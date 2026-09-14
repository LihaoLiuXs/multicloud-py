"""Unit tests for AWS SNS topic implementation."""

from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError
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
from multicloud.pubsub.pubsub_aws.aws_sns_topic import AwsSnsTopic
from multicloud.pubsub.pubsub_client.driver import Message


@pytest.fixture
def mock_sns_client():
    """Create a mock boto3 SNS client."""
    client = Mock()
    client.publish_batch.return_value = {"Successful": [], "Failed": []}
    client.get_topic_attributes.return_value = {"Attributes": {}}  # For validation
    return client


@pytest.fixture
def sns_topic(mock_sns_client):
    """Create an AwsSnsTopic instance with mocked SNS client."""
    topic = AwsSnsTopic(
        topic_name="arn:aws:sns:us-east-1:123456789012:test-topic",
        region="us-east-1",
    )
    topic._client = mock_sns_client
    return topic


class TestAwsSnsTopicBasics:
    """Test basic AWS SNS topic functionality."""

    def test_provider_id(self, sns_topic):
        """Test that provider ID is correct."""
        assert sns_topic.get_provider_id() == "awssns"

    def test_batcher_options(self, sns_topic):
        """Test that batcher options match SNS limits."""
        options = sns_topic.create_batcher_options()
        assert options.max_handlers == 100
        assert options.min_batch_size == 1
        assert options.max_batch_size == 10  # SNS PublishBatch limit
        assert options.max_batch_byte_size == 256 * 1024  # 256 KB


class TestAwsSnsTopicSend:
    """Test message sending functionality."""

    def test_send_simple_message(self, sns_topic, mock_sns_client):
        """Test sending a simple message."""
        message = Message.builder().with_body(b"test message").build()

        sns_topic.do_send_batch([message])

        # Verify publish_batch was called
        assert mock_sns_client.publish_batch.called
        call_args = mock_sns_client.publish_batch.call_args
        assert (
            call_args[1]["TopicArn"] == "arn:aws:sns:us-east-1:123456789012:test-topic"
        )
        entries = call_args[1]["PublishBatchRequestEntries"]
        assert len(entries) == 1
        assert entries[0]["Message"] == "test message"

    def test_send_with_metadata(self, sns_topic, mock_sns_client):
        """Test sending message with metadata."""
        metadata = {
            "key1": "value1",
            "key2": "value2",
        }
        message = Message.builder().with_body(b"test").with_metadata(metadata).build()

        sns_topic.do_send_batch([message])

        call_args = mock_sns_client.publish_batch.call_args
        entry = call_args[1]["PublishBatchRequestEntries"][0]
        # Metadata becomes message attributes
        assert "MessageAttributes" in entry
        assert "key1" in entry["MessageAttributes"]
        assert "key2" in entry["MessageAttributes"]

    def test_send_with_subject(self, sns_topic, mock_sns_client):
        """Test sending message with SNS Subject."""
        metadata = {"Subject": "Test Subject"}
        message = Message.builder().with_body(b"test").with_metadata(metadata).build()

        sns_topic.do_send_batch([message])

        call_args = mock_sns_client.publish_batch.call_args
        entry = call_args[1]["PublishBatchRequestEntries"][0]
        # Subject should be a direct entry property, not in MessageAttributes
        assert entry["Subject"] == "Test Subject"
        assert "Subject" not in entry["MessageAttributes"]

    def test_send_with_fifo_attributes(self, sns_topic, mock_sns_client):
        """Test sending message with FIFO-specific attributes."""
        metadata = {
            "DeduplicationId": "dedup-123",
            "MessageGroupId": "group-456",
        }
        message = Message.builder().with_body(b"test").with_metadata(metadata).build()

        sns_topic.do_send_batch([message])

        call_args = mock_sns_client.publish_batch.call_args
        entry = call_args[1]["PublishBatchRequestEntries"][0]
        # FIFO attributes should be direct entry properties
        assert entry["MessageDeduplicationId"] == "dedup-123"
        assert entry["MessageGroupId"] == "group-456"
        # Should not be in MessageAttributes
        assert "DeduplicationId" not in entry["MessageAttributes"]
        assert "MessageGroupId" not in entry["MessageAttributes"]

    def test_send_binary_message_base64_encoding(self, sns_topic, mock_sns_client):
        """Test that binary messages are base64 encoded."""
        # Create binary data that's not valid UTF-8
        binary_data = b"\x80\x81\x82\x83"
        message = Message.builder().with_body(binary_data).build()

        sns_topic.do_send_batch([message])

        call_args = mock_sns_client.publish_batch.call_args
        entry = call_args[1]["PublishBatchRequestEntries"][0]
        # Message should be base64 encoded
        import base64

        expected = base64.b64encode(binary_data).decode("ascii")
        assert entry["Message"] == expected
        # Should have base64encoded attribute
        assert "base64encoded" in entry["MessageAttributes"]

    def test_send_batch_failure(self, sns_topic, mock_sns_client):
        """Test handling of partial batch failure."""
        mock_sns_client.publish_batch.return_value = {
            "Successful": [],
            "Failed": [
                {
                    "Id": "0",
                    "Code": "InvalidParameter",
                    "Message": "Invalid message",
                }
            ],
        }

        message = Message.builder().with_body(b"test").build()

        with pytest.raises(SubstrateSdkException, match="SNS PublishBatch failed"):
            sns_topic.do_send_batch([message])

    def test_send_empty_batch(self, sns_topic, mock_sns_client):
        """Test that sending empty batch is a no-op."""
        sns_topic.do_send_batch([])
        assert not mock_sns_client.publish_batch.called


class TestAwsSnsTopicErrorTranslation:
    """Test exception translation for AWS SNS errors."""

    def test_translate_topic_not_found(self, sns_topic):
        """Test translation of NotFound error."""
        error = ClientError(
            {"Error": {"Code": "NotFound", "Message": "Topic not found"}},
            "PublishBatch",
        )
        translated = sns_topic.translate_error(error)
        assert isinstance(translated, ResourceNotFoundException)

    def test_translate_invalid_credentials(self, sns_topic):
        """Test translation of authentication error."""
        error = ClientError(
            {"Error": {"Code": "ExpiredToken", "Message": "Token expired"}},
            "PublishBatch",
        )
        translated = sns_topic.translate_error(error)
        assert isinstance(translated, UnAuthorizedException)

    def test_translate_invalid_parameter(self, sns_topic):
        """Test translation of invalid parameter error."""
        error = ClientError(
            {"Error": {"Code": "InvalidParameter", "Message": "Invalid"}},
            "PublishBatch",
        )
        translated = sns_topic.translate_error(error)
        assert isinstance(translated, InvalidArgumentException)

    def test_translate_already_substrate_exception(self, sns_topic):
        """Test that SubstrateSdkException is returned unchanged."""
        original = InvalidArgumentException("test")
        translated = sns_topic.translate_error(original)
        assert translated is original


class TestAwsSnsTopicMetadataEncoding:
    """Test metadata key encoding."""

    def test_encode_metadata_key_special_chars(self):
        """Test encoding of special characters."""
        result = AwsSnsTopic._encode_metadata_key("file/path")
        assert result == "file__0x2F__path"

    def test_encode_metadata_key_consecutive_periods(self):
        """Test encoding of consecutive periods."""
        result = AwsSnsTopic._encode_metadata_key("key..name")
        assert result == "key__0x2E____0x2E__name"

    def test_encode_metadata_key_period_at_start(self):
        """Test encoding of period at start."""
        result = AwsSnsTopic._encode_metadata_key(".key")
        assert result == "__0x2E__key"


class TestAwsSnsTopicClose:
    """Test resource cleanup."""

    def test_close_cleans_up_client(self, sns_topic, mock_sns_client):
        """Test that close cleans up the SNS client."""
        sns_topic.close()

        assert mock_sns_client.close.called
        # Client should be set to None
        assert sns_topic._client is None


class TestAwsSnsTopicValidation:
    """Test topic ARN validation."""

    def test_empty_topic_arn_raises_on_construction(self):
        """Test that empty topic ARN raises InvalidArgumentException on construction."""
        from multicloud.common.exceptions import InvalidArgumentException
        from multicloud.pubsub.pubsub_aws.aws_sns_topic import AwsSnsTopic

        # Empty topic_name should raise immediately in base class
        with pytest.raises(InvalidArgumentException, match="topic_name is required"):
            AwsSnsTopic(topic_name="", region="us-west-2")

    def test_whitespace_topic_arn_raises(self):
        """Test that whitespace-only topic ARN raises InvalidArgumentException."""
        from multicloud.common.exceptions import InvalidArgumentException
        from multicloud.pubsub.pubsub_aws.aws_sns_topic import AwsSnsTopic

        # Create topic with whitespace ARN
        topic = AwsSnsTopic(topic_name="   ", region="us-west-2")

        # Validation happens lazily on first send
        with pytest.raises(InvalidArgumentException, match="cannot be null or empty"):
            topic.do_send_batch([Message.builder().with_body(b"test").build()])


class TestAwsSnsTopicEmptyBody:
    """Test empty message body handling."""

    def test_encode_empty_body(self):
        """Test that empty message body returns empty string without encoding."""
        from multicloud.pubsub.pubsub_aws.aws_sns_topic import AwsSnsTopic

        topic = AwsSnsTopic(
            topic_name="arn:aws:sns:us-west-2:123:test", region="us-west-2"
        )

        # Empty body should return empty string, not base64 encoded
        message = Message.builder().with_body(b"").build()
        body, is_base64 = topic._encode_message_body(message)

        assert body == ""
        assert is_base64 is False


class TestAwsSnsTopicCredentialsOverrider:
    """Test that credentials_overrider is resolved and used to build the client.

    The resolver itself (``get_session``) is unit-tested in
    ``tests/aws_common/test_aws_credentials.py``; these tests verify the
    provider forwards its stored overrider to that resolver and builds the
    SNS client from the resolved session.
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
            "multicloud.pubsub.pubsub_aws.aws_sns_topic.get_session"
        ) as mock_get_session:
            mock_session = Mock()
            mock_client = Mock()
            mock_client.get_topic_attributes.return_value = {"Attributes": {}}
            mock_session.client.return_value = mock_client
            mock_get_session.return_value = mock_session

            topic = AwsSnsTopic(
                topic_name="arn:aws:sns:us-east-1:123456789012:test-topic",
                region="us-east-1",
                credentials_overrider=overrider,
            )
            client = topic._get_or_create_client()

            # Resolver received the exact overrider + region this topic holds.
            mock_get_session.assert_called_once_with(overrider, "us-east-1")
            # Client was built from the resolved session, not created directly.
            mock_session.client.assert_called_once_with("sns", region_name="us-east-1")
            assert client is mock_client

    def test_no_overrider_passes_none_to_get_session(self):
        """Without an overrider, None is forwarded so defaults are used."""
        with patch(
            "multicloud.pubsub.pubsub_aws.aws_sns_topic.get_session"
        ) as mock_get_session:
            mock_session = Mock()
            mock_client = Mock()
            mock_client.get_topic_attributes.return_value = {"Attributes": {}}
            mock_session.client.return_value = mock_client
            mock_get_session.return_value = mock_session

            topic = AwsSnsTopic(
                topic_name="arn:aws:sns:us-east-1:123456789012:test-topic",
                region="us-east-1",
            )
            topic._get_or_create_client()

            mock_get_session.assert_called_once_with(None, "us-east-1")
