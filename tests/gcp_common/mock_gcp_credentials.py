"""Mock GCP credentials for conformance test replay mode.

- Pre-set static token with far-future expiry (never triggers refresh).
- ``refresh()`` is a safe no-op that returns the same static token.
- Can sign, so URL-signing paths work offline (see ``MockGcpCredentials``).

Usage::

    from tests.gcp_common.mock_gcp_credentials import (
        make_mock_credentials,
        make_mock_impersonated_credentials,
    )

    fake_creds = make_mock_credentials()                          # blob tests
    fake_creds = make_mock_impersonated_credentials(principal)    # STS tests
"""

from __future__ import annotations

import hashlib
from datetime import datetime

import google.auth.credentials
import google.auth.impersonated_credentials
import google.oauth2.credentials

_MOCK_TOKEN = "mock-gcp-oauth2-token"
_MOCK_IMPERSONATED_TOKEN = "mock-gcp-impersonated-token"
# Year 2099 — must be a naive datetime (no tzinfo) because
# google.auth._helpers.utcnow() returns naive datetimes internally;
# mixing tz-aware with naive raises TypeError on comparison.
_FAR_FUTURE_EXPIRY = datetime(2099, 1, 1)
_MOCK_SERVICE_ACCOUNT = "mock-service-account@mock-project.iam.gserviceaccount.com"


class MockGcpCredentials(
    google.oauth2.credentials.Credentials,
    google.auth.credentials.Signing,
):
    """Mock OAuth2 credentials with a no-op refresh, able to sign.

    Signing capability is required for URL-signing paths. Real keyless
    credentials sign by calling the IAM ``signBlob`` API, but that host is on
    the recorder's ignore list and so is never captured, which would leave
    replay with no way to sign at all.

    These model *keyless* credentials -- impersonated, ADC, or GCE metadata --
    which is what production resolves in practice, and two details follow from
    that:

    ``service_account_email`` is exposed, not just ``signer_email``. The storage
    library only takes its IAM-signing branch when *both* an access token and
    that attribute are present; withhold it and the library quietly falls back
    to local signing instead -- the opposite branch from the one production
    takes, which leaves the keyless path untested. Tests keep that branch
    offline by stubbing the library's ``_sign_message``, not by withholding the
    email.

    ``sign_bytes`` returns a digest rather than real RSA. Tests assert the
    structure of a signed URL and never dereference it, so the bytes need only
    be deterministic, not verifiable. Pass ``allow_local_signing=False`` to make
    it refuse instead, modelling the absent private key faithfully -- see
    :func:`make_mock_credentials` for when that is worth doing.
    """

    def __init__(
        self, token: str = _MOCK_TOKEN, *, allow_local_signing: bool = True
    ) -> None:
        super().__init__(token=token)
        self.expiry = _FAR_FUTURE_EXPIRY
        self._allow_local_signing = allow_local_signing

    def refresh(self, request) -> None:  # noqa: ARG002
        """No-op refresh — always returns the same static token."""
        self.token = self.token or _MOCK_TOKEN

    def with_quota_project(self, quota_project_id):
        return self

    @property
    def signer_email(self) -> str:
        """Identity the signature is attributed to."""
        return _MOCK_SERVICE_ACCOUNT

    @property
    def service_account_email(self) -> str:
        """Identity the IAM signing branch is keyed on."""
        return _MOCK_SERVICE_ACCOUNT

    @property
    def signer(self) -> "MockGcpCredentials":
        """This object signs on its own behalf."""
        return self

    def sign_bytes(self, message: bytes) -> bytes:
        """Return a deterministic stand-in for an RSA signature.

        Refuses when local signing is disallowed: keyless credentials have no
        private key, so reaching here means the code under test chose local
        signing where production would sign via IAM signBlob.
        """
        if not self._allow_local_signing:
            raise NotImplementedError(
                "MockGcpCredentials was built without local signing, modelling "
                "keyless credentials that have no private key. Reaching "
                "sign_bytes means the code under test chose local signing where "
                "production would sign via IAM signBlob."
            )
        return hashlib.sha256(message).digest()


class MockImpersonatedCredentials(google.auth.impersonated_credentials.Credentials):
    """Mock ImpersonatedCredentials with a no-op refresh.

    Required for STS because ``IDTokenCredentials.__init__`` checks
    ``isinstance(target_credentials, impersonated_credentials.Credentials)``.

    Constructor signature must match the parent exactly — ``_make_copy()``
    calls ``self.__class__(source_credentials, target_principal=..., ...)``.
    """

    def __init__(
        self,
        source_credentials=None,
        target_principal: str = _MOCK_SERVICE_ACCOUNT,
        target_scopes=None,
        delegates=None,
        subject=None,
        lifetime=3600,
        quota_project_id=None,
        iam_endpoint_override=None,
        trust_boundary=None,
    ) -> None:
        if source_credentials is None:
            source_credentials = MockGcpCredentials()
        if target_scopes is None:
            target_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        super().__init__(
            source_credentials=source_credentials,
            target_principal=target_principal,
            target_scopes=target_scopes,
            delegates=delegates,
            subject=subject,
            lifetime=lifetime,
            quota_project_id=quota_project_id,
            iam_endpoint_override=iam_endpoint_override,
            trust_boundary=trust_boundary,
        )
        self.token = _MOCK_IMPERSONATED_TOKEN
        self.expiry = _FAR_FUTURE_EXPIRY

    def refresh(self, request) -> None:  # noqa: ARG002
        """No-op refresh — always returns the same static token."""
        self.token = self.token or _MOCK_IMPERSONATED_TOKEN


def make_mock_credentials(*, allow_local_signing: bool = True) -> MockGcpCredentials:
    """Create mock GCP credentials for blob/simple operations.

    Args:
        allow_local_signing: When ``False``, ``sign_bytes`` raises instead of
            returning a digest. Worth doing for a suite that specifically covers
            URL signing: production resolves keyless credentials and signs via
            IAM, and a mock that can also sign locally lets *either* branch
            succeed, so a break in the keyless path goes unnoticed. Such a suite
            must stub the library's ``_sign_message`` to stay offline. Left
            enabled by default because suites that merely need credentials to
            exist can reach local signing incidentally, and raising there sends
            the auth libraries into a retry loop that stalls the run.
    """
    return MockGcpCredentials(allow_local_signing=allow_local_signing)


def make_mock_impersonated_credentials(
    target_principal: str = _MOCK_SERVICE_ACCOUNT,
    target_scopes: list[str] | None = None,
) -> MockImpersonatedCredentials:
    """Create mock ImpersonatedCredentials for STS operations."""
    return MockImpersonatedCredentials(
        target_principal=target_principal,
        target_scopes=target_scopes,
    )
