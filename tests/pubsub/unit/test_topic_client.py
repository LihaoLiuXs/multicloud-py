"""
Unit tests for TopicClient.
"""

from unittest.mock import Mock

import pytest
from multicloud.common.exceptions import FailedPreconditionException
from multicloud.pubsub.pubsub_client import Message, TopicClient
from multicloud.pubsub.pubsub_client.driver import TOPIC_REGISTRY, BaseTopic


class MockBaseTopic(BaseTopic):
    """Mock BaseTopic for testing."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent_messages = []

    def get_provider_id(self) -> str:
        """Return provider ID."""
        return "test"

    def do_send_batch(self, messages):
        """Mock implementation of do_send_batch."""
        self.sent_messages.extend(messages)

    def translate_error(self, exc):
        """Mock implementation of translate_error."""
        from multicloud.common.exceptions import SubstrateSdkException, UnknownException

        # Pass through already-translated exceptions
        if isinstance(exc, SubstrateSdkException):
            return exc
        return UnknownException(str(exc))

    def get_max_message_size_bytes(self) -> int:
        """Return message size limit for testing."""
        return 256 * 1024  # 256 KB


class TestTopicClient:
    """Test suite for TopicClient class."""

    @pytest.fixture
    def mock_topic(self) -> Mock:
        """Create a mock BaseTopic."""
        return Mock(spec=BaseTopic)

    @pytest.fixture(autouse=True)
    def register_mock_provider(self):
        """Register mock provider for testing."""

        # Register a test provider
        @TOPIC_REGISTRY.register("test")
        class TestTopic(MockBaseTopic):
            pass

        yield

        # Clean up after test
        if "test" in TOPIC_REGISTRY._providers:
            del TOPIC_REGISTRY._providers["test"]

    def test_constructor_with_required_params(self):
        """Test TopicClient construction with required parameters."""
        client = TopicClient(provider="test", topic_name="my-topic")

        assert client.topic_name == "my-topic"
        assert client.provider == "test"

    def test_constructor_with_all_params(self):
        """Test TopicClient construction with all parameters."""
        client = TopicClient(
            provider="test",
            topic_name="my-topic",
            region="us-west-2",
            endpoint="https://test.example.com",
        )

        assert client.topic_name == "my-topic"
        assert client.provider == "test"
        assert client._topic.region == "us-west-2"
        assert client._topic.endpoint == "https://test.example.com"

    def test_send(self):
        """Test send method."""
        client = TopicClient(provider="test", topic_name="my-topic")
        message = Message.builder().with_body(b"test").build()

        client.send(message)

        # Verify message was sent
        assert len(client._topic.sent_messages) == 1
        assert client._topic.sent_messages[0].body == b"test"

    def test_close(self):
        """Test close method."""
        client = TopicClient(provider="test", topic_name="my-topic")

        # Should not raise
        client.close()

        assert client._topic.is_shutdown is True

    def test_context_manager(self):
        """Test context manager support."""
        with TopicClient(provider="test", topic_name="my-topic") as client:
            message = Message.builder().with_body(b"test").build()
            client.send(message)

        # Should be closed after context exit
        assert client._topic.is_shutdown is True

    def test_send_after_close_raises(self):
        """Test that sending after close raises FailedPreconditionException."""
        client = TopicClient(provider="test", topic_name="my-topic")
        client.close()

        message = Message.builder().with_body(b"test").build()

        with pytest.raises(FailedPreconditionException):
            client.send(message)

    def test_multiple_sends(self):
        """Test multiple send operations."""
        client = TopicClient(provider="test", topic_name="my-topic")

        message1 = Message.builder().with_body(b"test1").build()
        message2 = Message.builder().with_body(b"test2").build()

        client.send(message1)
        client.send(message2)

        # Both sends should complete without error
        client.close()

    def test_provider_not_found_raises(self):
        """Test constructor raises when provider is not registered."""
        with pytest.raises(ValueError, match="No provider"):
            TopicClient(provider="missing-provider", topic_name="my-topic")

    def test_context_manager_close_exception_without_user_exception(self):
        """Test __exit__ propagates close() exception when no user exception."""
        from multicloud.common.exceptions import UnknownException

        class FailingCloseTopic(MockBaseTopic):
            def close(self):
                raise RuntimeError("Close failed")

        TOPIC_REGISTRY.register("test-failing-close")(FailingCloseTopic)

        # Client layer translates the RuntimeError to UnknownException
        with pytest.raises(UnknownException, match="Close failed"):
            with TopicClient(
                provider="test-failing-close", topic_name="test"
            ) as client:
                msg = Message.builder().with_body(b"test").build()
                client.send(msg)

        # Cleanup
        del TOPIC_REGISTRY._providers["test-failing-close"]

    def test_context_manager_close_exception_with_user_exception(self):
        """Test __exit__ propagates close() exception even with user exception."""
        from multicloud.common.exceptions import UnknownException

        class FailingCloseTopic(MockBaseTopic):
            def close(self):
                raise RuntimeError("Close failed")

        TOPIC_REGISTRY.register("test-failing-close2")(FailingCloseTopic)

        # close() exception is translated and propagated by current __exit__ behavior
        with pytest.raises(UnknownException, match="Close failed"):
            with TopicClient(
                provider="test-failing-close2", topic_name="test"
            ) as client:
                raise ValueError("User error")

        # Cleanup
        del TOPIC_REGISTRY._providers["test-failing-close2"]


class TestTopicClientErrorPaths:
    """Test TopicClient error handling paths."""

    def test_send_translates_handler_exception(self):
        """Test that send translates handler exceptions."""
        from multicloud.common.exceptions import UnknownException

        class FailingTopic(MockBaseTopic):
            def do_send_batch(self, messages):
                raise RuntimeError("Send failed")

        TOPIC_REGISTRY.register("test-failing")(FailingTopic)

        client = TopicClient(provider="test-failing", topic_name="test")
        message = Message.builder().with_body(b"test").build()

        with pytest.raises(UnknownException, match="Send failed"):
            client.send(message)

        client.close()
        del TOPIC_REGISTRY._providers["test-failing"]

    def test_send_propagates_substrate_exception(self):
        """Test that send propagates SubstrateSdkException."""
        from multicloud.common.exceptions import UnknownException

        class FailingTopic(MockBaseTopic):
            def do_send_batch(self, messages):
                raise UnknownException("Already translated")

        TOPIC_REGISTRY.register("test-failing2")(FailingTopic)

        client = TopicClient(provider="test-failing2", topic_name="test")
        message = Message.builder().with_body(b"test").build()

        with pytest.raises(UnknownException, match="Already translated"):
            client.send(message)

        client.close()
        del TOPIC_REGISTRY._providers["test-failing2"]


class TestTopicClientCredentialsOverride:
    """Test TopicClient credentials_overrider parameter."""

    def test_credentials_overrider_passed_to_provider(self):
        """Test that credentials_overrider is passed through to provider."""
        from unittest.mock import Mock

        from multicloud.common.credentials import CredentialsOverrider

        # Create a mock credentials overrider
        mock_overrider = Mock(spec=CredentialsOverrider)

        # Create a topic that captures the credentials_overrider it receives
        captured_kwargs = {}

        class CapturingTopic(MockBaseTopic):
            def __init__(self, **kwargs):
                nonlocal captured_kwargs
                captured_kwargs = kwargs.copy()
                super().__init__(**kwargs)

        TOPIC_REGISTRY.register("test-creds")(CapturingTopic)

        # Create client with credentials_overrider
        client = TopicClient(
            provider="test-creds",
            topic_name="test-topic",
            credentials_overrider=mock_overrider,
        )

        # Verify credentials_overrider was passed to the provider
        assert "credentials_overrider" in captured_kwargs
        assert captured_kwargs["credentials_overrider"] is mock_overrider

        client.close()
        del TOPIC_REGISTRY._providers["test-creds"]
