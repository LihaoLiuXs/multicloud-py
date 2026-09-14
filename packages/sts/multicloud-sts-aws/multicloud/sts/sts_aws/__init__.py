"""AWS STS implementation of the security-token-service driver.

The provider class registers itself with ``STS_REGISTRY`` via the
``@STS_REGISTRY.register("aws")`` decorator at import time. Loaded
automatically by the ``multicloud.sts.providers`` entry-point mechanism
whenever ``multicloud-sts-aws`` is installed.
"""

from multicloud.sts.sts_aws.aws_sts import AwsSts
from multicloud.sts.sts_aws.aws_sts_utilities import AwsStsUtilities

__all__ = ["AwsSts", "AwsStsUtilities"]
