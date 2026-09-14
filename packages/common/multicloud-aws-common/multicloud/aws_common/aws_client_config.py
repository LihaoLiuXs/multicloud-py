"""Shared botocore client configuration for AWS S3 providers.

Isolates the S3 client settings that must be pinned explicitly rather than
inherited from AWS SDK defaults.
"""

from __future__ import annotations

from typing import Any, Optional

from multicloud.common.proxy import ProxyConfig

try:
    from botocore.config import Config  # type: ignore[import-not-found]
except ImportError as _exc:
    raise ImportError(
        "AWS dependencies not found. Install with: "
        "pip install 'python-multicloud-sdk[aws]'"
    ) from _exc

# Signature version for S3 clients.
#
# The AWS SDK still defaults *presigned URL* generation to the legacy SigV2
# algorithm in every region whose published endpoint metadata lists SigV2 as
# supported (us-west-2, us-east-1, eu-west-1, the GovCloud regions, and
# others). The downgrade is applied by botocore's ``choose-signer.s3`` event
# handler, not by client config.
#
# SigV2 is deprecated by AWS and is rejected outright by buckets with KMS
# default encryption: such buckets answer a SigV2 presigned request with
# SignatureDoesNotMatch or InvalidArgument. Pinning "s3v4" makes presigned
# URLs SigV4 in every region.
#
# This does not change ordinary (non-presigned) requests -- those already
# resolve to SigV4 by default, so their signed request line and
# Authorization header are unaffected.
#
# Deliberately absent: ``s3={"addressing_style": "virtual"}``. The default
# global endpoint already accepts a correctly-scoped SigV4 signature, so
# forcing virtual addressing fixes nothing while breaking custom endpoints
# (MinIO/LocalStack become ``bucket.localhost:9000``) and pinning the host to
# the configured region, which turns a region mismatch into a hard HTTP 301.
_S3_SIGNATURE_VERSION = "s3v4"


def s3_client_config(
    proxy_config: Optional[ProxyConfig] = None, **overrides: Any
) -> Config:
    """Build the botocore ``Config`` for an S3 client.

    The signature version is always pinned to SigV4. A ``signature_version``
    supplied in ``overrides`` is ignored -- callers cannot silently
    reintroduce the deprecated SigV2 presigned-URL behavior this helper exists
    to prevent.

    Args:
        proxy_config: How to resolve the proxy. When omitted, botocore reads
            the environment as usual.
        **overrides: Additional botocore ``Config`` keyword arguments. Any
            ``signature_version`` entry is superseded by SigV4.

    Returns:
        A ``Config`` pinning the S3 signature version to SigV4.
    """
    settings = dict(overrides)
    proxies = _proxies_for(proxy_config)
    if proxies is not None:
        settings["proxies"] = proxies
    return Config(**{**settings, "signature_version": _S3_SIGNATURE_VERSION})


def _proxies_for(proxy_config: Optional[ProxyConfig]) -> Optional[dict[str, str]]:
    """Translate a ProxyConfig into botocore's ``proxies`` setting.

    None leaves it unset, which is what makes botocore read the environment;
    an empty mapping is how a direct connection is requested.
    """
    if proxy_config is None:
        return None
    if proxy_config.endpoint is not None:
        return {"http": proxy_config.endpoint, "https": proxy_config.endpoint}
    if not proxy_config.use_environment_variable_proxy_values:
        return {}
    return None
