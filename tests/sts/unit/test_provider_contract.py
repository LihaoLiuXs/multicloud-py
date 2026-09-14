"""Contract tests — verify every registered STS provider accepts client kwargs.

These tests catch the class of bug where StsClient passes a kwarg
(like endpoint_url) to the provider constructor, but a provider forgets
to accept it. By testing ALL registered providers with the FULL set of
kwargs the client can pass, we prevent silent TypeError crashes that
only surface when a specific provider + kwarg combination is used.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from multicloud.sts.sts_client.driver import STS_REGISTRY

# All kwargs that StsClient.__init__ can pass to providers.
# Keep this in sync with StsClient.__init__ — if a new kwarg is
# added there, add it here too.
_CLIENT_KWARGS = {
    "region": "us-west-2",
    "endpoint_url": "http://localhost:9999",
}


@pytest.fixture(autouse=True)
def _mock_gcp_auth():
    """Prevent GcpSts from needing real GCP credentials."""
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.token = "ya29.contract-test"
    with patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.default") as mock_default:
        mock_default.return_value = (mock_creds, "contract-test-project")
        yield


@pytest.mark.parametrize("provider_id", ["aws", "gcp"])
class TestStsProviderContract:
    """Every registered STS provider must accept all client kwargs."""

    def test_accepts_all_client_kwargs(self, provider_id):
        """Provider constructor must not TypeError on any kwarg the client sends."""
        cls = STS_REGISTRY.get(provider_id)
        # This would raise TypeError if the provider doesn't accept a kwarg
        instance = cls(**_CLIENT_KWARGS)
        assert instance.region == "us-west-2"

    def test_accepts_minimal_kwargs(self, provider_id):
        """Provider must also work with no kwargs (all optional)."""
        cls = STS_REGISTRY.get(provider_id)
        instance = cls()
        assert instance.region is None
