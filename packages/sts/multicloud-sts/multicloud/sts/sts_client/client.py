"""User-facing :class:`StsClient` — the public entry point for STS ops.

Three-layer architecture:

1. **Client** (this file) — what users import; provider-agnostic API.
2. **Driver** (:class:`BaseSts` in ``driver.py``) — internal abstract
   contract; defines public methods + provider hooks.
3. **Provider** — concrete impls callers never reference directly.

User code stays cloud-agnostic; only the ``provider`` kwarg selects the
registered implementation::

    from multicloud.sts.sts_client import StsClient

    with StsClient(provider="<provider-id>", region="<region>") as sts:
        identity = sts.get_caller_identity()
        creds = sts.assume_role(
            "<role-resource-identifier>",
            session_name="dev",
        )
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from multicloud.sts.sts_client.driver import (
    STS_REGISTRY,
    BaseSts,
    CallerIdentity,
    CredentialScope,
    CredentialsOverrider,
    StsCredentials,
)
from multicloud.sts.sts_client.utilities import (
    STS_UTILITIES_REGISTRY,
    BaseStsUtilities,
    SignedAuthRequest,
)


class StsClient:
    """Cloud-agnostic Security Token Service client.

    Args:
        provider: Registered provider id used to select the implementation.
        region: Region (interpreted per provider). Optional; falls back
            to provider defaults.
        endpoint: Endpoint override URL. Optional.
        credentials_overrider: Override default credential resolution.
            Optional.
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
        sts_cls = STS_REGISTRY.get(provider)
        kwargs: dict = {}
        if region is not None:
            kwargs["region"] = region
        if endpoint is not None:
            kwargs["endpoint_url"] = endpoint
        if credentials_overrider is not None:
            kwargs["credentials_overrider"] = credentials_overrider
        if properties:
            kwargs.update(properties)
        self._sts: BaseSts = sts_cls(**kwargs)

    # properties

    @property
    def provider(self) -> str:
        return self._sts.get_provider_id()

    @property
    def region(self) -> Optional[str]:
        return self._sts.region

    # operations (forwarding to the driver)

    def get_caller_identity(self, *, aud: Optional[str] = None) -> CallerIdentity:
        """Return identity info for the current credentials.

        Args:
            aud: Target audience for the identity token. Some providers
                use this for audience-scoped token generation; others
                ignore it.
        """
        try:
            return self._sts.get_caller_identity(aud=aud)
        except Exception as exc:
            raise self._sts.translate_error(exc) from exc

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
        """
        try:
            return self._sts.assume_role(
                role,
                session_name=session_name,
                expiration=expiration,
                external_id=external_id,
                policy=policy,
                tags=tags,
                credential_scope=credential_scope,
            )
        except Exception as exc:
            raise self._sts.translate_error(exc) from exc

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
        """
        try:
            return self._sts.get_access_token(
                audience=audience, scopes=scopes, duration_seconds=duration_seconds
            )
        except Exception as exc:
            raise self._sts.translate_error(exc) from exc

    def generate_service_account_token(
        self,
        target_principal: str,
        *,
        scopes: list[str],
        lifetime: int = 3600,
    ) -> StsCredentials:
        """Generate a token for a target service account via impersonation.

        Args:
            target_principal: Service account to impersonate. Format is
                interpreted per provider; see the provider implementation
                for details.
            scopes: OAuth scopes to request on the generated token.
            lifetime: Token lifetime in seconds (default 3600). Supported
                range and behaviour are provider-specific; see the provider
                implementation for details.
        """
        try:
            return self._sts.generate_service_account_token(
                target_principal, scopes=scopes, lifetime=lifetime
            )
        except Exception as exc:
            raise self._sts.translate_error(exc) from exc

    def assume_role_with_web_identity(
        self,
        role: str,
        web_identity_token: str,
        *,
        session_name: str = "",
        expiration: int = 3600,
    ) -> StsCredentials:
        """Exchange a web identity (OIDC) token for temporary credentials.

        Args:
            role: Cloud resource identifier to assume (interpretation is
                provider-specific).
            web_identity_token: The OIDC/JWT token proving external identity.
            session_name: Session identifier (optional; provider support
                varies).
            expiration: Requested duration in seconds (default 3600).
                Provider support varies; some providers may ignore this
                parameter.
        """
        try:
            return self._sts.assume_role_with_web_identity(
                role,
                web_identity_token,
                session_name=session_name,
                expiration=expiration,
            )
        except Exception as exc:
            raise self._sts.translate_error(exc) from exc

    # lifecycle

    def close(self) -> None:
        """Release underlying resources."""
        self._sts.close()

    def __enter__(self) -> "StsClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class StsUtilities:
    """Cloud-agnostic STS utility functions (offline request signing).

    Companion to :class:`StsClient` for utilities that sign a request
    locally instead of calling the provider. The signed request is handed
    to another system that replays it to verify the caller's identity.

    Args:
        provider: Registered provider id used to select the implementation.
        region: Region (interpreted per provider). Optional for some
            providers; required by others for request signing.
        credentials_overrider: Override default credential resolution.
            Optional.
        properties: Provider-specific extras passed through to the driver
            constructor as keyword arguments.
    """

    def __init__(
        self,
        *,
        provider: str,
        region: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
        properties: Optional[Mapping[str, Any]] = None,
    ) -> None:
        utils_cls = STS_UTILITIES_REGISTRY.get(provider)
        kwargs: dict = {}
        if region is not None:
            kwargs["region"] = region
        if credentials_overrider is not None:
            kwargs["credentials_overrider"] = credentials_overrider
        if properties:
            kwargs.update(properties)
        self._utils: BaseStsUtilities = utils_cls(**kwargs)

    # properties

    @property
    def provider(self) -> str:
        return self._utils.get_provider_id()

    @property
    def region(self) -> Optional[str]:
        return self._utils.region

    # operations (forwarding to the driver)

    def create_signed_auth_request(
        self,
        *,
        custom_headers: Optional[Mapping[str, str]] = None,
    ) -> SignedAuthRequest:
        """Produce a signed identity request for cloud-native auth.

        Args:
            custom_headers: Extra headers to fold into the signature so the
                system replaying the request can bind them to the identity.
        """
        try:
            return self._utils.create_signed_auth_request(custom_headers=custom_headers)
        except Exception as exc:
            raise self._utils.translate_error(exc) from exc

    # lifecycle

    def close(self) -> None:
        self._utils.close()

    def __enter__(self) -> "StsUtilities":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
