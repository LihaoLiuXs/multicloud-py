"""Unit tests for GCP Pub/Sub subscription implementation."""

from datetime import timedelta
from unittest.mock import MagicMock, Mock, patch

import pytest
from google.api_core import exceptions as gcp_exceptions
from google.cloud.pubsub_v1.types import (
    PubsubMessage,
    PullResponse,
    ReceivedMessage,
)
from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
    StsCredentials,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceNotFoundException,
    SubstrateSdkException,
    UnAuthorizedException,
)
from multicloud.gcp_common.gcp_credentials import DEFAULT_SCOPES
from multicloud.pubsub.pubsub_client.driver import AckInfo
from multicloud.pubsub.pubsub_gcp.gcp_subscription import GcpAckID, GcpSubscription


@pytest.fixture
def mock_subscriber():
    """Create a mock GCP SubscriberClient."""
    subscriber = Mock()
    subscriber.pull.return_value = PullResponse(received_messages=[])
    subscriber.acknowledge.return_value = None
    subscriber.modify_ack_deadline.return_value = None
    return subscriber


@pytest.fixture
def gcp_subscription(mock_subscriber):
    """Create a GcpSubscription instance with mocked subscriber."""
    subscription = GcpSubscription(
        subscription_name="projects/test-project/subscriptions/test-sub",
        region="us-central1",
        nack_visibility_timeout=timedelta(seconds=30),
    )
    subscription._subscriber = mock_subscriber
    return subscription


class TestGcpAckID:
    """Test GcpAckID wrapper class."""

    def test_create_with_valid_ack_id(self):
        """Test creating GcpAckID with valid ack ID."""
        ack_id = GcpAckID("valid-ack-id")
        assert str(ack_id) == "valid-ack-id"

    def test_create_with_empty_ack_id(self):
        """Test that empty ack ID raises error."""
        with pytest.raises(InvalidArgumentException, match="cannot be null or empty"):
            GcpAckID("")

    def test_create_with_whitespace_ack_id(self):
        """Test that whitespace-only ack ID raises error."""
        with pytest.raises(InvalidArgumentException, match="cannot be null or empty"):
            GcpAckID("   ")

    def test_repr(self):
        """Test string representation."""
        ack_id = GcpAckID("test-ack-id")
        assert "GcpAckID" in repr(ack_id)
        assert "test-ack-id" in repr(ack_id)


class TestGcpSubscriptionBasics:
    """Test basic GCP subscription functionality."""

    def test_provider_id(self, gcp_subscription):
        """Test that provider ID is correct."""
        assert gcp_subscription.get_provider_id() == "gcp"

    def test_can_nack(self, gcp_subscription):
        """Test that GCP supports nacking."""
        assert gcp_subscription.can_nack() is True

    def test_is_retryable(self, gcp_subscription):
        """Test retryable error detection."""
        # GCP specific retryable errors
        assert gcp_subscription.is_retryable(gcp_exceptions.DeadlineExceeded("timeout"))

        # Non-retryable errors
        assert not gcp_subscription.is_retryable(
            gcp_exceptions.ServiceUnavailable("unavailable")
        )
        assert not gcp_subscription.is_retryable(gcp_exceptions.NotFound("not found"))
        assert not gcp_subscription.is_retryable(RuntimeError("unknown"))

    def test_receive_batcher_options(self, gcp_subscription):
        """Test receive batcher options."""
        options = gcp_subscription.create_receive_batcher_options()
        assert options.max_handlers == 10
        assert options.min_batch_size == 1
        assert options.max_batch_size == 1000

    def test_ack_batcher_options(self, gcp_subscription):
        """Test ack batcher options."""
        options = gcp_subscription.create_ack_batcher_options()
        assert options.max_handlers == 2
        assert options.min_batch_size == 1
        assert options.max_batch_size == 1000

    def test_validate_subscription_name_valid(self):
        """Test validation of valid subscription name."""
        # Should not raise
        GcpSubscription(subscription_name="projects/my-project/subscriptions/my-sub")

    def test_validate_subscription_name_invalid(self):
        """Test validation of invalid subscription name."""
        with pytest.raises(
            InvalidArgumentException,
            match="projects/\\{projectId\\}/subscriptions/\\{subscriptionId\\}",
        ):
            GcpSubscription(subscription_name="just-a-subscription")


class TestGcpSubscriptionReceive:
    """Test message receiving functionality."""

    def test_receive_empty_queue(self, gcp_subscription, mock_subscriber):
        """Test receiving from empty queue."""
        mock_subscriber.pull.return_value = PullResponse(received_messages=[])

        messages = gcp_subscription.do_receive_batch(5)

        assert messages == []
        assert mock_subscriber.pull.called

    def test_receive_simple_message(self, gcp_subscription, mock_subscriber):
        """Test receiving a simple message."""
        pubsub_msg = PubsubMessage(
            data=b"test message",
            message_id="msg-123",
            attributes={},
        )
        received = ReceivedMessage(message=pubsub_msg, ack_id="ack-123")
        mock_subscriber.pull.return_value = PullResponse(received_messages=[received])

        messages = gcp_subscription.do_receive_batch(1)

        assert len(messages) == 1
        assert messages[0].body == b"test message"
        assert messages[0].loggable_id == "msg-123"
        assert str(messages[0].ack_id) == "ack-123"

    def test_receive_with_attributes(self, gcp_subscription, mock_subscriber):
        """Test receiving message with attributes."""
        pubsub_msg = PubsubMessage(
            data=b"test",
            message_id="msg-123",
            attributes={"key1": "value1", "key2": "value2"},
        )
        received = ReceivedMessage(message=pubsub_msg, ack_id="ack-123")
        mock_subscriber.pull.return_value = PullResponse(received_messages=[received])

        messages = gcp_subscription.do_receive_batch(1)

        assert len(messages) == 1
        assert messages[0].metadata["key1"] == "value1"
        assert messages[0].metadata["key2"] == "value2"

    def test_receive_batch_size_passed(self, gcp_subscription, mock_subscriber):
        """Test that batch size is passed to pull request."""
        mock_subscriber.pull.return_value = PullResponse(received_messages=[])

        gcp_subscription.do_receive_batch(10)

        call_args = mock_subscriber.pull.call_args
        # Check that max_messages is set correctly
        assert (
            call_args.kwargs["request"]["subscription"]
            == "projects/test-project/subscriptions/test-sub"
        )
        assert call_args.kwargs["request"]["max_messages"] == 10


class TestGcpSubscriptionAck:
    """Test message acknowledgment functionality."""

    def test_ack_single_message(self, gcp_subscription, mock_subscriber):
        """Test acknowledging a single message."""
        ack_ids = [GcpAckID("ack-123")]
        gcp_subscription.do_send_acks(ack_ids)

        assert mock_subscriber.acknowledge.called
        call_args = mock_subscriber.acknowledge.call_args
        assert (
            call_args.kwargs["request"]["subscription"]
            == "projects/test-project/subscriptions/test-sub"
        )
        assert call_args.kwargs["request"]["ack_ids"] == ["ack-123"]

    def test_ack_batch(self, gcp_subscription, mock_subscriber):
        """Test acknowledging multiple messages."""
        ack_ids = [GcpAckID(f"ack-{i}") for i in range(5)]
        gcp_subscription.do_send_acks(ack_ids)

        assert mock_subscriber.acknowledge.called
        call_args = mock_subscriber.acknowledge.call_args
        assert len(call_args.kwargs["request"]["ack_ids"]) == 5

    def test_ack_empty_list(self, gcp_subscription, mock_subscriber):
        """Test that acking empty list is a no-op."""
        gcp_subscription.do_send_acks([])
        assert not mock_subscriber.acknowledge.called

    def test_ack_failure_propagates(self, gcp_subscription, mock_subscriber):
        """Test that ack failures propagate from provider layer."""
        mock_subscriber.acknowledge.side_effect = gcp_exceptions.NotFound(
            "Sub not found"
        )

        ack_ids = [GcpAckID("ack-123")]

        with pytest.raises(gcp_exceptions.NotFound):
            gcp_subscription.do_send_acks(ack_ids)


class TestGcpSubscriptionNack:
    """Test message negative acknowledgment functionality."""

    def test_nack_lazy_mode(self, mock_subscriber):
        """Test that nack_lazy mode bypasses ModifyAckDeadline."""
        sub = GcpSubscription(
            subscription_name="projects/test/subscriptions/test",
            nack_lazy=True,
        )
        sub._subscriber = mock_subscriber

        nacks = [AckInfo(GcpAckID("ack-123"), False, None)]
        sub.do_send_nacks(nacks)

        # Should not call modify_ack_deadline
        assert not mock_subscriber.modify_ack_deadline.called

    def test_nack_with_visibility_timeout(self, gcp_subscription, mock_subscriber):
        """Test nacking with visibility timeout."""
        nacks = [AckInfo(GcpAckID("ack-123"), False, timedelta(seconds=10))]
        gcp_subscription.do_send_nacks(nacks)

        assert mock_subscriber.modify_ack_deadline.called
        call_args = mock_subscriber.modify_ack_deadline.call_args
        assert call_args.kwargs["request"]["ack_deadline_seconds"] == 10

    def test_nack_uses_subscription_default(self, gcp_subscription, mock_subscriber):
        """Test that nack uses subscription default timeout."""
        # nack without per-message timeout
        nacks = [AckInfo(GcpAckID("ack-123"), False, None)]
        gcp_subscription.do_send_nacks(nacks)

        call_args = mock_subscriber.modify_ack_deadline.call_args
        # Should use subscription default (30 seconds from fixture)
        assert call_args.kwargs["request"]["ack_deadline_seconds"] == 30

    def test_nack_timeout_clamped_to_max(self, gcp_subscription, mock_subscriber):
        """Test that nack timeout is clamped to 600 seconds."""
        # Try to set 1000 seconds (above max)
        nacks = [AckInfo(GcpAckID("ack-123"), False, timedelta(seconds=1000))]
        gcp_subscription.do_send_nacks(nacks)

        call_args = mock_subscriber.modify_ack_deadline.call_args
        # Should be clamped to 600 seconds
        assert call_args.kwargs["request"]["ack_deadline_seconds"] == 600

    def test_nack_empty_list(self, gcp_subscription, mock_subscriber):
        """Test that nacking empty list is a no-op."""
        gcp_subscription.do_send_nacks([])
        assert not mock_subscriber.modify_ack_deadline.called


class TestGcpSubscriptionGetAttributes:
    """Test subscription attribute retrieval."""

    def test_get_attributes_success(self, gcp_subscription, mock_subscriber):
        """Test retrieving subscription attributes."""
        from google.cloud.pubsub_v1.types import Subscription

        mock_sub = Subscription(
            name="projects/test/subscriptions/test",
            topic="projects/test/topics/test",
        )
        mock_subscriber.get_subscription.return_value = mock_sub

        result = gcp_subscription.get_attributes()

        # GCP doesn't provide ApproximateNumberOfMessages, so it should be None
        assert result.approximate_number_of_messages is None

    def test_get_attributes_failure_propagates(self, gcp_subscription, mock_subscriber):
        """Test that get_subscription failures propagate from provider layer."""
        mock_subscriber.get_subscription.side_effect = gcp_exceptions.NotFound(
            "Subscription not found"
        )

        with pytest.raises(gcp_exceptions.NotFound):
            gcp_subscription.get_attributes()


class TestGcpSubscriptionErrorTranslation:
    """Test exception translation for GCP errors."""

    def test_translate_not_found(self, gcp_subscription):
        """Test translation of NotFound error."""
        error = gcp_exceptions.NotFound("Resource not found")
        translated = gcp_subscription.translate_error(error)
        assert isinstance(translated, ResourceNotFoundException)

    def test_translate_permission_denied(self, gcp_subscription):
        """Test translation of PermissionDenied error."""
        error = gcp_exceptions.PermissionDenied("Access denied")
        translated = gcp_subscription.translate_error(error)
        assert isinstance(translated, UnAuthorizedException)

    def test_translate_unauthenticated(self, gcp_subscription):
        """Test translation of Unauthenticated error."""
        error = gcp_exceptions.Unauthenticated("Invalid credentials")
        translated = gcp_subscription.translate_error(error)
        assert isinstance(translated, UnAuthorizedException)

    def test_translate_invalid_argument(self, gcp_subscription):
        """Test translation of InvalidArgument error."""
        error = gcp_exceptions.InvalidArgument("Bad request")
        translated = gcp_subscription.translate_error(error)
        assert isinstance(translated, InvalidArgumentException)

    def test_translate_already_substrate_exception(self, gcp_subscription):
        """Test that SubstrateSdkException is returned unchanged."""
        original = InvalidArgumentException("test")
        translated = gcp_subscription.translate_error(original)
        assert translated is original


class TestGcpSubscriptionClose:
    """Test resource cleanup."""

    def test_close_cleans_up_subscriber(self, gcp_subscription, mock_subscriber):
        """Test that close cleans up the subscriber."""
        gcp_subscription.close()

        # Subscriber should be set to None
        assert gcp_subscription._subscriber is None

    def test_close_idempotent(self, gcp_subscription):
        """Test that close can be called multiple times safely."""
        gcp_subscription.close()
        gcp_subscription.close()  # Should not raise


class TestGcpSubscriptionLazyInit:
    """Test lazy initialization of subscriber client."""

    def test_subscriber_created_on_first_receive(self):
        """Test that subscriber is created lazily on first receive."""
        with (
            patch("google.auth.default") as mock_auth_default,
            patch("google.cloud.pubsub_v1.SubscriberClient") as mock_subscriber_class,
        ):
            mock_auth_default.return_value = (Mock(), "test-project")
            mock_sub = Mock()
            mock_sub.pull.return_value = PullResponse(received_messages=[])
            mock_subscriber_class.return_value = mock_sub

            subscription = GcpSubscription(
                subscription_name="projects/test/subscriptions/test"
            )
            # Subscriber should not be created yet
            assert subscription._subscriber is None

            # Receive messages
            subscription.do_receive_batch(1)

            # Subscriber should now be created and be the mock instance
            assert subscription._subscriber is mock_sub
            assert mock_subscriber_class.called


class TestGcpSubscriptionErrorTranslation:
    """Test error translation for additional GCP exception types."""

    def test_translate_deadline_exceeded(self):
        """Test DeadlineExceeded maps to DeadlineExceededException."""
        from multicloud.common.exceptions import DeadlineExceededException

        subscription = GcpSubscription(
            subscription_name="projects/test/subscriptions/sub"
        )
        gcp_error = gcp_exceptions.DeadlineExceeded("Request timed out")

        result = subscription.translate_error(gcp_error)

        assert isinstance(result, DeadlineExceededException)
        assert "Request timed out" in str(result)

    def test_translate_resource_exhausted(self):
        """Test ResourceExhausted maps to ResourceExhaustedException."""
        from multicloud.common.exceptions import ResourceExhaustedException

        subscription = GcpSubscription(
            subscription_name="projects/test/subscriptions/sub"
        )
        gcp_error = gcp_exceptions.ResourceExhausted("Quota exceeded")

        result = subscription.translate_error(gcp_error)

        assert isinstance(result, ResourceExhaustedException)
        assert "Quota exceeded" in str(result)

    def test_translate_failed_precondition(self):
        """Test FailedPrecondition maps to FailedPreconditionException."""
        from multicloud.common.exceptions import FailedPreconditionException

        subscription = GcpSubscription(
            subscription_name="projects/test/subscriptions/sub"
        )
        gcp_error = gcp_exceptions.FailedPrecondition("Precondition not met")

        result = subscription.translate_error(gcp_error)

        assert isinstance(result, FailedPreconditionException)
        assert "Precondition not met" in str(result)

    def test_translate_aborted(self):
        """Test Aborted maps to DeadlineExceededException."""
        from multicloud.common.exceptions import DeadlineExceededException

        subscription = GcpSubscription(
            subscription_name="projects/test/subscriptions/sub"
        )
        gcp_error = gcp_exceptions.Aborted("Operation aborted")

        result = subscription.translate_error(gcp_error)

        assert isinstance(result, DeadlineExceededException)
        assert "Operation aborted" in str(result)

    def test_translate_out_of_range(self):
        """Test OutOfRange maps to InvalidArgumentException."""
        from multicloud.common.exceptions import InvalidArgumentException

        subscription = GcpSubscription(
            subscription_name="projects/test/subscriptions/sub"
        )
        gcp_error = gcp_exceptions.OutOfRange("Value out of range")

        result = subscription.translate_error(gcp_error)

        assert isinstance(result, InvalidArgumentException)
        assert "Value out of range" in str(result)

    def test_translate_method_not_implemented(self):
        """Test MethodNotImplemented maps to UnsupportedOperationException."""
        from multicloud.common.exceptions import UnsupportedOperationException

        subscription = GcpSubscription(
            subscription_name="projects/test/subscriptions/sub"
        )
        gcp_error = gcp_exceptions.MethodNotImplemented("Method not supported")

        result = subscription.translate_error(gcp_error)

        assert isinstance(result, UnsupportedOperationException)
        assert "Method not supported" in str(result)

    def test_translate_internal_server_error(self):
        """Test InternalServerError maps to UnknownException."""
        from multicloud.common.exceptions import UnknownException

        subscription = GcpSubscription(
            subscription_name="projects/test/subscriptions/sub"
        )
        gcp_error = gcp_exceptions.InternalServerError("Internal error")

        result = subscription.translate_error(gcp_error)

        assert isinstance(result, UnknownException)
        assert "Internal error" in str(result)

    def test_translate_service_unavailable(self):
        """Test ServiceUnavailable maps to UnknownException."""
        from multicloud.common.exceptions import UnknownException

        subscription = GcpSubscription(
            subscription_name="projects/test/subscriptions/sub"
        )
        gcp_error = gcp_exceptions.ServiceUnavailable("Service down")

        result = subscription.translate_error(gcp_error)

        assert isinstance(result, UnknownException)
        assert "Service down" in str(result)

    def test_translate_cancelled(self):
        """Test Cancelled maps to UnknownException."""
        from multicloud.common.exceptions import UnknownException

        subscription = GcpSubscription(
            subscription_name="projects/test/subscriptions/sub"
        )
        gcp_error = gcp_exceptions.Cancelled("Request cancelled")

        result = subscription.translate_error(gcp_error)

        assert isinstance(result, UnknownException)
        assert "Request cancelled" in str(result)

    def test_translate_generic_google_api_error(self):
        """Test GoogleAPICallError maps to UnknownException."""
        from multicloud.common.exceptions import UnknownException

        subscription = GcpSubscription(
            subscription_name="projects/test/subscriptions/sub"
        )
        gcp_error = gcp_exceptions.GoogleAPICallError("Unknown API error")

        result = subscription.translate_error(gcp_error)

        assert isinstance(result, UnknownException)
        assert "Unknown API error" in str(result)


class TestGcpSubscriptionValidation:
    """Test subscription name validation."""

    def test_validate_empty_subscription_name_raises(self):
        """Test that empty subscription name raises InvalidArgumentException."""
        with pytest.raises(InvalidArgumentException, match="cannot be null or empty"):
            GcpSubscription(subscription_name="")


class TestGcpSubscriptionCredentialsOverrider:
    """Test that credentials_overrider is resolved and used to build the client.

    The resolver itself (``get_credentials``) is unit-tested in
    ``tests/gcp_common/test_gcp_credentials.py``; these tests verify the
    subscription forwards its stored overrider to that resolver, builds the
    subscriber from the resolved credentials, and falls back to ADC when the
    resolver returns None (an unsupported override type).
    """

    @pytest.mark.parametrize(
        "overrider",
        [
            pytest.param(
                CredentialsOverrider(
                    credentials_type=CredentialsType.SESSION,
                    session_credentials=StsCredentials(security_token="bearer-token"),
                ),
                id="session",
            ),
            pytest.param(
                CredentialsOverrider(
                    credentials_type=CredentialsType.ASSUME_ROLE,
                    role="sa@test-project.iam.gserviceaccount.com",
                ),
                id="assume_role",
            ),
        ],
    )
    def test_overrider_resolved_and_used_for_subscriber(self, overrider):
        """A resolvable override reaches get_credentials and builds the subscriber."""
        resolved_creds = Mock(name="resolved_credentials")
        with (
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_subscription.get_credentials",
                return_value=resolved_creds,
            ) as mock_get_credentials,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_subscription.pubsub_v1.SubscriberClient"
            ) as mock_subscriber_cls,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_subscription.google.auth.default"
            ) as mock_adc,
        ):
            subscription = GcpSubscription(
                subscription_name="projects/test-project/subscriptions/test-sub",
                credentials_overrider=overrider,
            )
            subscriber = subscription._get_or_create_client()

            # Overrider AND scopes were forwarded to the resolver...
            mock_get_credentials.assert_called_once_with(
                overrider, scopes=DEFAULT_SCOPES
            )
            # ...ADC was NOT consulted (the override resolved)...
            mock_adc.assert_not_called()
            # ...and the subscriber was built from the resolved credentials.
            _, kwargs = mock_subscriber_cls.call_args
            assert kwargs["credentials"] is resolved_creds
            assert subscriber is mock_subscriber_cls.return_value

    def test_unsupported_override_falls_back_to_adc(self):
        """When get_credentials returns None, the subscription uses ADC instead."""
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="sa@test-project.iam.gserviceaccount.com",
            web_identity_token_supplier=lambda: "oidc-token",
        )
        adc_creds = Mock(name="adc_credentials")
        with (
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_subscription.get_credentials",
                return_value=None,
            ) as mock_get_credentials,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_subscription.pubsub_v1.SubscriberClient"
            ) as mock_subscriber_cls,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_subscription.google.auth.default",
                return_value=(adc_creds, "test-project"),
            ) as mock_adc,
        ):
            subscription = GcpSubscription(
                subscription_name="projects/test-project/subscriptions/test-sub",
                credentials_overrider=overrider,
            )
            subscription._get_or_create_client()

            # Resolver was tried with scopes and returned None, so ADC was consulted...
            mock_get_credentials.assert_called_once_with(
                overrider, scopes=DEFAULT_SCOPES
            )
            mock_adc.assert_called_once_with(scopes=DEFAULT_SCOPES)
            # ...and the subscriber was built from the ADC credentials.
            _, kwargs = mock_subscriber_cls.call_args
            assert kwargs["credentials"] is adc_creds

    def test_no_overrider_uses_adc(self):
        """Without an overrider, the subscription resolves via ADC directly."""
        adc_creds = Mock(name="adc_credentials")
        with (
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_subscription.get_credentials"
            ) as mock_get_credentials,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_subscription.pubsub_v1.SubscriberClient"
            ) as mock_subscriber_cls,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_subscription.google.auth.default",
                return_value=(adc_creds, "test-project"),
            ) as mock_adc,
        ):
            subscription = GcpSubscription(
                subscription_name="projects/test-project/subscriptions/test-sub",
            )
            subscription._get_or_create_client()

            mock_get_credentials.assert_not_called()
            mock_adc.assert_called_once_with(scopes=DEFAULT_SCOPES)
            _, kwargs = mock_subscriber_cls.call_args
            assert kwargs["credentials"] is adc_creds
