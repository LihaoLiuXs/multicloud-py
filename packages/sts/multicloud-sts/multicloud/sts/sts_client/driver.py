"""Driver-level abstractions for the Security Token Service (STS).

Same ABC + template-method shape as
``multicloud.blob.blob_client.driver.BaseBlobStore``: providers
implement ``do_*`` hooks; the public ``get_caller_identity`` /
``assume_role`` / ``get_access_token`` methods on :class:`BaseSts`
validate input then delegate.

Users never reach this layer directly — they construct a
:class:`StsClient` (in ``client.py``), which holds a ``BaseSts``
instance internally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, Optional

from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
    StsCredentials,
)
from multicloud.common.exceptions import InvalidArgumentException
from multicloud.common.provider import Provider, ProviderRegistry

# Service-scoped registry. Provider distributions advertise themselves
# under the ``multicloud.sts.providers`` entry-point group; the registry
# imports those modules lazily on first use, at which point each provider's
# ``@STS_REGISTRY.register("aws")`` decorator stamps the class.
STS_REGISTRY = ProviderRegistry(
    service_name="sts",
    entry_point_group="multicloud.sts.providers",
)

# Default session identifiers applied in the driver layer so every provider
# receives a non-empty, uniform session name regardless of provider support.
_DEFAULT_SESSION_NAME = "multicloud-session"
_DEFAULT_WEB_IDENTITY_SESSION_NAME = "multicloud-web-identity"


@dataclass(frozen=True)
class AvailabilityCondition:
    """Condition that must be met for a ScopeRule to apply.

    Currently supports prefix-based resource restriction.
    """

    resource_prefix: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class ScopeRule:
    """A single resource + permissions entry within a CredentialScope.

    Attributes:
        available_resource: URI of the resource.
        available_permissions: Tuple of permission strings granted on the resource.
        availability_condition: Optional condition to further narrow access.
    """

    available_resource: str
    available_permissions: tuple[str, ...]
    availability_condition: Optional[AvailabilityCondition] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "available_permissions", tuple(self.available_permissions)
        )


@dataclass(frozen=True)
class CredentialScope:
    """Used with assume_role to downscope credentials.

    Empty rules means no restrictions applied.
    """

    rules: tuple[ScopeRule, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rules", tuple(self.rules))


# StsCredentials is defined in multicloud.common.credentials and re-exported
# here for backward compatibility.


@dataclass(frozen=True)
class CallerIdentity:
    """Cloud-agnostic caller identity.

    - ``user_id``: unique identifier of the calling entity
    - ``cloud_resource_name``: cloud resource identifier or identity proof
    - ``account_id``: cloud account/project that owns the identity
    """

    user_id: str = ""
    cloud_resource_name: str = ""
    account_id: str = ""


# CredentialsOverrider and CredentialsType are defined in
# multicloud.common.credentials and re-exported here for backward
# compatibility.


class StsValidator:
    """Input validator; raises :class:`InvalidArgumentException`."""

    def validate_assume_role(
        self,
        role: str,
        expiration: int,
        credential_scope: Optional[CredentialScope] = None,
    ) -> None:
        if not role:
            raise InvalidArgumentException("assume_role: role is required")
        if expiration <= 0:
            raise InvalidArgumentException("assume_role: expiration must be positive")
        if credential_scope is not None:
            for rule in credential_scope.rules:
                if not rule.available_permissions:
                    raise InvalidArgumentException(
                        "ScopeRule.available_permissions must not be empty"
                    )

    def validate_get_access_token(
        self,
        audience: Optional[str],
        scopes: Optional[list[str]],
    ) -> None:
        if audience is not None and scopes is not None:
            raise InvalidArgumentException(
                "get_access_token: audience and scopes are mutually exclusive"
            )

    def validate_generate_service_account_token(
        self, target_principal: str, scopes: list[str]
    ) -> None:
        if not target_principal:
            raise InvalidArgumentException(
                "generate_service_account_token: target_principal is required"
            )
        if not scopes:
            raise InvalidArgumentException(
                "generate_service_account_token: scopes must be non-empty"
            )

    def validate_assume_role_with_web_identity(
        self, role: str, web_identity_token: str
    ) -> None:
        # Note: expiration is intentionally not validated here. Providers
        # either ignore it or enforce their own bounds, surfacing an
        # out-of-range value as a provider error mapped to
        # InvalidArgumentException. A blanket ``> 0`` check would be arbitrary.
        if not role:
            raise InvalidArgumentException(
                "assume_role_with_web_identity: role is required"
            )
        if not web_identity_token:
            raise InvalidArgumentException(
                "assume_role_with_web_identity: web_identity_token is required"
            )


class BaseSts(Provider, ABC):
    """Base class for substrate-specific STS implementations.

    Providers extend this class and implement :meth:`do_get_caller_identity`,
    :meth:`do_assume_role`, :meth:`do_get_access_token`, and
    :meth:`do_generate_service_account_token`.
    All cross-cutting concerns (validation, default arg handling) happen in
    the public methods so providers don't have to repeat them.
    """

    def __init__(
        self,
        region: Optional[str] = None,
        *,
        endpoint_url: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
        validator: Optional[StsValidator] = None,
    ) -> None:
        self._region = region
        self._endpoint_url = endpoint_url
        self._credentials_overrider = credentials_overrider
        self._validator = validator or StsValidator()

    # properties
    @property
    def region(self) -> Optional[str]:
        return self._region

    # public API
    def get_caller_identity(self, *, aud: Optional[str] = None) -> CallerIdentity:
        """Return identity info for the current credentials.

        Args:
            aud: Target audience for the identity token. Some providers
                use this for audience-scoped token generation; others
                ignore it.
        """
        return self.do_get_caller_identity(aud=aud)

    def assume_role(
        self,
        role: str,
        *,
        session_name: str = "",
        expiration: int = 3600,
        external_id: Optional[str] = None,
        policy: Optional[str] = None,
        tags: Optional[Mapping[str, str]] = None,
        credential_scope: Optional[CredentialScope] = None,
    ) -> StsCredentials:
        """Assume a role and return temporary credentials.

        Args:
            role: Cloud resource name to assume.
            session_name: Session identifier.
            expiration: Duration in seconds (default 3600).
            external_id: External ID for cross-account access (optional).
            policy: Inline policy to scope down permissions (optional).
            tags: Session tags (optional).
            credential_scope: Optional scope to downscope credentials.

        Returns:
            StsCredentials with temporary access tokens.
        """
        self._validator.validate_assume_role(role, expiration, credential_scope)
        return self.do_assume_role(
            role=role,
            session_name=session_name or _DEFAULT_SESSION_NAME,
            expiration=expiration,
            external_id=external_id,
            policy=policy,
            tags=tags or {},
            credential_scope=credential_scope,
        )

    def get_access_token(
        self,
        *,
        audience: Optional[str] = None,
        scopes: Optional[list[str]] = None,
        duration_seconds: Optional[int] = None,
    ) -> StsCredentials:
        """Retrieve a fresh access token from the provider.

        Args:
            audience: Target audience for an audience-scoped token.
                Interpreted per provider; see the provider implementation
                for details. Mutually exclusive with ``scopes``.
            scopes: OAuth scopes for a scoped token. Interpreted per
                provider; see the provider implementation for details.
                Mutually exclusive with ``audience``.
            duration_seconds: Requested token duration. Interpreted per
                provider; some providers may ignore or cap this value
                (optional).

        Returns:
            StsCredentials with the token in ``security_token``.
        """
        self._validator.validate_get_access_token(audience, scopes)
        return self.do_get_access_token(
            audience=audience, scopes=scopes, duration_seconds=duration_seconds
        )

    def generate_service_account_token(
        self,
        target_principal: str,
        *,
        scopes: list[str],
        lifetime: int = 3600,
    ) -> StsCredentials:
        """Generate a token for a target service account via impersonation.

        Behaviour, required permissions, and supported lifetime range are
        provider-specific; see the provider ``do_generate_service_account_token``
        implementation for details.

        Args:
            target_principal: Service account to impersonate. Format is
                interpreted per provider; see the provider implementation
                for details.
            scopes: OAuth scopes to request on the generated token.
            lifetime: Token lifetime in seconds (default 3600). Supported
                range and behaviour are provider-specific; see the provider
                implementation for details.

        Returns:
            StsCredentials with the impersonated token in ``security_token``.
        """
        self._validator.validate_generate_service_account_token(
            target_principal, scopes
        )
        return self.do_generate_service_account_token(
            target_principal, scopes=scopes, lifetime=lifetime
        )

    def assume_role_with_web_identity(
        self,
        role: str,
        web_identity_token: str,
        *,
        session_name: str = "",
        expiration: int = 3600,
    ) -> StsCredentials:
        """Exchange a web identity (OIDC) token for temporary credentials.

        Federated sign-in: the caller presents an OIDC/JWT token minted by
        an external identity provider and receives short-lived cloud
        credentials in return — no long-lived cloud keys required.

        Args:
            role: Cloud resource name to assume, interpreted by each
                provider as its federated-identity resource identifier.
            web_identity_token: The OIDC/JWT token proving the caller's
                external identity.
            session_name: Session identifier (optional; provider may ignore).
            expiration: Requested duration in seconds (default 3600).
                Interpreted per provider; some providers may honor, cap,
                or ignore it.

        Returns:
            StsCredentials with temporary access tokens.
        """
        self._validator.validate_assume_role_with_web_identity(role, web_identity_token)
        return self.do_assume_role_with_web_identity(
            role=role,
            web_identity_token=web_identity_token,
            session_name=session_name or _DEFAULT_WEB_IDENTITY_SESSION_NAME,
            expiration=expiration,
        )

    # extension points (substrate-specific)
    @abstractmethod
    def do_get_caller_identity(self, *, aud: Optional[str] = None) -> CallerIdentity:
        """Provider-specific implementation of ``get_caller_identity``."""

    @abstractmethod
    def do_assume_role(
        self,
        role: str,
        *,
        session_name: str,
        expiration: int,
        external_id: Optional[str],
        policy: Optional[str],
        tags: Mapping[str, str],
        credential_scope: Optional[CredentialScope],
    ) -> StsCredentials:
        """Provider-specific implementation of ``assume_role``."""

    @abstractmethod
    def do_get_access_token(
        self,
        *,
        audience: Optional[str] = None,
        scopes: Optional[list[str]] = None,
        duration_seconds: Optional[int] = None,
    ) -> StsCredentials:
        """Provider-specific implementation of ``get_access_token``."""

    @abstractmethod
    def do_generate_service_account_token(
        self,
        target_principal: str,
        *,
        scopes: list[str],
        lifetime: int,
    ) -> StsCredentials:
        """Provider-specific implementation of ``generate_service_account_token``."""

    @abstractmethod
    def do_assume_role_with_web_identity(
        self,
        role: str,
        web_identity_token: str,
        *,
        session_name: str,
        expiration: int,
    ) -> StsCredentials:
        """Provider-specific implementation of ``assume_role_with_web_identity``."""

    # lifecycle
    def close(self) -> None:
        """Release underlying resources. Default is a no-op."""

    def __enter__(self) -> "BaseSts":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        self.close()
