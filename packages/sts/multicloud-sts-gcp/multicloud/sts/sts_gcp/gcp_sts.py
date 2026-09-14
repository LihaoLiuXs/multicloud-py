"""Google Cloud Platform implementation of :class:`BaseSts`.

Translates GCP IAM / auth exceptions into the cloud-agnostic exception
hierarchy.

GCP Concept Mapping:
    - ``get_caller_identity`` → generates an OIDC ID token (JWT) and
      returns it as ``cloud_resource_name``.
    - ``get_access_token`` → refreshes the OAuth2 token and returns it
      as ``security_token``; with ``audience`` generates an audience-scoped
      ID token; with ``scopes`` generates a scoped access token for the
      caller's own service account.
    - ``assume_role`` → service account impersonation via
      ``ImpersonatedCredentials``.
    - ``generate_service_account_token`` → IAM Credentials
      ``generateAccessToken`` for a target service account.

GCP does not use access-key/secret-key pairs. All credential fields
except ``security_token`` are empty strings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import google.auth  # type: ignore[import-not-found]
import google.auth.downscoped  # type: ignore[import-not-found]
import google.auth.exceptions  # type: ignore[import-not-found]
import google.auth.impersonated_credentials  # type: ignore[import-not-found]
import google.auth.transport.requests  # type: ignore[import-not-found]
import google.oauth2.sts  # type: ignore[import-not-found]
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
    UnAuthorizedException,
    UnknownException,
    UnsupportedOperationException,
)
from multicloud.gcp_common.gcp_credentials import get_credentials
from multicloud.sts.sts_client.driver import (
    STS_REGISTRY,
    BaseSts,
    CallerIdentity,
    CredentialScope,
    CredentialsOverrider,
    StsCredentials,
)

_RESOURCE_SCHEME = "storage://"
_PERMISSION_PREFIX = "storage:"

# Default scopes required for IAM impersonation and general GCP API access.
_DEFAULT_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Google STS endpoint for the RFC 8693 token-exchange grant used by
# Workload Identity Federation.
_STS_TOKEN_ENDPOINT = "https://sts.googleapis.com/v1/token"

# RFC 8693 token-exchange grant + token type URNs.
_TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_REQUESTED_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
_SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"


@STS_REGISTRY.register("gcp")
class GcpSts(BaseSts):
    """GCP IAM-backed security token service."""

    def __init__(
        self,
        region: Optional[str] = None,
        *,
        endpoint_url: Optional[str] = None,
        credentials: Any = None,
        project: Optional[str] = None,
        scopes: Optional[list[str]] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
    ) -> None:
        super().__init__(
            region=region,
            endpoint_url=endpoint_url,
            credentials_overrider=credentials_overrider,
        )
        self._scopes = scopes or _DEFAULT_SCOPES

        if credentials is not None:
            # Caller-supplied credentials take precedence over credentials_overrider.
            self._credentials = credentials
            self._project = project
        elif credentials_overrider is not None:
            resolved = get_credentials(credentials_overrider, scopes=self._scopes)
            if resolved is not None:
                self._credentials = resolved
                self._project = project
            else:
                try:
                    self._credentials, detected_project = google.auth.default(
                        scopes=self._scopes,
                    )
                except google.auth.exceptions.DefaultCredentialsError as exc:
                    raise UnAuthorizedException(
                        "Could not find default credentials"
                    ) from exc
                self._project = project or detected_project
        else:
            try:
                self._credentials, detected_project = google.auth.default(
                    scopes=self._scopes,
                )
            except google.auth.exceptions.DefaultCredentialsError as exc:
                raise UnAuthorizedException(
                    "Could not find default credentials"
                ) from exc
            self._project = project or detected_project

    @property
    def project(self) -> Optional[str]:
        """Return the GCP project ID."""
        return self._project

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate GCP auth/IAM exceptions to common hierarchy."""
        if isinstance(exc, SubstrateSdkException):
            return exc
        if isinstance(exc, google.auth.exceptions.DefaultCredentialsError):
            return UnAuthorizedException(str(exc))
        if isinstance(exc, google.auth.exceptions.RefreshError):
            return UnAuthorizedException(str(exc))
        if isinstance(exc, google.auth.exceptions.OAuthError):
            return UnAuthorizedException(str(exc))
        if isinstance(exc, google.auth.exceptions.TransportError):
            return UnknownException(str(exc))
        if isinstance(exc, (ValueError, TypeError)):
            return InvalidArgumentException(str(exc))
        return UnknownException(str(exc))

    # STS operations

    def do_get_caller_identity(self, *, aud: Optional[str] = None) -> CallerIdentity:
        """Return identity as an OIDC ID token JWT.

        Generates an ID token (JWT signed by Google) and returns it as
        ``cloud_resource_name``. The JWT contains claims (email, sub, aud)
        that prove the caller's identity to a third party.

        The ``user_id`` and ``account_id`` fields are intentionally empty —
        the identity information is encoded in the JWT itself. Callers
        should decode the JWT to extract email, sub, and audience claims.
        """
        self._ensure_valid_credentials()

        audience = aud or "multicloud-python-sdk"

        try:
            id_creds = google.auth.impersonated_credentials.IDTokenCredentials(
                target_credentials=self._credentials,
                target_audience=audience,
                include_email=True,
            )
            auth_request = google.auth.transport.requests.Request()
            id_creds.refresh(auth_request)
        except (AttributeError, TypeError) as exc:
            raise UnsupportedOperationException(
                "get_caller_identity requires service account or impersonated "
                "credentials. User OAuth credentials (gcloud auth) do not "
                "support ID token generation."
            ) from exc

        return CallerIdentity(
            user_id="",
            cloud_resource_name=id_creds.token,
            account_id="",
        )

    def do_get_access_token(
        self,
        *,
        audience: Optional[str] = None,
        scopes: Optional[list[str]] = None,
        duration_seconds: Optional[int] = None,
    ) -> StsCredentials:
        """Get an access token with optional audience or scope targeting.

        Three paths depending on which parameters are supplied:

        - **audience**: Calls ``generateIdToken`` (via
          ``IDTokenCredentials``) to produce an audience-scoped JWT.
          Requires service account or impersonated credentials.
          ``duration_seconds`` is not applicable; token lifetime is fixed
          by Google (~1h).
        - **scopes**: Calls ``generateAccessToken`` (via
          ``ImpersonatedCredentials`` targeting the caller's own SA email)
          to produce a scoped OAuth access token. ``duration_seconds`` sets
          the token lifetime (default 3600s).
        - **neither**: Plain OAuth2 credential refresh; returns the
          current access token. ``duration_seconds`` is accepted for
          interface compatibility but ignored — GCP OAuth tokens have a
          fixed ~1h lifetime that cannot be customised via refresh.
        """
        self._ensure_valid_credentials()

        if audience is not None:
            return self._get_id_token(audience)
        if scopes is not None:
            return self._get_scoped_access_token(scopes, duration_seconds)
        return self._get_refreshed_access_token()

    def _get_id_token(self, audience: str) -> StsCredentials:
        """Generate an audience-scoped ID token via generateIdToken."""
        try:
            id_creds = google.auth.impersonated_credentials.IDTokenCredentials(
                target_credentials=self._credentials,
                target_audience=audience,
                include_email=True,
            )
            auth_request = google.auth.transport.requests.Request()
            id_creds.refresh(auth_request)
        except (AttributeError, TypeError) as exc:
            raise UnsupportedOperationException(
                "get_access_token with audience requires service account or "
                "impersonated credentials. User OAuth credentials do not "
                "support ID token generation."
            ) from exc
        return StsCredentials(
            access_key_id="",
            access_key_secret="",
            security_token=id_creds.token or "",
            expiration=id_creds.expiry,
        )

    def _get_scoped_access_token(
        self, scopes: list[str], duration_seconds: Optional[int]
    ) -> StsCredentials:
        """Generate a scoped OAuth access token via generateAccessToken."""
        sa_email = getattr(self._credentials, "service_account_email", None)
        if not sa_email:
            raise UnsupportedOperationException(
                "get_access_token with scopes requires service account "
                "credentials (service_account_email not available on the "
                "current credential type)."
            )
        impersonated = google.auth.impersonated_credentials.Credentials(
            source_credentials=self._credentials,
            target_principal=sa_email,
            target_scopes=scopes,
            lifetime=duration_seconds or 3600,
        )
        auth_request = google.auth.transport.requests.Request()
        impersonated.refresh(auth_request)
        return StsCredentials(
            access_key_id="",
            access_key_secret="",
            security_token=impersonated.token or "",
            expiration=impersonated.expiry,
        )

    def _get_refreshed_access_token(self) -> StsCredentials:
        """Return the current OAuth2 access token after a plain refresh."""
        # Plain OAuth2 refresh — duration_seconds not applicable for this path.
        if not self._credentials.token:
            raise UnAuthorizedException(
                "Credentials refreshed but no access token available"
            )
        return StsCredentials(
            access_key_id="",
            access_key_secret="",
            security_token=self._credentials.token,
            expiration=self._credentials.expiry,
        )

    def do_assume_role(
        self,
        role: str,
        *,
        session_name: str,
        expiration: int,
        external_id: Optional[str],
        policy: Optional[str],
        tags: Mapping[str, str],
        credential_scope: Optional[CredentialScope] = None,
    ) -> StsCredentials:
        """Impersonate a GCP service account.

        Uses ``ImpersonatedCredentials`` with the target service
        account email to generate a short-lived access token.
        If ``credential_scope`` is provided, applies downscoping via
        ``DownscopedCredentials``.

        GCP-specific mapping:
            - ``role`` → target service account email to impersonate
            - ``expiration`` → token lifetime in seconds
            - ``session_name``, ``external_id``, ``policy``, ``tags``
              are not applicable for GCP and are ignored.
        """
        self._ensure_valid_credentials()

        impersonated = google.auth.impersonated_credentials.Credentials(
            source_credentials=self._credentials,
            target_principal=role,
            target_scopes=self._scopes,
            lifetime=expiration,
        )
        auth_request = google.auth.transport.requests.Request()

        if credential_scope is not None:
            boundary = self._convert_scope_to_boundary(credential_scope)

            downscoped_creds = google.auth.downscoped.Credentials(
                source_credentials=impersonated,
                credential_access_boundary=boundary,
            )
            downscoped_creds.refresh(auth_request)
            return StsCredentials(
                access_key_id="",
                access_key_secret="",
                security_token=downscoped_creds.token or "",
                expiration=downscoped_creds.expiry,
            )

        impersonated.refresh(auth_request)
        return StsCredentials(
            access_key_id="",
            access_key_secret="",
            security_token=impersonated.token or "",
            expiration=impersonated.expiry,
        )

    @staticmethod
    def _convert_scope_to_boundary(scope: CredentialScope) -> Any:
        """Convert CredentialScope to GCP CredentialAccessBoundary."""
        rules = []
        for rule in scope.rules:
            resource = rule.available_resource
            if resource.startswith(_RESOURCE_SCHEME):
                bucket = resource[len(_RESOURCE_SCHEME) :]
                resource = f"//storage.googleapis.com/projects/_/buckets/{bucket}"

            permissions = [
                _convert_permission_to_gcp(p) for p in rule.available_permissions
            ]

            condition = None
            if (
                rule.availability_condition
                and rule.availability_condition.resource_prefix
            ):
                expression = _build_gcp_prefix_expression(
                    rule.availability_condition.resource_prefix
                )
                condition = google.auth.downscoped.AvailabilityCondition(
                    expression,
                    title=rule.availability_condition.title,
                    description=rule.availability_condition.description,
                )

            boundary_rule = google.auth.downscoped.AccessBoundaryRule(
                available_resource=resource,
                available_permissions=permissions,
                availability_condition=condition,
            )
            rules.append(boundary_rule)

        return google.auth.downscoped.CredentialAccessBoundary(rules=rules)

    def do_generate_service_account_token(
        self,
        target_principal: str,
        *,
        scopes: list[str],
        lifetime: int,
    ) -> StsCredentials:
        """Generate an access token for a target service account.

        Calls IAM Credentials ``generateAccessToken`` to produce a
        short-lived OAuth access token for ``target_principal``. The
        caller must have ``roles/iam.serviceAccountTokenCreator`` on the
        target service account.

        Unlike :meth:`do_get_access_token` with ``scopes`` (which targets
        the caller's own SA), this method explicitly impersonates a
        *different* principal. ``lifetime`` may exceed 3600s if the target
        SA's max session duration is configured for it (up to 43200s / 12h).
        """
        self._ensure_valid_credentials()

        impersonated = google.auth.impersonated_credentials.Credentials(
            source_credentials=self._credentials,
            target_principal=target_principal,
            target_scopes=scopes,
            lifetime=lifetime,
        )
        auth_request = google.auth.transport.requests.Request()
        impersonated.refresh(auth_request)
        return StsCredentials(
            access_key_id="",
            access_key_secret="",
            security_token=impersonated.token or "",
            expiration=impersonated.expiry,
        )

    def do_assume_role_with_web_identity(
        self,
        role: str,
        web_identity_token: str,
        *,
        session_name: str,
        expiration: int,
    ) -> StsCredentials:
        """Exchange an external OIDC token for an access token.

        Implements the RFC 8693 token exchange via the ``google.oauth2.sts``
        client. ``role`` carries the federation provider/audience resource
        name; the resulting federated access token is returned in
        ``security_token``.

        ``session_name`` and ``expiration`` are not used by the token
        exchange and are accepted only for cross-provider interface parity.
        Native token-exchange errors propagate to the client layer, which
        routes them through :meth:`translate_error`.
        """
        endpoint = self._endpoint_url or _STS_TOKEN_ENDPOINT
        sts_client = google.oauth2.sts.Client(token_exchange_endpoint=endpoint)
        auth_request = google.auth.transport.requests.Request()

        response = sts_client.exchange_token(
            request=auth_request,
            grant_type=_TOKEN_EXCHANGE_GRANT_TYPE,
            subject_token=web_identity_token,
            subject_token_type=_SUBJECT_TOKEN_TYPE,
            audience=role,
            scopes=self._scopes,
            requested_token_type=_REQUESTED_TOKEN_TYPE,
        )

        access_token = response.get("access_token")
        if not access_token:
            raise UnAuthorizedException("Token exchange returned no access_token")

        expiry: Optional[datetime] = None
        expires_in = response.get("expires_in")
        if expires_in is not None:
            expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
        return StsCredentials(
            access_key_id="",
            access_key_secret="",
            security_token=access_token,
            expiration=expiry,
        )

    # Internal helpers

    def _ensure_valid_credentials(self) -> None:
        """Refresh the credential if expired or not yet fetched."""
        if not self._credentials.valid:
            auth_request = google.auth.transport.requests.Request()
            self._credentials.refresh(auth_request)

    def close(self) -> None:
        """Release underlying resources (no-op for GCP auth)."""


def _convert_permission_to_gcp(permission: str) -> str:
    """Convert cloud-agnostic permission to GCP role."""
    if not permission.startswith(_PERMISSION_PREFIX):
        return permission
    action = permission[len(_PERMISSION_PREFIX) :]
    mapping = {
        "GetObject": "inRole:roles/storage.objectViewer",
        "PutObject": "inRole:roles/storage.objectCreator",
        "DeleteObject": "inRole:roles/storage.objectAdmin",
        "ListBucket": "inRole:roles/storage.objectViewer",
    }
    return mapping.get(action, "inRole:roles/storage.objectViewer")


def _escape_cel_string(value: str) -> str:
    """Escape a value for safe interpolation into a CEL string literal."""
    for i, ch in enumerate(value):
        if ord(ch) < 0x20:
            raise InvalidArgumentException(
                f"resource_prefix contains control character at index {i}"
            )
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _build_gcp_prefix_expression(resource_prefix: str) -> str:
    """Convert cloud-agnostic resource prefix to GCP CEL expression."""
    if not resource_prefix.startswith(_RESOURCE_SCHEME):
        raise InvalidArgumentException(
            f"resource_prefix must start with '{_RESOURCE_SCHEME}'. "
            f"Found: {resource_prefix}"
        )
    path = resource_prefix[len(_RESOURCE_SCHEME) :]
    slash_idx = path.find("/")
    if slash_idx < 1:
        raise InvalidArgumentException(
            f"resource_prefix must contain bucket name followed by '/'. "
            f"Found: {resource_prefix}"
        )
    bucket = _escape_cel_string(path[:slash_idx])
    prefix = _escape_cel_string(path[slash_idx + 1 :])
    gcp_path = f"projects/_/buckets/{bucket}/objects/{prefix}"
    return (
        f"resource.name.startsWith('{gcp_path}')"
        f" || api.getAttribute("
        f"'storage.googleapis.com/objectListPrefix', '')"
        f".startsWith('{prefix}')"
    )
