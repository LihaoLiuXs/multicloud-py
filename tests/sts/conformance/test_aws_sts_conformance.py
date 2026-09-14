"""AWS STS conformance tests with VCR.py record/replay.

Extends :class:`BaseStsConformance`, supplying an AWS-specific
``Harness`` that wires up the :class:`StsClient`.

Run modes::

    # Record mode (requires AWS credentials)
    pytest tests/sts/conformance/test_aws_sts_conformance.py \\
        --record-mode=rewrite -v

    # Replay mode (no credentials needed, uses cassettes)
    pytest tests/sts/conformance/test_aws_sts_conformance.py \\
        --record-mode=none -v

Note on get_access_token:
    AWS GetSessionToken cannot be called with temporary credentials
    (i.e. when AWS_SESSION_TOKEN is set). The harness detects this
    and marks get_access_token as unsupported in that case.

Note on assume_role_with_web_identity:
    Requires a real OIDC token and a role trust policy configured for
    the identity provider. Set CONFORMANCE_AWS_WEB_IDENTITY_TOKEN and
    CONFORMANCE_AWS_WEB_IDENTITY_ROLE_ARN before recording.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

import pytest
from multicloud.common.exceptions import UnsupportedOperationException
from multicloud.sts.sts_client import StsClient

from tests.sts.conformance.base_sts_conformance import (
    BaseStsConformance,
    Harness,
)

_CASSETTES_DIR = Path(__file__).parent / "cassettes" / "aws"

AWS_REGION = os.environ.get("CONFORMANCE_AWS_REGION", "us-west-2")
AWS_ROLE_ARN = os.environ.get(
    "CONFORMANCE_AWS_ROLE_ARN",
    "arn:aws:iam::123456789012:role/MultiCloudPyTestRole",
)
AWS_WEB_IDENTITY_ROLE_ARN = os.environ.get(
    "CONFORMANCE_AWS_WEB_IDENTITY_ROLE_ARN",
    "arn:aws:iam::123456789012:role/MultiCloudPyWebIdentityTestRole",
)
AWS_WEB_IDENTITY_TOKEN = os.environ.get("CONFORMANCE_AWS_WEB_IDENTITY_TOKEN", "")


class AwsStsHarness(Harness):
    """AWS STS test harness.

    Detects whether session credentials are in use to determine
    if get_access_token (GetSessionToken) is callable.
    """

    def __init__(self) -> None:
        self._client = StsClient(
            provider="aws",
            region=AWS_REGION,
        )
        # AWS GetSessionToken cannot be called with temporary credentials.
        # If AWS_SESSION_TOKEN is set, we have session creds.
        self._has_session_token = bool(os.environ.get("AWS_SESSION_TOKEN"))

    def create_client(self) -> StsClient:
        return self._client

    def get_provider_id(self) -> str:
        return "aws"

    def get_role_name(self) -> str:
        return AWS_ROLE_ARN

    def get_expected_identity_fields(self) -> List[str]:
        # GetCallerIdentity populates all three: UserId, Arn, Account.
        return ["user_id", "cloud_resource_name", "account_id"]

    def expects_access_key_pair(self) -> bool:
        # AssumeRole / GetSessionToken return an AccessKeyId + SecretAccessKey
        # alongside the session token.
        return True

    def supports_get_access_token(self) -> bool:
        # Replay works whenever a cassette exists, regardless of local creds.
        cassette_path = (
            _CASSETTES_DIR / "TestAwsStsConformance.test_get_access_token.yaml"
        )
        if cassette_path.exists():
            return True
        # Record mode (no cassette): GetSessionToken needs long-term
        # (non-session) creds — it fails when AWS_SESSION_TOKEN is set.
        if self._has_session_token:
            return False
        return bool(os.environ.get("AWS_ACCESS_KEY_ID"))

    def supports_web_identity(self) -> bool:
        # Requires both a recorded cassette and a live OIDC token env var.
        # When only a cassette exists (replay mode), the token env var is
        # not needed — VCR replays the recorded exchange. Require the env
        # var only when no cassette is present (i.e. record mode).
        cassette_path = (
            _CASSETTES_DIR
            / "TestAwsStsConformance.test_assume_role_with_web_identity.yaml"
        )
        if not cassette_path.exists():
            # No cassette: record mode requires the real OIDC token.
            return bool(AWS_WEB_IDENTITY_TOKEN)
        return True

    def get_web_identity_role(self) -> str:
        return AWS_WEB_IDENTITY_ROLE_ARN

    def get_web_identity_token(self) -> str:
        return AWS_WEB_IDENTITY_TOKEN

    def close(self) -> None:
        self._client.close()


@pytest.fixture(scope="module")
def harness():
    h = AwsStsHarness()
    yield h
    h.close()


@pytest.fixture(scope="module")
def client(harness):
    return harness.create_client()


@pytest.mark.vcr
class TestAwsStsConformance(BaseStsConformance):
    """AWS STS conformance tests.

    All test methods are inherited from
    :class:`BaseStsConformance`. Cassettes live at
    ``tests/sts/conformance/cassettes/aws/`` and are keyed by
    ``TestAwsStsConformance.<test_method>.yaml``.
    """

    def test_generate_service_account_token_unsupported(self, client: StsClient):
        """AWS does not support generate_service_account_token.

        Verifies that the AWS provider raises UnsupportedOperationException
        rather than silently succeeding or returning garbage credentials.
        No cassette is required — the error is raised before any HTTP call.
        """
        with pytest.raises(UnsupportedOperationException):
            client.generate_service_account_token("any-sa", scopes=["x"])
