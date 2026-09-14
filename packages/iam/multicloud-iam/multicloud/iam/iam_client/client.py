"""User-facing :class:`IamClient` — the public entry point for IAM ops.

Three-layer architecture:

1. **Client** (this file) — what users import; provider-agnostic API.
2. **Driver** (:class:`BaseIam` in ``driver.py``) — internal abstract
   contract; defines validated public methods + provider hooks.
3. **Provider** (e.g. ``AwsIam``, ``GcpIam``) — concrete impls that
   callers never reference directly.

User code stays cloud-agnostic; only the ``provider`` kwarg names a cloud::

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
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

from multicloud.iam.iam_client.driver import (
    IAM_REGISTRY,
    BaseIam,
    CreateOptions,
    CredentialsOverrider,
    TrustConfiguration,
)
from multicloud.iam.iam_client.model import PolicyDocument


class IamClient:
    """Cloud-agnostic IAM identity management client.

    Args:
        provider: Provider id — ``"aws"`` or ``"gcp"``.
        region: Region (interpreted per provider). Optional.
        endpoint: Endpoint override URL. Optional.
        credentials_overrider: Override default credential resolution. Optional.
        properties: Provider-specific extras passed through to the
            driver constructor as keyword arguments.
    """

    def __init__(
        self,
        *,
        provider: str,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
        properties: Optional[Mapping[str, Any]] = None,
    ) -> None:
        iam_cls = IAM_REGISTRY.get(provider)
        kwargs: dict = {}
        if region is not None:
            kwargs["region"] = region
        if endpoint is not None:
            kwargs["endpoint_url"] = endpoint
        if credentials_overrider is not None:
            kwargs["credentials_overrider"] = credentials_overrider
        if properties:
            kwargs.update(properties)
        self._iam: BaseIam = iam_cls(**kwargs)

    @property
    def provider(self) -> str:
        return self._iam.get_provider_id()

    @property
    def region(self) -> Optional[str]:
        return self._iam.region

    # Identity operations
    def create_identity(
        self,
        identity_name: str,
        *,
        description: Optional[str] = None,
        tenant_id: str,
        region: Optional[str] = None,
        trust_config: Optional[TrustConfiguration] = None,
        options: Optional[CreateOptions] = None,
    ) -> str:
        """Create an identity, or return the existing one if it already exists.

        This operation is idempotent: if the identity already exists,
        providers return the existing identity ID rather than raising
        an exception.

        Args:
            identity_name: Name of the identity to create.
            description: Human-readable description.
            tenant_id: Account or project identifier that owns the identity.
            region: Region override (optional).
            trust_config: Trust relationship configuration (optional).
            options: Additional creation options (optional).

        Returns:
            Provider-specific identity identifier (e.g. ARN, email).
        """
        return self._iam.create_identity(
            identity_name,
            description=description,
            tenant_id=tenant_id,
            region=region,
            trust_config=trust_config,
            options=options,
        )

    def get_identity(
        self,
        identity_name: str,
        *,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> str:
        """Retrieve an identity's provider-specific identifier.

        Args:
            identity_name: Name of the identity to look up.
            tenant_id: Account or project identifier that owns the identity.
            region: Region override (optional).

        Returns:
            Provider-specific identity identifier (e.g. ARN, email).
        """
        return self._iam.get_identity(identity_name, tenant_id=tenant_id, region=region)

    def delete_identity(
        self,
        identity_name: str,
        *,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> None:
        """Delete an identity from the cloud provider.

        Args:
            identity_name: Name of the identity to delete.
            tenant_id: Account or project identifier that owns the identity.
            region: Region override (optional).
        """
        self._iam.delete_identity(identity_name, tenant_id=tenant_id, region=region)

    # Policy operations
    def attach_inline_policy(
        self,
        identity_name: str,
        *,
        policy_document: PolicyDocument,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> None:
        """Attach an inline policy to an identity."""
        self._iam.attach_inline_policy(
            identity_name,
            policy_document=policy_document,
            tenant_id=tenant_id,
            region=region,
        )

    def get_inline_policy_details(
        self,
        identity_name: str,
        *,
        policy_name: str,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> Optional[str]:
        """Retrieve an inline policy document as JSON string."""
        return self._iam.get_inline_policy_details(
            identity_name,
            policy_name=policy_name,
            tenant_id=tenant_id,
            region=region,
        )

    def get_attached_policies(
        self,
        identity_name: str,
        *,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> List[str]:
        """List names of policies attached to an identity."""
        return self._iam.get_attached_policies(
            identity_name, tenant_id=tenant_id, region=region
        )

    def remove_policy(
        self,
        identity_name: str,
        *,
        policy_name: str,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> None:
        """Remove a policy from an identity."""
        self._iam.remove_policy(
            identity_name,
            policy_name=policy_name,
            tenant_id=tenant_id,
            region=region,
        )

    # Lifecycle
    def close(self) -> None:
        """Release underlying resources."""
        self._iam.close()

    def __enter__(self) -> "IamClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
