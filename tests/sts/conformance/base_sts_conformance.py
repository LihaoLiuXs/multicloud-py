"""Reusable STS conformance test suite.

Defines the conformance tests that every STS provider must pass.
Provider-specific test classes extend ``BaseStsConformance``
and supply a ``client`` fixture that returns a :class:`StsClient`
configured for the provider under test.

Test cases:
  - get_caller_identity
  - assume_role
  - assume_role_requires_role
  - assume_role_rejects_nonpositive_expiration
  - get_access_token
  - get_access_token_with_scopes
  - get_access_token_with_audience
  - get_access_token_rejects_audience_and_scopes
  - get_access_token_rejects_excess_duration
  - assume_role_with_web_identity
  - generate_service_account_token

Assertion philosophy:
  These tests assert *observable properties* of the returned value, not
  merely that a non-null object came back. Since STS is read-only token
  issuance (no create/delete round-trip to verify against), the observable
  properties available are: which identity fields the provider populates
  (declared per-harness), the full shape of the returned credential
  (security token + expiration + optional access-key pair), the audience
  claim actually encoded in an audience-scoped token, and specifically-typed
  exceptions on invalid input.

Class naming notes:
  - ``BaseStsConformance`` ends in ``Conformance`` (not ``Tests``)
    so pytest's default ``python_classes = Test*`` collection rule
    doesn't pick it up. Concrete subclasses must start with ``Test``
    to be collected.
"""

from __future__ import annotations

import base64
import binascii
import json
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

import pytest
from multicloud.common.exceptions import InvalidArgumentException
from multicloud.sts.sts_client import StsClient


def _looks_like_jwt(token: str) -> bool:
    """Whether ``token`` has the three-segment structure of a JWT.

    Used to tell a token that *claims* to be a JWT (and whose claims must
    therefore decode) apart from an opaque token that legitimately isn't one.
    A structural check only — it does not validate the segments' contents.
    """
    return token.count(".") == 2


def _decode_jwt_claims(token: str) -> Optional[dict]:
    """Decode the (unverified) claims payload of a JWT.

    Returns the decoded claims mapping, or ``None`` when the token is not a
    well-formed three-segment JWT *or* its payload cannot be decoded. Callers
    that need to distinguish "not a JWT" from "malformed JWT" should pair this
    with :func:`_looks_like_jwt`. Signature verification is intentionally
    skipped — the tests only need to read the claims the provider encoded
    (e.g. the audience) to prove a request parameter took effect.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded)
    except (binascii.Error, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


class Harness(ABC):
    """Test harness interface that providers implement.

    The harness owns the lifecycle of a :class:`StsClient` instance
    for a single test session. Providers wire up authentication,
    region, etc. inside the harness so tests stay cloud-agnostic.
    """

    @abstractmethod
    def create_client(self) -> StsClient:
        """Return an StsClient configured for the test environment."""
        ...

    @abstractmethod
    def get_provider_id(self) -> str:
        """Return the provider id (e.g. ``"aws"``, ``"gcp"``)."""
        ...

    @abstractmethod
    def get_role_name(self) -> str:
        """Return the role/service account to assume in tests."""
        ...

    def get_expected_identity_fields(self) -> List[str]:
        """Return the ``CallerIdentity`` fields this provider must populate.

        ``get_caller_identity`` returns a cloud-agnostic ``CallerIdentity``
        whose populated fields differ by provider — one provider proves
        identity with an account id + user id, another with a signed token
        in ``cloud_resource_name``. Rather than accept *any* non-empty field
        (which passes even when the wrong field is set), each harness declares
        exactly which field names MUST be non-empty for its provider, letting
        the shared test assert the identity is shaped the way that provider
        is contracted to shape it.

        Field names correspond to ``CallerIdentity`` attributes:
        ``user_id``, ``cloud_resource_name``, ``account_id``. An empty list
        falls back to the weaker "at least one field populated" check.
        """
        return []

    def expects_access_key_pair(self) -> bool:
        """Whether ``assume_role`` credentials carry an access-key pair.

        Some providers return a full access-key-id / secret pair alongside
        the session token; others return only a bearer token and leave the
        key fields empty. When True, the credential-shape assertions also
        require ``access_key_id`` and ``access_key_secret`` to be non-empty.
        """
        return False

    def supports_get_access_token(self) -> bool:
        """Whether this provider supports get_access_token in the test env.

        AWS GetSessionToken cannot be called with temporary (session)
        credentials. Override to return False when only session creds
        are available.
        """
        return True

    def supports_scoped_token(self) -> bool:
        """Whether this provider supports get_access_token with scopes.

        Override and return True (gated on a cassette file) when the
        provider can issue tokens scoped to specific OAuth scopes.
        """
        return False

    def get_token_scopes(self) -> list:
        """Return the OAuth scopes to request for scoped-token tests."""
        return []

    def supports_audience_token(self) -> bool:
        """Whether this provider supports get_access_token with an audience.

        Override and return True (gated on a cassette file) when the
        provider can issue audience-restricted tokens.
        """
        return False

    def get_token_audience(self) -> str:
        """Return the audience string for audience-token tests."""
        return ""

    def supports_duration_guard(self) -> bool:
        """Whether this provider enforces the 3600-second duration cap.

        When True, ``test_get_access_token_rejects_excess_duration`` runs
        a negative test (no cassette required — the error is raised before
        any HTTP call).
        """
        return False

    def supports_web_identity(self) -> bool:
        """Whether this provider supports assume_role_with_web_identity.

        Override and return True (gated on a cassette file and the
        presence of an OIDC token env var) to enable the web-identity test.
        """
        return False

    def get_web_identity_role(self) -> str:
        """Return the role ARN / workload-identity audience for web-identity tests."""
        return ""

    def get_web_identity_token(self) -> str:
        """Return the OIDC/JWT token for web-identity tests."""
        return ""

    def supports_generate_service_account_token(self) -> bool:
        """Whether this provider supports generate_service_account_token.

        Override and return True (gated on a cassette file) when the
        provider can impersonate a service account to generate tokens.
        """
        return False

    def get_target_principal(self) -> str:
        """Return the service account principal for generate_service_account_token."""
        return ""

    def get_sa_token_scopes(self) -> list:
        """Return OAuth scopes for generate_service_account_token tests."""
        return []

    def close(self):  # pragma: no cover - default no-op
        """Clean up resources. Override if needed."""


class BaseStsConformance:
    """Abstract conformance test suite for STS providers.

    Conformance tests are written ONCE here and executed by each
    provider. Provider test classes inherit from this class and supply
    a ``client`` and ``harness`` fixture.
    """

    # get_caller_identity

    def test_get_caller_identity(self, client: StsClient, harness: Harness):
        """Get caller identity populates the fields the provider contracts to.

        Rather than accepting *any* non-empty field, this asserts each field
        the harness declares in ``get_expected_identity_fields`` is populated,
        so a provider that sets the wrong field (or leaves its contracted
        field empty) fails. Harnesses that declare no fields fall back to the
        weaker "at least one field populated" check.
        """
        identity = client.get_caller_identity()
        assert identity is not None, "CallerIdentity must not be None"

        expected_fields = harness.get_expected_identity_fields()
        if expected_fields:
            for field in expected_fields:
                value = getattr(identity, field)
                assert value, (
                    f"CallerIdentity.{field} must be non-empty for this "
                    f"provider; got {value!r}"
                )
        else:
            has_user_id = bool(identity.user_id)
            has_crn = bool(identity.cloud_resource_name)
            has_account = bool(identity.account_id)
            assert has_user_id or has_crn or has_account, (
                "At least one of user_id, cloud_resource_name, or account_id "
                "must be non-empty"
            )

    # assume_role

    def _assert_credentials_shape(
        self, credentials, harness: Harness, *, context: str
    ) -> None:
        """Assert the returned credential is fully shaped, not just non-null.

        Every provider must return a non-empty ``security_token``. When the
        harness declares the provider issues an access-key pair, both key
        fields must also be populated. When an ``expiration`` is present it
        must be a real ``datetime`` (providers that don't surface expiry
        leave it ``None``, which is allowed).
        """
        assert credentials is not None, "StsCredentials must not be None"
        assert (
            credentials.security_token
        ), f"security_token must be non-empty after {context}"
        if harness.expects_access_key_pair():
            assert (
                credentials.access_key_id
            ), f"access_key_id must be non-empty after {context}"
            assert (
                credentials.access_key_secret
            ), f"access_key_secret must be non-empty after {context}"
        if credentials.expiration is not None:
            assert isinstance(
                credentials.expiration, datetime
            ), "expiration must be a datetime when present"

    def test_assume_role(self, client: StsClient, harness: Harness):
        """Assume role returns fully shaped temporary credentials.

        Beyond a non-empty security token, this asserts the whole credential
        shape the provider contracts to return (access-key pair when
        declared, a real ``datetime`` expiration when present).
        """
        role = harness.get_role_name()
        credentials = client.assume_role(
            role,
            session_name="conformance-test-session",
            expiration=3600,
        )
        self._assert_credentials_shape(credentials, harness, context="assuming role")

    def test_assume_role_requires_role(self, client: StsClient, harness: Harness):
        """Assume role rejects an empty role identifier.

        Verifies input validation raises the cloud-agnostic
        InvalidArgumentException before any HTTP call is made — no cassette
        required.
        """
        with pytest.raises(InvalidArgumentException):
            client.assume_role("", session_name="conformance-test-session")

    def test_assume_role_rejects_nonpositive_expiration(
        self, client: StsClient, harness: Harness
    ):
        """Assume role rejects a non-positive expiration.

        Verifies input validation raises the cloud-agnostic
        InvalidArgumentException before any HTTP call is made — no cassette
        required.
        """
        role = harness.get_role_name()
        with pytest.raises(InvalidArgumentException):
            client.assume_role(role, expiration=0)

    # get_access_token

    def test_get_access_token(self, client: StsClient, harness: Harness):
        """Get access token returns valid temporary credentials.

        Verifies that the provider can retrieve a fresh access token.
        Note: AWS requires long-term (non-session) credentials for
        GetSessionToken; test is skipped when only session creds exist.
        """
        if not harness.supports_get_access_token():
            pytest.skip(
                "Skipping: harness does not support get_access_token "
                "(e.g. AWS with session credentials cannot call GetSessionToken)"
            )

        credentials = client.get_access_token()
        self._assert_credentials_shape(credentials, harness, context="get_access_token")

    def test_get_access_token_with_scopes(self, client: StsClient, harness: Harness):
        """Get access token with explicit OAuth scopes returns a valid token.

        Verifies the provider honours the ``scopes`` parameter and returns a
        fully shaped credential (non-empty token; real ``datetime`` expiry
        when present).
        """
        if not harness.supports_scoped_token():
            pytest.skip(
                "Skipping: harness does not support get_access_token with scopes"
            )

        credentials = client.get_access_token(scopes=harness.get_token_scopes())
        self._assert_credentials_shape(
            credentials, harness, context="get_access_token with scopes"
        )

    def test_get_access_token_with_audience(self, client: StsClient, harness: Harness):
        """Get access token with an audience restriction encodes that audience.

        Beyond a non-empty token, this decodes the returned JWT and asserts
        its ``aud`` claim equals the requested audience — proving the
        ``audience`` parameter actually took effect rather than merely that
        some token came back. Providers whose audience-scoped token is not a
        JWT (opaque token, no decodable claims) fall back to the shape check.
        A token that *looks* like a JWT (three segments) but fails to decode
        is treated as a failure, not a fallback — otherwise a provider that
        starts returning a corrupted JWT would silently pass on shape alone.
        """
        if not harness.supports_audience_token():
            pytest.skip(
                "Skipping: harness does not support get_access_token with audience"
            )

        audience = harness.get_token_audience()
        credentials = client.get_access_token(audience=audience)
        self._assert_credentials_shape(
            credentials, harness, context="get_access_token with audience"
        )
        token = credentials.security_token
        claims = _decode_jwt_claims(token)
        if claims is None:
            # An opaque (non-JWT) token can't carry an aud claim; the shape
            # check above is the strongest available assertion. But a token
            # with JWT structure whose payload won't decode is a real defect.
            assert not _looks_like_jwt(token), (
                "Audience-scoped token has JWT structure but its claims payload "
                "could not be decoded; expected a well-formed JWT encoding the "
                f"requested audience {audience!r}"
            )
            return
        token_aud = claims.get("aud")
        # ``aud`` may be a string or a list of strings per RFC 7519.
        audiences = token_aud if isinstance(token_aud, list) else [token_aud]
        assert audience in audiences, (
            f"Audience-scoped token must encode the requested audience "
            f"{audience!r}; token aud claim was {token_aud!r}"
        )

    def test_get_access_token_rejects_audience_and_scopes(
        self, client: StsClient, harness: Harness
    ):
        """Get access token rejects audience and scopes together.

        ``audience`` and ``scopes`` are mutually exclusive; requesting both
        must raise the cloud-agnostic InvalidArgumentException before any HTTP
        call is made — no cassette required.
        """
        with pytest.raises(InvalidArgumentException):
            client.get_access_token(audience="some-audience", scopes=["some-scope"])

    def test_get_access_token_rejects_excess_duration(
        self, client: StsClient, harness: Harness
    ):
        """Get access token rejects duration_seconds > 3600.

        Verifies that the provider raises InvalidArgumentException when
        the caller requests a token lifetime exceeding the maximum allowed
        value. No cassette is required — the error is raised before any
        HTTP call is made.
        """
        if not harness.supports_duration_guard():
            pytest.skip("Skipping: harness does not enforce duration_seconds cap")

        with pytest.raises(InvalidArgumentException):
            client.get_access_token(duration_seconds=3601)

    # assume_role_with_web_identity

    def test_assume_role_with_web_identity(self, client: StsClient, harness: Harness):
        """Assume role with a web identity token returns valid credentials.

        Verifies that the provider exchanges an OIDC/JWT token for
        temporary credentials with a non-empty security_token.
        """
        if not harness.supports_web_identity():
            pytest.skip(
                "Skipping: harness does not support assume_role_with_web_identity "
                "(cassette not recorded or OIDC token env var not set)"
            )

        credentials = client.assume_role_with_web_identity(
            harness.get_web_identity_role(),
            harness.get_web_identity_token(),
            session_name="conformance-test-session",
        )
        self._assert_credentials_shape(
            credentials, harness, context="assume_role_with_web_identity"
        )

    # generate_service_account_token

    def test_generate_service_account_token(self, client: StsClient, harness: Harness):
        """Generate service account token returns valid credentials.

        Verifies that the provider can impersonate a target service
        account principal and return a token with a non-empty
        security_token.
        """
        if not harness.supports_generate_service_account_token():
            pytest.skip(
                "Skipping: harness does not support generate_service_account_token "
                "(cassette not recorded or feature unavailable for this provider)"
            )

        credentials = client.generate_service_account_token(
            harness.get_target_principal(),
            scopes=harness.get_sa_token_scopes(),
        )
        self._assert_credentials_shape(
            credentials, harness, context="generate_service_account_token"
        )
