"""Cloud-agnostic STS client package.

Public entry point — :class:`StsClient`::

    from multicloud.sts.sts_client import StsClient

    with StsClient(provider="aws", region="us-west-2") as sts:
        identity = sts.get_caller_identity()
        creds = sts.assume_role(
            "arn:aws:iam::123:role/MyRole",
            session_name="dev",
        )

Provider classes (``AwsSts``, ``GcpSts``, ...) live in sibling
distributions (``multicloud-sts-aws``, ``-gcp``, ``-ali``) and are
discovered at runtime via setuptools entry points. They are hidden
behind the registry; users never reference them directly.
"""

from multicloud.sts.sts_client.client import StsClient, StsUtilities
from multicloud.sts.sts_client.driver import (
    STS_REGISTRY,
    AvailabilityCondition,
    BaseSts,
    CallerIdentity,
    CredentialScope,
    CredentialsOverrider,
    CredentialsType,
    ScopeRule,
    StsCredentials,
    StsValidator,
)
from multicloud.sts.sts_client.utilities import (
    STS_UTILITIES_REGISTRY,
    BaseStsUtilities,
    SignedAuthRequest,
)

__all__ = [
    "StsClient",
    "StsUtilities",
    "AvailabilityCondition",
    "CallerIdentity",
    "CredentialScope",
    "CredentialsOverrider",
    "CredentialsType",
    "ScopeRule",
    "SignedAuthRequest",
    "StsCredentials",
    # Internal: driver layer (re-exported for advanced/testing use; not
    # part of the user-facing surface)
    "BaseSts",
    "BaseStsUtilities",
    "STS_REGISTRY",
    "STS_UTILITIES_REGISTRY",
    "StsValidator",
]
