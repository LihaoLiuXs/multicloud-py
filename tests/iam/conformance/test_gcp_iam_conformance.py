"""GCP IAM conformance tests with VCR.py record/replay.

Extends :class:`BaseIamConformance`, supplying a GCP-specific
``Harness`` that wires up the :class:`IamClient`.

Run modes::

    # Record mode (requires GCP service account credentials)
    pytest tests/iam/conformance/test_gcp_iam_conformance.py \\
        --record-mode=rewrite -v

    # Replay mode (no credentials needed, uses cassettes)
    pytest tests/iam/conformance/test_gcp_iam_conformance.py \\
        --record-mode=none -v

Note on GCP:
    - The tenant id is the project resource name (``projects/<id>``).
    - Policy operations are project-scoped; the policy name is the role
      string (e.g. ``"roles/storage.objectViewer"``) the provider matches
      against the project's IAM bindings.
    - Service-account names are constrained (6-30 chars, lowercase, start
      with a letter); the create/get/delete base name and its per-test
      suffixes must satisfy that at record time.
"""

from __future__ import annotations

import os
import time
from typing import List
from unittest.mock import patch

import pytest
from multicloud.iam.iam_client import IamClient

from tests.gcp_common.mock_gcp_credentials import make_mock_credentials
from tests.iam.conformance.base_iam_conformance import (
    BaseIamConformance,
    Harness,
)
from tests.iam.conformance.grpc_clients import GcpGrpcClients, build_clients

GCP_PROJECT = os.environ.get("CONFORMANCE_GCP_PROJECT", "projects/example-project")
GCP_REGION = os.environ.get("CONFORMANCE_GCP_REGION", "us-west1")
# Base service-account id the create/get/delete/policy tests provision.
# Must be 6-30 chars, lowercase, start with a letter; the longest derived
# name (base + "-lifecycle") must stay within 30 chars.
GCP_TEST_IDENTITY_NAME = os.environ.get(
    "CONFORMANCE_GCP_TEST_IDENTITY_NAME", "mcp-conf-sa"
)
GCP_TRUSTED_PRINCIPAL = os.environ.get(
    "CONFORMANCE_GCP_TRUSTED_PRINCIPAL",
    "test-service-account@example-project.iam.gserviceaccount.com",
)
# Seconds to wait (record mode only) for a freshly created service account
# to become visible to a follow-up get / policy write.
GCP_SETTLE_SECONDS = float(os.environ.get("CONFORMANCE_GCP_SETTLE_SECONDS", "10"))


class GcpIamHarness(Harness):
    """GCP IAM test harness.

    The GCP IAM Admin client is gRPC-only, so its service-account
    operations are recorded/replayed through an injected gRPC interceptor
    (``grpc_clients.build_clients``); the project policy operations ride a
    REST client recorded by VCR. Both pre-built clients are injected into
    the provider so the same harness serves record and replay modes.

    In record mode: real service account credentials (ADC) back both
    clients. In replay mode: the gRPC channel is never dialed and the REST
    client uses anonymous credentials, so no credentials are required.
    """

    def __init__(self, grpc_clients: GcpGrpcClients, recording: bool) -> None:
        self._grpc_clients = grpc_clients
        self._recording = recording
        self._client = IamClient(
            provider="gcp",
            region=None,
            properties={
                "iam_client": grpc_clients.iam_client,
                "projects_client": grpc_clients.projects_client,
            },
        )

    @property
    def interceptor(self):
        """Expose the gRPC interceptor so tests can scope its fixtures."""
        return self._grpc_clients.interceptor

    def create_client(self) -> IamClient:
        return self._client

    def get_provider_id(self) -> str:
        return "gcp"

    def get_tenant_id(self) -> str:
        return GCP_PROJECT

    def get_region(self):
        return GCP_REGION

    def get_test_identity_name(self) -> str:
        # GCP service-account ids must be 6-30 chars, lowercase, start with
        # a letter; the per-test suffixes keep every derived name valid.
        return GCP_TEST_IDENTITY_NAME

    def get_trusted_principal(self) -> str:
        return GCP_TRUSTED_PRINCIPAL

    def get_test_policy_name(self) -> str:
        # Policy operations are project-scoped; the policy name is the role
        # string the provider matches against the project's IAM bindings.
        return "roles/storage.objectViewer"

    def get_test_policy_actions(self) -> List[str]:
        return ["storage:GetObject", "storage:PutObject"]

    def get_expected_policy_detail_tokens(self) -> List[str]:
        # get_inline_policy_details returns a JSON object carrying the matched
        # role binding; assert it reflects the role the test policy maps to.
        return ["roles/storage.objectViewer"]

    def settle(self) -> None:
        # A freshly created service account is not immediately visible to a
        # follow-up get or policy write; wait for it to propagate. Only in
        # record mode (a real backend) — in replay there is no backend, so
        # the canned/cassette responses return instantly and this is a no-op.
        if self._recording:
            time.sleep(GCP_SETTLE_SECONDS)

    def close(self) -> None:
        self._client.close()


@pytest.fixture(scope="module", autouse=True)
def mock_gcp_auth(request):
    """Patch credential discovery in replay mode to bypass ADC lookup.

    The clients are pre-built with anonymous/injected credentials, so no
    code path should reach default credential discovery in replay. This is
    a defensive guard: if any path ever lazily calls credential discovery,
    replay returns a static mock instead of failing CI on a missing ADC.
    In record mode the patch is NOT applied — real credentials are used.
    """
    record_mode = request.config.getoption("--record-mode", default="none")
    if record_mode == "none":
        fake_creds = make_mock_credentials()
        with patch("google.auth.default", return_value=(fake_creds, "fake-project")):
            yield
    else:
        yield


@pytest.fixture(scope="module")
def harness(request, mock_gcp_auth):
    record_mode = request.config.getoption("--record-mode") or "none"
    recording = record_mode != "none"
    h = GcpIamHarness(build_clients(record_mode), recording=recording)
    yield h
    h.close()


@pytest.fixture(scope="module")
def client(harness):
    return harness.create_client()


@pytest.fixture(autouse=True)
def _scope_grpc_fixtures(request, harness):
    """Scope the gRPC interceptor's fixtures to the running test.

    Each test's service-account calls record into (and replay from) files
    named ``<TestClass>.<test_method>.<Method>.<n>.json`` (mirroring the VCR
    cassette naming) so tests don't cross-contaminate each other's fixtures.
    """
    cls = getattr(request, "cls", None)
    class_name = cls.__name__ if cls is not None else request.module.__name__
    method_name = request.node.originalname or request.node.name
    harness.interceptor.set_test(f"{class_name}.{method_name}")
    yield


@pytest.mark.vcr
class TestGcpIamConformance(BaseIamConformance):
    """GCP IAM conformance tests.

    All test methods are inherited from :class:`BaseIamConformance`.
    Cassettes live at ``tests/iam/conformance/cassettes/gcp/`` and are
    keyed by ``TestGcpIamConformance.<test_method>.yaml``.
    """

    pass
