"""Unit tests for GCP Pub/Sub topic implementation."""

from unittest.mock import Mock, patch

import pytest
from google.api_core import exceptions as gcp_exceptions
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
from multicloud.pubsub.pubsub_client.driver import Message
from multicloud.pubsub.pubsub_gcp.gcp_topic import GcpTopic


@pytest.fixture
def mock_publisher():
    """Mock GCP PublisherClient whose unary publish() returns a response directly."""
    publisher = Mock()
    publisher.publish.return_value = Mock(message_ids=["msg-id-123"])
    return publisher


@pytest.fixture
def gcp_topic(mock_publisher):
    """Create a GcpTopic instance with mocked publisher."""
    topic = GcpTopic(
        topic_name="projects/test-project/topics/test-topic",
        region="us-central1",
    )
    topic._publisher = mock_publisher
    return topic


class TestGcpTopicBasics:
    """Test basic GCP topic functionality."""

    def test_provider_id(self, gcp_topic):
        """Test that provider ID is correct."""
        assert gcp_topic.get_provider_id() == "gcp"

    def test_batcher_options(self, gcp_topic):
        """Test that batcher options match GCP Pub/Sub limits."""
        options = gcp_topic.create_batcher_options()
        assert options.max_handlers == 2
        assert options.min_batch_size == 1
        assert options.max_batch_size == 1000
        assert options.max_batch_byte_size == 8 * 1024 * 1024
        assert gcp_topic.get_max_message_size_bytes() == options.max_batch_byte_size

    def test_validate_topic_name_valid(self):
        """Test validation of valid topic names."""
        topic = GcpTopic(topic_name="projects/my-project/topics/my-topic")
        assert topic.topic_name == "projects/my-project/topics/my-topic"

    def test_validate_topic_name_invalid_format(self):
        """Test validation of invalid topic name format."""
        with pytest.raises(
            InvalidArgumentException,
            match="projects/\\{projectId\\}/topics/\\{topicId\\}",
        ):
            GcpTopic(topic_name="just-a-topic")

    def test_validate_topic_name_wrong_prefix(self):
        """Test validation of topic name with wrong prefix."""
        with pytest.raises(
            InvalidArgumentException,
            match="projects/\\{projectId\\}/topics/\\{topicId\\}",
        ):
            GcpTopic(topic_name="projects/test/subscriptions/test")

    def test_constructor_requires_topic_name(self):
        """Test that topic_name is required."""
        with pytest.raises(InvalidArgumentException, match="cannot be null or empty"):
            GcpTopic(topic_name="")

    def test_default_publish_timeout(self, gcp_topic):
        """Test that publish_timeout defaults to the documented ceiling."""
        assert gcp_topic._publish_timeout == 120.0

    def test_custom_publish_timeout(self):
        """Test that a custom publish_timeout is stored on the topic."""
        topic = GcpTopic(topic_name="projects/test/topics/test", publish_timeout=30.0)
        assert topic._publish_timeout == 30.0

    @pytest.mark.parametrize("bad_timeout", [0, -1.0])
    def test_invalid_publish_timeout_rejected(self, bad_timeout):
        """Test that a non-positive publish_timeout is rejected."""
        with pytest.raises(
            InvalidArgumentException, match="publish_timeout must be greater than 0"
        ):
            GcpTopic(
                topic_name="projects/test/topics/test", publish_timeout=bad_timeout
            )


def _publish_request(mock_publisher):
    """Return the single PublishRequest passed to the unary publish() call."""
    return mock_publisher.publish.call_args.kwargs["request"]


class TestGcpTopicSend:
    """Test message sending functionality."""

    def test_send_simple_message(self, gcp_topic, mock_publisher):
        """Test sending a simple message via a single unary publish RPC."""
        message = Message.builder().with_body(b"test message").build()

        gcp_topic.do_send_batch([message])

        assert mock_publisher.publish.call_count == 1
        request = _publish_request(mock_publisher)
        assert request.topic == "projects/test-project/topics/test-topic"
        assert len(request.messages) == 1
        assert request.messages[0].data == b"test message"

    def test_send_passes_configured_timeout_and_bounded_retry(self):
        """Test publish() gets the configured timeout and a deadline-bounded retry."""
        publisher = Mock()
        publisher.publish.return_value = Mock(message_ids=["msg-id"])

        topic = GcpTopic(
            topic_name="projects/test-project/topics/test-topic",
            publish_timeout=5.0,
        )
        topic._publisher = publisher

        topic.do_send_batch([Message.builder().with_body(b"test").build()])

        _, kwargs = publisher.publish.call_args
        assert kwargs["timeout"] == 5.0
        # Retry deadline bounded to the configured timeout.
        assert kwargs["retry"]._deadline == 5.0

    def test_send_retry_preserves_retryable_error_set(self, gcp_topic, mock_publisher):
        """Test bounding the retry deadline keeps the retryable-error set intact."""
        gcp_topic.do_send_batch([Message.builder().with_body(b"test").build()])

        retry = mock_publisher.publish.call_args.kwargs["retry"]
        # Transient errors stay retryable; terminal errors do not.
        assert retry._predicate(gcp_exceptions.ServiceUnavailable("unavailable"))
        assert retry._predicate(gcp_exceptions.InternalServerError("internal"))
        assert retry._predicate(gcp_exceptions.ResourceExhausted("exhausted"))
        assert retry._predicate(gcp_exceptions.DeadlineExceeded("deadline"))
        assert not retry._predicate(gcp_exceptions.NotFound("missing"))
        assert not retry._predicate(gcp_exceptions.InvalidArgument("bad"))

    def test_send_with_metadata(self, gcp_topic, mock_publisher):
        """Test sending message with metadata as message attributes."""
        metadata = {
            "key1": "value1",
            "key2": "value2",
        }
        message = Message.builder().with_body(b"test").with_metadata(metadata).build()

        gcp_topic.do_send_batch([message])

        request = _publish_request(mock_publisher)
        assert request.messages[0].attributes["key1"] == "value1"
        assert request.messages[0].attributes["key2"] == "value2"

    def test_send_batch_multiple_messages(self, gcp_topic, mock_publisher):
        """Test that a multi-message batch is sent as one unary publish RPC."""
        messages = [
            Message.builder().with_body(b"msg1").build(),
            Message.builder().with_body(b"msg2").build(),
            Message.builder().with_body(b"msg3").build(),
        ]

        gcp_topic.do_send_batch(messages)

        # Single unary RPC carrying all three messages (no per-message futures).
        assert mock_publisher.publish.call_count == 1
        request = _publish_request(mock_publisher)
        assert [m.data for m in request.messages] == [b"msg1", b"msg2", b"msg3"]

    def test_send_empty_batch(self, gcp_topic, mock_publisher):
        """Test that sending empty batch is a no-op."""
        gcp_topic.do_send_batch([])
        assert not mock_publisher.publish.called

    def test_send_failure_propagates(self, gcp_topic, mock_publisher):
        """Test that a synchronous publish failure propagates as a native GCP exception."""
        mock_publisher.publish.side_effect = gcp_exceptions.NotFound(
            "404 Topic not found"
        )

        message = Message.builder().with_body(b"test").build()

        with pytest.raises(gcp_exceptions.NotFound) as exc_info:
            gcp_topic.do_send_batch([message])

        assert "404 Topic not found" in str(exc_info.value)

    def test_send_does_not_wait_on_future_result(self, gcp_topic, mock_publisher):
        """Test publish does not block on Future.result (the old 120s wait)."""
        future = Mock()
        future.result.side_effect = AssertionError(
            "do_send_batch must not wait on Future.result"
        )
        mock_publisher.publish.return_value = future

        gcp_topic.do_send_batch([Message.builder().with_body(b"test").build()])

        future.result.assert_not_called()

    def test_full_batch_at_byte_cap_stays_under_publish_server_limit(self, gcp_topic):
        """A max-size batch must serialize under Pub/Sub's 10_000_000-byte request cap."""
        from google.pubsub_v1 import PublishRequest, PubsubMessage

        options = gcp_topic.create_batcher_options()
        n = options.max_batch_size
        attrs = {f"k{i:02d}": "v" for i in range(100)}
        attr_bytes = sum(len(k) + len(v) for k, v in attrs.items())
        body_len = max(0, (options.max_batch_byte_size // n) - attr_bytes)
        messages = [
            PubsubMessage(data=b"x" * body_len, attributes=attrs) for _ in range(n)
        ]
        request = PublishRequest(topic=gcp_topic.topic_name, messages=messages)
        assert request._pb.ByteSize() <= 10_000_000

        accounted = Message(body=b"x" * body_len, metadata=attrs).get_byte_size() * n
        assert accounted <= options.max_batch_byte_size
        assert accounted > options.max_batch_byte_size - n


class TestGcpTopicErrorTranslation:
    """Test exception translation for GCP errors."""

    def test_translate_not_found(self, gcp_topic):
        """Test translation of NotFound error."""
        error = gcp_exceptions.NotFound("Resource not found")
        translated = gcp_topic.translate_error(error)
        assert isinstance(translated, ResourceNotFoundException)

    def test_translate_permission_denied(self, gcp_topic):
        """Test translation of PermissionDenied error."""
        error = gcp_exceptions.PermissionDenied("Access denied")
        translated = gcp_topic.translate_error(error)
        assert isinstance(translated, UnAuthorizedException)

    def test_translate_unauthenticated(self, gcp_topic):
        """Test translation of Unauthenticated error."""
        error = gcp_exceptions.Unauthenticated("Invalid credentials")
        translated = gcp_topic.translate_error(error)
        assert isinstance(translated, UnAuthorizedException)

    def test_translate_invalid_argument(self, gcp_topic):
        """Test translation of InvalidArgument error."""
        error = gcp_exceptions.InvalidArgument("Bad request")
        translated = gcp_topic.translate_error(error)
        assert isinstance(translated, InvalidArgumentException)

    def test_translate_generic_google_api_error(self, gcp_topic):
        """Test translation of generic GoogleAPICallError."""
        error = gcp_exceptions.GoogleAPICallError("Generic error")
        translated = gcp_topic.translate_error(error)
        from multicloud.common.exceptions import UnknownException

        assert isinstance(translated, UnknownException)

    def test_translate_already_substrate_exception(self, gcp_topic):
        """Test that SubstrateSdkException is returned unchanged."""
        original = InvalidArgumentException("test")
        translated = gcp_topic.translate_error(original)
        assert translated is original

    def test_translate_generic_exception(self, gcp_topic):
        """Test translation of unknown exceptions."""
        error = RuntimeError("Unknown error")
        translated = gcp_topic.translate_error(error)
        from multicloud.common.exceptions import UnknownException

        assert isinstance(translated, UnknownException)

    def test_translate_retry_error_is_deadline_exceeded(self, gcp_topic):
        """RetryError from a publish_timeout ceiling maps to DeadlineExceededException."""
        from multicloud.common.exceptions import DeadlineExceededException

        error = gcp_exceptions.RetryError("Timeout of 5.0s exceeded", None)
        translated = gcp_topic.translate_error(error)
        assert isinstance(translated, DeadlineExceededException)
        assert "Timeout of 5.0s exceeded" in str(translated)


class TestGcpTopicClose:
    """Test resource cleanup."""

    def test_close_cleans_up_publisher(self, gcp_topic, mock_publisher):
        """Test that close cleans up the publisher."""
        gcp_topic.close()

        # Closing releases the transport channel (no background thread to stop).
        assert mock_publisher.transport.close.called
        # Publisher should be set to None
        assert gcp_topic._publisher is None

    def test_close_idempotent(self, gcp_topic):
        """Test that close can be called multiple times safely."""
        gcp_topic.close()
        gcp_topic.close()  # Should not raise


class TestGcpTopicLazyInit:
    """Test lazy initialization of publisher client."""

    def test_publisher_created_on_first_send(self):
        """Test that publisher is created lazily on first send."""
        with (
            patch("google.auth.default") as mock_auth_default,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_topic.PublisherClient"
            ) as mock_publisher_class,
        ):
            mock_auth_default.return_value = (Mock(), "test-project")
            mock_pub = Mock()
            mock_pub.publish.return_value = Mock(message_ids=["msg-id"])
            mock_publisher_class.return_value = mock_pub

            topic = GcpTopic(topic_name="projects/test/topics/test")
            # Publisher should not be created yet
            assert topic._publisher is None

            # Send a message
            message = Message.builder().with_body(b"test").build()
            topic.do_send_batch([message])

            # Publisher should now be created and be the mock instance
            assert topic._publisher is mock_pub
            assert mock_publisher_class.called


class TestGcpTopicExceptionTranslation:
    """Test comprehensive GCP exception code translations."""

    def test_translate_cancelled(self):
        """Test CANCELLED status code maps to UnknownException."""
        from google.api_core.exceptions import Cancelled
        from multicloud.common.exceptions import UnknownException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = Cancelled("operation cancelled")
        translated = topic.translate_error(exc)
        assert isinstance(translated, UnknownException)

    def test_translate_unknown(self):
        """Test UNKNOWN status code maps to UnknownException."""
        from google.api_core.exceptions import Unknown
        from multicloud.common.exceptions import UnknownException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = Unknown("unknown error")
        translated = topic.translate_error(exc)
        assert isinstance(translated, UnknownException)

    def test_translate_invalid_argument(self):
        """Test INVALID_ARGUMENT status code maps to InvalidArgumentException."""
        from google.api_core.exceptions import InvalidArgument
        from multicloud.common.exceptions import InvalidArgumentException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = InvalidArgument("invalid argument")
        translated = topic.translate_error(exc)
        assert isinstance(translated, InvalidArgumentException)

    def test_translate_deadline_exceeded(self):
        """Test DEADLINE_EXCEEDED status code maps to DeadlineExceededException."""
        from google.api_core.exceptions import DeadlineExceeded
        from multicloud.common.exceptions import DeadlineExceededException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = DeadlineExceeded("deadline exceeded")
        translated = topic.translate_error(exc)
        assert isinstance(translated, DeadlineExceededException)

    def test_translate_not_found(self):
        """Test NOT_FOUND status code maps to ResourceNotFoundException."""
        from google.api_core.exceptions import NotFound
        from multicloud.common.exceptions import ResourceNotFoundException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = NotFound("resource not found")
        translated = topic.translate_error(exc)
        assert isinstance(translated, ResourceNotFoundException)

    def test_translate_already_exists(self):
        """Test ALREADY_EXISTS status code maps to ResourceAlreadyExistsException."""
        from google.api_core.exceptions import AlreadyExists
        from multicloud.common.exceptions import ResourceAlreadyExistsException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = AlreadyExists("resource already exists")
        translated = topic.translate_error(exc)
        assert isinstance(translated, ResourceAlreadyExistsException)

    def test_translate_permission_denied(self):
        """Test PERMISSION_DENIED status code maps to UnAuthorizedException."""
        from google.api_core.exceptions import PermissionDenied
        from multicloud.common.exceptions import UnAuthorizedException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = PermissionDenied("permission denied")
        translated = topic.translate_error(exc)
        assert isinstance(translated, UnAuthorizedException)

    def test_translate_resource_exhausted(self):
        """Test RESOURCE_EXHAUSTED status code maps to ResourceExhaustedException."""
        from google.api_core.exceptions import ResourceExhausted
        from multicloud.common.exceptions import ResourceExhaustedException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = ResourceExhausted("resource exhausted")
        translated = topic.translate_error(exc)
        assert isinstance(translated, ResourceExhaustedException)

    def test_translate_failed_precondition(self):
        """Test FAILED_PRECONDITION status code maps to FailedPreconditionException."""
        from google.api_core.exceptions import FailedPrecondition
        from multicloud.common.exceptions import FailedPreconditionException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = FailedPrecondition("failed precondition")
        translated = topic.translate_error(exc)
        assert isinstance(translated, FailedPreconditionException)

    def test_translate_aborted(self):
        """Test ABORTED status code maps to DeadlineExceededException."""
        from google.api_core.exceptions import Aborted
        from multicloud.common.exceptions import DeadlineExceededException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = Aborted("aborted")
        translated = topic.translate_error(exc)
        assert isinstance(translated, DeadlineExceededException)

    def test_translate_out_of_range(self):
        """Test OUT_OF_RANGE status code maps to InvalidArgumentException."""
        from google.api_core.exceptions import OutOfRange
        from multicloud.common.exceptions import InvalidArgumentException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = OutOfRange("out of range")
        translated = topic.translate_error(exc)
        assert isinstance(translated, InvalidArgumentException)

    def test_translate_unimplemented(self):
        """Test UNIMPLEMENTED status code maps to UnsupportedOperationException."""
        from google.api_core.exceptions import MethodNotImplemented
        from multicloud.common.exceptions import UnsupportedOperationException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = MethodNotImplemented("method not implemented")
        translated = topic.translate_error(exc)
        assert isinstance(translated, UnsupportedOperationException)

    def test_translate_internal(self):
        """Test INTERNAL status code maps to UnknownException."""
        from google.api_core.exceptions import InternalServerError
        from multicloud.common.exceptions import UnknownException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = InternalServerError("internal server error")
        translated = topic.translate_error(exc)
        assert isinstance(translated, UnknownException)

    def test_translate_unavailable(self):
        """Test UNAVAILABLE status code maps to UnknownException."""
        from google.api_core.exceptions import ServiceUnavailable
        from multicloud.common.exceptions import UnknownException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = ServiceUnavailable("service unavailable")
        translated = topic.translate_error(exc)
        assert isinstance(translated, UnknownException)

    def test_translate_data_loss(self):
        """Test DATA_LOSS status code maps to UnknownException."""
        from google.api_core.exceptions import DataLoss
        from multicloud.common.exceptions import UnknownException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = DataLoss("data loss")
        translated = topic.translate_error(exc)
        assert isinstance(translated, UnknownException)

    def test_translate_unauthenticated(self):
        """Test UNAUTHENTICATED status code maps to UnAuthorizedException."""
        from google.api_core.exceptions import Unauthenticated
        from multicloud.common.exceptions import UnAuthorizedException

        topic = GcpTopic(topic_name="projects/test/topics/test")
        exc = Unauthenticated("unauthenticated")
        translated = topic.translate_error(exc)
        assert isinstance(translated, UnAuthorizedException)


class TestGcpTopicCredentialsOverrider:
    """Test that credentials_overrider is resolved and used to build the client.

    The resolver itself (``get_credentials``) is unit-tested in
    ``tests/gcp_common/test_gcp_credentials.py``; these tests verify the topic
    forwards its stored overrider to that resolver, builds the publisher from
    the resolved credentials, and falls back to ADC when the resolver returns
    None (an unsupported override type).
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
    def test_overrider_resolved_and_used_for_publisher(self, overrider):
        """A resolvable override reaches get_credentials and builds the publisher."""
        resolved_creds = Mock(name="resolved_credentials")
        with (
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_topic.get_credentials",
                return_value=resolved_creds,
            ) as mock_get_credentials,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_topic.PublisherClient"
            ) as mock_publisher_cls,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_topic.google.auth.default"
            ) as mock_adc,
        ):
            topic = GcpTopic(
                topic_name="projects/test-project/topics/test-topic",
                credentials_overrider=overrider,
            )
            publisher = topic._get_or_create_client()

            # Overrider AND scopes were forwarded to the resolver...
            mock_get_credentials.assert_called_once_with(
                overrider, scopes=DEFAULT_SCOPES
            )
            # ...ADC was NOT consulted (the override resolved)...
            mock_adc.assert_not_called()
            # ...and the publisher was built from the resolved credentials.
            _, kwargs = mock_publisher_cls.call_args
            assert kwargs["credentials"] is resolved_creds
            assert publisher is mock_publisher_cls.return_value

    def test_unsupported_override_falls_back_to_adc(self):
        """When get_credentials returns None, the topic uses ADC instead."""
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="sa@test-project.iam.gserviceaccount.com",
            web_identity_token_supplier=lambda: "oidc-token",
        )
        adc_creds = Mock(name="adc_credentials")
        with (
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_topic.get_credentials",
                return_value=None,
            ) as mock_get_credentials,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_topic.PublisherClient"
            ) as mock_publisher_cls,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_topic.google.auth.default",
                return_value=(adc_creds, "test-project"),
            ) as mock_adc,
        ):
            topic = GcpTopic(
                topic_name="projects/test-project/topics/test-topic",
                credentials_overrider=overrider,
            )
            topic._get_or_create_client()

            # Resolver was tried with scopes and returned None, so ADC was consulted...
            mock_get_credentials.assert_called_once_with(
                overrider, scopes=DEFAULT_SCOPES
            )
            mock_adc.assert_called_once_with(scopes=DEFAULT_SCOPES)
            # ...and the publisher was built from the ADC credentials.
            _, kwargs = mock_publisher_cls.call_args
            assert kwargs["credentials"] is adc_creds

    def test_no_overrider_uses_adc(self):
        """Without an overrider, the topic resolves via ADC and never calls get_credentials."""
        adc_creds = Mock(name="adc_credentials")
        with (
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_topic.get_credentials"
            ) as mock_get_credentials,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_topic.PublisherClient"
            ) as mock_publisher_cls,
            patch(
                "multicloud.pubsub.pubsub_gcp.gcp_topic.google.auth.default",
                return_value=(adc_creds, "test-project"),
            ) as mock_adc,
        ):
            topic = GcpTopic(
                topic_name="projects/test-project/topics/test-topic",
            )
            topic._get_or_create_client()

            mock_get_credentials.assert_not_called()
            mock_adc.assert_called_once_with(scopes=DEFAULT_SCOPES)
            _, kwargs = mock_publisher_cls.call_args
            assert kwargs["credentials"] is adc_creds
