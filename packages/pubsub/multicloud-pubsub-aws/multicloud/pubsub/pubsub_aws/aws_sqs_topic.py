"""AWS SQS topic implementation."""

from __future__ import annotations

import base64
import threading
from typing import List, Optional

import boto3
from multicloud.aws_common.aws_credentials import get_session
from multicloud.aws_common.error_mapping import translate_aws_sqs_error
from multicloud.common.credentials import CredentialsOverrider
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
)
from multicloud.pubsub.pubsub_aws.aws_base_topic import (
    AwsBaseTopic,
    TopicOptions,
)
from multicloud.pubsub.pubsub_client.batcher import Batcher
from multicloud.pubsub.pubsub_client.driver import TOPIC_REGISTRY, Message


@TOPIC_REGISTRY.register("awssqs")
class AwsSqsTopic(AwsBaseTopic):
    """AWS SQS implementation of BaseTopic.

    Uses boto3 SQS client to send messages to an SQS queue.
    Supports batching up to 10 messages per request (AWS SQS limit).
    """

    # AWS SQS limits
    MAX_BATCH_SIZE = 10
    MAX_BATCH_BYTE_SIZE = 256 * 1024  # 256 KB
    MAX_SQS_ATTRIBUTES = 10

    def __init__(
        self,
        *,
        topic_name: str,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        credentials_overrider: Optional["CredentialsOverrider"] = None,
        topic_options: Optional[TopicOptions] = None,
    ) -> None:
        """Initialize AWS SQS topic.

        Args:
            topic_name: SQS queue name or URL
            region: AWS region (e.g., 'us-west-2')
            endpoint: Optional endpoint override for testing
            credentials_overrider: Override default credential resolution
            topic_options: Configuration options for base64 encoding (default: AUTO)
        """
        super().__init__(
            topic_name=topic_name,
            region=region,
            endpoint=endpoint,
            credentials_overrider=credentials_overrider,
            topic_options=topic_options,
        )
        self._client: Optional[boto3.client] = None
        self._client_lock = threading.Lock()
        self._queue_url: Optional[str] = None

    def create_batcher_options(self) -> Batcher.Options:
        """Override batcher options to align with AWS SQS limits.

        AWS SQS supports max 10 messages per batch, 256KB total size.
        """
        return Batcher.Options(
            max_handlers=100,
            min_batch_size=1,
            max_batch_size=self.MAX_BATCH_SIZE,
            max_batch_byte_size=self.MAX_BATCH_BYTE_SIZE,
        )

    def get_max_message_size_bytes(self) -> int:
        """Return the maximum message size for AWS SQS.

        Returns:
            256 KB (262144 bytes) - AWS SQS message size limit
        """
        return 256 * 1024  # 256 KB

    def do_send_batch(self, messages: List[Message]) -> None:
        """Provider-specific batch send implementation for AWS SQS.

        Args:
            messages: List of messages to send

        Raises:
            SubstrateSdkException: If sending fails
        """
        if not messages:
            return

        client = self._get_or_create_client()
        queue_url = self._get_queue_url()

        entries = []
        for i, msg in enumerate(messages):
            # Encode message body
            body, is_base64 = self._encode_message_body(msg)

            # Convert metadata to SQS message attributes
            attributes = self._convert_metadata_to_attributes(msg.metadata, is_base64)

            entry = {
                "Id": str(i),
                "MessageBody": body,
                "MessageAttributes": attributes,
            }

            # Set FIFO-specific attributes if present in metadata
            if msg.metadata:
                if self.MetadataKeys.DEDUPLICATION_ID in msg.metadata:
                    entry["MessageDeduplicationId"] = msg.metadata[
                        self.MetadataKeys.DEDUPLICATION_ID
                    ]
                if self.MetadataKeys.MESSAGE_GROUP_ID in msg.metadata:
                    entry["MessageGroupId"] = msg.metadata[
                        self.MetadataKeys.MESSAGE_GROUP_ID
                    ]

            entries.append(entry)

        response = client.send_message_batch(QueueUrl=queue_url, Entries=entries)

        # Check for failures
        if "Failed" in response and response["Failed"]:
            first_failure = response["Failed"][0]
            raise SubstrateSdkException(
                f"SQS SendMessageBatch failed for {len(response['Failed'])} message(s): "
                f"{first_failure.get('Code')}, {first_failure.get('Message')}"
            )

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate AWS SQS exceptions to common exception hierarchy.

        Maps AWS SDK error codes to appropriate SubstrateSdkException types:
        - Auth/credential errors → UnAuthorizedException
        - Throttling/rate-limit errors → ResourceExhaustedException
        - Invalid parameters → InvalidArgumentException
        - Missing queues → ResourceNotFoundException
        - Internal failures → TransactionFailedException
        - All unmapped errors → UnknownException

        Args:
            exc: The AWS-specific exception

        Returns:
            Appropriate SubstrateSdkException subclass
        """
        return translate_aws_sqs_error(exc)

    def get_provider_id(self) -> str:
        """Return the provider identifier."""
        return "awssqs"

    def close(self) -> None:
        """Close the AWS topic and release resources."""
        try:
            super().close()
        finally:
            with self._client_lock:
                if self._client is not None and hasattr(self._client, "close"):
                    self._client.close()
                self._client = None

    def _get_or_create_client(self):
        """Lazily initialize the boto3 SQS client with thread-safe locking."""
        with self._client_lock:
            if self._client is None:
                session = get_session(self._credentials_overrider, self.region)
                config = {}
                if self.region:
                    config["region_name"] = self.region
                if self.endpoint:
                    config["endpoint_url"] = self.endpoint

                self._client = session.client("sqs", **config)

        return self._client

    def _get_queue_url(self) -> str:
        """Get or resolve the SQS queue URL."""
        if self._queue_url:
            return self._queue_url

        # If topic_name looks like a URL, use it directly
        if self.topic_name.startswith("https://"):
            self._queue_url = self.topic_name
            return self._queue_url

        # Otherwise, resolve queue name to URL
        # Let exceptions propagate to client layer for translation
        client = self._get_or_create_client()
        response = client.get_queue_url(QueueName=self.topic_name)
        self._queue_url = response["QueueUrl"]
        return self._queue_url

    def _encode_message_body(self, message: Message) -> tuple[str, bool]:
        """Encode message body, optionally using base64 for non-UTF-8 content.

        Args:
            message: The message to encode

        Returns:
            Tuple of (encoded_body, is_base64_encoded)
        """
        if not message.body:
            return "", False

        if self._should_encode_body(message.body):
            # Base64 encode binary content
            encoded = base64.b64encode(message.body).decode("ascii")
            return encoded, True
        else:
            # Use as UTF-8 string
            return message.body.decode("utf-8"), False

    def _convert_metadata_to_attributes(self, metadata: dict, is_base64: bool) -> dict:
        """Convert message metadata to SQS message attributes.

        Args:
            metadata: Message metadata dictionary
            is_base64: Whether the body was base64-encoded

        Returns:
            SQS MessageAttributes dict
        """
        attributes = {}

        if is_base64:
            attributes[self.MetadataKeys.BASE64_ENCODED] = {
                "DataType": "String",
                "StringValue": "true",
            }

        if not metadata:
            return attributes

        # Add user metadata, excluding FIFO-specific keys
        for key, value in metadata.items():
            if key in (
                self.MetadataKeys.DEDUPLICATION_ID,
                self.MetadataKeys.MESSAGE_GROUP_ID,
            ):
                continue

            if len(attributes) >= self.MAX_SQS_ATTRIBUTES:
                break

            encoded_key = self._encode_metadata_key(key)
            encoded_value = self._encode_metadata_value(value)

            attributes[encoded_key] = {
                "DataType": "String",
                "StringValue": encoded_value,
            }

        return attributes
