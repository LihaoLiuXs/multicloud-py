"""AWS-specific cross-service utilities.

Shared helpers used by AWS provider modules across services
(STS, Blob, PubSub, DocStore). This package depends on AWS SDKs and
must not be imported by cloud-agnostic code (multicloud.common.*).
"""

from multicloud.aws_common.aws_client_config import s3_client_config
from multicloud.aws_common.aws_credentials import get_session
from multicloud.aws_common.error_mapping import (
    translate_aws_sns_error,
    translate_aws_sqs_error,
)

__all__ = [
    "get_session",
    "s3_client_config",
    "translate_aws_sns_error",
    "translate_aws_sqs_error",
]
