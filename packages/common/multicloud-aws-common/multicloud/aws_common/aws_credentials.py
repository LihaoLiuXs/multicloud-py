"""AWS credential resolution from CredentialsOverrider.

Shared utility for all AWS provider modules (STS, Blob, PubSub, DocStore).

For ASSUME_ROLE and ASSUME_ROLE_WEB_IDENTITY, credentials are
auto-refreshed via botocore's built-in credential fetchers — the session
automatically re-assumes the role when the token approaches expiry.
"""

from __future__ import annotations

import time
from typing import Any, Optional

try:
    import boto3  # type: ignore[import-not-found]
    from botocore.credentials import (  # type: ignore[import-not-found]
        AssumeRoleCredentialFetcher,
        AssumeRoleWithWebIdentityCredentialFetcher,
        DeferredRefreshableCredentials,
    )
    from botocore.session import (
        get_session as _get_botocore_session,  # type: ignore[import-not-found]
    )
except ImportError as _exc:
    raise ImportError(
        "AWS dependencies not found. Install with: "
        "pip install 'python-multicloud-sdk[aws]'"
    ) from _exc

from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
)
from multicloud.common.exceptions import InvalidArgumentException

_ASSUME_ROLE_METHOD = "sts-assume-role"
_WEB_IDENTITY_METHOD = "sts-assume-role-with-web-identity"
_DEFAULT_SESSION_NAME_PREFIX = "multicloud"


def _default_session_name() -> str:
    return f"{_DEFAULT_SESSION_NAME_PREFIX}-{int(time.time())}"


def get_session(
    overrider: Optional[CredentialsOverrider],
    region: Optional[str] = None,
) -> Any:
    """Build a boto3 Session from a CredentialsOverrider, or use defaults.

    For ASSUME_ROLE and ASSUME_ROLE_WEB_IDENTITY, the returned session
    auto-refreshes credentials when the token approaches expiry.

    Credential resolution for ASSUME_ROLE and ASSUME_ROLE_WEB_IDENTITY is
    deferred to the first AWS API call (botocore's
    ``DeferredRefreshableCredentials`` semantics). Invalid role ARNs,
    expired source credentials, or missing IAM permissions surface at use
    time rather than at Session construction.

    Args:
        overrider: Credential override configuration. If None, returns
            a default boto3 session using environment/config discovery.
        region: AWS region name for the session.

    Returns:
        A ``boto3.Session`` configured with the resolved credentials.
    """
    if overrider is None:
        return boto3.Session()

    if overrider.credentials_type == CredentialsType.SESSION:
        # CredentialsOverrider.__post_init__ guarantees session_credentials
        # is non-None for SESSION type.
        creds = overrider.session_credentials
        return boto3.Session(
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.access_key_secret,
            aws_session_token=creds.security_token or None,
            region_name=region,
        )

    if overrider.credentials_type == CredentialsType.ASSUME_ROLE:
        return _build_session_with_fetcher(
            _make_assume_role_fetcher(overrider, region),
            method=_ASSUME_ROLE_METHOD,
            region=region,
        )

    if overrider.credentials_type == CredentialsType.ASSUME_ROLE_WEB_IDENTITY:
        return _build_session_with_fetcher(
            _make_web_identity_fetcher(overrider, region),
            method=_WEB_IDENTITY_METHOD,
            region=region,
        )

    raise InvalidArgumentException(
        f"Unsupported credentials_type: {overrider.credentials_type}"
    )


def _build_session_with_fetcher(
    fetcher: Any,
    method: str,
    region: Optional[str],
) -> Any:
    """Create a boto3 Session backed by a botocore credential fetcher."""
    refreshable = DeferredRefreshableCredentials(
        method=method,
        refresh_using=fetcher.fetch_credentials,
    )

    # No public API exists for injecting credentials into a botocore
    # session — this private-attr pattern is the standard approach.
    botocore_session = _get_botocore_session()
    botocore_session._credentials = refreshable
    if region:
        botocore_session.set_config_variable("region", region)

    return boto3.Session(botocore_session=botocore_session)


def _make_assume_role_fetcher(
    overrider: CredentialsOverrider,
    region: Optional[str],
) -> Any:
    """Build a botocore ``AssumeRoleCredentialFetcher`` from an overrider."""
    source_session = boto3.Session()
    source_credentials = source_session.get_credentials()

    extra_args: dict[str, Any] = {
        "RoleSessionName": overrider.session_name or _default_session_name(),
    }
    if overrider.duration_seconds is not None:
        extra_args["DurationSeconds"] = overrider.duration_seconds

    return AssumeRoleCredentialFetcher(
        client_creator=source_session._session.create_client,
        source_credentials=source_credentials,
        role_arn=overrider.role,
        extra_args=extra_args,
    )


def _make_web_identity_fetcher(
    overrider: CredentialsOverrider,
    region: Optional[str],
) -> Any:
    """Build a botocore ``AssumeRoleWithWebIdentityCredentialFetcher`` from an overrider.

    The STS client used for the token exchange is anonymous — we do not
    want it to recursively pick up ambient credentials.
    """
    from botocore.credentials import Credentials as AnonymousCredentials

    anon_botocore_session = _get_botocore_session()
    anon_botocore_session._credentials = AnonymousCredentials("", "")
    anon_session = boto3.Session(botocore_session=anon_botocore_session)

    extra_args: dict[str, Any] = {
        "RoleSessionName": overrider.session_name or _default_session_name(),
    }
    if overrider.duration_seconds is not None:
        extra_args["DurationSeconds"] = overrider.duration_seconds

    return AssumeRoleWithWebIdentityCredentialFetcher(
        client_creator=anon_session._session.create_client,
        web_identity_token_loader=overrider.web_identity_token_supplier,  # type: ignore[arg-type]
        role_arn=overrider.role,
        extra_args=extra_args,
    )


async def get_aio_session(
    overrider: Optional[CredentialsOverrider],
    region: Optional[str] = None,
) -> Any:
    """Build an aiobotocore session from a CredentialsOverrider, or use defaults.

    Async counterpart of :func:`get_session`. aiobotocore is imported lazily
    so this module stays importable without the ``async`` extra installed.

    For ASSUME_ROLE and ASSUME_ROLE_WEB_IDENTITY the returned session
    auto-refreshes credentials via aiobotocore's deferred fetchers. Source
    credentials are resolved asynchronously, which is why this helper is a
    coroutine.

    Args:
        overrider: Credential override configuration. If None, returns a
            default aiobotocore session using environment/config discovery.
        region: AWS region name for the session.

    Returns:
        An ``aiobotocore.session.AioSession`` configured with the resolved
        credentials.
    """
    try:
        from aiobotocore.credentials import (  # type: ignore[import-not-found]
            AioAssumeRoleCredentialFetcher,
            AioAssumeRoleWithWebIdentityCredentialFetcher,
            AioDeferredRefreshableCredentials,
        )
        from aiobotocore.session import (
            get_session as _get_aio_session,  # type: ignore[import-not-found]
        )
    except ImportError as exc:
        raise ImportError(
            "aiobotocore is required for async AWS credential resolution. "
            "Install it with: pip install 'multicloud-py-blob-aws[async]'"
        ) from exc

    session = _get_aio_session()
    if region:
        session.set_config_variable("region", region)

    if overrider is None:
        return session

    if overrider.credentials_type == CredentialsType.SESSION:
        # CredentialsOverrider.__post_init__ guarantees session_credentials
        # is non-None for SESSION type.
        creds = overrider.session_credentials
        session.set_credentials(
            creds.access_key_id,
            creds.access_key_secret,
            creds.security_token or None,
        )
        return session

    if overrider.credentials_type == CredentialsType.ASSUME_ROLE:
        source_credentials = await session.get_credentials()
        fetcher = AioAssumeRoleCredentialFetcher(
            client_creator=session.create_client,
            source_credentials=source_credentials,
            role_arn=overrider.role,
            extra_args=_assume_role_extra_args(overrider),
        )
        session._credentials = AioDeferredRefreshableCredentials(
            method=_ASSUME_ROLE_METHOD,
            refresh_using=fetcher.fetch_credentials,
        )
        return session

    if overrider.credentials_type == CredentialsType.ASSUME_ROLE_WEB_IDENTITY:
        # The web-identity fetcher signs the STS exchange as UNSIGNED
        # internally, so no source credentials are needed.
        fetcher = AioAssumeRoleWithWebIdentityCredentialFetcher(
            client_creator=session.create_client,
            web_identity_token_loader=overrider.web_identity_token_supplier,
            role_arn=overrider.role,
            extra_args=_assume_role_extra_args(overrider),
        )
        session._credentials = AioDeferredRefreshableCredentials(
            method=_WEB_IDENTITY_METHOD,
            refresh_using=fetcher.fetch_credentials,
        )
        return session

    raise InvalidArgumentException(
        f"Unsupported credentials_type: {overrider.credentials_type}"
    )


def _assume_role_extra_args(overrider: CredentialsOverrider) -> dict[str, Any]:
    """Build the STS assume-role extra args shared by both async fetchers."""
    extra_args: dict[str, Any] = {
        "RoleSessionName": overrider.session_name or _default_session_name(),
    }
    if overrider.duration_seconds is not None:
        extra_args["DurationSeconds"] = overrider.duration_seconds
    return extra_args
