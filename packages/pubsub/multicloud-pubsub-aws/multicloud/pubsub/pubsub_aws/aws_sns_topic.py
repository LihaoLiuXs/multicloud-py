"""AWS SNS topic implementation."""

from __future__ import annotations

import threading
from typing import List, Optional

import boto3
from multicloud.aws_common.aws_credentials import get_session
from multicloud.aws_common.error_mapping import translate_aws_sns_error
from multicloud.common.credentials import CredentialsOverrider
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
)
from multicloud.pubsub.pubsub_aws.aws_base_topic import AwsBaseTopic, TopicOptions
from multicloud.pubsub.pubsub_client.batcher import Batcher
from multicloud.pubsub.pubsub_client.driver import TOPIC_REGISTRY, Message


@TOPIC_REGISTRY.register("awssns")
class AwsSnsTopic(AwsBaseTopic):
    """AWS SNS implementation of BaseTopic.

    Uses boto3 SNS client to publish messages to an SNS topic.
    Supports FIFO topics, message deduplication, and subject metadata.
    """

    MAX_SNS_ATTRIBUTES = 10
    MAX_BATCH_HANDLERS = 100
    MAX_BATCH_BYTE_SIZE = 256 * 1024  # 256 KB

    def __init__(
        self,
        *,
        topic_name: str,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
        topic_options: Optional[TopicOptions] = None,
    ) -> None:
        """Initialize AWS SNS topic.

        Args:
            topic_name: SNS topic ARN (arn:aws:sns:region:account:topic-name)
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
        self._topic_arn = topic_name
        self._validated = False  # Lazy validation flag

    def create_batcher_options(self) -> Batcher.Options:
        """Override batcher options to align with SNS limits.

        SNS supports up to 10 messages per batch and 256KB per message.
        """
        return Batcher.Options(
            max_handlers=self.MAX_BATCH_HANDLERS,
            min_batch_size=1,
            max_batch_size=10,  # SNS PublishBatch limit
            max_batch_byte_size=self.MAX_BATCH_BYTE_SIZE,
        )

    def get_max_message_size_bytes(self) -> int:
        """Return the maximum message size for AWS SNS.

        Returns:
            256 KB (262144 bytes) - AWS SNS message size limit
        """
        return 256 * 1024  # 256 KB

    def do_send_batch(self, messages: List[Message]) -> None:
        """Provider-specific batch send implementation for AWS SNS.

        Args:
            messages: List of messages to publish

        Raises:
            SubstrateSdkException: If publishing fails
        """
        if not messages:
            return

        client = self._get_or_create_client()

        entries = []
        for i, message in enumerate(messages):
            # Encode message body
            body, is_base64 = self._encode_message_body(message)

            # Convert metadata to SNS attributes
            attributes = self._convert_metadata_to_sns_attributes(
                message.metadata, is_base64
            )

            # Build entry
            entry = {
                "Id": str(i),
                "Message": body,
                "MessageAttributes": attributes,
            }

            # Set SNS-specific attributes from metadata
            if message.metadata:
                if self.MetadataKeys.SUBJECT in message.metadata:
                    entry["Subject"] = message.metadata[self.MetadataKeys.SUBJECT]
                if self.MetadataKeys.DEDUPLICATION_ID in message.metadata:
                    entry["MessageDeduplicationId"] = message.metadata[
                        self.MetadataKeys.DEDUPLICATION_ID
                    ]
                if self.MetadataKeys.MESSAGE_GROUP_ID in message.metadata:
                    entry["MessageGroupId"] = message.metadata[
                        self.MetadataKeys.MESSAGE_GROUP_ID
                    ]

            entries.append(entry)

        response = client.publish_batch(
            TopicArn=self._topic_arn, PublishBatchRequestEntries=entries
        )

        # Check for failed messages
        if "Failed" in response and response["Failed"]:
            first_failure = response["Failed"][0]
            raise SubstrateSdkException(
                f"SNS PublishBatch failed for {len(response['Failed'])} message(s): "
                f"{first_failure.get('Code')}, {first_failure.get('Message')}"
            )

    def _convert_metadata_to_sns_attributes(
        self, metadata: Optional[dict], is_base64: bool
    ) -> dict:
        """Convert message metadata to SNS message attributes.

        Args:
            metadata: Message metadata dictionary
            is_base64: Whether the body is base64 encoded

        Returns:
            Dictionary of SNS message attributes
        """
        attributes = {}

        # Add base64 encoding flag if needed
        if is_base64:
            attributes[self.MetadataKeys.BASE64_ENCODED] = {
                "DataType": "String",
                "StringValue": "true",
            }

        if not metadata:
            return attributes

        # Add user metadata, excluding SNS-specific keys
        for key, value in metadata.items():
            if key in (
                self.MetadataKeys.SUBJECT,
                self.MetadataKeys.DEDUPLICATION_ID,
                self.MetadataKeys.MESSAGE_GROUP_ID,
            ):
                continue

            if len(attributes) >= self.MAX_SNS_ATTRIBUTES:
                break

            encoded_key = self._encode_metadata_key(key)
            encoded_value = self._encode_metadata_value(value)

            attributes[encoded_key] = {
                "DataType": "String",
                "StringValue": encoded_value,
            }

        return attributes

    def _encode_message_body(self, message: Message) -> tuple[str, bool]:
        """Encode message body as string, applying base64 if needed.

        Args:
            message: The message to encode

        Returns:
            Tuple of (encoded_body_string, is_base64_encoded)
        """
        if not message.body:
            return "", False

        # Determine if encoding is required based on topic options
        is_encoding_required = self._should_encode_body(message.body)

        if is_encoding_required:
            import base64

            encoded = base64.b64encode(message.body).decode("ascii")
            return encoded, True
        else:
            # Valid UTF-8, return as-is
            body_str = message.body.decode("utf-8")
            return body_str, False

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate AWS SNS exceptions to common exception hierarchy.

        Maps AWS SDK error codes to appropriate SubstrateSdkException types:
        - Auth/credential errors → UnAuthorizedException
        - Throttling/rate-limit errors → ResourceExhaustedException
        - Invalid parameters → InvalidArgumentException
        - Missing resources → ResourceNotFoundException
        - Internal failures → TransactionFailedException
        - All unmapped errors → UnknownException

        Args:
            exc: The AWS-specific exception

        Returns:
            Appropriate SubstrateSdkException subclass
        """
        return translate_aws_sns_error(exc)

    def get_provider_id(self) -> str:
        """Return the provider identifier."""
        return "awssns"

    def close(self) -> None:
        """Close the AWS SNS topic and release resources."""
        try:
            super().close()
        finally:
            with self._client_lock:
                if self._client is not None and hasattr(self._client, "close"):
                    self._client.close()
                self._client = None

    def _get_or_create_client(self):
        """Lazily initialize the boto3 SNS client with thread-safe locking."""
        with self._client_lock:
            if self._client is None:
                session = get_session(self._credentials_overrider, self.region)
                config = {}
                if self.region:
                    config["region_name"] = self.region
                if self.endpoint:
                    config["endpoint_url"] = self.endpoint

                self._client = session.client("sns", **config)

            # Lazy validation: validate topic ARN exists on first use.
            # Keep validation in the same critical section as init so close()
            # cannot null self._client between lock acquisitions.
            if not self._validated:
                self._validate_topic_arn()
                self._validated = True

            return self._client

    def _validate_topic_arn(self) -> None:
        """Validate that the provided topic ARN points to an existing topic.

        Raises:
            InvalidArgumentException: If topic ARN is invalid or empty
            SubstrateSdkException: If topic does not exist or is inaccessible
        """
        if not self._topic_arn or not self._topic_arn.strip():
            raise InvalidArgumentException("Topic ARN cannot be null or empty")

        # Use GetTopicAttributes to verify the topic exists
        # Let exceptions propagate to client layer for translation
        self._client.get_topic_attributes(TopicArn=self._topic_arn)
