"""GCP Pub/Sub subscription implementation."""

from __future__ import annotations

import re
import threading
from datetime import timedelta
from typing import List, Optional

import google.auth
from google.api_core import exceptions as gcp_exceptions
from google.cloud import pubsub_v1
from multicloud.common.credentials import CredentialsOverrider
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
)
from multicloud.gcp_common.gcp_credentials import DEFAULT_SCOPES, get_credentials
from multicloud.pubsub.pubsub_client.batcher import Batcher
from multicloud.pubsub.pubsub_client.driver import (
    SUBSCRIPTION_REGISTRY,
    AckID,
    AckInfo,
    BaseSubscription,
    GetAttributeResult,
    Message,
)


class GcpAckID:
    """GCP-specific acknowledgment ID wrapper.

    GCP Pub/Sub uses acknowledgment ID strings that must be preserved exactly for
    acknowledge() and modifyAckDeadline() operations. These IDs are opaque tokens
    returned by the pull() API. This class validates and wraps them for type safety.
    """

    def __init__(self, ack_id: str):
        if not ack_id or not ack_id.strip():
            raise InvalidArgumentException("AckID string cannot be null or empty")
        self._ack_id = ack_id

    def __str__(self) -> str:
        return self._ack_id

    def __repr__(self) -> str:
        return f"GcpAckID({self._ack_id!r})"


@SUBSCRIPTION_REGISTRY.register("gcp")
class GcpSubscription(BaseSubscription):
    """GCP Pub/Sub implementation of BaseSubscription.

    Uses the google-cloud-pubsub library to receive messages from GCP Pub/Sub subscriptions.
    Supports pull-based message retrieval with configurable batching.
    """

    # Pub/Sub caps ModifyAckDeadline at 600s (10 minutes)
    # https://cloud.google.com/pubsub/docs/reference/rest/v1/projects.subscriptions/modifyAckDeadline
    PUBSUB_MAX_ACK_DEADLINE_SECONDS = 600

    def __init__(
        self,
        *,
        subscription_name: str,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        nack_visibility_timeout: Optional[timedelta] = None,
        nack_lazy: bool = False,
        transport: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
    ) -> None:
        """Initialize GCP subscription.

        Args:
            subscription_name: Full resource name in format
                projects/{project-id}/subscriptions/{subscription-id}
            region: Ignored for GCP (subscriptions are global within a project)
            endpoint: Optional endpoint override for testing
            nack_visibility_timeout: Default visibility timeout for nacked messages
            nack_lazy: If True, nacks will not call ModifyAckDeadline
                (messages redelivered after existing deadline expires)
            transport: Transport type ("grpc", "rest", or None for default gRPC).
                Use "rest" for VCR.py compatibility in tests.
            credentials_overrider: Override default credential resolution.
                Optional.
        """
        self._validate_subscription_name(subscription_name)
        super().__init__(
            subscription_name=subscription_name,
            region=region,
            endpoint=endpoint,
            nack_visibility_timeout=nack_visibility_timeout,
            credentials_overrider=credentials_overrider,
        )
        self._subscriber: Optional[pubsub_v1.SubscriberClient] = None
        self._subscriber_lock = threading.Lock()
        self._endpoint = endpoint
        self._nack_lazy = nack_lazy
        self._transport = transport
        self._credentials = None

    def do_receive_batch(self, batch_size: int) -> List[Message]:
        """Provider-specific receive batch implementation for GCP Pub/Sub.

        Args:
            batch_size: Maximum number of messages to receive

        Returns:
            List of received messages with ack IDs populated

        Raises:
            SubstrateSdkException: If receiving fails
        """
        client = self._get_or_create_client()

        # GCP pull request
        request = {
            "subscription": self.subscription_name,
            "max_messages": max(1, batch_size),
            "return_immediately": True,  # TODO: replace deprecated flag with equivalent non-blocking pull behavior
        }
        response = client.pull(request=request)

        received_messages = []
        for received_message in response.received_messages:
            msg = self._convert_to_message(received_message)
            received_messages.append(msg)

        return received_messages

    def do_send_acks(self, ack_ids: List[AckID]) -> None:
        """Provider-specific ack batch implementation for GCP Pub/Sub.

        Args:
            ack_ids: List of acknowledgment IDs to acknowledge

        Raises:
            SubstrateSdkException: If acknowledgment fails
        """
        if not ack_ids:
            return

        client = self._get_or_create_client()

        # Convert AckID objects to strings
        ack_id_strings = [str(ack_id) for ack_id in ack_ids]

        request = {
            "subscription": self.subscription_name,
            "ack_ids": ack_id_strings,
        }
        client.acknowledge(request=request)

    def do_send_nacks(self, nacks: List[AckInfo]) -> None:
        """Provider-specific nack batch implementation for GCP Pub/Sub.

        Args:
            nacks: List of nack info objects

        Raises:
            SubstrateSdkException: If negative acknowledgment fails
        """
        # Lazy mode waits for the existing deadline to expire instead of calling ModifyAckDeadline
        if self._nack_lazy:
            return

        if not nacks:
            return

        client = self._get_or_create_client()

        # Two layers of nack visibility timeout:
        #   1) subscription-level default (nack_visibility_timeout constructor parameter)
        #   2) per-message override (AckInfo.visibility_timeout)
        # Resolve per-message > subscription default for each AckInfo, then bucket ack ids by the
        # resolved deadline because the provider API only accepts one deadline per request.
        subscription_default = self.nack_visibility_timeout

        ack_ids_by_deadline: dict[int, List[str]] = {}
        for nack in nacks:
            # Per-message override wins; otherwise fall back to the subscription default
            timeout_to_apply = (
                nack.visibility_timeout
                if nack.visibility_timeout is not None
                else subscription_default
            )
            # Pub/Sub only accepts 0 (immediate redelivery) ... 600s (10 minutes)
            ack_deadline_seconds = min(
                int(timeout_to_apply.total_seconds()),
                self.PUBSUB_MAX_ACK_DEADLINE_SECONDS,
            )
            if ack_deadline_seconds not in ack_ids_by_deadline:
                ack_ids_by_deadline[ack_deadline_seconds] = []
            ack_ids_by_deadline[ack_deadline_seconds].append(str(nack.ack_id))

        for deadline_seconds, ack_ids_for_this_deadline in ack_ids_by_deadline.items():
            request = {
                "subscription": self.subscription_name,
                "ack_ids": ack_ids_for_this_deadline,
                "ack_deadline_seconds": deadline_seconds,
            }
            client.modify_ack_deadline(request=request)

    def can_nack(self) -> bool:
        """GCP Pub/Sub supports negative acknowledgment."""
        return True

    def is_retryable(self, error: BaseException) -> bool:
        """Check if an error is retryable.

        Args:
            error: The error to check

        Returns:
            True if the error is transient and retryable
        """
        return isinstance(error, gcp_exceptions.DeadlineExceeded)

    def get_attributes(self) -> GetAttributeResult:
        """Retrieve subscription attributes from GCP.

        Returns:
            GetAttributeResult with subscription metadata

        Raises:
            SubstrateSdkException: If retrieval fails
        """
        client = self._get_or_create_client()

        request = {"subscription": self.subscription_name}
        subscription = client.get_subscription(request=request)

        return GetAttributeResult(
            name=subscription.name,
            topic=subscription.topic,
            approximate_number_of_messages=None,  # GCP doesn't expose this directly
        )

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate GCP Pub/Sub exceptions to common exception hierarchy.

        Maps GCP gRPC status codes to appropriate SubstrateSdkException types:
        - NOT_FOUND → ResourceNotFoundException
        - PERMISSION_DENIED, UNAUTHENTICATED → UnAuthorizedException
        - INVALID_ARGUMENT, OUT_OF_RANGE → InvalidArgumentException
        - DEADLINE_EXCEEDED, ABORTED → DeadlineExceededException
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

    def create_receive_batcher_options(self) -> Batcher.Options:
        """Override receive batcher options for GCP Pub/Sub limits.

        GCP Pub/Sub returns at most 1000 messages per RPC.
        """
        return Batcher.Options(
            max_handlers=10,
            min_batch_size=1,
            max_batch_size=1000,
            max_batch_byte_size=0,
        )

    def create_ack_batcher_options(self) -> Batcher.Options:
        """Override ack batcher options for GCP Pub/Sub limits."""
        return Batcher.Options(
            max_handlers=2, min_batch_size=1, max_batch_size=1000, max_batch_byte_size=0
        )

    def close(self) -> None:
        """Close the GCP subscription and release resources."""
        try:
            super().close()
        finally:
            with self._subscriber_lock:
                if self._subscriber is not None:
                    try:
                        self._subscriber.close()
                    except AttributeError:
                        # REST transport doesn't have grpc_channel, safe to ignore
                        pass
                    self._subscriber = None

    def _get_or_create_client(self) -> pubsub_v1.SubscriberClient:
        """Lazily initialize the GCP Subscriber client with thread-safe locking."""
        with self._subscriber_lock:
            if self._subscriber is None:
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

                # Create subscriber with resolved credentials and transport
                if self._transport:
                    self._subscriber = pubsub_v1.SubscriberClient(
                        credentials=self._credentials,
                        transport=self._transport,
                        **client_options,
                    )
                else:
                    self._subscriber = pubsub_v1.SubscriberClient(
                        credentials=self._credentials, **client_options
                    )

        return self._subscriber

    def _convert_to_message(self, received_message) -> Message:
        """Convert a GCP ReceivedMessage to our Message format.

        Args:
            received_message: GCP ReceivedMessage object

        Returns:
            Message with ack_id and loggable_id populated
        """
        pubsub_message = received_message.message
        ack_id = received_message.ack_id

        return (
            Message.builder()
            .with_body(pubsub_message.data)
            .with_metadata(dict(pubsub_message.attributes))
            .with_ack_id(GcpAckID(ack_id))
            .with_loggable_id(pubsub_message.message_id)
            .build()
        )

    @staticmethod
    def _validate_subscription_name(subscription_name: str) -> None:
        """Validate that subscription name is in correct GCP format.

        Args:
            subscription_name: The subscription name to validate

        Raises:
            InvalidArgumentException: If subscription name is invalid
        """
        if not subscription_name or not subscription_name.strip():
            raise InvalidArgumentException("Subscription name cannot be null or empty")

        # Expected format: projects/{project-id}/subscriptions/{subscription-id}
        # Strict validation rejects empty segments and double slashes
        if not re.match(r"^projects/[^/]+/subscriptions/[^/]+$", subscription_name):
            raise InvalidArgumentException(
                f"Subscription name must be in format: "
                f"projects/{{projectId}}/subscriptions/{{subscriptionId}}, "
                f"got: {subscription_name}"
            )
