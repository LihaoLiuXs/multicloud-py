"""GCP IAM provider — registers GcpIam with IAM_REGISTRY at import time."""

from multicloud.iam.iam_gcp.gcp_iam import GcpIam

__all__ = ["GcpIam"]
