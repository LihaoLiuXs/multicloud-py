"""AWS SNS topic conformance tests with VCR.py record/replay.

Extends :class:`BaseTopicConformance`, supplying an AWS SNS-specific
``Harness`` that wires up the :class:`TopicClient`. SNS does NOT support
receiving messages, so all receive/ack/nack tests are disabled (SNS
publish-only pattern).

Run modes::

    # Record mode (requires AWS credentials and real SNS topic)
    pytest tests/pubsub/conformance/test_aws_sns_topic_conformance.py \
        --record-mode=all -v

    # Replay mode (no credentials needed, uses cassettes)
    pytest tests/pubsub/conformance/test_aws_sns_topic_conformance.py \
        --record-mode=none -v
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Optional

import pytest
from multicloud.pubsub.pubsub_client import TopicClient

from tests.pubsub.conformance.base_topic_conformance import (
    BaseTopicConformance,
    Harness,
)

# Module-level constants for AWS configuration (follows STS conformance pattern)
AWS_ACCOUNT_ID = os.environ.get("CONFORMANCE_AWS_ACCOUNT_ID", "000000000000")


class AwsSnsHarness(Harness):
    """AWS SNS test harness.

    SNS is publish-only; it does not support subscriptions directly.
    Subscription functionality requires separate SQS setup (see test_aws_sqs_topic_conformance.py).
    """

    def __init__(self, topic_name: str, region: str) -> None:
        self._topic_name = topic_name
        self._region = region
        self._topic_client = None

    def create_topic_client(self) -> TopicClient:
        """Create TopicClient."""
        if self._topic_client is None:
            self._topic_client = TopicClient(
                provider="awssns", topic_name=self._topic_name, region=self._region
            )
        return self._topic_client

    def create_subscription_client(
        self, nack_visibility_timeout: Optional[timedelta] = None
    ):
        """AWS SNS does not support subscriptions (publish-only).

        Args:
            nack_visibility_timeout: Optional nack visibility timeout (ignored for SNS).

        Returns None so conformance tests requiring subscriptions are skipped.
        """
        return None

    def get_provider_id(self) -> str:
        return "awssns"

    def close(self) -> None:
        if self._topic_client is not None:
            self._topic_client.close()


@pytest.fixture(scope="function")
def harness(aws_sns_topic_name, aws_region, request, vcr_cassette_dir):
    """Create SNS topic harness.

    NOTE: Topic creation is a pre-test setup step, not part of the test itself.
    In record mode, create the topic manually before running tests. In replay mode,
    VCR will replay all recorded interactions including GetTopicAttributes.

    The fixture reads the TopicArn from the cassette (replay mode) or constructs
    it from the topic name (record mode, assuming topic exists).
    """
    import re
    from pathlib import Path

    import yaml

    topic_arn = None
    record_mode = request.config.getoption("--record-mode") or "none"

    if record_mode == "none":
        # Replay mode: extract TopicArn from cassette
        test_name = request.node.name
        cassette_file = (
            Path(vcr_cassette_dir) / f"{request.cls.__name__}.{test_name}.yaml"
        )

        if cassette_file.exists():
            with open(cassette_file, "r") as f:
                cassette = yaml.safe_load(f)
                # Find GetTopicAttributes request and extract TopicArn
                for interaction in cassette.get("interactions", []):
                    body = interaction.get("request", {}).get("body", "")
                    if "GetTopicAttributes" in body:
                        # Parse URL-encoded TopicArn parameter
                        # Example: TopicArn=arn%3Aaws%3Asns%3Aus-west-2%3A123456789012%3Atest-sns-testSendBatchMessages
                        from urllib.parse import unquote

                        match = re.search(r"TopicArn=([^&]+)", body)
                        if match:
                            topic_arn = unquote(match.group(1))
                            break

    if not topic_arn:
        # Record mode or fallback: construct ARN from topic name
        # Assumes topic was created manually before running tests
        topic_arn = f"arn:aws:sns:{aws_region}:{AWS_ACCOUNT_ID}:{aws_sns_topic_name}"

    h = AwsSnsHarness(topic_arn, aws_region)
    yield h
    h.close()


@pytest.fixture(scope="function")
def client(harness):
    """Create topic client from harness."""
    return harness.create_topic_client()


@pytest.fixture(scope="function")
def subscription_client(harness):
    """AWS SNS does not support subscriptions.

    Returns None so tests requiring subscription_client are skipped.
    """
    return harness.create_subscription_client()


@pytest.mark.vcr
class TestAwsSnsPubSubConformance(BaseTopicConformance):
    """AWS SNS topic conformance tests.

    All test methods are inherited from
    :class:`BaseTopicConformance`. Cassettes live at
    ``tests/pubsub/conformance/cassettes/aws/`` and are keyed by
    ``TestAwsSnsPubSubConformance.<test_method>.yaml``.

    SNS does NOT support receiving messages. The ``subscription_client``
    fixture returns None, so tests requiring subscriptions are
    automatically skipped by the base class.
    """
