"""AWS IAM provider — registers AwsIam with IAM_REGISTRY at import time."""

from multicloud.iam.iam_aws.aws_iam import AwsIam

__all__ = ["AwsIam"]
