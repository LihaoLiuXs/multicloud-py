"""Unit tests for the cloud-agnostic exception hierarchy."""

from __future__ import annotations

import pytest
from multicloud.common.exceptions import (
    FailedPreconditionException,
    InvalidArgumentException,
    ResourceExistsException,
    ResourceNotFoundException,
    SubstrateSdkException,
    UnAuthorizedException,
    UnknownException,
    UnsupportedOperationException,
)


class TestExceptionHierarchy:
    """Verify all exceptions inherit from SubstrateSdkException."""

    @pytest.mark.parametrize(
        "exc_cls",
        [
            InvalidArgumentException,
            ResourceNotFoundException,
            ResourceExistsException,
            UnAuthorizedException,
            FailedPreconditionException,
            UnsupportedOperationException,
            UnknownException,
        ],
    )
    def test_inherits_from_base(self, exc_cls):
        exc = exc_cls("test message")
        assert isinstance(exc, SubstrateSdkException)
        assert isinstance(exc, Exception)

    @pytest.mark.parametrize(
        "exc_cls",
        [
            InvalidArgumentException,
            ResourceNotFoundException,
            ResourceExistsException,
            UnAuthorizedException,
            FailedPreconditionException,
            UnsupportedOperationException,
            UnknownException,
        ],
    )
    def test_message_preserved(self, exc_cls):
        exc = exc_cls("some detail")
        assert str(exc) == "some detail"

    def test_can_catch_with_base_class(self):
        with pytest.raises(SubstrateSdkException):
            raise ResourceNotFoundException("bucket not found")

    def test_can_catch_specific_class(self):
        with pytest.raises(UnAuthorizedException):
            raise UnAuthorizedException("access denied")

    def test_exception_args(self):
        exc = InvalidArgumentException("bad key", "extra")
        assert exc.args == ("bad key", "extra")
