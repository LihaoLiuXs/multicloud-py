"""AWS SQS topic conformance tests with VCR.py record/replay.

Extends :class:`BaseTopicConformance`, supplying an AWS SQS-specific
``Harness`` that wires up the :class:`TopicClient` and :class:`SubscriptionClient`.
Uses SQS queue as BOTH topic (for send) and subscription (for receive),
following the AWS SQS unified queue pattern.

Run modes::

    # Record mode (requires AWS credentials and real SQS resources)
    pytest tests/pubsub/conformance/test_aws_sqs_topic_conformance.py \
        --record-mode=all -v

    # Replay mode (no credentials needed, uses cassettes)
    pytest tests/pubsub/conformance/test_aws_sqs_topic_conformance.py \
        --record-mode=none -v
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

import pytest
from multicloud.pubsub.pubsub_client import SubscriptionClient, TopicClient

from tests.pubsub.conformance.base_topic_conformance import (
    BaseTopicConformance,
    Harness,
)


class AwsSqsHarness(Harness):
    """AWS SQS test harness.

    Uses the same SQS queue as BOTH topic (for send via AwsSqsTopic) and
    subscription (for receive via AwsSubscription). Each test gets a unique
    queue name.
    """

    def __init__(self, queue_name: str, region: str) -> None:
        self._queue_name = queue_name
        self._region = region
        self._topic_client = None
        self._subscription_client = None

    def create_topic_client(self) -> TopicClient:
        """Create TopicClient using SQS queue as the topic."""
        if self._topic_client is None:
            # AwsSqsTopic uses queue URL or name as topic_name
            self._topic_client = TopicClient(
                provider="awssqs", topic_name=self._queue_name, region=self._region
            )
        return self._topic_client

    def create_subscription_client(
        self, nack_visibility_timeout: Optional[timedelta] = None
    ) -> SubscriptionClient:
        """Create SubscriptionClient using the same SQS queue.

        Args:
            nack_visibility_timeout: Optional nack visibility timeout for the subscription.
        """
        kwargs = {
            "provider": "aws",
            "subscription_name": self._queue_name,
            "region": self._region,
            "wait_time_seconds": 1,
        }
        if nack_visibility_timeout is not None:
            kwargs["nack_visibility_timeout"] = nack_visibility_timeout

        return SubscriptionClient(**kwargs)

    def get_provider_id(self) -> str:
        return "awssqs"

    def close(self) -> None:
        if self._topic_client is not None:
            self._topic_client.close()
        if self._subscription_client is not None:
            self._subscription_client.close()


@pytest.fixture(scope="function")
def harness(aws_sqs_queue_name, aws_region, request):
    """Create SQS harness.

    Ensures the queue exists before running tests.
    CreateQueue is idempotent; in replay mode this call is served by VCR.
    """
    import boto3

    sqs = boto3.client("sqs", region_name=aws_region)
    # CreateQueue is idempotent: creates if doesn't exist, returns URL if exists
    sqs.create_queue(QueueName=aws_sqs_queue_name)

    h = AwsSqsHarness(aws_sqs_queue_name, aws_region)
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
class TestAwsSqsPubSubConformance(BaseTopicConformance):
    """AWS SQS topic conformance tests.

    All test methods are inherited from
    :class:`BaseTopicConformance`. Cassettes live at
    ``tests/pubsub/conformance/cassettes/aws/`` and are keyed by
    ``TestAwsSqsPubSubConformance.<test_method>.yaml``.

    Uses SQS queue as BOTH topic and subscription (AWS SQS unified queue pattern).

    """

    pass
