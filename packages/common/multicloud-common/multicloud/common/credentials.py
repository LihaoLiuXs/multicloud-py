"""Cross-service credential models.

Defines the cloud-agnostic credential types shared across all services
(STS, Blob, PubSub, DocStore). These models describe *how* to obtain
credentials — the provider-specific resolution logic lives in the
per-cloud ``*_common`` packages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Callable, Optional

from multicloud.common.exceptions import InvalidArgumentException


@dataclass(frozen=True)
class StsCredentials:
    """Cloud-agnostic representation of temporary credentials.

    All providers normalize their native credential type into this shape.

    - ``access_key_id``: identity key or empty when not applicable
    - ``access_key_secret``: secret key or empty when not applicable
    - ``security_token``: session/bearer token (all providers)
    - ``expiration``: token expiry time (all providers)
    """

    access_key_id: str = ""
    access_key_secret: str = ""
    security_token: str = ""
    expiration: Optional[datetime] = None

    def __repr__(self) -> str:
        # Mask secret fields so accidental logging (e.g. of an exception
        # whose message contains an StsCredentials) does not leak creds.
        return (
            f"StsCredentials(access_key_id={self.access_key_id!r}, "
            f"access_key_secret=***, security_token=***, "
            f"expiration={self.expiration!r})"
        )


class CredentialsType(Enum):
    """Type of credential override to apply."""

    SESSION = auto()
    ASSUME_ROLE = auto()
    ASSUME_ROLE_WEB_IDENTITY = auto()


@dataclass(frozen=True)
class CredentialsOverrider:
    """Override the default credentials resolution.

    Modeled as a grouped configuration object because its fields are
    interdependent and validated as a unit — the required subset of fields
    changes based on ``credentials_type``.

    Used when a service needs to:
    1. Supply explicit session credentials (SESSION)
    2. Assume a role from another account (ASSUME_ROLE)
    3. Exchange a web identity token for credentials (ASSUME_ROLE_WEB_IDENTITY)

    If ``credentials_type`` is SESSION, ``session_credentials`` must be provided.
    If ``credentials_type`` is ASSUME_ROLE, ``role`` is required.
    If ``credentials_type`` is ASSUME_ROLE_WEB_IDENTITY, ``role`` and
    ``web_identity_token_supplier`` are required.

    ``role`` is provider-interpreted: each provider maps this string to its
    native role identifier format (e.g. an ARN, a service-account email, or
    a RAM role name).
    """

    credentials_type: CredentialsType
    session_credentials: Optional[StsCredentials] = None
    role: Optional[str] = None
    duration_seconds: Optional[int] = None
    session_name: Optional[str] = None
    web_identity_token_supplier: Optional[Callable[[], str]] = None

    def __post_init__(self) -> None:
        if (
            self.credentials_type == CredentialsType.SESSION
            and self.session_credentials is None
        ):
            raise InvalidArgumentException(
                "session_credentials required when credentials_type is SESSION"
            )
        if self.credentials_type == CredentialsType.ASSUME_ROLE and not self.role:
            raise InvalidArgumentException(
                "role required when credentials_type is ASSUME_ROLE"
            )
        if self.credentials_type == CredentialsType.ASSUME_ROLE_WEB_IDENTITY:
            if not self.role:
                raise InvalidArgumentException(
                    "role required when credentials_type is ASSUME_ROLE_WEB_IDENTITY"
                )
            if self.web_identity_token_supplier is None:
                raise InvalidArgumentException(
                    "web_identity_token_supplier required when credentials_type is ASSUME_ROLE_WEB_IDENTITY"
                )
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise InvalidArgumentException("duration_seconds must be positive")
