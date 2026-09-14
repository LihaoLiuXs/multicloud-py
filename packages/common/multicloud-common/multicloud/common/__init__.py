"""Cross-provider common abstractions (exceptions, provider registry).

This package is the foundation all services share. It provides the
exception hierarchy and the plugin-discovery mechanism that let
downstream service packages stay cloud-agnostic.
"""

from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
    StsCredentials,
)
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
    ResourceExistsException,
    ResourceNotFoundException,
    SubstrateSdkException,
    UnAuthorizedException,
    UnknownException,
    UnsupportedOperationException,
)
from multicloud.common.provider import Provider, ProviderRegistry
from multicloud.common.providers import get_available_providers
from multicloud.common.proxy import ProxyConfig

__all__ = [
    "CredentialsOverrider",
    "ProxyConfig",
    "CredentialsType",
    "FailedPreconditionException",
    "InvalidArgumentException",
    "Provider",
    "ProviderRegistry",
    "ResourceExistsException",
    "ResourceNotFoundException",
    "StsCredentials",
    "SubstrateSdkException",
    "UnAuthorizedException",
    "UnknownException",
    "UnsupportedOperationException",
    "get_available_providers",
]
