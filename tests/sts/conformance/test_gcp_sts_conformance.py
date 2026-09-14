"""GCP STS conformance tests with VCR.py record/replay.

Extends :class:`BaseStsConformance`, supplying a GCP-specific
``Harness`` that wires up the :class:`StsClient`.

Run modes::

    # Record mode (requires GCP service account credentials)
    pytest tests/sts/conformance/test_gcp_sts_conformance.py \\
        --record-mode=rewrite -v

    # Replay mode (no credentials needed, uses cassettes)
    pytest tests/sts/conformance/test_gcp_sts_conformance.py \\
        --record-mode=none -v

Note on GCP:
    - get_caller_identity requires service account credentials
      (not user OAuth/gcloud auth)
    - assume_role targets a service account email for impersonation
    - get_access_token refreshes the current credential's OAuth token
    - assume_role_with_web_identity uses Workload Identity Federation
      and requires a trust configuration in the GCP project
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
from multicloud.sts.sts_client import StsClient

from tests.gcp_common.mock_gcp_credentials import make_mock_impersonated_credentials
from tests.sts.conformance.base_sts_conformance import (
    BaseStsConformance,
    Harness,
)

_CASSETTES_DIR = Path(__file__).parent / "cassettes" / "gcp"

GCP_SERVICE_ACCOUNT = os.environ.get(
    "CONFORMANCE_GCP_SERVICE_ACCOUNT",
    "test-service-account@example-project.iam.gserviceaccount.com",
)

GCP_TOKEN_AUDIENCE = os.environ.get(
    "CONFORMANCE_GCP_TOKEN_AUDIENCE",
    "https://example-project.iam.gserviceaccount.com",
)

GCP_WEB_IDENTITY_ROLE = os.environ.get(
    "CONFORMANCE_GCP_WEB_IDENTITY_ROLE",
    "//iam.googleapis.com/projects/example-project/locations/global/"
    "workloadIdentityPools/conformance-pool/providers/conformance-provider",
)

GCP_WEB_IDENTITY_TOKEN = os.environ.get("CONFORMANCE_GCP_WEB_IDENTITY_TOKEN", "")


# Harness


class GcpStsHarness(Harness):
    """GCP STS test harness.

    In record mode: uses real GCP service account credentials (ADC).
    In replay mode: VCR.py intercepts all HTTP calls.
    """

    def __init__(self) -> None:
        self._client = StsClient(
            provider="gcp",
            region=None,
        )

    def create_client(self) -> StsClient:
        return self._client

    def get_provider_id(self) -> str:
        return "gcp"

    def get_role_name(self) -> str:
        return GCP_SERVICE_ACCOUNT

    def get_expected_identity_fields(self) -> List[str]:
        # Identity is proved by a signed ID token (JWT) returned in
        # cloud_resource_name; user_id and account_id are intentionally empty.
        return ["cloud_resource_name"]

    def supports_get_access_token(self) -> bool:
        # Replay works whenever a cassette exists (mock_gcp_auth stubs ADC).
        # In record mode (no cassette), real service-account creds are needed.
        cassette_path = (
            _CASSETTES_DIR / "TestGcpStsConformance.test_get_access_token.yaml"
        )
        if not cassette_path.exists():
            return bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
        return True

    def supports_scoped_token(self) -> bool:
        cassette_path = (
            _CASSETTES_DIR
            / "TestGcpStsConformance.test_get_access_token_with_scopes.yaml"
        )
        if not cassette_path.exists():
            return bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
        return True

    def get_token_scopes(self) -> list:
        return ["https://www.googleapis.com/auth/cloud-platform"]

    def supports_audience_token(self) -> bool:
        cassette_path = (
            _CASSETTES_DIR
            / "TestGcpStsConformance.test_get_access_token_with_audience.yaml"
        )
        if not cassette_path.exists():
            return bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
        return True

    def get_token_audience(self) -> str:
        return GCP_TOKEN_AUDIENCE

    def supports_duration_guard(self) -> bool:
        # GCP does not enforce a client-side duration_seconds cap;
        # large values are passed through to the provider. No guard test.
        return False

    def supports_web_identity(self) -> bool:
        # Requires both a recorded cassette and a live OIDC token env var.
        # When only a cassette exists (replay mode), the token env var is
        # not needed — VCR replays the recorded exchange. Require the env
        # var only when no cassette is present (i.e. record mode).
        cassette_path = (
            _CASSETTES_DIR
            / "TestGcpStsConformance.test_assume_role_with_web_identity.yaml"
        )
        if not cassette_path.exists():
            return bool(GCP_WEB_IDENTITY_TOKEN)
        return True

    def get_web_identity_role(self) -> str:
        return GCP_WEB_IDENTITY_ROLE

    def get_web_identity_token(self) -> str:
        return GCP_WEB_IDENTITY_TOKEN

    def supports_generate_service_account_token(self) -> bool:
        cassette_path = (
            _CASSETTES_DIR
            / "TestGcpStsConformance.test_generate_service_account_token.yaml"
        )
        if not cassette_path.exists():
            return bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
        return True

    def get_target_principal(self) -> str:
        return GCP_SERVICE_ACCOUNT

    def get_sa_token_scopes(self) -> list:
        return ["https://www.googleapis.com/auth/cloud-platform"]

    def close(self) -> None:
        self._client.close()


@pytest.fixture(scope="module", autouse=True)
def mock_gcp_auth(request):
    """Patch google.auth.default() in replay mode to bypass ADC lookup.

    In record mode the patch is NOT applied — real credentials are used.
    """
    record_mode = request.config.getoption("--record-mode", default="none")
    if record_mode == "none":
        fake_creds = make_mock_impersonated_credentials(
            target_principal=GCP_SERVICE_ACCOUNT,
        )
        with patch("google.auth.default", return_value=(fake_creds, "fake-project")):
            yield
    else:
        yield


@pytest.fixture(scope="module")
def harness(mock_gcp_auth):
    h = GcpStsHarness()
    yield h
    h.close()


@pytest.fixture(scope="module")
def client(harness):
    return harness.create_client()


@pytest.mark.vcr
class TestGcpStsConformance(BaseStsConformance):
    """GCP STS conformance tests.

    All test methods are inherited from
    :class:`BaseStsConformance`. Cassettes live at
    ``tests/sts/conformance/cassettes/gcp/`` and are keyed by
    ``TestGcpStsConformance.<test_method>.yaml``.
    """

    pass
