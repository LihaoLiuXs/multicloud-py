"""Driver-level abstractions for IAM identity operations.

Same ABC + template-method shape as ``multicloud.blob.blob_client.driver``:
providers implement ``do_*`` hooks; the public methods on :class:`BaseIam`
validate input then delegate.

Users never reach this layer directly — they construct an
:class:`IamClient` (in ``client.py``), which holds a ``BaseIam``
instance internally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
)
from multicloud.common.exceptions import InvalidArgumentException
from multicloud.common.provider import Provider, ProviderRegistry
from multicloud.iam.iam_client.model import PolicyDocument

# Service-scoped registry. Provider distributions advertise themselves
# under the ``multicloud.iam.providers`` entry-point group; the registry
# imports those modules lazily on first use, at which point each provider's
# ``@IAM_REGISTRY.register("aws")`` decorator stamps the class.
IAM_REGISTRY = ProviderRegistry(
    service_name="iam",
    entry_point_group="multicloud.iam.providers",
)


@dataclass(frozen=True)
class TrustConfiguration:
    """Trust relationship configuration for identity creation.

    Principal identifiers are accepted in their provider-native format.
    """

    trusted_principals: List[str] = field(default_factory=list)
    conditions: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class CreateOptions:
    """Optional configuration for identity creation."""

    path: Optional[str] = None
    max_session_duration: Optional[int] = None
    permission_boundary: Optional[str] = None


@dataclass(frozen=True)
class CreateIdentityRequest:
    """Internal request object for create_identity — used between driver and provider."""

    identity_name: str
    description: Optional[str] = None
    tenant_id: str = ""
    region: Optional[str] = None
    trust_config: Optional[TrustConfiguration] = None
    options: Optional[CreateOptions] = None


@dataclass(frozen=True)
class GetIdentityRequest:
    """Internal request object for get_identity."""

    identity_name: str
    tenant_id: str = ""
    region: Optional[str] = None


@dataclass(frozen=True)
class DeleteIdentityRequest:
    """Internal request object for delete_identity."""

    identity_name: str
    tenant_id: str = ""
    region: Optional[str] = None


@dataclass(frozen=True)
class AttachInlinePolicyRequest:
    """Internal request object for attach_inline_policy."""

    identity_name: str
    policy_document: PolicyDocument
    tenant_id: str = ""
    region: Optional[str] = None


@dataclass(frozen=True)
class GetInlinePolicyDetailsRequest:
    """Internal request object for get_inline_policy_details."""

    identity_name: str
    policy_name: str = ""
    tenant_id: str = ""
    region: Optional[str] = None


@dataclass(frozen=True)
class GetAttachedPoliciesRequest:
    """Internal request object for get_attached_policies."""

    identity_name: str
    tenant_id: str = ""
    region: Optional[str] = None


@dataclass(frozen=True)
class RemovePolicyRequest:
    """Internal request object for remove_policy."""

    identity_name: str
    policy_name: str = ""
    tenant_id: str = ""
    region: Optional[str] = None


class IamValidator:
    """Input validator; raises :class:`InvalidArgumentException`."""

    def validate_create_identity(
        self,
        identity_name: str,
        tenant_id: str,
    ) -> None:
        if not identity_name or not identity_name.strip():
            raise InvalidArgumentException("identity_name is required")
        if not tenant_id or not tenant_id.strip():
            raise InvalidArgumentException("tenant_id is required")

    def validate_get_identity(self, identity_name: str, tenant_id: str) -> None:
        if not identity_name or not identity_name.strip():
            raise InvalidArgumentException("identity_name is required")
        if not tenant_id or not tenant_id.strip():
            raise InvalidArgumentException("tenant_id is required")

    def validate_delete_identity(self, identity_name: str, tenant_id: str) -> None:
        if not identity_name or not identity_name.strip():
            raise InvalidArgumentException("identity_name is required")
        if not tenant_id or not tenant_id.strip():
            raise InvalidArgumentException("tenant_id is required")

    def validate_attach_inline_policy(
        self,
        identity_name: str,
        policy_document: Optional["PolicyDocument"],
        tenant_id: str,
    ) -> None:
        if not identity_name or not identity_name.strip():
            raise InvalidArgumentException("identity_name is required")
        if policy_document is None:
            raise InvalidArgumentException("policy_document is required")
        if not policy_document.name or not policy_document.name.strip():
            raise InvalidArgumentException("policy_document.name is required")
        if not policy_document.statements:
            raise InvalidArgumentException(
                "policy_document must have at least one statement"
            )
        if not tenant_id or not tenant_id.strip():
            raise InvalidArgumentException("tenant_id is required")

    def validate_get_inline_policy_details(
        self, identity_name: str, policy_name: str, tenant_id: str
    ) -> None:
        if not identity_name or not identity_name.strip():
            raise InvalidArgumentException("identity_name is required")
        if not policy_name or not policy_name.strip():
            raise InvalidArgumentException("policy_name is required")
        if not tenant_id or not tenant_id.strip():
            raise InvalidArgumentException("tenant_id is required")

    def validate_get_attached_policies(
        self, identity_name: str, tenant_id: str
    ) -> None:
        if not identity_name or not identity_name.strip():
            raise InvalidArgumentException("identity_name is required")
        if not tenant_id or not tenant_id.strip():
            raise InvalidArgumentException("tenant_id is required")

    def validate_remove_policy(
        self, identity_name: str, policy_name: str, tenant_id: str
    ) -> None:
        if not identity_name or not identity_name.strip():
            raise InvalidArgumentException("identity_name is required")
        if not policy_name or not policy_name.strip():
            raise InvalidArgumentException("policy_name is required")
        if not tenant_id or not tenant_id.strip():
            raise InvalidArgumentException("tenant_id is required")


class BaseIam(Provider, ABC):
    """Base class for substrate-specific IAM implementations.

    Providers extend this class and implement ``do_*`` abstract methods
    plus ``translate_error``. Validation and request construction happen
    in the public methods.
    """

    def __init__(
        self,
        region: Optional[str] = None,
        *,
        credentials_overrider: Optional[CredentialsOverrider] = None,
        validator: Optional[IamValidator] = None,
    ) -> None:
        self._region = region
        self._credentials_overrider = credentials_overrider
        self._validator = validator or IamValidator()

    @property
    def region(self) -> Optional[str]:
        return self._region

    # Create Identity
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

        This operation has idempotent/upsert semantics: if the identity
        already exists, providers return the existing identity ID rather
        than raising ResourceExistsException.
        """
        self._validator.validate_create_identity(identity_name, tenant_id)
        request = CreateIdentityRequest(
            identity_name=identity_name,
            description=description,
            tenant_id=tenant_id,
            region=region,
            trust_config=trust_config,
            options=options,
        )
        return self.do_create_identity(request)

    # Get Identity
    def get_identity(
        self,
        identity_name: str,
        *,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> str:
        """Validate, build request, then delegate to :meth:`do_get_identity`."""
        self._validator.validate_get_identity(identity_name, tenant_id)
        request = GetIdentityRequest(
            identity_name=identity_name,
            tenant_id=tenant_id,
            region=region,
        )
        return self.do_get_identity(request)

    # Delete Identity
    def delete_identity(
        self,
        identity_name: str,
        *,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> None:
        """Validate, build request, then delegate to :meth:`do_delete_identity`."""
        self._validator.validate_delete_identity(identity_name, tenant_id)
        request = DeleteIdentityRequest(
            identity_name=identity_name,
            tenant_id=tenant_id,
            region=region,
        )
        self.do_delete_identity(request)

    # Attach Inline Policy
    def attach_inline_policy(
        self,
        identity_name: str,
        *,
        policy_document: PolicyDocument,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> None:
        """Attach an inline policy to an identity."""
        self._validator.validate_attach_inline_policy(
            identity_name, policy_document, tenant_id
        )
        request = AttachInlinePolicyRequest(
            identity_name=identity_name,
            policy_document=policy_document,
            tenant_id=tenant_id,
            region=region,
        )
        self.do_attach_inline_policy(request)

    # Get Inline Policy Details
    def get_inline_policy_details(
        self,
        identity_name: str,
        *,
        policy_name: str,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> Optional[str]:
        """Retrieve an inline policy document as JSON string."""
        self._validator.validate_get_inline_policy_details(
            identity_name, policy_name, tenant_id
        )
        request = GetInlinePolicyDetailsRequest(
            identity_name=identity_name,
            policy_name=policy_name,
            tenant_id=tenant_id,
            region=region,
        )
        return self.do_get_inline_policy_details(request)

    # Get Attached Policies
    def get_attached_policies(
        self,
        identity_name: str,
        *,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> List[str]:
        """List names of policies attached to an identity."""
        self._validator.validate_get_attached_policies(identity_name, tenant_id)
        request = GetAttachedPoliciesRequest(
            identity_name=identity_name,
            tenant_id=tenant_id,
            region=region,
        )
        return self.do_get_attached_policies(request)

    # Remove Policy
    def remove_policy(
        self,
        identity_name: str,
        *,
        policy_name: str,
        tenant_id: str,
        region: Optional[str] = None,
    ) -> None:
        """Remove a policy from an identity."""
        self._validator.validate_remove_policy(identity_name, policy_name, tenant_id)
        request = RemovePolicyRequest(
            identity_name=identity_name,
            policy_name=policy_name,
            tenant_id=tenant_id,
            region=region,
        )
        self.do_remove_policy(request)

    # Abstract hooks (providers must implement)
    @abstractmethod
    def do_create_identity(self, request: CreateIdentityRequest) -> str:
        """Provider-specific identity creation."""

    @abstractmethod
    def do_get_identity(self, request: GetIdentityRequest) -> str:
        """Provider-specific identity retrieval."""

    @abstractmethod
    def do_delete_identity(self, request: DeleteIdentityRequest) -> None:
        """Provider-specific identity deletion."""

    @abstractmethod
    def do_attach_inline_policy(self, request: AttachInlinePolicyRequest) -> None:
        """Provider-specific policy attachment."""

    @abstractmethod
    def do_get_inline_policy_details(
        self, request: GetInlinePolicyDetailsRequest
    ) -> Optional[str]:
        """Provider-specific policy retrieval."""

    @abstractmethod
    def do_get_attached_policies(
        self, request: GetAttachedPoliciesRequest
    ) -> List[str]:
        """Provider-specific policy listing."""

    @abstractmethod
    def do_remove_policy(self, request: RemovePolicyRequest) -> None:
        """Provider-specific policy removal."""

    # Lifecycle
    def close(self) -> None:
        """Release underlying resources. Default is a no-op."""

    def __enter__(self) -> "BaseIam":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
