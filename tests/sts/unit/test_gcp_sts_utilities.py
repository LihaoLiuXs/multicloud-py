"""Unit tests for :class:`GcpStsUtilities` — the GCP STS utility provider.

GCP does not support cloud-native signed auth requests (an AWS SigV4
mechanism), so the provider surfaces a clear ``UnsupportedOperationException``.
"""

from __future__ import annotations

import pytest
from multicloud.common.exceptions import (
    SubstrateSdkException,
    UnknownException,
    UnsupportedOperationException,
)
from multicloud.sts.sts_gcp.gcp_sts_utilities import GcpStsUtilities


class TestGcpStsUtilities:

    def test_create_signed_auth_request_unsupported(self):
        utils = GcpStsUtilities(region="us-central1")
        with pytest.raises(
            UnsupportedOperationException, match="not supported for GCP"
        ):
            utils.create_signed_auth_request()

    def test_unsupported_even_with_custom_headers(self):
        utils = GcpStsUtilities()
        with pytest.raises(UnsupportedOperationException):
            utils.create_signed_auth_request(custom_headers={"X-Broker": "vault"})

    def test_provider_id(self):
        assert GcpStsUtilities().get_provider_id() == "gcp"

    def test_translate_error_passthrough(self):
        utils = GcpStsUtilities()
        exc = UnsupportedOperationException("nope")
        assert utils.translate_error(exc) is exc

    def test_translate_error_wraps_unknown(self):
        utils = GcpStsUtilities()
        result = utils.translate_error(RuntimeError("boom"))
        assert isinstance(result, UnknownException)
        assert isinstance(result, SubstrateSdkException)
