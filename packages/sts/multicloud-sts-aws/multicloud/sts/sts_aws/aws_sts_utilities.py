"""AWS implementation of :class:`BaseStsUtilities`.

Signs an STS ``GetCallerIdentity`` request with SigV4 but does not send
it. The caller forwards the signed request to an identity broker (e.g.
HashiCorp Vault's AWS-IAM auth method), which replays it against STS to
verify the caller's AWS identity. Signing never sends the request, but
resolving the credentials can still contact AWS (e.g. an ``assume_role``
overrider or an IMDS/SSO chain, fetched lazily at signing time).

Uses the public ``botocore`` signing API (``SigV4Auth`` + ``AWSRequest``)
rather than private client internals, so it stays stable across botocore
upgrades.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from botocore.auth import SigV4Auth  # type: ignore[import-not-found]
from botocore.awsrequest import AWSRequest  # type: ignore[import-not-found]
from botocore.exceptions import (  # type: ignore[import-not-found]
    NoCredentialsError,
)
from multicloud.aws_common.aws_credentials import get_session
from multicloud.common.credentials import CredentialsOverrider, StsCredentials
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
)
from multicloud.sts.sts_aws._aws_common import translate_aws_sts_error
from multicloud.sts.sts_client.utilities import (
    STS_UTILITIES_REGISTRY,
    BaseStsUtilities,
    SignedAuthRequest,
)

# Regional STS endpoint and virtual host used for both the request URL and
# the signed ``Host`` header.
_STS_ENDPOINT = "https://sts.{region}.amazonaws.com/"
_STS_HOST = "sts.{region}.amazonaws.com"

# GetCallerIdentity is the smallest STS action a broker can replay to prove
# identity. The body is a fixed form-encoded payload.
_GET_CALLER_IDENTITY_BODY = b"Action=GetCallerIdentity&Version=2011-06-15"
_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"
_SIGNING_SERVICE_NAME = "sts"

# Headers this signer sets itself. A custom header colliding case-insensitively
# with one of these would be signed with one value but returned with another
# (botocore's header container appends duplicates and comma-joins them for the
# signature, while ``dict(request.headers)`` keeps only the first), yielding a
# request whose signature does not match its headers. Reject such collisions.
_RESERVED_HEADER_KEYS = frozenset({"host", "content-type"})


def _signed_header_names(authorization: str) -> frozenset[str]:
    """Return the lowercased header names SigV4 folded into the signature."""
    for part in authorization.split(", "):
        if part.startswith("SignedHeaders="):
            return frozenset(part[len("SignedHeaders=") :].split(";"))
    return frozenset()


@STS_UTILITIES_REGISTRY.register("aws")
class AwsStsUtilities(BaseStsUtilities):
    """AWS SigV4-backed signed-auth-request utility."""

    def __init__(
        self,
        region: Optional[str] = None,
        *,
        credentials: Any = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
    ) -> None:
        super().__init__(
            region=region,
            credentials_overrider=credentials_overrider,
        )

        # Resolve credentials lazily at signing time (see _resolve_credentials),
        # not here: resolving in __init__ would pin a transient None from the
        # default chain for the object's lifetime, and would let a native
        # botocore error escape the constructor before translate_error runs.
        # An injected credentials object (test hook) takes precedence.
        self._injected_credentials = credentials
        self._resolved_credentials: Any = None

    def _resolve_credentials(self) -> Any:
        """Resolve signing credentials lazily, caching only a non-null result.

        A transient ``None`` is never cached, so a later call can still recover.
        """
        if self._injected_credentials is not None:
            return self._injected_credentials
        if self._resolved_credentials is None:
            self._resolved_credentials = get_session(
                self._credentials_overrider, self._region
            ).get_credentials()
        return self._resolved_credentials

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate AWS credential / signing errors to common hierarchy."""
        return translate_aws_sts_error(exc)

    def do_create_signed_auth_request(
        self,
        *,
        custom_headers: Mapping[str, str],
    ) -> SignedAuthRequest:
        """SigV4-sign an STS GetCallerIdentity request without sending it.

        Custom headers are folded in before signing so they appear in the
        signed ``SignedHeaders`` set; a broker that replays the request can
        then trust them as part of the caller's identity proof. A custom header
        that collides (case-insensitively) with a header the signer sets is
        rejected with ``InvalidArgumentException``.
        """
        if not self._region:
            raise InvalidArgumentException(
                "create_signed_auth_request requires a region for SigV4 signing"
            )
        credentials = self._resolve_credentials()
        if credentials is None:
            raise NoCredentialsError()

        # Freeze once before signing. A refreshable credential read live during
        # signing and again for the returned metadata could straddle a refresh,
        # signing with one key but reporting another. One snapshot both signs
        # the request and populates the result.
        frozen = credentials.get_frozen_credentials()

        for key in custom_headers:
            if key.lower() in _RESERVED_HEADER_KEYS:
                raise InvalidArgumentException(
                    f"custom_headers must not override the reserved header {key!r}; "
                    "Host and Content-Type are set by the signer"
                )

        url = _STS_ENDPOINT.format(region=self._region)
        headers = {
            "Host": _STS_HOST.format(region=self._region),
            "Content-Type": _CONTENT_TYPE,
        }
        headers.update(custom_headers)

        request = AWSRequest(
            method="POST",
            url=url,
            data=_GET_CALLER_IDENTITY_BODY,
            headers=headers,
        )
        SigV4Auth(frozen, _SIGNING_SERVICE_NAME, self._region).add_auth(request)

        # SigV4 drops some headers (e.g. User-Agent, Expect) from SignedHeaders.
        # A custom header that is not signed would ride along unbound, so a
        # broker could not trust it as identity proof. Reject any that the
        # signer excluded rather than return a silently unbound header.
        signed = _signed_header_names(request.headers["Authorization"])
        for key in custom_headers:
            if key.lower() not in signed:
                raise InvalidArgumentException(
                    f"custom header {key!r} was excluded from the SigV4 "
                    "SignedHeaders set, so it cannot be trusted by a broker"
                )

        # The secret key is a signing input only — the signature already lives
        # in the Authorization header — so access_key_secret is left empty and
        # never rides along if a caller serializes the returned request.
        return SignedAuthRequest(
            method="POST",
            url=url,
            headers=dict(request.headers),
            body=_GET_CALLER_IDENTITY_BODY,
            credentials=StsCredentials(
                access_key_id=frozen.access_key,
                security_token=frozen.token or "",
            ),
        )
