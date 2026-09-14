"""
Unit tests for SubscriptionClient.
"""

from concurrent.futures import Future
from datetime import timedelta
from unittest.mock import Mock

import pytest
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
)
from multicloud.pubsub.pubsub_client import (
    AckID,
    GetAttributeResult,
    Message,
    SubscriptionClient,
)
from multicloud.pubsub.pubsub_client.driver import (
    SUBSCRIPTION_REGISTRY,
    BaseSubscription,
)


class TestAckID:
    """Test implementation of AckID."""

    def __init__(self, id: str) -> None:
        self.id = id

    def __str__(self) -> str:
        return self.id

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if not isinstance(other, TestAckID):
            return False
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id) if self.id else 0


class MockBaseSubscription(BaseSubscription):
    """Mock BaseSubscription for testing."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.received_messages = []
        self.acked_ids = []
        self.nacked_ids = []

    def get_provider_id(self) -> str:
        """Return provider ID."""
        return "test"

    def do_receive_batch(self, batch_size):
        """Mock implementation of do_receive_batch."""
        if self.received_messages:
            return self.received_messages[:batch_size]
        return []

    def do_send_acks(self, ack_ids):
        """Mock implementation of do_send_acks."""
        self.acked_ids.extend(ack_ids)

    def do_send_nacks(self, nacks):
        """Mock implementation of do_send_nacks."""
        self.nacked_ids.extend([info.ack_id for info in nacks])

    def can_nack(self):
        """Mock implementation of can_nack."""
        return True

    def is_retryable(self, error):
        """Mock implementation of is_retryable."""
        return False

    def get_attributes(self):
        """Mock implementation of get_attributes."""
        return GetAttributeResult(approximate_number_of_messages=10)

    def translate_error(self, exc):
        """Mock implementation of translate_error."""
        from multicloud.common.exceptions import SubstrateSdkException, UnknownException

        # Pass through already-translated exceptions
        if isinstance(exc, SubstrateSdkException):
            return exc
        return UnknownException(str(exc))


class TestSubscriptionClient:
    """Test suite for SubscriptionClient class."""

    @pytest.fixture(autouse=True)
    def register_mock_provider(self):
        """Register mock provider for testing."""

        # Register a test provider
        @SUBSCRIPTION_REGISTRY.register("test")
        class TestSubscription(MockBaseSubscription):
            pass

        yield

        # Clean up after test
        if "test" in SUBSCRIPTION_REGISTRY._providers:
            del SUBSCRIPTION_REGISTRY._providers["test"]

    def test_constructor_with_required_params(self):
        """Test SubscriptionClient construction with required parameters."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")

        assert client.subscription_name == "my-sub"
        assert client.provider == "test"

    def test_constructor_with_all_params(self):
        """Test SubscriptionClient construction with all parameters."""
        client = SubscriptionClient(
            provider="test",
            subscription_name="my-sub",
            region="us-west-2",
            endpoint="https://test.example.com",
            nack_visibility_timeout=timedelta(seconds=30),
        )

        assert client.subscription_name == "my-sub"
        assert client.provider == "test"
        assert client._subscription.region == "us-west-2"
        assert client._subscription.endpoint == "https://test.example.com"
        assert client._subscription.nack_visibility_timeout == timedelta(seconds=30)

    def test_constructor_warns_on_provider_kwargs(self):
        """Test SubscriptionClient warns when provider kwargs are passed."""

        class ProviderKwargSubscription(MockBaseSubscription):
            def __init__(self, **kwargs):
                kwargs.pop("custom_option", None)
                super().__init__(**kwargs)

        SUBSCRIPTION_REGISTRY.register("test-provider-kwargs")(
            ProviderKwargSubscription
        )

        with pytest.warns(
            UserWarning, match="Unrecognized kwargs will be ignored by the provider"
        ):
            SubscriptionClient(
                provider="test-provider-kwargs",
                subscription_name="my-sub",
                custom_option="value",
            )

        del SUBSCRIPTION_REGISTRY._providers["test-provider-kwargs"]

    def test_send_ack(self):
        """Test send_ack method."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")
        ack_id = TestAckID("test-ack-id")

        client.send_ack(ack_id)

        # Give batcher time to process
        client.close()
        assert ack_id in client._subscription.acked_ids

    def test_send_acks(self):
        """Test send_acks method."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")
        ack_ids = [TestAckID("test-ack-id-1"), TestAckID("test-ack-id-2")]

        future = client.send_acks(ack_ids)

        assert isinstance(future, Future)
        future.result()  # Wait for completion
        client.close()

        for ack_id in ack_ids:
            assert ack_id in client._subscription.acked_ids

    def test_send_nack(self):
        """Test send_nack method."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")
        ack_id = TestAckID("test-ack-id")

        client.send_nack(ack_id)

        client.close()
        assert ack_id in client._subscription.nacked_ids

    def test_send_nacks(self):
        """Test send_nacks method."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")
        ack_ids = [TestAckID("test-ack-id-1"), TestAckID("test-ack-id-2")]

        future = client.send_nacks(ack_ids)

        assert isinstance(future, Future)
        future.result()  # Wait for completion
        client.close()

        for ack_id in ack_ids:
            assert ack_id in client._subscription.nacked_ids

    def test_send_nack_with_visibility_timeout(self):
        """Test send_nack with visibility timeout."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")
        ack_id = TestAckID("test-ack-id")
        timeout = timedelta(seconds=60)

        client.send_nack(ack_id, visibility_timeout=timeout)

        client.close()
        assert ack_id in client._subscription.nacked_ids

    def test_can_nack(self):
        """Test can_nack method."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")

        result = client.can_nack()

        assert result is True

    def test_get_attributes(self):
        """Test get_attributes method."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")

        result = client.get_attributes()

        assert isinstance(result, GetAttributeResult)
        assert result.approximate_number_of_messages == 10

    def test_is_retryable(self):
        """Test is_retryable method."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")
        error = RuntimeError("test error")

        result = client.is_retryable(error)

        assert result is False

    def test_close(self):
        """Test close method."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")

        # Should not raise
        client.close()

        assert client._subscription.is_shutdown is True

    def test_context_manager(self):
        """Test context manager support."""
        with SubscriptionClient(provider="test", subscription_name="my-sub") as client:
            ack_id = TestAckID("test-ack-id")
            client.send_ack(ack_id)

        # Should be closed after context exit
        assert client._subscription.is_shutdown is True

    def test_send_ack_after_close_raises(self):
        """Test that ack after close raises FailedPreconditionException."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")
        client.close()

        ack_id = TestAckID("test-ack-id")

        with pytest.raises(FailedPreconditionException):
            client.send_ack(ack_id)

    def test_send_ack_null_raises(self):
        """Test that null ack_id raises InvalidArgumentException."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")

        with pytest.raises(InvalidArgumentException):
            client.send_ack(None)

    def test_send_nack_with_negative_timeout_raises(self):
        """Test that negative timeout raises InvalidArgumentException."""
        client = SubscriptionClient(provider="test", subscription_name="my-sub")
        ack_id = TestAckID("test-ack-id")

        with pytest.raises(InvalidArgumentException):
            client.send_nack(ack_id, visibility_timeout=timedelta(seconds=-1))

    def test_context_manager_close_exception_without_user_exception(self):
        """Test __exit__ propagates close() exception when no user exception."""
        from multicloud.common.exceptions import UnknownException

        class FailingCloseSubscription(MockBaseSubscription):
            def close(self):
                raise RuntimeError("Close failed")

        SUBSCRIPTION_REGISTRY.register("test-failing-close")(FailingCloseSubscription)

        # Client layer translates the RuntimeError to UnknownException
        with pytest.raises(UnknownException, match="Close failed"):
            with SubscriptionClient(
                provider="test-failing-close", subscription_name="test"
            ) as client:
                pass  # Just test context manager

        # Cleanup
        del SUBSCRIPTION_REGISTRY._providers["test-failing-close"]

    def test_context_manager_close_exception_with_user_exception(self):
        """Test __exit__ propagates close() exception even with user exception."""
        from multicloud.common.exceptions import UnknownException

        class FailingCloseSubscription(MockBaseSubscription):
            def close(self):
                raise RuntimeError("Close failed")

        SUBSCRIPTION_REGISTRY.register("test-failing-close2")(FailingCloseSubscription)

        # close() exception is translated and propagated by current __exit__ behavior
        with pytest.raises(UnknownException, match="Close failed"):
            with SubscriptionClient(
                provider="test-failing-close2", subscription_name="test"
            ) as client:
                raise ValueError("User error")

        # Cleanup
        del SUBSCRIPTION_REGISTRY._providers["test-failing-close2"]
