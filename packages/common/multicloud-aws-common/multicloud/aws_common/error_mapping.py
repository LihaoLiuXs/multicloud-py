"""Shared AWS error translation helpers."""

from __future__ import annotations

from botocore import exceptions as boto_exceptions
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceExhaustedException,
    ResourceNotFoundException,
    SubstrateSdkException,
    TransactionFailedException,
    UnAuthorizedException,
    UnknownException,
)

#: Error codes meaning the caller is being throttled or the service is briefly
#: unavailable. Covers every code botocore itself retries as throttling, plus
#: ServiceUnavailable and RequestExpired, which services return for the same
#: condition without botocore counting them. Note
#: several arrive with HTTP 400 rather than 429 -- bare ``Throttling`` is what
#: the query-protocol services (STS, IAM) send -- which is why callers must
#: match on the code before falling back to the status.
#:
#: ``tests/common/unit/test_throttling_error_mapping.py`` asserts this stays a
#: superset of botocore's own list, so a new code added upstream is caught.
AWS_THROTTLING_CODES = frozenset(
    {
        "BandwidthLimitExceeded",
        "EC2ThrottledException",
        "LimitExceededException",
        "PriorRequestNotComplete",
        "ProvisionedThroughputExceededException",
        "RequestExpired",
        "RequestLimitExceeded",
        "RequestThrottled",
        "RequestThrottledException",
        "ServiceUnavailable",
        "SlowDown",
        "Throttling",
        "ThrottledException",
        "ThrottlingException",
        "TooManyRequestsException",
        "TransactionInProgressException",
    }
)

_SQS_AUTH_CODES = frozenset(
    {
        "InvalidClientTokenId",
        "UnrecognizedClientException",
        "ExpiredToken",
        "IncompleteSignature",
        "NotAuthorized",
        "OptInRequired",
        "AccountNotAuthorized",
        "AccountProblem",
        "AllAccessDisabled",
        "InvalidAccessKeyId",
        "InvalidPayer",
        "InvalidSecurity",
        "NotSignedUp",
        "TokenRefreshRequired",
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "InvalidSecurityException",
    }
)
#: SQS reports its queue-level limit with a code of its own.
_SQS_THROTTLING_CODES = AWS_THROTTLING_CODES | {"OverLimit"}
_SQS_INVALID_CODES = frozenset(
    {
        "InvalidAction",
        "ValidationError",
        "RequestTimeTooSkewed",
        "SignatureDoesNotMatch",
        "BadDigest",
        "InvalidParameterValue",
        "InvalidParameter",
        "InvalidMessageBody",
        "InvalidMessageAttributes",
        "MessageTooLarge",
        "BatchEntryIdsNotDistinct",
        "InvalidIdFormat",
        "ReceiptHandleIsInvalid",
        "MessageNotInflight",
        "InvalidAttributeName",
        "InvalidBatchEntryId",
        "InvalidMessageContents",
        "TooManyEntriesInBatchRequest",
        "UnsupportedOperation",
        "EmptyBatchRequest",
        "BatchRequestTooLong",
        "PurgeQueueInProgress",
        "QueueDeletedRecently",
        "QueueNameExists",
        "InvalidArgument",
    }
)
_SQS_NOT_FOUND_CODES = frozenset(
    {
        "QueueDoesNotExist",
        "AWS.SimpleQueueService.NonExistentQueue",
        "NotFoundException",
    }
)
_SQS_UNKNOWN_CODES = frozenset({"InternalError", "RequestCanceled"})

_SNS_AUTH_CODES = frozenset(
    {
        "InvalidClientTokenId",
        "UnrecognizedClientException",
        "ExpiredToken",
        "IncompleteSignature",
        "NotAuthorized",
        "OptInRequired",
        "AccountNotAuthorized",
        "AccountProblem",
        "AllAccessDisabled",
        "InvalidAccessKeyId",
        "InvalidPayer",
        "InvalidSecurity",
        "NotSignedUp",
        "TokenRefreshRequired",
        "AuthorizationError",
        "AccessDenied",
        "AccessDeniedException",
        "KMSAccessDenied",
        "KMSInvalidState",
    }
)
#: SNS reports throttling and oversized batches with codes of its own.
_SNS_THROTTLING_CODES = AWS_THROTTLING_CODES | {
    "Throttled",
    "TooManyEntriesInBatchRequest",
}
_SNS_INVALID_CODES = frozenset(
    {
        "InvalidAction",
        "ValidationError",
        "RequestTimeTooSkewed",
        "SignatureDoesNotMatch",
        "BadDigest",
        "InvalidParameter",
        "InvalidParameterValue",
        "InvalidMessage",
        "InvalidMessageAttributes",
        "MessageTooLong",
        "InvalidTopicArn",
        "InvalidTargetArn",
        "InvalidPhoneNumber",
        "InvalidSubject",
        "PlatformApplicationDisabled",
    }
)
_SNS_NOT_FOUND_CODES = frozenset(
    {
        "NotFound",
        "TopicNotFound",
        "EndpointNotFound",
        "SubscriptionNotFound",
    }
)


def translate_aws_sqs_error(exc: Exception) -> SubstrateSdkException:
    """Translate AWS SQS errors to common exception hierarchy."""
    return _translate_aws_error(
        exc=exc,
        auth_codes=_SQS_AUTH_CODES,
        throttling_codes=_SQS_THROTTLING_CODES,
        invalid_codes=_SQS_INVALID_CODES,
        not_found_codes=_SQS_NOT_FOUND_CODES,
        unknown_codes=_SQS_UNKNOWN_CODES,
    )


def translate_aws_sns_error(exc: Exception) -> SubstrateSdkException:
    """Translate AWS SNS errors to common exception hierarchy."""
    return _translate_aws_error(
        exc=exc,
        auth_codes=_SNS_AUTH_CODES,
        throttling_codes=_SNS_THROTTLING_CODES,
        invalid_codes=_SNS_INVALID_CODES,
        not_found_codes=_SNS_NOT_FOUND_CODES,
        unknown_codes=frozenset(),
    )


def _translate_aws_error(
    *,
    exc: Exception,
    auth_codes: frozenset[str],
    throttling_codes: frozenset[str],
    invalid_codes: frozenset[str],
    not_found_codes: frozenset[str],
    unknown_codes: frozenset[str],
) -> SubstrateSdkException:
    if isinstance(exc, SubstrateSdkException):
        return exc

    if isinstance(exc, boto_exceptions.ClientError):
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in auth_codes:
            return UnAuthorizedException(str(exc))
        if error_code in throttling_codes:
            return ResourceExhaustedException(str(exc))
        if error_code in invalid_codes:
            return InvalidArgumentException(str(exc))
        if error_code == "InternalFailure":
            return TransactionFailedException(str(exc))
        if error_code in not_found_codes:
            return ResourceNotFoundException(str(exc))
        if error_code in unknown_codes:
            return UnknownException(str(exc))
        return UnknownException(str(exc))

    if isinstance(exc, boto_exceptions.NoCredentialsError):
        return UnAuthorizedException(str(exc))
    return UnknownException(str(exc))
