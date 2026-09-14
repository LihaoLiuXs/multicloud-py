"""GCP Pub/Sub topic implementation."""

from __future__ import annotations

import re
import threading
from typing import List, Optional

import google.auth
from google.api_core import exceptions as gcp_exceptions
from google.api_core.retry import Retry, if_exception_type
from google.pubsub_v1 import PublisherClient, PublishRequest, PubsubMessage
from multicloud.common.credentials import CredentialsOverrider
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
)
from multicloud.gcp_common.gcp_credentials import DEFAULT_SCOPES, get_credentials
from multicloud.pubsub.pubsub_client.batcher import Batcher
from multicloud.pubsub.pubsub_client.driver import TOPIC_REGISTRY, BaseTopic, Message

# Default ceiling (seconds) bounding a publish call, applied as both the
# per-attempt RPC timeout and the total retry deadline.
DEFAULT_PUBLISH_TIMEOUT_SECONDS = 120.0

# Pub/Sub caps a PublishRequest at 10_000_000 bytes (decimal, not 10 MiB).
# get_byte_size() counts body plus raw metadata bytes and omits protobuf
# framing (per-attribute map entries, per-message tags, topic field). At the
# configured maxima — 1000 messages with 100 attributes each — that undercount
# is ~606 KiB, which overflows a 9 MiB cap. 8 MiB leaves enough headroom.
_MAX_PUBLISH_BYTE_SIZE = 8 * 1024 * 1024

# Hand-maintained snapshot of gapic's retryable set for Publish, pinned so our
# bounded-deadline Retry does not retry a different set than the generated client.
# Not derived from the library: update by hand if its defaults change.
_PUBLISH_RETRY_PREDICATE = if_exception_type(
    gcp_exceptions.Aborted,
    gcp_exceptions.Cancelled,
    gcp_exceptions.DeadlineExceeded,
    gcp_exceptions.InternalServerError,
    gcp_exceptions.ResourceExhausted,
    gcp_exceptions.ServiceUnavailable,
    gcp_exceptions.Unknown,
)


@TOPIC_REGISTRY.register("gcp")
class GcpTopic(BaseTopic):
    """GCP Pub/Sub implementation of BaseTopic.

    Uses the google-cloud-pubsub library to publish messages to GCP Pub/Sub topics.
    Supports batching up to 1000 messages or 8 MiB per batch (headroom under the
    10_000_000-byte PublishRequest limit for protobuf framing).
    """

    def __init__(
        self,
        *,
        topic_name: str,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        transport: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
        publish_timeout: float = DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize GCP topic.

        Args:
            topic_name: Full resource name in format
                projects/{project-id}/topics/{topic-id}
            region: Ignored for GCP (topics are global within a project)
            endpoint: Optional endpoint override for testing
            transport: Transport type ("grpc", "rest", or None for default gRPC).
                Use "rest" for VCR.py compatibility in tests.
            credentials_overrider: Override default credential resolution.
                Optional.
            publish_timeout: Ceiling in seconds bounding each publish call. Used
                as both the per-attempt RPC timeout and the total retry deadline.
        """
        self._validate_topic_name(topic_name)
        if publish_timeout <= 0:
            raise InvalidArgumentException("publish_timeout must be greater than 0")
        super().__init__(
            topic_name=topic_name,
            region=region,
            endpoint=endpoint,
            credentials_overrider=credentials_overrider,
        )
        self._publisher: Optional[PublisherClient] = None
        self._publisher_lock = threading.Lock()
        self._endpoint = endpoint
        self._transport = transport
        self._credentials = None
        self._publish_timeout = publish_timeout
        # Retry with the deadline bounded to publish_timeout so retries stay
        # within the caller's window (see _PUBLISH_RETRY_PREDICATE).
        self._publish_retry = Retry(
            initial=0.1,
            maximum=60.0,
            multiplier=4,
            predicate=_PUBLISH_RETRY_PREDICATE,
            deadline=publish_timeout,
        )

    def create_batcher_options(self) -> Batcher.Options:
        """Override batcher options to align with GCP Pub/Sub limits.

        GCP allows 1000 messages per Publish RPC and 10_000_000 bytes per
        request. The byte cap is 8 MiB so protobuf framing omitted by
        get_byte_size() cannot push a full batch over the server limit.
        """
        return Batcher.Options(
            max_handlers=2,
            min_batch_size=1,
            max_batch_size=1000,
            max_batch_byte_size=_MAX_PUBLISH_BYTE_SIZE,
        )

    def get_max_message_size_bytes(self) -> int:
        """Return the maximum message size for GCP Pub/Sub.

        Returns:
            8 MiB, matching the batch byte cap so a max-size message fits
            in a one-item batch.
        """
        return _MAX_PUBLISH_BYTE_SIZE

    def do_send_batch(self, messages: List[Message]) -> None:
        """Provider-specific batch send implementation for GCP Pub/Sub.

        Sends the whole batch as one synchronous unary Publish RPC (bounded by
        publish_timeout), so it succeeds or fails as a unit and a failure surfaces
        as soon as the RPC completes.

        Args:
            messages: List of messages to publish

        Raises:
            Native GCP exceptions propagate to client layer for translation
        """
        if not messages:
            return

        client = self._get_or_create_client()

        pubsub_messages = []
        for msg in messages:
            data = msg.body if msg.body else b""

            # GCP requires string attribute values. Metadata maps 1:1 onto
            # PubsubMessage attributes.
            attributes = {}
            if msg.metadata:
                for k, v in msg.metadata.items():
                    if not isinstance(v, str):
                        raise InvalidArgumentException(
                            f"GCP Pub/Sub requires string metadata values. "
                            f"Key '{k}' has type {type(v).__name__}"
                        )
                    attributes[k] = v

            pubsub_messages.append(PubsubMessage(data=data, attributes=attributes))

        # Single unary RPC carrying the whole batch, bounded by publish_timeout.
        request = PublishRequest(topic=self.topic_name, messages=pubsub_messages)
        client.publish(
            request=request,
            timeout=self._publish_timeout,
            retry=self._publish_retry,
        )

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate GCP Pub/Sub exceptions to common exception hierarchy.

        Maps GCP gRPC status codes to appropriate SubstrateSdkException types:
        - NOT_FOUND → ResourceNotFoundException
        - PERMISSION_DENIED, UNAUTHENTICATED → UnAuthorizedException
        - INVALID_ARGUMENT, OUT_OF_RANGE → InvalidArgumentException
        - DEADLINE_EXCEEDED, ABORTED, RetryError → DeadlineExceededException
        - RESOURCE_EXHAUSTED → ResourceExhaustedException
        - ALREADY_EXISTS → ResourceAlreadyExistsException
        - FAILED_PRECONDITION → FailedPreconditionException
        - UNIMPLEMENTED → UnsupportedOperationException
        - All unmapped errors → UnknownException

        Args:
            exc: The GCP-specific exception

        Returns:
            Appropriate SubstrateSdkException subclass
        """
        if isinstance(exc, SubstrateSdkException):
            return exc

        from multicloud.common.exceptions import (
            DeadlineExceededException,
            FailedPreconditionException,
            InvalidArgumentException,
            ResourceAlreadyExistsException,
            ResourceExhaustedException,
            ResourceNotFoundException,
            UnAuthorizedException,
            UnknownException,
            UnsupportedOperationException,
        )

        # GCP gRPC status code mappings
        # https://github.com/grpc/grpc/blob/master/doc/statuscodes.md
        if isinstance(exc, gcp_exceptions.NotFound):
            return ResourceNotFoundException(str(exc))
        elif isinstance(
            exc, (gcp_exceptions.Unauthenticated, gcp_exceptions.PermissionDenied)
        ):
            return UnAuthorizedException(str(exc))
        elif isinstance(exc, gcp_exceptions.InvalidArgument):
            return InvalidArgumentException(str(exc))
        elif isinstance(exc, gcp_exceptions.DeadlineExceeded):
            return DeadlineExceededException(str(exc))
        elif isinstance(exc, gcp_exceptions.RetryError):
            return DeadlineExceededException(str(exc))
        elif isinstance(exc, gcp_exceptions.AlreadyExists):
            return ResourceAlreadyExistsException(str(exc))
        elif isinstance(exc, gcp_exceptions.ResourceExhausted):
            return ResourceExhaustedException(str(exc))
        elif isinstance(exc, gcp_exceptions.FailedPrecondition):
            return FailedPreconditionException(str(exc))
        elif isinstance(exc, gcp_exceptions.Aborted):
            return DeadlineExceededException(str(exc))
        elif isinstance(exc, gcp_exceptions.OutOfRange):
            return InvalidArgumentException(str(exc))
        elif isinstance(exc, gcp_exceptions.MethodNotImplemented):
            return UnsupportedOperationException(str(exc))
        elif isinstance(exc, (gcp_exceptions.Cancelled, gcp_exceptions.Unknown)):
            return UnknownException(str(exc))
        elif isinstance(
            exc,
            (
                gcp_exceptions.InternalServerError,
                gcp_exceptions.ServiceUnavailable,
                gcp_exceptions.DataLoss,
            ),
        ):
            return UnknownException(str(exc))
        elif isinstance(exc, gcp_exceptions.GoogleAPICallError):
            return UnknownException(str(exc))
        else:
            return UnknownException(str(exc))

    def get_provider_id(self) -> str:
        """Return the provider identifier."""
        return "gcp"

    def execute_before_send_batch_hooks(self, messages: List[Message]) -> None:
        """GCP topic does not use before-send hooks."""
        pass

    def execute_after_send_batch_hooks(self, messages: List[Message]) -> None:
        """GCP topic does not use after-send hooks."""
        pass

    def close(self) -> None:
        """Close the GCP topic and release resources."""
        try:
            super().close()
        finally:
            with self._publisher_lock:
                if self._publisher is not None:
                    # Release the transport channel (no background threads).
                    self._publisher.transport.close()
                self._publisher = None

    def _get_or_create_client(self) -> PublisherClient:
        """Lazily initialize the GCP Publisher client (thread-safe)."""
        with self._publisher_lock:
            if self._publisher is None:
                client_options = {}
                if self._endpoint:
                    client_options["api_endpoint"] = self._endpoint

                # Resolve credentials based on override or ADC
                if self._credentials_overrider is not None:
                    resolved = get_credentials(
                        self._credentials_overrider, scopes=DEFAULT_SCOPES
                    )
                    if resolved is not None:
                        self._credentials = resolved
                    else:
                        # Unsupported override type → fall through to ADC
                        self._credentials, _ = google.auth.default(
                            scopes=DEFAULT_SCOPES
                        )
                else:
                    # Use Application Default Credentials (google.auth.default())
                    self._credentials, _ = google.auth.default(scopes=DEFAULT_SCOPES)

                # Create publisher with resolved credentials and transport
                if self._transport:
                    self._publisher = PublisherClient(
                        credentials=self._credentials,
                        transport=self._transport,
                        client_options=client_options,
                    )
                else:
                    self._publisher = PublisherClient(
                        credentials=self._credentials,
                        client_options=client_options,
                    )

            return self._publisher

    @staticmethod
    def _validate_topic_name(topic_name: str) -> None:
        """Validate that topic name is in correct GCP format.

        Args:
            topic_name: The topic name to validate

        Raises:
            InvalidArgumentException: If topic name is invalid
        """
        if not topic_name or not topic_name.strip():
            raise InvalidArgumentException("Topic name cannot be null or empty")

        # Expected format: projects/{project-id}/topics/{topic-id}
        # Strict validation rejects empty segments and double slashes
        if not re.match(r"^projects/[^/]+/topics/[^/]+$", topic_name):
            raise InvalidArgumentException(
                f"Topic name must be in format: projects/{{projectId}}/topics/{{topicId}}, "
                f"got: {topic_name}"
            )
