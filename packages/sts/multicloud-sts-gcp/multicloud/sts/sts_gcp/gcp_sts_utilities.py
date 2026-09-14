"""GCP implementation of :class:`BaseStsUtilities`.

Cloud-native signed auth requests are an AWS-specific mechanism (a
SigV4-signed STS ``GetCallerIdentity`` request replayed by an identity
broker). GCP has no equivalent, so this provider registers only to give a
clear :class:`UnsupportedOperationException` at call time rather than a
missing-provider error for GCP callers.
"""

from __future__ import annotations

from typing import Mapping

from multicloud.common.exceptions import (
    SubstrateSdkException,
    UnknownException,
    UnsupportedOperationException,
)
from multicloud.sts.sts_client.utilities import (
    STS_UTILITIES_REGISTRY,
    BaseStsUtilities,
    SignedAuthRequest,
)


@STS_UTILITIES_REGISTRY.register("gcp")
class GcpStsUtilities(BaseStsUtilities):
    """GCP STS utility provider — signed auth requests are unsupported."""

    def do_create_signed_auth_request(
        self,
        *,
        custom_headers: Mapping[str, str],
    ) -> SignedAuthRequest:
        """Not supported for GCP.

        Signed auth requests rely on the SigV4 GetCallerIdentity mechanism,
        which is AWS-specific. GCP identity federation uses token exchange
        instead; see ``StsClient.assume_role_with_web_identity``.
        """
        raise UnsupportedOperationException(
            "create_signed_auth_request is not supported for GCP. "
            "Signed auth requests use the AWS-specific SigV4 GetCallerIdentity "
            "mechanism; GCP uses token exchange for identity federation."
        )

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        if isinstance(exc, SubstrateSdkException):
            return exc
        return UnknownException(str(exc))
