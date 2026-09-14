"""Reusable pubsub topic conformance test suite.

Defines the conformance tests that every pubsub topic provider must pass.
Provider-specific test classes extend ``BaseTopicConformance``
and supply ``client``, ``subscription_client``, and ``harness`` fixtures.

Class naming notes:
  - ``BaseTopicConformance`` ends in ``Conformance`` (not
    ``Tests``) so pytest's default ``python_classes = Test*`` collection
    rule doesn't pick it up. Concrete subclasses must start with ``Test``
    to be collected.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Optional

import pytest
from multicloud.common.exceptions import InvalidArgumentException
from multicloud.pubsub.pubsub_client import (
    AckID,
    Message,
    SubscriptionClient,
    TopicClient,
)


class Harness(ABC):
    """Test harness interface that providers implement.

    The harness owns the lifecycle of :class:`TopicClient` and
    :class:`SubscriptionClient` instances for a single test session.
    Providers wire up authentication, topic name, subscription name,
    region, etc. inside the harness so tests stay cloud-agnostic.
    """

    @abstractmethod
    def create_topic_client(self) -> TopicClient:
        """Return a TopicClient configured for the test topic."""
        ...

    @abstractmethod
    def create_subscription_client(
        self, nack_visibility_timeout: Optional[timedelta] = None
    ) -> SubscriptionClient:
        """Return a SubscriptionClient configured for the test subscription.

        Args:
            nack_visibility_timeout: Optional nack visibility timeout for the subscription.
                Pass None to use provider default. Used for conformance tests that validate
                provider behavior when nacked messages must remain invisible for a configured
                duration before redelivery.

        Providers that do not support subscriptions (publish-only providers)
        should return None. The base conformance test will skip tests that
        require subscription_client when it is None.
        """
        ...

    @abstractmethod
    def get_provider_id(self) -> str:
        """Return the provider id (e.g. ``"aws"``, ``"gcp"``)."""
        ...

    def close(self):  # pragma: no cover - default no-op
        """Clean up resources. Override if needed."""


class BaseTopicConformance:
    """Abstract conformance test suite for pubsub topic providers.

    Conformance tests are written ONCE here and executed by each
    provider. Provider test classes inherit from this class and supply
    ``client``, ``subscription_client``, and ``harness`` fixtures.
    """

    def _wait_for_message(
        self,
        subscription: SubscriptionClient,
        timeout_sec: int = 10,
        poll_interval_sec: float = 0.2,
    ) -> Optional[Message]:
        """Poll for a message with timeout.

        Args:
            subscription: Subscription client to receive from
            timeout_sec: Maximum time to wait in seconds
            poll_interval_sec: Time between receive attempts

        Returns:
            Received message or None if timeout
        """
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            msg = subscription.receive()
            if msg is not None:
                return msg
            time.sleep(poll_interval_sec)
        return None

    def _assert_message_matches(
        self,
        message: Optional[Message],
        *,
        expected_body: bytes,
        metadata_key: str,
        metadata_value: str,
        context: str,
    ) -> None:
        """Assert that a received message matches expected content and shape."""
        assert message is not None, f"Should receive a message for {context}"
        assert (
            message.body == expected_body
        ), f"Received body must match sent body for {context}"
        assert (
            message.metadata.get(metadata_key) == metadata_value
        ), f"Received metadata must match sent metadata for {context}"
        assert (
            message.ack_id is not None
        ), f"Received message must have ack_id for {context}"

    def test_send_batch_messages(self, client, harness: Harness):
        """Send multiple messages and verify they are accepted by the provider.

        This test verifies that the send operation succeeds without raising
        exceptions. Message content validation happens in test_receive_after_send
        via round-trip verification.
        """
        messages = [
            Message.builder()
            .with_body(b"Message 1")
            .with_metadata({"batch-id": "1"})
            .build(),
            Message.builder()
            .with_body(b"Message 2")
            .with_metadata({"batch-id": "2"})
            .build(),
            Message.builder()
            .with_body(b"Message 3")
            .with_metadata({"batch-id": "3"})
            .build(),
        ]

        # Send all messages - success is indicated by no exception
        for message in messages:
            client.send(message)

    def test_receive_after_send(self, client, subscription_client, harness: Harness):
        """Send a message and verify it can be received (round-trip test).

        Verifies end-to-end publish-subscribe flow works correctly.
        Does not test ack/nack operations (those are in subscription tests).
        """
        if subscription_client is None:
            pytest.skip(
                f"Skipping: {harness.get_provider_id()} does not support "
                "subscriptions (publish-only provider)"
            )

        to_send = (
            Message.builder()
            .with_body(b"it-receive-test")
            .with_metadata({"case": "receive"})
            .build()
        )

        # Network call: publish message
        client.send(to_send)

        # Network call: poll for message delivery
        received = self._wait_for_message(subscription_client, timeout_sec=10)

        # Round-trip verification: received message must match sent message
        self._assert_message_matches(
            received,
            expected_body=b"it-receive-test",
            metadata_key="case",
            metadata_value="receive",
            context="receive test",
        )

    def test_ack_after_receive(self, client, subscription_client, harness: Harness):
        """Send a message, receive it, and acknowledge it.

        Round-trip test: verifies received body and metadata match sent content,
        and ack operation completes successfully.
        """
        if subscription_client is None:
            pytest.skip(
                f"Skipping: {harness.get_provider_id()} does not support subscriptions"
            )

        to_send = (
            Message.builder()
            .with_body(b"it-ack-test")
            .with_metadata({"case": "ack"})
            .build()
        )

        # Network call: publish message
        client.send(to_send)

        # Network call: poll for message delivery
        received = self._wait_for_message(subscription_client, timeout_sec=10)

        # Round-trip verification: received message must match sent message
        self._assert_message_matches(
            received,
            expected_body=b"it-ack-test",
            metadata_key="case",
            metadata_value="ack",
            context="ack test",
        )

        # Network call: acknowledge message
        subscription_client.send_ack(received.ack_id)

    def test_nack_after_receive(self, client, subscription_client, harness: Harness):
        """Send a message, receive it, and nack it.

        Round-trip test: verifies received body and metadata match sent content,
        and nack operation completes successfully.
        """
        if subscription_client is None:
            pytest.skip(
                f"Skipping: {harness.get_provider_id()} does not support subscriptions"
            )

        to_send = (
            Message.builder()
            .with_body(b"it-nack-test")
            .with_metadata({"case": "nack"})
            .build()
        )

        # Network call: publish message
        client.send(to_send)

        # Network call: poll for message delivery
        received = self._wait_for_message(subscription_client, timeout_sec=10)

        # Round-trip verification: received message must match sent message
        self._assert_message_matches(
            received,
            expected_body=b"it-nack-test",
            metadata_key="case",
            metadata_value="nack",
            context="nack test",
        )

        # Network call: nack message
        subscription_client.send_nack(received.ack_id)

    def test_batch_ack(self, client, subscription_client, harness: Harness):
        """Send multiple messages, receive them, and acknowledge them in batch.

        Round-trip test: verifies all expected messages are received with correct
        body and metadata content, and batch ack operation completes successfully.
        Tolerates at-least-once delivery duplicates.
        """
        if subscription_client is None:
            pytest.skip(
                f"Skipping: {harness.get_provider_id()} does not support subscriptions"
            )

        to_send = [
            Message.builder()
            .with_body(b"batch-ack-1")
            .with_metadata({"batch": "ack"})
            .build(),
            Message.builder()
            .with_body(b"batch-ack-2")
            .with_metadata({"batch": "ack"})
            .build(),
            Message.builder()
            .with_body(b"batch-ack-3")
            .with_metadata({"batch": "ack"})
            .build(),
        ]

        # Network call: publish all messages
        for m in to_send:
            client.send(m)

        # Allow time for message delivery
        time.sleep(0.5)

        expected_bodies = {b"batch-ack-1", b"batch-ack-2", b"batch-ack-3"}
        received_messages_by_body, all_ack_ids = (
            self._receive_messages_by_expected_bodies(
                subscription_client,
                expected_bodies,
                timeout_sec=60,
                expected_metadata_key="batch",
                expected_metadata_value="ack",
            )
        )
        received_bodies = set(received_messages_by_body.keys())

        assert expected_bodies.issubset(received_bodies), (
            f"Should receive all expected message bodies. Missing: "
            f"{expected_bodies - received_bodies}. "
            f"Received bodies: {received_bodies}."
        )

        # Network call: batch acknowledge
        subscription_client.send_acks(all_ack_ids)

    def test_nack_with_visibility_timeout(
        self, client, subscription_client, harness: Harness
    ):
        """Send a message, receive it with nack visibility timeout configured, and nack it.

        Round-trip test: verifies subscription can be created with nack visibility
        timeout, received body and metadata match sent content, and nack operation
        completes successfully.

        NOTE: SubscriptionClient does not expose nack_visibility_timeout as a public
        property (it's passed to the underlying driver implementation). This test
        verifies the timeout parameter is accepted at construction time and nack
        works correctly, without asserting on the property value.
        """
        if subscription_client is None:
            pytest.skip(
                f"Skipping: {harness.get_provider_id()} does not support subscriptions"
            )

        # Create a subscription client with nack visibility timeout configured
        nack_visibility_timeout = timedelta(seconds=5)
        subscription_with_timeout = harness.create_subscription_client(
            nack_visibility_timeout=nack_visibility_timeout
        )

        # NOTE: Python SDK does not expose nack_visibility_timeout as a property
        # We verify the parameter is accepted at construction time (no exception)

        try:
            to_send = (
                Message.builder()
                .with_body(b"it-nack-visibility-timeout")
                .with_metadata({"case": "nack-visibility-timeout"})
                .build()
            )

            # Network call: publish message
            client.send(to_send)

            # Network call: poll for message delivery
            received = self._wait_for_message(subscription_with_timeout, timeout_sec=10)

            # Round-trip verification: received message must match sent message
            self._assert_message_matches(
                received,
                expected_body=b"it-nack-visibility-timeout",
                metadata_key="case",
                metadata_value="nack-visibility-timeout",
                context="nack visibility timeout test",
            )

            # Network call: nack with visibility timeout
            subscription_with_timeout.send_nacks([received.ack_id])
        finally:
            # Ensure cleanup happens even if assertions fail
            subscription_with_timeout.close()

    def test_ack_null_throws(self, subscription_client, harness: Harness):
        """Verify that acking None raises InvalidArgumentException."""
        if subscription_client is None:
            pytest.skip(
                f"Skipping: {harness.get_provider_id()} does not support subscriptions"
            )

        with pytest.raises(InvalidArgumentException):
            subscription_client.send_ack(None)

    def test_get_attributes(self, subscription_client, harness: Harness):
        """Verify that get_attributes returns essential subscription metadata.

        Essential attributes: name, topic.
        """
        if subscription_client is None:
            pytest.skip(
                f"Skipping: {harness.get_provider_id()} does not support subscriptions"
            )

        # Network call: get subscription attributes
        attributes = subscription_client.get_attributes()

        # Verify that attributes are returned
        assert attributes is not None, "Attributes should not be null"

        # Verify essential attributes that should be present across all providers
        assert attributes.name is not None, "Name should not be null"
        assert len(attributes.name) > 0, "Name should not be empty"

        assert attributes.topic is not None, "Topic should not be null"
        assert len(attributes.topic) > 0, "Topic should not be empty"

    def _receive_messages(
        self,
        subscription: SubscriptionClient,
        expected_count: int,
        subscription_id: str,
        timeout_sec: int = 60,
    ) -> list[Message]:
        """Helper function: Receives messages from a subscription until expected count is reached.

        Args:
            subscription: Subscription client to receive from
            expected_count: Number of messages expected
            subscription_id: Identifier for logging
            timeout_sec: Maximum time to wait in seconds

        Returns:
            List of received messages

        Raises:
            AssertionError: If expected_count is not reached within timeout
        """
        deadline = time.time() + timeout_sec
        received = []
        last_exception = None

        while len(received) < expected_count and time.time() < deadline:
            try:
                r = subscription.receive()
                if r is not None:
                    received.append(r)
                else:
                    time.sleep(0.1)
            except Exception as e:
                last_exception = e
                time.sleep(0.1)

        assert len(received) == expected_count, (
            f"[{subscription_id}] Timeout waiting for messages: "
            f"Received {len(received)}/{expected_count} messages. "
            f"Last exception: {last_exception}"
            if last_exception
            else ""
        )

        return received

    def _receive_messages_by_expected_bodies(
        self,
        subscription: SubscriptionClient,
        expected_bodies: set[bytes],
        expected_metadata_key: str,
        expected_metadata_value: str,
        timeout_sec: int = 60,
    ) -> tuple[dict[bytes, Message], list[AckID]]:
        """Receive expected bodies and collect ack IDs for queue draining.

        The returned ack-id list includes both expected and unexpected bodies so
        callers can drain strays and avoid cross-run redelivery noise.
        """
        deadline = time.time() + timeout_sec
        received_messages_by_body: dict[bytes, Message] = {}
        all_ack_ids: list[AckID] = []
        last_exception = None

        while (
            not expected_bodies.issubset(received_messages_by_body.keys())
            and time.time() < deadline
        ):
            try:
                message = subscription.receive()
                if message is not None and message.ack_id is not None:
                    assert (
                        message.body is not None
                    ), "Provider returned message with null body"
                    # Always collect ack IDs so unexpected messages are drained.
                    all_ack_ids.append(message.ack_id)
                    if message.body in expected_bodies:
                        assert (
                            message.metadata.get(expected_metadata_key)
                            == expected_metadata_value
                        ), (
                            "Message metadata must match expected value for duplicates. "
                            f"Expected {expected_metadata_key}={expected_metadata_value}, "
                            f"got {message.metadata}"
                        )
                        received_messages_by_body.setdefault(message.body, message)
                else:
                    time.sleep(0.1)
            except Exception as exc:
                last_exception = exc
                time.sleep(0.1)

        observed_bodies = set(received_messages_by_body.keys())
        assert expected_bodies.issubset(observed_bodies), (
            f"Timeout waiting for expected message bodies. Missing: "
            f"{expected_bodies - observed_bodies}. "
            f"Received: {observed_bodies}. "
            f"Last exception: {last_exception}"
        )
        return received_messages_by_body, all_ack_ids
