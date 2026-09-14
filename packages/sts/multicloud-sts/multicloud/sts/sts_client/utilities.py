"""Driver-level abstractions for STS utility functions.

Offline signing helpers built on the Security Token Service. Unlike the
:class:`~multicloud.sts.sts_client.driver.BaseSts` operations, these do
not issue the identity request themselves — they produce a
signed-but-unsent identity request that the caller hands to another
system (e.g. an identity broker), which replays it to the provider to
verify the caller's identity. Resolving the signing credentials may
itself require a network call.

Same ABC + template-method shape as ``BaseSts``: providers implement the
``do_*`` hook; the public ``create_signed_auth_request`` method on
:class:`BaseStsUtilities` normalizes input then delegates.

Users never reach this layer directly — they construct an
:class:`~multicloud.sts.sts_client.client.StsUtilities` (in
``client.py``), which holds a ``BaseStsUtilities`` instance internally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, Optional

from multicloud.common.credentials import CredentialsOverrider, StsCredentials
from multicloud.common.provider import Provider, ProviderRegistry

# Utility-scoped registry. It shares the ``multicloud.sts.providers``
# entry-point group with ``STS_REGISTRY`` so a single provider-package
# import registers both the STS driver and its utility counterpart.
STS_UTILITIES_REGISTRY = ProviderRegistry(
    service_name="sts-utilities",
    entry_point_group="multicloud.sts.providers",
)


@dataclass(frozen=True)
class SignedAuthRequest:
    """A signed-but-unsent identity request.

    Produced by :meth:`BaseStsUtilities.create_signed_auth_request`. The
    caller forwards the request itself — ``method``, ``url``, ``headers``,
    and ``body`` — to an identity broker, which replays it to the provider
    to prove the caller's identity.

    Security: this is a **replayable identity proof** — the signed ``headers``
    (and any session token on ``credentials``) let anyone holding them
    impersonate the signer until the signature expires. Treat it as a secret:
    ``__repr__`` redacts the sensitive parts, but ``dataclasses.asdict`` and
    direct field access do not.

    - ``method``: HTTP method of the signed request
    - ``url``: fully-qualified request URL
    - ``headers``: signed request headers; redacted in ``repr``
    - ``body``: request body bytes
    - ``credentials``: non-secret signer identity (key id + session token);
      the long-term signing secret is omitted
    """

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    credentials: StsCredentials

    def __repr__(self) -> str:
        # Redact the signed headers (a replayable identity proof) so accidental
        # logging can't leak them; ``credentials`` masks its own secrets.
        return (
            f"SignedAuthRequest(method={self.method!r}, url={self.url!r}, "
            f"headers=<{len(self.headers)} signed headers; redacted>, "
            f"body=<{len(self.body)} bytes>, credentials={self.credentials!r})"
        )


class BaseStsUtilities(Provider, ABC):
    """Base class for substrate-specific STS utility implementations.

    Providers extend this class and implement
    :meth:`do_create_signed_auth_request`. Cross-cutting concerns
    (argument normalization) happen in the public method so providers
    don't have to repeat them.
    """

    def __init__(
        self,
        region: Optional[str] = None,
        *,
        credentials_overrider: Optional[CredentialsOverrider] = None,
    ) -> None:
        self._region = region
        self._credentials_overrider = credentials_overrider

    # properties
    @property
    def region(self) -> Optional[str]:
        return self._region

    # public API
    def create_signed_auth_request(
        self,
        *,
        custom_headers: Optional[Mapping[str, str]] = None,
    ) -> SignedAuthRequest:
        """Produce a signed identity request for cloud-native auth.

        Args:
            custom_headers: Extra headers to fold into the signature so the
                broker replaying the request can bind them to the identity.

        Returns:
            SignedAuthRequest with the signed method, url, headers, body,
            and the credentials used to sign it.
        """
        return self.do_create_signed_auth_request(
            custom_headers=dict(custom_headers or {})
        )

    # extension point (substrate-specific)
    @abstractmethod
    def do_create_signed_auth_request(
        self,
        *,
        custom_headers: Mapping[str, str],
    ) -> SignedAuthRequest:
        """Provider-specific implementation of ``create_signed_auth_request``."""

    # lifecycle
    def close(self) -> None:
        """Release underlying resources. Default is a no-op."""

    def __enter__(self) -> "BaseStsUtilities":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
