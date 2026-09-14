"""Driver-level abstractions for pubsub operations.

The driver layer defines the abstract contract every provider implements.
``BaseTopic`` and ``BaseSubscription`` expose public methods that validate
input and delegate to ``do_*`` template methods, which providers override
with substrate-specific code.

Users never reach this layer directly — they construct a ``TopicClient``
or ``SubscriptionClient`` (in ``client.py``), which holds a ``BaseTopic``
or ``BaseSubscription`` instance internally.

Organization
------------
This module contains (in order):

1. **Registries**
   - TOPIC_REGISTRY, SUBSCRIPTION_REGISTRY

2. **Data classes for messages and acknowledgments**
   - AckID, AckInfo, Message (with Builder), GetAttributeResult

3. **Validation utilities**
   - MessageValidator

4. **Base driver classes**
   - BaseTopic, BaseSubscription
"""

from __future__ import annotations

import math
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from threading import Condition, Lock
from typing import (
    TYPE_CHECKING,
    Callable,
    Deque,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    TypeVar,
)

from multicloud.common.credentials import CredentialsOverrider
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
    SubstrateSdkException,
)
from multicloud.common.provider import Provider, ProviderRegistry
from multicloud.pubsub.pubsub_client.batcher import Batcher

# Service-scoped registries. Provider distributions advertise themselves
# under entry-point groups; the registries import those modules lazily on
# first use, at which point each provider's
# ``@TOPIC_REGISTRY.register("<provider>")`` /
# ``@SUBSCRIPTION_REGISTRY.register("<provider>")`` decorator stamps the class.
TOPIC_REGISTRY = ProviderRegistry(
    service_name="pubsub-topic",
    entry_point_group="multicloud.pubsub.topic.providers",
)
SUBSCRIPTION_REGISTRY = ProviderRegistry(
    service_name="pubsub-subscription",
    entry_point_group="multicloud.pubsub.subscription.providers",
)


class AckID(Protocol):
    """Marker protocol for provider-specific acknowledgment IDs."""


@dataclass(frozen=True)
class AckInfo:
    """Single ack or nack operation descriptor."""

    ack_id: AckID
    is_ack: bool
    visibility_timeout: Optional[timedelta] = None


AsFunction = Callable[[object], bool]
SendHook = Callable[[AsFunction], Optional[Exception]]


@dataclass
class Message:
    """Message represents a message in the pubsub system.

    Messages contain the actual data being transmitted along with metadata and
    acknowledgment information when received from subscriptions.

    Message is mutable by design because it is assembled incrementally through
    the builder and may be enriched during pubsub flow (for example: metadata,
    acknowledgement id, loggable id, and send hooks).
    Use Message.builder() to create new messages.

    Attributes:
        loggable_id: Loggable ID for this message (useful for debugging).
            This is set for received messages. Should not be set for outgoing messages.
        body: The message body content as bytes.
        metadata: A map of metadata key-value pairs.
        ack_id: The acknowledgment ID for this message. Only set for received messages.
        as_func: Optional function for driver-specific type conversion.
        before_send: Optional callback invoked before sending.
        after_send: Optional callback invoked after sending.
    """

    loggable_id: Optional[str] = None
    body: Optional[bytes] = None
    metadata: Dict[str, str] = field(default_factory=dict)
    ack_id: Optional[AckID] = None
    as_func: Optional[AsFunction] = None
    before_send: Optional[SendHook] = None
    after_send: Optional[SendHook] = None

    def get_byte_size(self) -> int:
        """Return the encoded size used for per-message and batch limits.

        Includes message body bytes plus metadata key/value UTF-8 byte lengths.
        """
        size = len(self.body or b"")
        for key, value in (self.metadata or {}).items():
            size += len(str(key).encode("utf-8"))
            size += len(str(value).encode("utf-8"))
        return size

    def as_type(self, target: object) -> bool:
        """Convert the message to a driver-specific type.

        Args:
            target: The target type to convert to

        Returns:
            True if the conversion was successful, False otherwise
        """
        if self.as_func is None:
            return False
        return self.as_func(target)

    class Builder:
        """Builder for creating Message instances.

        Example:
            >>> message = (Message.builder()
            ...     .with_body("Hello World")
            ...     .with_metadata("key", "value")
            ...     .build())
        """

        def __init__(self) -> None:
            self._message = Message()

        def with_body(self, body: object) -> "Message.Builder":
            """Set the message body.

            Args:
                body: The message body as bytes or string (will be UTF-8 encoded)

            Returns:
                This builder instance for chaining

            Raises:
                TypeError: if body is not bytes or str
            """
            if isinstance(body, str):
                self._message.body = body.encode()
            elif isinstance(body, bytes):
                self._message.body = body
            else:
                raise TypeError("Message body must be bytes or str")
            return self

        def with_metadata(
            self, metadata: object, value: Optional[str] = None
        ) -> "Message.Builder":
            """Set the message metadata.

            Can be called with a dict to set all metadata, or with key-value pairs
            to add individual metadata entries.

            Args:
                metadata: Either a dict of metadata key-value pairs, or a string key
                value: If metadata is a string key, this is the value. Defaults to empty string.

            Returns:
                This builder instance for chaining

            Raises:
                TypeError: if metadata is neither dict nor str
            """
            if isinstance(metadata, dict):
                self._message.metadata = dict(metadata)
            elif isinstance(metadata, str):
                if self._message.metadata is None:
                    self._message.metadata = {}
                self._message.metadata[metadata] = "" if value is None else value
            else:
                raise TypeError("metadata must be dict or key string")
            return self

        def with_loggable_id(self, loggable_id: str) -> "Message.Builder":
            """Set the loggable ID (typically used internally by drivers).

            Args:
                loggable_id: The loggable ID

            Returns:
                This builder instance for chaining
            """
            self._message.loggable_id = loggable_id
            return self

        def with_ack_id(self, ack_id: AckID) -> "Message.Builder":
            """Set the acknowledgment ID (typically used internally by drivers).

            Args:
                ack_id: The acknowledgment ID

            Returns:
                This builder instance for chaining
            """
            self._message.ack_id = ack_id
            return self

        def with_as_func(self, as_func: AsFunction) -> "Message.Builder":
            """Set the as function for driver-specific type conversion.

            Args:
                as_func: The as function

            Returns:
                This builder instance for chaining
            """
            self._message.as_func = as_func
            return self

        def with_before_send(self, before_send: SendHook) -> "Message.Builder":
            """Set the before send callback.

            Args:
                before_send: The before send callback

            Returns:
                This builder instance for chaining
            """
            self._message.before_send = before_send
            return self

        def with_after_send(self, after_send: SendHook) -> "Message.Builder":
            """Set the after send callback.

            Args:
                after_send: The after send callback

            Returns:
                This builder instance for chaining
            """
            self._message.after_send = after_send
            return self

        def build(self) -> "Message":
            """Build and return the configured Message instance.

            Returns:
                The Message with the configured properties
            """
            return self._message

    @staticmethod
    def builder() -> "Message.Builder":
        """Create a new message builder.

        Returns:
            A new Builder instance
        """
        return Message.Builder()


@dataclass(frozen=True)
class GetAttributeResult:
    """Result of getting subscription attributes.

    Attributes:
        name: Subscription name or identifier (provider-specific format)
        topic: Topic name or resource identifier (provider-specific format)
        approximate_number_of_messages: Approximate count of messages in subscription (optional)
    """

    name: Optional[str] = None
    topic: Optional[str] = None
    approximate_number_of_messages: Optional[int] = None


class MessageValidator:
    """Input validator for message operations."""

    @staticmethod
    def validate_message(message: Message) -> None:
        """Validate that a message is not null and has valid content.

        Args:
            message: The message to validate

        Raises:
            InvalidArgumentException: if the message is invalid
        """
        if message is None:
            raise InvalidArgumentException("Message cannot be null")

        if message.loggable_id is not None:
            raise InvalidArgumentException(
                "LoggableID cannot be set on outgoing messages. "
                "This field is reserved for received messages and is set internally by drivers."
            )

        if message.metadata is not None:
            MessageValidator._validate_metadata(message.metadata)

    @staticmethod
    def validate_message_batch(messages: List[Message]) -> None:
        """Validate a list of messages for batch operations.

        Args:
            messages: The messages to validate

        Raises:
            InvalidArgumentException: if any message is invalid
        """
        if messages is None:
            raise InvalidArgumentException("Messages list cannot be null")

        for idx, message in enumerate(messages):
            if message is None:
                raise InvalidArgumentException(f"Message at index {idx} cannot be null")
            MessageValidator.validate_message(message)

    @staticmethod
    def calculate_byte_size(message: Message) -> int:
        """Calculate the byte size of a message body.

        Args:
            message: The message to calculate size for

        Returns:
            The message body byte size, or 0 if message or body is None
        """
        if message is None:
            return 0

        if message.body is not None:
            return len(message.body)

        return 0

    @staticmethod
    def _validate_metadata(metadata: dict) -> None:
        """Validate metadata keys and values.

        Args:
            metadata: The metadata dictionary to validate

        Raises:
            InvalidArgumentException: if metadata contains invalid keys or values
        """
        for key, value in metadata.items():
            if key is None or key.strip() == "":
                raise InvalidArgumentException("Metadata keys cannot be null or empty")

            if not MessageValidator._is_valid_utf8_string(key):
                raise InvalidArgumentException(
                    f"Metadata key is not valid UTF-8: {key}"
                )

            if value is not None and not MessageValidator._is_valid_utf8_string(value):
                raise InvalidArgumentException(
                    f"Metadata value is not valid UTF-8 for key: {key}"
                )

    @staticmethod
    def _is_valid_utf8_string(s: str) -> bool:
        """Check if a string is valid UTF-8.

        Args:
            s: The string to validate

        Returns:
            True if the string is valid UTF-8, False otherwise
        """
        if s is None:
            return True

        try:
            s.encode("utf-8")
            return True
        except UnicodeEncodeError:
            return False


T_Topic = TypeVar("T_Topic", bound="BaseTopic")


class BaseTopic(Provider, ABC):
    """Base class for provider topic drivers.

    Providers override do_send_batch() to implement substrate-specific
    publishing logic. The public send() method handles validation and
    batching before delegating to the provider hook.
    """

    def __init__(
        self,
        *,
        topic_name: str,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
    ) -> None:
        if not topic_name:
            raise InvalidArgumentException("topic_name is required")
        self.topic_name = topic_name
        self.region = region
        self.endpoint = endpoint
        self._credentials_overrider = credentials_overrider
        self._is_shutdown = False
        self._shutdown_lock = threading.Lock()
        self._batcher = Batcher(self._handle_batch, self.create_batcher_options())

    def create_batcher_options(self) -> Batcher.Options:
        """Provider-overridable batching defaults."""
        return Batcher.Options(
            max_handlers=1,
            min_batch_size=1,
            max_batch_size=0,
            max_batch_byte_size=0,
        )

    def send(self, message: Message) -> None:
        """Send one message synchronously."""
        with self._shutdown_lock:
            if self._is_shutdown:
                raise FailedPreconditionException("Topic has been shut down")
        self._send_batch([message])

    def _send_batch(self, messages: List[Message]) -> None:
        with self._shutdown_lock:
            if self._is_shutdown:
                raise FailedPreconditionException("Topic has been shut down")

        # Single-pass validation: validates message structure AND metadata keys together
        # Check for None first (before empty list check)
        if messages is None:
            raise InvalidArgumentException("Messages list cannot be null")

        if not messages:
            return

        for idx, message in enumerate(messages):
            if message is None:
                raise InvalidArgumentException(f"Message at index {idx} cannot be null")
            MessageValidator.validate_message(message)
            self._validate_metadata_keys(message)
            message_size = message.get_byte_size()
            max_size = self.get_max_message_size_bytes()
            if message_size > max_size:
                raise InvalidArgumentException(
                    f"Message size ({message_size} bytes) exceeds "
                    f"maximum allowed size ({max_size} bytes)"
                )

        # Add all messages to batcher and wait for completion to ensure synchronous behavior
        futures: List[Future[None]] = []
        for message in messages:
            futures.append(self._batcher.add_no_wait(message))

        for future in futures:
            future.result()  # Let exceptions propagate to client layer for translation

    def _handle_batch(self, messages: List[Message]) -> None:
        if self._is_shutdown:
            raise FailedPreconditionException("Topic has been shut down")
        self.execute_before_send_batch_hooks(messages)
        try:
            self.do_send_batch(messages)
        finally:
            self.execute_after_send_batch_hooks(messages)

    def _validate_metadata_keys(self, message: Message) -> None:
        """Validate that message metadata does not contain provider-reserved keys.

        Args:
            message: The message to validate

        Raises:
            InvalidArgumentException: if metadata contains reserved keys
        """
        if message.metadata:
            reserved = self._get_reserved_metadata_keys()
            if reserved:
                collisions = set(message.metadata.keys()) & reserved
                if collisions:
                    raise InvalidArgumentException(
                        f"Message metadata contains reserved keys: {', '.join(sorted(collisions))}. "
                        f"Reserved keys are: {', '.join(sorted(reserved))}"
                    )

    def _get_reserved_metadata_keys(self) -> set:
        """Return provider-specific reserved metadata keys.

        Override this method in provider implementations to specify keys that
        cannot be used in message metadata (e.g., keys that collide with
        provider SDK parameters).

        Returns:
            Set of reserved key names, or empty set if no restrictions
        """
        return set()

    @abstractmethod
    def get_max_message_size_bytes(self) -> int:
        """Return the maximum message size in bytes for this provider.

        Returns:
            Maximum message body size in bytes
        """

    @abstractmethod
    def do_send_batch(self, messages: List[Message]) -> None:
        """Provider-specific sync batch send."""

    def execute_before_send_batch_hooks(self, messages: List[Message]) -> None:
        """Hook for provider customization."""
        # Default implementation does nothing

    def execute_after_send_batch_hooks(self, messages: List[Message]) -> None:
        """Hook for provider customization."""
        # Default implementation does nothing

    def close(self) -> None:
        """Flush and close topic resources."""
        with self._shutdown_lock:
            if self._is_shutdown:
                return
            self._is_shutdown = True
        self._batcher.shutdown_and_drain()

    @property
    def is_shutdown(self) -> bool:
        return self._is_shutdown


T_Subscription = TypeVar("T_Subscription", bound="BaseSubscription")


class BaseSubscription(Provider, ABC):
    """Base class for provider subscription drivers.

    Providers override do_receive_batch(), do_send_acks(), and do_send_nacks()
    to implement substrate-specific receiving and acknowledgment logic.
    """

    def __init__(
        self,
        *,
        subscription_name: str,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        nack_visibility_timeout: Optional[timedelta] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
    ) -> None:
        if not subscription_name:
            raise InvalidArgumentException("subscription_name is required")
        self.subscription_name = subscription_name
        self.region = region
        self.endpoint = endpoint
        self._credentials_overrider = credentials_overrider

        if nack_visibility_timeout is None:
            self.nack_visibility_timeout = timedelta(seconds=0)
        elif nack_visibility_timeout < timedelta(0):
            raise InvalidArgumentException("nack_visibility_timeout cannot be negative")
        else:
            self.nack_visibility_timeout = nack_visibility_timeout

        self._is_shutdown = False
        self._lock = Lock()
        self._batch_arrived = Condition(self._lock)
        self._queue: Deque[Message] = deque()
        self._prefetch_in_flight = False
        self._permanent_error: Optional[Exception] = None
        self._unreported_ack_error: Optional[Exception] = None
        self._running_batch_size = 1.0
        self._throughput_start_ns = 0
        self._throughput_count = 0

        self._ack_batcher = Batcher(
            self._handle_ack_and_nack_batch, self.create_ack_batcher_options()
        )
        self._background_pool = ThreadPoolExecutor(
            max_workers=max(1, self.create_receive_batcher_options().max_handlers)
        )

    def receive(self) -> Message:
        """Blocking receive."""
        while True:
            with self._lock:
                # Check if subscription has been shut down
                if self._is_shutdown:
                    raise FailedPreconditionException("Subscription has been shut down")
                # Check for permanent error state
                if self._permanent_error is not None:
                    self._unreported_ack_error = None
                    # Re-raise original exception for client layer to translate
                    raise self._permanent_error

                # If we have messages in queue, return one
                if self._queue:
                    self._throughput_count += 1
                    return self._queue.popleft()

                # Start a prefetch when queue is below threshold and no request is in flight.
                if not self._prefetch_in_flight:
                    threshold = int(self._running_batch_size * 0.5)
                    if len(self._queue) <= threshold:
                        batch_size = self._update_batch_size()
                        self._prefetch_in_flight = True
                        self._background_pool.submit(self._do_prefetch, batch_size)

                # No messages available, wait for prefetch completion.
                if self._prefetch_in_flight:
                    self._batch_arrived.wait()
                else:
                    self._batch_arrived.wait(timeout=0.1)

    def send_ack(self, ack_id: AckID) -> None:
        if self._is_shutdown:
            raise FailedPreconditionException("Subscription has been shut down")
        if ack_id is None:
            error = InvalidArgumentException("AckID cannot be null")
            with self._lock:
                self._permanent_error = error
            raise error
        self._ack_batcher.add_no_wait(AckInfo(ack_id=ack_id, is_ack=True))

    def send_acks(self, ack_ids: List[AckID]) -> Future[None]:
        """Send acknowledgments for a batch of messages."""
        if self._is_shutdown:
            raise FailedPreconditionException("Subscription has been shut down")

        if ack_ids is None:
            error = InvalidArgumentException("AckID list cannot be null")
            with self._lock:
                self._permanent_error = error
            raise error

        if not ack_ids:
            completed: Future[None] = Future()
            completed.set_result(None)
            return completed

        for ack_id in ack_ids:
            if ack_id is None:
                error = InvalidArgumentException(
                    "AckID cannot be null in batch acknowledgment"
                )
                with self._lock:
                    self._permanent_error = error
                raise error

        self._ack_batcher.add_all_no_wait(
            [AckInfo(ack_id=ack_id, is_ack=True) for ack_id in ack_ids]
        )

        completed: Future[None] = Future()
        completed.set_result(None)
        return completed

    def send_nack(
        self, ack_id: AckID, *, visibility_timeout: Optional[timedelta] = None
    ) -> None:
        if self._is_shutdown:
            raise FailedPreconditionException("Subscription has been shut down")
        if ack_id is None:
            error = InvalidArgumentException("AckID cannot be null")
            with self._lock:
                self._permanent_error = error
            raise error
        if visibility_timeout is not None and visibility_timeout.total_seconds() < 0:
            error = InvalidArgumentException(
                f"Nack visibility timeout cannot be negative: {visibility_timeout}"
            )
            with self._lock:
                self._permanent_error = error
            raise error
        self._ack_batcher.add_no_wait(
            AckInfo(ack_id=ack_id, is_ack=False, visibility_timeout=visibility_timeout)
        )

    def send_nacks(
        self, ack_ids: List[AckID], *, visibility_timeout: Optional[timedelta] = None
    ) -> Future[None]:
        """Send negative acknowledgments for a batch of messages."""
        if self._is_shutdown:
            raise FailedPreconditionException("Subscription has been shut down")

        if ack_ids is None:
            error = InvalidArgumentException("AckIDs list cannot be null")
            with self._lock:
                self._permanent_error = error
            raise error

        if visibility_timeout is not None and visibility_timeout.total_seconds() < 0:
            error = InvalidArgumentException(
                f"Nack visibility timeout cannot be negative: {visibility_timeout}"
            )
            with self._lock:
                self._permanent_error = error
            raise error

        if not ack_ids:
            completed: Future[None] = Future()
            completed.set_result(None)
            return completed

        for ack_id in ack_ids:
            if ack_id is None:
                error = InvalidArgumentException(
                    "AckID cannot be null in batch negative acknowledgment"
                )
                with self._lock:
                    self._permanent_error = error
                raise error

        self._ack_batcher.add_all_no_wait(
            [
                AckInfo(
                    ack_id=ack_id,
                    is_ack=False,
                    visibility_timeout=visibility_timeout,
                )
                for ack_id in ack_ids
            ]
        )

        completed: Future[None] = Future()
        completed.set_result(None)
        return completed

    def _handle_ack_and_nack_batch(self, ack_infos: List[AckInfo]) -> None:
        acks: List[AckID] = []
        nacks: List[AckInfo] = []
        for info in ack_infos:
            if info.is_ack:
                acks.append(info.ack_id)
            else:
                nacks.append(info)
        # Send acks
        if acks:
            try:
                self.do_send_acks(acks)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — store permanent error state and notify waiters
                if not self.is_retryable(exc):
                    with self._lock:
                        if self._permanent_error is None:
                            self._permanent_error = exc
                            self._unreported_ack_error = exc
                raise  # Re-raise original exception for client layer to translate
        # Send nacks
        if nacks:
            try:
                self.do_send_nacks(nacks)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — store permanent error state and notify waiters
                if not self.is_retryable(exc):
                    with self._lock:
                        if self._permanent_error is None:
                            self._permanent_error = exc
                            self._unreported_ack_error = exc
                raise  # Re-raise original exception for client layer to translate

    def _aggregate_futures(self, futures: List[Future[None]]) -> Future[None]:
        """Aggregate multiple futures into a single future."""
        composite: Future[None] = Future()

        if not futures:
            composite.set_result(None)
            return composite

        completed = [0]
        first_exception = [None]
        lock = Lock()

        def on_done(f: Future[None]) -> None:
            with lock:
                if f.exception() is not None and first_exception[0] is None:
                    first_exception[0] = f.exception()

                completed[0] += 1

                if completed[0] == len(futures):
                    if first_exception[0] is not None:
                        composite.set_exception(first_exception[0])
                    else:
                        composite.set_result(None)

        for future in futures:
            future.add_done_callback(on_done)

        return composite

    def _update_batch_size(self) -> int:
        options = self.create_receive_batcher_options()
        if options.max_handlers == 1 and options.max_batch_size == 1:
            return 1

        now_ns = time.monotonic_ns()
        if self._throughput_start_ns != 0:
            elapsed_ns = max(now_ns - self._throughput_start_ns, 100_000_000)  # 100ms
            # Update runningBatchSize based on throughput since our last time here,
            # as measured by the ratio of the number of messages returned to elapsed time.
            elapsed_sec = elapsed_ns / 1_000_000_000
            # Avoid divide-by-zero and huge numbers.
            msgs_per_sec = self._throughput_count / elapsed_sec

            # The "ideal" batch size is how many messages we'd need in the queue to
            # support desiredQueueDuration at the msgsPerSec rate.
            ideal_batch_size = 2.0 * msgs_per_sec
            # Move runningBatchSize towards the ideal.
            # We first combine the previous value and the new value, with weighting
            # based on decay, and then cap the growth/shrinkage.
            new_batch_size = self._running_batch_size * 0.5 + ideal_batch_size * 0.5

            max_growth = self._running_batch_size * 2.0
            min_shrink = self._running_batch_size * 0.75

            if new_batch_size > max_growth:
                self._running_batch_size = max_growth
            elif new_batch_size < min_shrink:
                self._running_batch_size = min_shrink
            else:
                self._running_batch_size = new_batch_size

        self._throughput_start_ns = now_ns
        self._throughput_count = 0

        max_size = 3000 if options.max_batch_size == 0 else options.max_batch_size
        capped = min(self._running_batch_size, float(max_size))
        return max(1, int(math.ceil(capped)))

    def _do_prefetch(self, batch_size: int) -> None:
        try:
            batch = self._get_next_batch(batch_size)
            with self._lock:
                self._queue.extend(batch)
                self._prefetch_in_flight = False
                self._batch_arrived.notify_all()
        except (
            Exception
        ) as exc:  # noqa: BLE001 — store error state and notify condition variable
            with self._lock:
                if not self.is_retryable(exc):
                    if self._permanent_error is None:
                        self._permanent_error = exc
                self._prefetch_in_flight = False
                self._batch_arrived.notify_all()

    def _get_next_batch(self, n_messages: int) -> List[Message]:
        batches = Batcher.split(n_messages, self.create_receive_batcher_options())
        if not batches:
            return []
        if len(batches) == 1:
            return self.do_receive_batch(batches[0])
        combined: List[Message] = []
        futures = [
            self._background_pool.submit(self.do_receive_batch, size)
            for size in batches
        ]
        for future in futures:
            combined.extend(future.result())
        return combined

    def close(self) -> None:
        if self._is_shutdown:
            return
        self._is_shutdown = True
        with self._lock:
            self._batch_arrived.notify_all()
        self._ack_batcher.shutdown_and_drain()
        self._background_pool.shutdown(wait=True)

        with self._lock:
            if self._unreported_ack_error is not None:
                ack_error = self._unreported_ack_error
                self._unreported_ack_error = None
                # Re-raise original exception for client layer to translate
                raise ack_error

    @abstractmethod
    def do_receive_batch(self, batch_size: int) -> List[Message]:
        """Provider-specific receive batch implementation."""

    @abstractmethod
    def do_send_acks(self, ack_ids: List[AckID]) -> None:
        """Provider-specific ack batch implementation."""

    @abstractmethod
    def do_send_nacks(self, nacks: List[AckInfo]) -> None:
        """Provider-specific nack batch implementation."""

    @abstractmethod
    def can_nack(self) -> bool:
        """Whether provider supports nacking."""

    @abstractmethod
    def is_retryable(self, error: BaseException) -> bool:
        """Whether the error is retryable."""

    @abstractmethod
    def get_attributes(self) -> GetAttributeResult:
        """Retrieve provider subscription attributes."""

    def create_receive_batcher_options(self) -> Batcher.Options:
        return Batcher.Options(max_handlers=1, min_batch_size=1, max_batch_size=3000)

    def create_ack_batcher_options(self) -> Batcher.Options:
        return Batcher.Options(max_handlers=1, min_batch_size=1, max_batch_size=3000)

    @property
    def is_shutdown(self) -> bool:
        return self._is_shutdown
