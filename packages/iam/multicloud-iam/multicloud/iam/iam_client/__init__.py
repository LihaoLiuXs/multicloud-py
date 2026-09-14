"""Cloud-agnostic IAM client package.

Public entry point — :class:`IamClient`::

    from multicloud.iam.iam_client import (
        IamClient,
        TrustConfiguration,
        CreateOptions,
    )

    with IamClient(provider="aws", region="us-west-2") as client:
        identity_id = client.create_identity(
            "MyRole",
            tenant_id="123456789012",
            trust_config=TrustConfiguration(),
            options=CreateOptions(),
        )

Provider classes (``AwsIam``, ``GcpIam``) live in sibling distributions
(``multicloud-iam-aws``, ``multicloud-iam-gcp``) and are discovered at
runtime via setuptools entry points. They are hidden behind the
registry; users never reference them directly.
"""

from multicloud.iam.iam_client.client import IamClient
from multicloud.iam.iam_client.driver import (
    IAM_REGISTRY,
    AttachInlinePolicyRequest,
    BaseIam,
    CreateIdentityRequest,
    CreateOptions,
    CredentialsOverrider,
    CredentialsType,
    DeleteIdentityRequest,
    GetAttachedPoliciesRequest,
    GetIdentityRequest,
    GetInlinePolicyDetailsRequest,
    IamValidator,
    RemovePolicyRequest,
    TrustConfiguration,
)
from multicloud.iam.iam_client.model import (
    Action,
    ComputeActions,
    ConditionOperator,
    Effect,
    IamActions,
    PolicyDocument,
    Statement,
    StorageActions,
)

__all__ = [
    "IamClient",
    "TrustConfiguration",
    "CreateOptions",
    "CredentialsOverrider",
    "CredentialsType",
    # Policy model types
    "Action",
    "ComputeActions",
    "ConditionOperator",
    "Effect",
    "IamActions",
    "PolicyDocument",
    "Statement",
    "StorageActions",
    # Policy request types
    "AttachInlinePolicyRequest",
    "GetAttachedPoliciesRequest",
    "GetInlinePolicyDetailsRequest",
    "RemovePolicyRequest",
    # Internal: driver layer (re-exported for advanced/testing use; not
    # part of the user-facing surface)
    "BaseIam",
    "IAM_REGISTRY",
    "IamValidator",
    "CreateIdentityRequest",
    "GetIdentityRequest",
    "DeleteIdentityRequest",
]
