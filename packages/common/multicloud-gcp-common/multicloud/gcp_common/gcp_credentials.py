"""GCP credential resolution from CredentialsOverrider.

Shared utility for all GCP provider modules (STS, Blob, PubSub, DocStore).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

try:
    import google.auth  # type: ignore[import-not-found]
    import google.auth.exceptions  # type: ignore[import-not-found]
    import google.auth.impersonated_credentials  # type: ignore[import-not-found]
except ImportError as _exc:
    raise ImportError(
        "GCP dependencies not found. Install with: "
        "pip install 'python-multicloud-sdk[gcp]'"
    ) from _exc

from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
)
from multicloud.common.exceptions import InvalidArgumentException, UnAuthorizedException

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

_logger = logging.getLogger(__name__)


def get_credentials(
    overrider: CredentialsOverrider,
    scopes: Optional[list[str]] = None,
) -> Any:
    """Resolve GCP credentials from a CredentialsOverrider.

    Args:
        overrider: Credential override configuration.
        scopes: OAuth scopes for the credentials. Defaults to
            cloud-platform scope.

    Returns:
        A ``google.auth.credentials.Credentials`` instance, or None if
        the override type is not supported (caller should fall through
        to default credentials).
    """

    effective_scopes = scopes or DEFAULT_SCOPES

    if overrider.credentials_type == CredentialsType.SESSION:
        creds = overrider.session_credentials
        if creds is None:
            raise InvalidArgumentException(
                "session_credentials required for SESSION override"
            )
        if not creds.security_token:
            # Unlike AWS (where security_token is an optional session token
            # layered over an access key), the GCP bearer token IS the
            # credential. An empty one builds a credential that fails opaquely
            # at request time, so reject it up front.
            raise InvalidArgumentException(
                "security_token required for SESSION override on GCP"
            )
        from google.oauth2.credentials import Credentials as OAuth2Credentials

        # SESSION wraps a static bearer token; the returned credential is
        # intentionally non-refreshable. Caller owns re-issuing before expiry.
        return OAuth2Credentials(token=creds.security_token)

    if overrider.credentials_type == CredentialsType.ASSUME_ROLE:
        try:
            source_credentials, _ = google.auth.default(scopes=effective_scopes)
        except google.auth.exceptions.DefaultCredentialsError as exc:
            raise UnAuthorizedException("Could not find default credentials") from exc
        kwargs: dict[str, Any] = {
            "source_credentials": source_credentials,
            "target_principal": overrider.role,
            "target_scopes": effective_scopes,
        }
        if overrider.duration_seconds is not None:
            kwargs["lifetime"] = overrider.duration_seconds
        return google.auth.impersonated_credentials.Credentials(**kwargs)

    # ASSUME_ROLE_WEB_IDENTITY (WIF) falls through to ADC. Warn loudly: the
    # caller explicitly requested federation but will instead authenticate as
    # whatever ambient ADC identity is present.
    _logger.warning(
        "credentials_overrider requested ASSUME_ROLE_WEB_IDENTITY, but "
        "Workload Identity Federation is not supported here; falling back to "
        "Application Default Credentials. The caller will authenticate as the "
        "ambient ADC identity, not the requested federated principal."
    )
    return None


class _StaticToken:
    """A minimal gcloud-aio-compatible token wrapping a static bearer string.

    ``gcloud.aio.auth.Token`` has no constructor for a pre-issued bearer
    token, so the SESSION override path uses this duck-typed stand-in. It
    exposes the async ``get()``/``close()`` surface ``Storage`` relies on.
    The token is intentionally non-refreshable — the caller owns re-issuing
    it before expiry.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    async def get(self) -> str:
        return self._token

    async def close(self) -> None:
        return None


def get_token(
    overrider: Optional[CredentialsOverrider],
    scopes: Optional[list[str]] = None,
) -> Any:
    """Resolve an async GCP auth token from a CredentialsOverrider.

    Async counterpart of :func:`get_credentials`. gcloud-aio is imported
    lazily so this module stays importable without the ``async`` extra.

    Args:
        overrider: Credential override configuration. If None, returns None
            so the caller falls through to Application Default Credentials.
        scopes: OAuth scopes for the token. Defaults to cloud-platform scope.

    Returns:
        A token object exposing async ``get()``/``close()`` (a static-bearer
        stand-in for SESSION, a ``gcloud.aio.auth.Token`` for ASSUME_ROLE),
        or None for ASSUME_ROLE_WEB_IDENTITY — gcloud-aio has no WIF token
        type — so the caller falls back to ADC.
    """
    if overrider is None:
        return None

    effective_scopes = scopes or DEFAULT_SCOPES

    if overrider.credentials_type == CredentialsType.SESSION:
        creds = overrider.session_credentials
        if creds is None:
            raise InvalidArgumentException(
                "session_credentials required for SESSION override"
            )
        if not creds.security_token:
            # The GCP bearer token IS the credential (unlike AWS, where it is
            # an optional session token). An empty one yields a token that
            # fails opaquely at request time, so reject it up front.
            raise InvalidArgumentException(
                "security_token required for SESSION override on GCP"
            )
        return _StaticToken(creds.security_token)

    if overrider.credentials_type == CredentialsType.ASSUME_ROLE:
        try:
            from gcloud.aio.auth import Token  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "gcloud-aio-storage is required for async GCP credential "
                "resolution. Install it with: "
                "pip install 'sfdc-multicloud-blob-gcp[async]'"
            ) from exc
        token = Token(
            target_principal=overrider.role,
            scopes=effective_scopes,
        )
        # gcloud-aio's Token takes no lifetime kwarg and defaults to a 3600s
        # impersonation TTL; set it per-instance so duration_seconds is honored,
        # matching the sync get_credentials() lifetime behavior.
        if overrider.duration_seconds is not None:
            token.default_token_ttl = overrider.duration_seconds
        return token

    # ASSUME_ROLE_WEB_IDENTITY (WIF) is not supported on the async path:
    # gcloud-aio's Token has no Workload Identity Federation / IdentityPool
    # token type, so there is nothing to build here. Fall through to ADC,
    # matching the sync get_credentials() behavior for this override type.
    #
    # Warn loudly: the caller explicitly requested federation but will instead
    # authenticate as whatever ambient ADC identity is present. Without this
    # signal the identity swap is silent and undebuggable in production.
    _logger.warning(
        "credentials_overrider requested ASSUME_ROLE_WEB_IDENTITY, but "
        "Workload Identity Federation is not supported on the async GCP path; "
        "falling back to Application Default Credentials. The caller will "
        "authenticate as the ambient ADC identity, not the requested "
        "federated principal."
    )
    return None
