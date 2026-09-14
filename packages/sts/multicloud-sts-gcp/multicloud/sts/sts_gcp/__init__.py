"""GCP IAM implementation of the security-token-service driver.

The provider class registers itself with ``STS_REGISTRY`` via the
``@STS_REGISTRY.register("gcp")`` decorator at import time. Loaded
automatically by the ``multicloud.sts.providers`` entry-point mechanism
whenever ``multicloud-sts-gcp`` is installed.
"""

from multicloud.sts.sts_gcp.gcp_sts import GcpSts
from multicloud.sts.sts_gcp.gcp_sts_utilities import GcpStsUtilities

__all__ = ["GcpSts", "GcpStsUtilities"]
