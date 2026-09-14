"""AWS base topic with shared constants and utilities."""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple, Optional

from multicloud.common.credentials import CredentialsOverrider
from multicloud.pubsub.pubsub_client.driver import BaseTopic


class Base64EncodingStrategy(Enum):
    """Strategy for when to base64-encode message bodies.

    AUTO: Automatically determines if encoding is required. Valid UTF-8 text is sent as-is;
          invalid sequences are base64 encoded, and a MessageAttribute with key "base64encoded"
          is added to the message.
    ALWAYS: Always base64-encode all message bodies. A MessageAttribute with key "base64encoded"
            is added to the message.
    NEVER: Never base64-encode message bodies. Non-UTF-8 bytes in message bodies may be modified
           by SNS/SQS.
    """

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class TopicOptions(NamedTuple):
    """Configuration options for AWS topics.

    Attributes:
        body_base64_encoding: Strategy for base64 encoding message bodies (default: AUTO)
    """

    body_base64_encoding: Base64EncodingStrategy = Base64EncodingStrategy.AUTO


class AwsBaseTopic(BaseTopic):
    """AWS-specific base class for SQS and SNS topics.

    Contains shared metadata keys and utilities for AWS pubsub providers.
    """

    def __init__(
        self,
        *,
        topic_name: str,
        region: str | None = None,
        endpoint: str | None = None,
        credentials_overrider: CredentialsOverrider | None = None,
        topic_options: TopicOptions | None = None,
    ):
        """Initialize AWS base topic with optional encoding configuration.

        Args:
            topic_name: Topic identifier (ARN for SNS, URL for SQS)
            region: AWS region
            endpoint: Optional endpoint override for testing
            credentials_overrider: Override default credential resolution
            topic_options: Configuration options for base64 encoding (default: AUTO)
        """
        super().__init__(
            topic_name=topic_name,
            region=region,
            endpoint=endpoint,
            credentials_overrider=credentials_overrider,
        )
        self.topic_options = (
            topic_options if topic_options is not None else TopicOptions()
        )

    class MetadataKeys:
        """Metadata keys used for setting message attributes on AWS messages (SQS and SNS)."""

        DEDUPLICATION_ID = "DeduplicationId"
        MESSAGE_GROUP_ID = "MessageGroupId"
        SUBJECT = "Subject"
        BASE64_ENCODED = "base64encoded"

    @staticmethod
    def _encode_metadata_key(key: Optional[str]) -> str:
        """Encode metadata key to be AWS-compliant.

        AWS attribute names can only contain alphanumeric, hyphen, underscore, and period.
        Special characters are encoded as __0xHH__ where HH is hex.

        Args:
            key: Original key (None is treated as empty string)

        Returns:
            Encoded key safe for AWS
        """
        if not key:
            return ""

        encoded = []
        for i, char in enumerate(key):
            if char.isalnum() or char in ("_", "-"):
                encoded.append(char)
            elif char == ".":
                # Period valid only if not at start and previous char is not period
                if i == 0 or (i > 0 and key[i - 1] == "."):
                    # If previous char was also '.', encode the previous one too (retroactively)
                    if i > 0 and encoded and encoded[-1] == ".":
                        encoded[-1] = "__0x2E__"
                    # Encode current period
                    encoded.append("__0x2E__")
                else:
                    encoded.append(char)
            else:
                # Encode special character using variable-length hex
                encoded.append(f"__0x{ord(char):X}__")

        return "".join(encoded)

    @staticmethod
    def _encode_metadata_value(value: Optional[str]) -> str:
        """Encode metadata value using URL encoding.

        Args:
            value: Metadata value to encode (None is treated as empty string)

        Returns:
            URL-encoded string
        """
        from urllib.parse import quote

        if not value:
            return ""
        return quote(value)

    @staticmethod
    def _is_valid_utf8(data: bytes) -> bool:
        """Check if bytes contain valid UTF-8."""
        try:
            data.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False

    def _should_encode_body(self, body: bytes) -> bool:
        """Decide whether the body should be base64-encoded based on encoding option.

        Args:
            body: Message body bytes

        Returns:
            True if encoding is required, False otherwise
        """
        if not body:
            return False

        strategy = self.topic_options.body_base64_encoding
        if strategy == Base64EncodingStrategy.ALWAYS:
            return True
        elif strategy == Base64EncodingStrategy.NEVER:
            return False
        else:  # AUTO
            return not self._is_valid_utf8(body)
