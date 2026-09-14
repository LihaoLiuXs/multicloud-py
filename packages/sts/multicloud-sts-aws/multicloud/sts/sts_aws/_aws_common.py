"""Shared STS helpers used by both AWS STS providers.

The credential service (:mod:`aws_sts`) and the offline signer
(:mod:`aws_sts_utilities`) translate the same STS / boto3 error codes, so the
error-mapping logic lives here once. A change to an STS error-code mapping is
then made in a single place instead of in lockstep across two files.
"""

from __future__ import annotations

from botocore.exceptions import (  # type: ignore[import-not-found]
    BotoCoreError,
    ClientError,
    NoCredentialsError,
)
from multicloud.aws_common.error_mapping import AWS_THROTTLING_CODES
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceExhaustedException,
    SubstrateSdkException,
    UnAuthorizedException,
    UnknownException,
)


def translate_aws_sts_error(exc: Exception) -> SubstrateSdkException:
    """Translate AWS STS exceptions to the common hierarchy."""
    if isinstance(exc, SubstrateSdkException):
        return exc

    if isinstance(exc, NoCredentialsError):
        return UnAuthorizedException(str(exc))

    if isinstance(exc, ClientError):
        error_info = exc.response.get("Error", {})
        code = error_info.get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")

        # Checked before the status branches below: AWS reports throttling
        # with HTTP 400 on some services, which would otherwise be read as
        # an invalid argument.
        if code in AWS_THROTTLING_CODES or status == 429:
            return ResourceExhaustedException(str(exc))

        if (
            code in ("InvalidClientTokenId", "ExpiredToken", "AccessDenied", "403")
            or status == 403
        ):
            return UnAuthorizedException(str(exc))
        if (
            code in ("InvalidDuration", "ValidationError", "InvalidArgument", "400")
            or status == 400
        ):
            return InvalidArgumentException(str(exc))

        return UnknownException(str(exc))

    if isinstance(exc, BotoCoreError):
        return InvalidArgumentException(str(exc))

    return UnknownException(str(exc))
