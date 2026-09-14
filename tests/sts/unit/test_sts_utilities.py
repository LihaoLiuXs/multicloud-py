"""Unit tests for the STS utilities client + driver layers.

Cloud-agnostic: exercises :class:`StsUtilities` (client) and
:class:`BaseStsUtilities` (driver) through a fake provider, with no
substrate-specific details.
"""

from __future__ import annotations

from typing import Mapping, Optional

import pytest
from multicloud.common.credentials import StsCredentials
from multicloud.common.exceptions import SubstrateSdkException, UnAuthorizedException
from multicloud.sts.sts_client import StsUtilities
from multicloud.sts.sts_client.utilities import (
    STS_UTILITIES_REGISTRY,
    BaseStsUtilities,
    SignedAuthRequest,
)


class _SentinelError(Exception):
    """Native-style error a provider would translate."""


@STS_UTILITIES_REGISTRY.register("fake-utils")
class _FakeStsUtilities(BaseStsUtilities):
    """Minimal concrete implementation for testing the utilities layers."""

    def __init__(
        self,
        region: Optional[str] = None,
        *,
        credentials_overrider=None,
        fail: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(region=region, credentials_overrider=credentials_overrider)
        self.received_headers: Optional[Mapping[str, str]] = None
        self.closed = False
        self._fail = fail

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        if isinstance(exc, SubstrateSdkException):
            return exc
        if isinstance(exc, _SentinelError):
            return UnAuthorizedException(str(exc))
        return SubstrateSdkException(str(exc))

    def do_create_signed_auth_request(
        self,
        *,
        custom_headers: Mapping[str, str],
    ) -> SignedAuthRequest:
        self.received_headers = custom_headers
        if self._fail:
            raise _SentinelError("boom")
        return SignedAuthRequest(
            method="POST",
            url="https://token-service.example/",
            headers={"X-Signed": "1", **custom_headers},
            body=b"action=verify",
            credentials=StsCredentials(
                access_key_id="AK", access_key_secret="SK", security_token="ST"
            ),
        )

    def close(self) -> None:
        self.closed = True


class TestBaseStsUtilities:
    """Template-method wiring on the driver base class."""

    def test_region_defaults_to_none(self):
        assert _FakeStsUtilities().region is None

    def test_delegates_and_passes_custom_headers(self):
        utils = _FakeStsUtilities()
        resp = utils.create_signed_auth_request(custom_headers={"X-Broker": "vault"})
        assert resp.method == "POST"
        assert resp.headers["X-Broker"] == "vault"
        assert utils.received_headers == {"X-Broker": "vault"}

    def test_none_custom_headers_normalized_to_empty_dict(self):
        utils = _FakeStsUtilities()
        utils.create_signed_auth_request()
        assert utils.received_headers == {}

    def test_custom_headers_copied_not_aliased(self):
        utils = _FakeStsUtilities()
        source = {"X-Broker": "vault"}
        utils.create_signed_auth_request(custom_headers=source)
        assert utils.received_headers == source
        assert utils.received_headers is not source

    def test_close_and_context_manager(self):
        with _FakeStsUtilities() as utils:
            assert utils.region is None
        assert utils.closed is True


class TestStsUtilitiesClient:
    """User-facing client layer forwards to the driver and translates errors."""

    def test_construction_selects_provider(self):
        client = StsUtilities(provider="fake-utils", region="region-1")
        assert client.provider == "fake-utils"
        assert client.region == "region-1"

    def test_provider_is_keyword_only(self):
        with pytest.raises(TypeError):
            StsUtilities("fake-utils")  # type: ignore[misc]

    def test_unknown_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="nope"):
            StsUtilities(provider="nope")

    def test_create_signed_auth_request_forwards(self):
        client = StsUtilities(provider="fake-utils")
        resp = client.create_signed_auth_request(custom_headers={"X-Broker": "vault"})
        assert resp.method == "POST"
        assert resp.url == "https://token-service.example/"
        assert resp.body == b"action=verify"
        assert resp.headers["X-Broker"] == "vault"
        assert resp.credentials.access_key_id == "AK"

    def test_error_is_translated_via_provider(self):
        client = StsUtilities(provider="fake-utils", properties={"fail": True})
        with pytest.raises(UnAuthorizedException, match="boom"):
            client.create_signed_auth_request()

    def test_error_preserves_original_cause(self):
        client = StsUtilities(provider="fake-utils", properties={"fail": True})
        with pytest.raises(UnAuthorizedException) as excinfo:
            client.create_signed_auth_request()
        assert isinstance(excinfo.value.__cause__, _SentinelError)

    def test_context_manager_closes_driver(self):
        client = StsUtilities(provider="fake-utils")
        with client as c:
            assert c.provider == "fake-utils"
        assert client._utils.closed is True


class TestSignedAuthRequest:
    """The signed-request value object."""

    def test_frozen_immutability(self):
        req = SignedAuthRequest(
            method="POST",
            url="https://token-service.example/",
            headers={},
            body=b"",
            credentials=StsCredentials(),
        )
        with pytest.raises(AttributeError):
            req.method = "GET"  # type: ignore[misc]

    def test_repr_redacts_replayable_identity_proof(self):
        # The request is a replayable identity proof; its repr must not leak the
        # signed headers or the session token into logs.
        req = SignedAuthRequest(
            method="POST",
            url="https://token-service.example/",
            headers={"Authorization": "SIGNED-abc123", "X-Token": "PROOF-TOKEN"},
            body=b"action=verify",
            credentials=StsCredentials(
                access_key_id="AK",
                access_key_secret="SK",
                security_token="SESSION-TOKEN",
            ),
        )
        text = repr(req)
        assert "SIGNED-abc123" not in text
        assert "PROOF-TOKEN" not in text
        assert "SESSION-TOKEN" not in text
        # Non-sensitive scaffolding stays visible for debugging.
        assert "POST" in text
        assert "token-service.example" in text
        assert "redacted" in text
