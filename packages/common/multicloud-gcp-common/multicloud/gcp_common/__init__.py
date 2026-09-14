"""GCP-specific cross-service utilities.

Shared helpers used by GCP provider modules across services
(STS, Blob, PubSub, DocStore). This package depends on google-auth
and must not be imported by cloud-agnostic code (multicloud.common.*).
"""

from multicloud.gcp_common.gcp_credentials import DEFAULT_SCOPES, get_credentials

__all__ = ["DEFAULT_SCOPES", "get_credentials"]
