"""GCP Pub/Sub topic conformance tests with VCR.py record/replay.

Extends :class:`BaseTopicConformance`, supplying a GCP-specific
``Harness`` that wires up the :class:`TopicClient` and :class:`SubscriptionClient`.

Run modes::

    # Record mode (requires GCP credentials and real Pub/Sub resources)
    pytest tests/pubsub/conformance/test_gcp_topic_conformance.py \\
        --record-mode=rewrite -v

    # Replay mode (no credentials needed, uses cassettes)
    pytest tests/pubsub/conformance/test_gcp_topic_conformance.py \\
        --record-mode=none -v
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional
from unittest.mock import patch

import pytest
from multicloud.pubsub.pubsub_client import SubscriptionClient, TopicClient

from tests.gcp_common.mock_gcp_credentials import make_mock_credentials
from tests.pubsub.conformance.base_topic_conformance import (
    BaseTopicConformance,
    Harness,
)


class GcpHarness(Harness):
    """GCP Pub/Sub test harness."""

    def __init__(self, topic_name: str, subscription_name: str) -> None:
        self._topic_name = topic_name
        self._subscription_name = subscription_name
        self._topic_client = None
        # Track every subscription client created so close() can drain them all.
        # Acks/nacks are fire-and-forget on a background batcher thread; leaving a
        # client undrained lets that thread issue HTTP after the test ends, hitting
        # the next test's VCR cassette and causing order-dependent flakes.
        self._subscription_clients: list[SubscriptionClient] = []

    def create_topic_client(self) -> TopicClient:
        """Create TopicClient for GCP Pub/Sub."""
        if self._topic_client is None:
            self._topic_client = TopicClient(
                provider="gcp", topic_name=self._topic_name, transport="rest"
            )
        return self._topic_client

    def create_subscription_client(
        self, nack_visibility_timeout: Optional[timedelta] = None
    ) -> SubscriptionClient:
        """Create SubscriptionClient for GCP Pub/Sub.

        Args:
            nack_visibility_timeout: Optional nack visibility timeout for the subscription.
        """
        kwargs = {
            "provider": "gcp",
            "subscription_name": self._subscription_name,
            "transport": "rest",
        }
        if nack_visibility_timeout is not None:
            kwargs["nack_visibility_timeout"] = nack_visibility_timeout

        subscription_client = SubscriptionClient(**kwargs)
        self._subscription_clients.append(subscription_client)
        return subscription_client

    def get_provider_id(self) -> str:
        return "gcp"

    def close(self) -> None:
        if self._topic_client is not None:
            self._topic_client.close()
        for subscription_client in self._subscription_clients:
            subscription_client.close()


@pytest.fixture(scope="module", autouse=True)
def mock_gcp_auth(request):
    """Patch google.auth.default() in replay mode to bypass ADC lookup.

    In record mode the patch is NOT applied — real credentials are used.
    """
    record_mode = request.config.getoption("--record-mode") or "none"
    if record_mode == "none":
        fake_creds = make_mock_credentials()
        with patch("google.auth.default", return_value=(fake_creds, "fake-project")):
            yield
    else:
        yield


@pytest.fixture(scope="function")
def harness(mock_gcp_auth, gcp_topic_name, gcp_subscription_name):
    """Create GCP Pub/Sub harness with topic and subscription."""
    h = GcpHarness(gcp_topic_name, gcp_subscription_name)
    yield h
    h.close()


@pytest.fixture(scope="function")
def client(harness):
    """Create topic client from harness."""
    return harness.create_topic_client()


@pytest.fixture(scope="function")
def subscription_client(harness):
    """Create subscription client from harness."""
    return harness.create_subscription_client()


@pytest.mark.vcr
class TestGcpPubSubConformance(BaseTopicConformance):
    """GCP Pub/Sub topic conformance tests.

    All test methods are inherited from
    :class:`BaseTopicConformance`. Cassettes live at
    ``tests/pubsub/conformance/cassettes/gcp/`` and are keyed by
    ``TestGcpPubSubConformance.<test_method>.yaml``.
    """

    pass
