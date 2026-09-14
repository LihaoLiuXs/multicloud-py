"""Cloud-agnostic exception hierarchy.

Providers translate their native errors into one of these types so
client code can write a single ``except`` clause that works across
substrates.
"""

from __future__ import annotations


class SubstrateSdkException(Exception):
    """Root of all SDK exceptions — catch this to handle any SDK error generically."""


class InvalidArgumentException(SubstrateSdkException):
    """Caller provided invalid input (empty key, bad type, failed validation)."""


class ResourceNotFoundException(SubstrateSdkException):
    """Target resource (bucket, object, role) does not exist."""


class ResourceExistsException(SubstrateSdkException):
    """Attempted to create a resource that already exists (e.g., duplicate bucket)."""


class ResourceConflictException(SubstrateSdkException):
    """Resource state conflicts with the requested operation (e.g., delete role with attached policies).

    Also raised when a concurrent write races a conditional write: the
    precondition may never have been evaluated and the write may not have
    happened, so this is a retry signal, distinct from the terminal
    ResourceExistsException / FailedPreconditionException. Retry the write.
    """


class UnAuthorizedException(SubstrateSdkException):
    """Credentials missing, expired, or lack required permissions."""


class FailedPreconditionException(SubstrateSdkException):
    """Operation rejected because a precondition wasn't met (e.g., non-empty bucket delete)."""


class UnsupportedOperationException(SubstrateSdkException):
    """The requested operation is not supported by this provider."""


class ResourceExhaustedException(SubstrateSdkException):
    """Rate limit exceeded, quota exhausted, or throttling applied."""


class DeadlineExceededException(SubstrateSdkException):
    """Operation timed out or deadline exceeded."""


class TransactionFailedException(SubstrateSdkException):
    """Internal service failure during transaction."""


class ResourceAlreadyExistsException(SubstrateSdkException):
    """Attempted to create a resource that already exists."""


class UnknownException(SubstrateSdkException):
    """Catch-all for errors that don't map to a more specific type."""
