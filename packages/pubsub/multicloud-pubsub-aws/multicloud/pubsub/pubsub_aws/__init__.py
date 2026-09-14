"""Pubsub provider implementation for AWS (SQS/SNS).

This package contains the AWS-specific implementations of BaseTopic
and BaseSubscription using the boto3 library with SQS and SNS.
"""

# Import implementations to trigger registration
from multicloud.pubsub.pubsub_aws.aws_base_topic import AwsBaseTopic
from multicloud.pubsub.pubsub_aws.aws_sns_topic import AwsSnsTopic
from multicloud.pubsub.pubsub_aws.aws_sqs_topic import AwsSqsTopic
from multicloud.pubsub.pubsub_aws.aws_subscription import (
    AwsAckID,
    AwsSubscription,
    AwsSubscriptionOptions,
    create_aws_subscription,
)

__all__ = [
    "AwsBaseTopic",
    "AwsSqsTopic",
    "AwsSnsTopic",
    "AwsSubscription",
    "AwsAckID",
    "AwsSubscriptionOptions",
    "create_aws_subscription",
]
