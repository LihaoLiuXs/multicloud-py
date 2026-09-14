"""AWS SQS subscription implementation."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional
from urllib.parse import unquote

import boto3
from multicloud.aws_common.aws_credentials import get_session
from multicloud.aws_common.error_mapping import translate_aws_sqs_error
from multicloud.common.credentials import CredentialsOverrider
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
)
from multicloud.pubsub.pubsub_aws.aws_base_topic import AwsBaseTopic
from multicloud.pubsub.pubsub_client.batcher import Batcher
from multicloud.pubsub.pubsub_client.driver import (
    SUBSCRIPTION_REGISTRY,
    AckID,
    AckInfo,
    BaseSubscription,
    GetAttributeResult,
    Message,
)


class AwsAckID:
    """AWS-specific acknowledgment ID wrapper.

    SQS uses opaque receipt handles as message acknowledgment tokens. These handles
    must be preserved exactly and passed to DeleteMessage or ChangeMessageVisibility.
    This class validates and wraps the receipt handle for type safety.
    """

    def __init__(self, receipt_handle: str):
        if not receipt_handle or not receipt_handle.strip():
            raise InvalidArgumentException("Receipt handle cannot be null or empty")
        self._receipt_handle = receipt_handle

    def __str__(self) -> str:
        return self._receipt_handle

    def __repr__(self) -> str:
        return f"AwsAckID({self._receipt_handle!r})"


@dataclass(frozen=True)
class AwsSubscriptionOptions:
    """AWS-specific subscription options.

    This options object is provider-specific by design and should be used with
    :func:`create_aws_subscription` to configure AWS-only subscription behavior
    without introducing provider-specific fields into cloud-agnostic clients.
    """

    wait_time_seconds: int = 0
    nack_lazy: bool = False
    raw_message_delivery: bool = False


def create_aws_subscription(
    *,
    subscription_name: str,
    region: Optional[str] = None,
    endpoint: Optional[str] = None,
    nack_visibility_timeout: Optional[timedelta] = None,
    options: Optional[AwsSubscriptionOptions] = None,
) -> "AwsSubscription":
    """Create an AWS subscription with provider-specific options."""
    resolved_options = options or AwsSubscriptionOptions()
    return AwsSubscription(
        subscription_name=subscription_name,
        region=region,
        endpoint=endpoint,
        nack_visibility_timeout=nack_visibility_timeout,
        nack_lazy=resolved_options.nack_lazy,
        wait_time_seconds=resolved_options.wait_time_seconds,
        raw_message_delivery=resolved_options.raw_message_delivery,
    )


@SUBSCRIPTION_REGISTRY.register("aws")
class AwsSubscription(BaseSubscription):
    """AWS SQS implementation of BaseSubscription.

    Uses boto3 SQS client to receive messages from an SQS queue.
    Supports configurable batching and visibility timeout.
    """

    # AWS SQS limits
    SQS_MAX_VISIBILITY_TIMEOUT_SECONDS = 43200  # 12 hours
    NO_MESSAGES_POLL_DURATION_MS = 250

    def __init__(
        self,
        *,
        subscription_name: str,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        nack_visibility_timeout: Optional[timedelta] = None,
        credentials_overrider: Optional["CredentialsOverrider"] = None,
        nack_lazy: bool = False,
        wait_time_seconds: int = 0,
        raw_message_delivery: bool = False,
    ) -> None:
        """Initialize AWS SQS subscription.

        Args:
            subscription_name: SQS queue name or URL
            region: AWS region (e.g., 'us-west-2')
            endpoint: Optional endpoint override for testing
            nack_visibility_timeout: Default visibility timeout for nacked messages
            credentials_overrider: Override default credential resolution
            nack_lazy: If True, nacks bypass ChangeMessageVisibility
                (messages redelivered after existing timeout expires)
            wait_time_seconds: Long polling wait time (0-20 seconds)
            raw_message_delivery: If True, treat all messages as raw SQS
                (skip SNS JSON unwrapping)
        """
        super().__init__(
            subscription_name=subscription_name,
            region=region,
            endpoint=endpoint,
            nack_visibility_timeout=nack_visibility_timeout,
            credentials_overrider=credentials_overrider,
        )
        self._client: Optional[boto3.client] = None
        self._client_lock = threading.Lock()
        self._queue_url: Optional[str] = None
        self._nack_lazy = nack_lazy
        self._wait_time_seconds = wait_time_seconds
        self._raw = raw_message_delivery

    def do_receive_batch(self, batch_size: int) -> List[Message]:
        """Provider-specific receive batch implementation for AWS SQS.

        Args:
            batch_size: Maximum number of messages to receive

        Returns:
            List of received messages with ack IDs populated

        Raises:
            SubstrateSdkException: If receiving fails
        """
        client = self._get_or_create_client()
        queue_url = self._get_queue_url()

        # SQS supports max 10 messages per request
        request = {
            "QueueUrl": queue_url,
            "MaxNumberOfMessages": min(batch_size, 10),
            "MessageAttributeNames": ["All"],
        }

        if self._wait_time_seconds > 0:
            request["WaitTimeSeconds"] = self._wait_time_seconds

        response = client.receive_message(**request)

        messages = []
        for sqs_message in response.get("Messages", []):
            msg = self._convert_to_message(sqs_message)
            messages.append(msg)

        # Sleep briefly if no messages (avoid tight loop)
        if not messages:
            time.sleep(self.NO_MESSAGES_POLL_DURATION_MS / 1000.0)

        return messages

    def do_send_acks(self, ack_ids: List[AckID]) -> None:
        """Provider-specific ack batch implementation for AWS SQS.

        Args:
            ack_ids: List of acknowledgment IDs (receipt handles) to delete

        Raises:
            SubstrateSdkException: If acknowledgment fails
        """
        if not ack_ids:
            return

        client = self._get_or_create_client()
        queue_url = self._get_queue_url()

        # SQS supports max 10 messages per batch operation
        for i in range(0, len(ack_ids), 10):
            batch = ack_ids[i : i + 10]
            entries = [
                {"Id": str(j), "ReceiptHandle": str(ack_id)}
                for j, ack_id in enumerate(batch)
            ]

            response = client.delete_message_batch(QueueUrl=queue_url, Entries=entries)

            if "Failed" in response and response["Failed"]:
                first_failure = response["Failed"][0]
                raise SubstrateSdkException(
                    f"SQS DeleteMessageBatch failed for {len(response['Failed'])} message(s): "
                    f"{first_failure.get('Code')}, {first_failure.get('Message')}"
                )

    def do_send_nacks(self, nacks: List[AckInfo]) -> None:
        """Provider-specific nack batch implementation for AWS SQS.

        Args:
            nacks: List of nack info objects

        Raises:
            SubstrateSdkException: If negative acknowledgment fails
        """
        # NackLazy mode: bypass ChangeMessageVisibility call
        # Messages will be redelivered after existing visibility timeout expires
        if self._nack_lazy:
            return

        if not nacks:
            return

        client = self._get_or_create_client()
        queue_url = self._get_queue_url()

        # Two layers of nack visibility timeout:
        #   1) subscription-level default (nack_visibility_timeout constructor parameter)
        #   2) per-message override (AckInfo.visibility_timeout)
        # Resolve per-message > subscription default for each AckInfo, then bucket ack_ids by the
        # resolved timeout because SQS ChangeMessageVisibilityBatch only accepts one timeout per request.
        subscription_default = self.nack_visibility_timeout

        ack_ids_by_deadline = {}
        for nack in nacks:
            # Per-message override wins; otherwise fall back to the subscription default
            timeout_to_apply = (
                nack.visibility_timeout
                if nack.visibility_timeout is not None
                else subscription_default
            )
            # SQS accepts 0 (immediate redelivery) ... 43200s (12 hours)
            visibility_timeout = min(
                int(timeout_to_apply.total_seconds()),
                self.SQS_MAX_VISIBILITY_TIMEOUT_SECONDS,
            )
            if visibility_timeout not in ack_ids_by_deadline:
                ack_ids_by_deadline[visibility_timeout] = []
            ack_ids_by_deadline[visibility_timeout].append(str(nack.ack_id))

        # Send one batch per unique timeout value, splitting into chunks of 10
        for timeout_seconds, ack_ids_for_deadline in ack_ids_by_deadline.items():
            # SQS supports max 10 messages per batch operation
            for i in range(0, len(ack_ids_for_deadline), 10):
                ack_ids_for_this_deadline = ack_ids_for_deadline[i : i + 10]
                entries = [
                    {
                        "Id": str(j),
                        "ReceiptHandle": ack_id,
                        "VisibilityTimeout": timeout_seconds,
                    }
                    for j, ack_id in enumerate(ack_ids_for_this_deadline)
                ]

                response = client.change_message_visibility_batch(
                    QueueUrl=queue_url, Entries=entries
                )

                # Filter out ReceiptHandleIsInvalid errors (message already processed)
                if "Failed" in response and response["Failed"]:
                    actual_failures = [
                        f
                        for f in response["Failed"]
                        if f.get("Code") != "ReceiptHandleIsInvalid"
                    ]

                    if actual_failures:
                        first_failure = actual_failures[0]
                        raise SubstrateSdkException(
                            f"SQS ChangeMessageVisibilityBatch failed for "
                            f"{len(actual_failures)} message(s): "
                            f"{first_failure.get('Code')}, {first_failure.get('Message')}"
                        )

    def can_nack(self) -> bool:
        """AWS SQS supports negative acknowledgment via visibility timeout."""
        return True

    def is_retryable(self, error: BaseException) -> bool:
        """Check if an error is retryable.

        Args:
            error: The error to check

        Returns:
            True if the error is transient and retryable
        """
        # AWS SQS doesn't have specific retryable exceptions like GCP
        # Most errors are configuration/permission issues
        return False

    def get_attributes(self) -> GetAttributeResult:
        """Retrieve subscription (queue) attributes from AWS SQS.

        Returns:
            GetAttributeResult with name (queue URL) and topic (queue ARN)

        Raises:
            SubstrateSdkException: If retrieval fails
        """
        client = self._get_or_create_client()
        queue_url = self._get_queue_url()

        response = client.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=["QueueArn"],
        )
        queue_arn = response.get("Attributes", {}).get("QueueArn")
        return GetAttributeResult(name=queue_url, topic=queue_arn)

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate AWS exceptions to common exception hierarchy.

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
        return "aws"

    def create_receive_batcher_options(self) -> Batcher.Options:
        """Override receive batcher options for AWS SQS limits.

        SQS supports max 10 messages per ReceiveMessage request.
        """
        return Batcher.Options(
            max_handlers=100, min_batch_size=1, max_batch_size=10, max_batch_byte_size=0
        )

    def create_ack_batcher_options(self) -> Batcher.Options:
        """Override ack batcher options for AWS SQS limits.

        SQS supports max 10 messages per DeleteMessageBatch request.
        """
        return Batcher.Options(
            max_handlers=100, min_batch_size=1, max_batch_size=10, max_batch_byte_size=0
        )

    def close(self) -> None:
        """Close the AWS subscription and release resources."""
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
        """Get or resolve the SQS queue URL and validate queue exists."""
        if self._queue_url:
            return self._queue_url

        # If subscription_name looks like a URL, use it directly
        if self.subscription_name.startswith("https://"):
            self._queue_url = self.subscription_name
        else:
            # Otherwise, resolve queue name to URL
            # Let exceptions propagate to client layer for translation
            client = self._get_or_create_client()
            response = client.get_queue_url(QueueName=self.subscription_name)
            self._queue_url = response["QueueUrl"]

        # Validate queue exists by calling GetQueueAttributes (fail-fast validation)
        # Let exceptions propagate to client layer for translation
        client = self._get_or_create_client()
        client.get_queue_attributes(
            QueueUrl=self._queue_url, AttributeNames=["QueueArn"]
        )

        return self._queue_url

    def _convert_to_message(self, sqs_message: dict) -> Message:
        """Convert an SQS message to our Message format.

        Handles SNS JSON unwrapping if the message came from an SNS topic.

        Args:
            sqs_message: SQS message dict from boto3

        Returns:
            Message with ack_id and loggable_id populated
        """
        body_str = sqs_message.get("Body", "")
        raw_attrs = {}

        # Extract message attributes from SQS message
        for key, attr_value in sqs_message.get("MessageAttributes", {}).items():
            raw_attrs[key] = attr_value.get("StringValue", "")

        is_raw = self._raw or bool(raw_attrs)

        # Unwrap SNS JSON messages if present (unless raw mode)
        if not is_raw:
            sns_payload = self._extract_sns_message(body_str)
            if sns_payload:
                body_str, sns_attrs = sns_payload
                raw_attrs.update(sns_attrs)

        # Decode metadata attributes
        attrs = {}
        decode_body = False

        for key, value in raw_attrs.items():
            if key == AwsBaseTopic.MetadataKeys.BASE64_ENCODED:
                decode_body = True
                continue

            decoded_key = self._decode_metadata_key(key)
            # Decode metadata value with error handling (fallback on malformed encoding)
            try:
                decoded_value = unquote(value)
            except Exception:
                # Fall back to original value on malformed URL encoding
                decoded_value = value
            attrs[decoded_key] = decoded_value

        # Decode body if base64-encoded
        if decode_body:
            try:
                body_bytes = base64.b64decode(body_str)
            except Exception:
                # Fall back to UTF-8 if base64 decode fails
                body_bytes = body_str.encode("utf-8")
        else:
            body_bytes = body_str.encode("utf-8")

        return (
            Message.builder()
            .with_body(body_bytes)
            .with_metadata(attrs)
            .with_ack_id(AwsAckID(sqs_message["ReceiptHandle"]))
            .with_loggable_id(sqs_message.get("MessageId", ""))
            .build()
        )

    @staticmethod
    def _extract_sns_message(body: str) -> Optional[tuple[str, dict]]:
        """Extract SNS message and attributes from JSON-wrapped body.

        Args:
            body: Potentially SNS-wrapped JSON string

        Returns:
            Tuple of (message, attributes) if SNS format, None otherwise
        """
        if not body or not body.strip():
            return None

        try:
            # Strip leading/trailing whitespace before parsing
            data = json.loads(body.strip())
            if not isinstance(data, dict):
                return None

            # Check for SNS structure (must have non-null TopicArn to be SNS message)
            topic_arn = data.get("TopicArn")
            if not topic_arn:
                # TopicArn missing or null → not SNS format
                return None

            # Message field can be missing → defaults to empty string
            message = data.get("Message", "")
            attributes = {}

            # Extract MessageAttributes if present
            if "MessageAttributes" in data:
                for key, attr_obj in data["MessageAttributes"].items():
                    if isinstance(attr_obj, dict) and "Value" in attr_obj:
                        attributes[key] = attr_obj["Value"]

            return message, attributes

        except (json.JSONDecodeError, KeyError, TypeError):
            pass

        return None

    @staticmethod
    def _decode_metadata_key(key: str) -> str:
        """Decode metadata key that contains hex-encoded special characters.

        Reverses the encoding: __0xHH__ → original character

        Args:
            key: Encoded key

        Returns:
            Decoded key
        """
        if not key:
            return ""

        # Pattern: __0xHH__ using variable-length hex encoding
        # Supports multi-byte Unicode (e.g., __0x1F600__ for emoji)
        pattern = r"__0x([0-9A-Fa-f]+)__"

        def replace_hex(match):
            hex_str = match.group(1)
            return chr(int(hex_str, 16))

        return re.sub(pattern, replace_hex, key)
