"""User-facing :class:`TopicClient` and :class:`SubscriptionClient` — the public entry points for pubsub ops.

Three-layer architecture:

1. **Client** (this file) — what users import; provider-agnostic API.
2. **Driver** (:class:`BaseTopic` and :class:`BaseSubscription` in ``driver.py``) —
   internal abstract contract; defines validated public methods + provider hooks.
3. **Provider** — concrete implementations that callers never reference directly.

User code stays cloud-agnostic; only the ``provider`` kwarg names a cloud::

    from multicloud.pubsub import TopicClient, SubscriptionClient, Message

    # Publishing
    with TopicClient(
        provider="aws", topic_name="my-topic", region="us-west-2"
    ) as client:
        msg = Message.builder().with_body(b"hello").build()
        client.send(msg)

    # Subscribing
    with SubscriptionClient(
        provider="aws", subscription_name="my-sub", region="us-west-2"
    ) as client:
        msg = client.receive()
        client.send_ack(msg.ack_id)
"""

from __future__ import annotations

import warnings
from concurrent.futures import Future
from datetime import timedelta
from typing import Any, List, Optional

from multicloud.common.credentials import CredentialsOverrider
from multicloud.pubsub.pubsub_client.driver import (
    SUBSCRIPTION_REGISTRY,
    TOPIC_REGISTRY,
    AckID,
    BaseSubscription,
    BaseTopic,
    GetAttributeResult,
    Message,
)


class TopicClient:
    """Cloud-agnostic topic client for publishing messages.

    The user-facing API surface for publishing. Construction takes a ``provider``
    id and a unified set of cloud-agnostic options; the underlying provider class
    is selected from the registry. Operations (``send``) take cloud-agnostic
    value objects and never reference a provider.

    Args:
        provider: Provider id (e.g., ``"awssns"``, ``"awssqs"``, ``"gcp"``,
            ``"ali"``). See the registry for all registered providers.

        topic_name: Topic name/identifier.
        region: Region identifier (interpretation varies by provider).
            Optional; falls back to provider default credential discovery.
        endpoint: Endpoint override URL. Optional.
        credentials_overrider: Override default credential resolution.
            Optional; used for test isolation, cross-account access, or
            role assumption.
        **provider_kwargs: Additional provider-specific keyword arguments.
            See provider implementation documentation for available options.

    Note:
        Credentials are sourced from environment variables or system default
        credential discovery (environment variables, credential files, or
        cloud-native authentication like IAM roles or workload identity).

        Use ``credentials_overrider`` for structured credential overrides
        (session credentials, role assumption, web identity). Individual
        credential parameters (``aws_access_key_id``, etc.) are not supported.
    """

    def __init__(
        self,
        *,
        provider: str,
        topic_name: str,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
        **provider_kwargs: Any,
    ) -> None:
        topic_cls = TOPIC_REGISTRY.get(provider)
        kwargs: dict = {"topic_name": topic_name}
        if region is not None:
            kwargs["region"] = region
        if endpoint is not None:
            kwargs["endpoint"] = endpoint
        if credentials_overrider is not None:
            kwargs["credentials_overrider"] = credentials_overrider
        # Pass through provider-specific kwargs with validation warning
        if provider_kwargs:
            warnings.warn(
                f"Passing provider-specific kwargs: {list(provider_kwargs.keys())}. "
                "Unrecognized kwargs will be ignored by the provider.",
                UserWarning,
                stacklevel=2,
            )
        kwargs.update(provider_kwargs)
        self._topic: BaseTopic = topic_cls(**kwargs)

    @property
    def topic_name(self) -> str:
        return self._topic.topic_name

    @property
    def provider(self) -> str:
        return self._topic.get_provider_id()

    def send(self, message: Message) -> None:
        """Send a single message to the topic.

        Pubsub send() accepts a Message object because messages can carry
        metadata and lifecycle hooks that are naturally composed with the
        builder pattern.

        Args:
            message: The message to send.

        Raises:
            InvalidArgumentException: if message is invalid
        """
        try:
            self._topic.send(message)
        except Exception as exc:
            raise self._topic.translate_error(exc) from exc

    def close(self) -> None:
        """Flush pending messages and close the topic."""
        try:
            self._topic.close()
        except Exception as exc:
            raise self._topic.translate_error(exc) from exc

    def __enter__(self) -> TopicClient:
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        self.close()


class SubscriptionClient:
    """Cloud-agnostic subscription client for receiving and acknowledging messages.

    The user-facing API surface for subscribing. Construction takes a ``provider``
    id and a unified set of cloud-agnostic options; the underlying provider class
    is selected from the registry.

    Args:
        provider: Provider id (e.g., ``"aws"``, ``"gcp"``, ``"ali"``).
            See the registry for all registered providers.

        subscription_name: Subscription name/identifier.
        region: Region (interpreted per provider). Optional.
        endpoint: Endpoint override URL. Optional.
        nack_visibility_timeout: Default visibility timeout for nacked messages.
            Optional; defaults to 0 (immediate redelivery).
        credentials_overrider: Override default credential resolution.
            Optional; used for test isolation, cross-account access, or
            role assumption.

    Note:
        Credentials are sourced from environment variables or system default
        credential discovery (environment variables, credential files, or
        cloud-native authentication like IAM roles or workload identity).

        Use ``credentials_overrider`` for structured credential overrides
        (session credentials, role assumption, web identity). Individual
        credential parameters (``aws_access_key_id``, etc.) are not supported.
    """

    def __init__(
        self,
        *,
        provider: str,
        subscription_name: str,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        nack_visibility_timeout: Optional[timedelta] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
        **provider_kwargs: Any,
    ) -> None:
        subscription_cls = SUBSCRIPTION_REGISTRY.get(provider)
        kwargs: dict = {"subscription_name": subscription_name}
        if region is not None:
            kwargs["region"] = region
        if endpoint is not None:
            kwargs["endpoint"] = endpoint
        if nack_visibility_timeout is not None:
            kwargs["nack_visibility_timeout"] = nack_visibility_timeout
        if credentials_overrider is not None:
            kwargs["credentials_overrider"] = credentials_overrider
        # Pass through provider-specific kwargs with validation warning
        if provider_kwargs:
            warnings.warn(
                f"Passing provider-specific kwargs: {list(provider_kwargs.keys())}. "
                "Unrecognized kwargs will be ignored by the provider.",
                UserWarning,
                stacklevel=2,
            )
        kwargs.update(provider_kwargs)
        self._subscription: BaseSubscription = subscription_cls(**kwargs)

    @property
    def subscription_name(self) -> str:
        return self._subscription.subscription_name

    @property
    def provider(self) -> str:
        return self._subscription.get_provider_id()

    def receive(self) -> Message:
        """Receive a single message from the subscription (blocking).

        Returns:
            The received message with ack_id populated.
        """
        try:
            return self._subscription.receive()
        except Exception as exc:
            raise self._subscription.translate_error(exc) from exc

    def send_ack(self, ack_id: AckID) -> None:
        """Acknowledge a single message.

        Args:
            ack_id: The acknowledgment ID from the received message.

        Raises:
            InvalidArgumentException: if ack_id is None
        """
        try:
            self._subscription.send_ack(ack_id)
        except Exception as exc:
            raise self._subscription.translate_error(exc) from exc

    def send_acks(self, ack_ids: List[AckID]) -> Future[None]:
        """Acknowledge multiple messages in a batch.

        Args:
            ack_ids: List of acknowledgment IDs.

        Returns:
            A Future that completes when all acks are processed.

        Raises:
            InvalidArgumentException: if any ack_id is None
        """
        try:
            return self._subscription.send_acks(ack_ids)
        except Exception as exc:
            raise self._subscription.translate_error(exc) from exc

    def send_nack(
        self, ack_id: AckID, *, visibility_timeout: Optional[timedelta] = None
    ) -> None:
        """Negatively acknowledge a single message (requeue for redelivery).

        Args:
            ack_id: The acknowledgment ID from the received message.
            visibility_timeout: Optional visibility timeout for the nacked message.

        Raises:
            InvalidArgumentException: if ack_id is None or timeout is negative
        """
        try:
            self._subscription.send_nack(ack_id, visibility_timeout=visibility_timeout)
        except Exception as exc:
            raise self._subscription.translate_error(exc) from exc

    def send_nacks(
        self, ack_ids: List[AckID], *, visibility_timeout: Optional[timedelta] = None
    ) -> Future[None]:
        """Negatively acknowledge multiple messages in a batch.

        Args:
            ack_ids: List of acknowledgment IDs.
            visibility_timeout: Optional visibility timeout for all nacked messages.

        Returns:
            A Future that completes when all nacks are processed.

        Raises:
            InvalidArgumentException: if ack_ids is None, any ack_id is None,
                or timeout is negative
        """
        try:
            return self._subscription.send_nacks(
                ack_ids, visibility_timeout=visibility_timeout
            )
        except Exception as exc:
            raise self._subscription.translate_error(exc) from exc

    def can_nack(self) -> bool:
        """Check if the provider supports negative acknowledgment.

        Returns:
            True if nack is supported, False otherwise.
        """
        return self._subscription.can_nack()

    def get_attributes(self) -> GetAttributeResult:
        """Retrieve subscription attributes.

        Returns:
            Subscription attributes including approximate message count.
        """
        try:
            return self._subscription.get_attributes()
        except Exception as exc:
            raise self._subscription.translate_error(exc) from exc

    def is_retryable(self, error: BaseException) -> bool:
        """Check if an error is retryable.

        Args:
            error: The error to check.

        Returns:
            True if the error is transient and retryable, False otherwise.
        """
        return self._subscription.is_retryable(error)

    def close(self) -> None:
        """Flush pending acknowledgments and close the subscription."""
        try:
            self._subscription.close()
        except Exception as exc:
            raise self._subscription.translate_error(exc) from exc

    def __enter__(self) -> SubscriptionClient:
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        self.close()
