"""AWS IAM conformance tests with VCR.py record/replay.

Extends :class:`BaseIamConformance`, supplying an AWS-specific
``Harness`` that wires up the :class:`IamClient`.

Run modes::

    # Record mode (requires AWS credentials)
    pytest tests/iam/conformance/test_aws_iam_conformance.py \\
        --record-mode=rewrite -v

    # Replay mode (no credentials needed, uses cassettes)
    pytest tests/iam/conformance/test_aws_iam_conformance.py \\
        --record-mode=none -v

Defaults for the environment-specific names match the established
integration-test fixtures and can be overridden via environment
variables for a different recording account.
"""

from __future__ import annotations

import os
from typing import List

import pytest
from multicloud.iam.iam_client import IamClient

from tests.iam.conformance.base_iam_conformance import (
    BaseIamConformance,
    Harness,
)

AWS_REGION = os.environ.get("CONFORMANCE_AWS_REGION", "us-west-2")
AWS_ACCOUNT_ID = os.environ.get("CONFORMANCE_AWS_ACCOUNT_ID", "123456789012")
AWS_TRUSTED_PRINCIPAL = os.environ.get(
    "CONFORMANCE_AWS_TRUSTED_PRINCIPAL",
    f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/MultiCloudPyTestRole",
)
AWS_POLICY_RESOURCE = os.environ.get(
    "CONFORMANCE_AWS_POLICY_RESOURCE",
    "arn:aws:s3:::multicloudj-iam-it-test-bucket/*",
)


class AwsIamHarness(Harness):
    """AWS IAM test harness.

    In record mode: uses real AWS credentials (system default discovery).
    In replay mode: VCR.py intercepts all HTTP calls.
    """

    def __init__(self) -> None:
        self._client = IamClient(provider="aws", region=AWS_REGION)

    def create_client(self) -> IamClient:
        return self._client

    def get_provider_id(self) -> str:
        return "aws"

    def get_tenant_id(self) -> str:
        return AWS_ACCOUNT_ID

    def get_region(self):
        return AWS_REGION

    def get_test_identity_name(self) -> str:
        return "MultiCloudPyTestRole"

    def get_trusted_principal(self) -> str:
        return AWS_TRUSTED_PRINCIPAL

    def get_test_policy_name(self) -> str:
        return "TestPolicy"

    def get_test_policy_actions(self) -> List[str]:
        return ["storage:GetObject", "storage:PutObject"]

    def get_policy_version(self) -> str:
        return "2012-10-17"

    def get_test_policy_resource(self):
        return AWS_POLICY_RESOURCE

    def get_expected_policy_detail_tokens(self) -> List[str]:
        # get_inline_policy_details returns the full IAM policy document JSON;
        # assert it carries the granted effect and the translated storage
        # actions from the attached policy.
        return ["Allow", "s3:GetObject", "s3:PutObject"]

    def close(self) -> None:
        self._client.close()


@pytest.fixture(scope="module")
def harness():
    h = AwsIamHarness()
    yield h
    h.close()


@pytest.fixture(scope="module")
def client(harness):
    return harness.create_client()


@pytest.mark.vcr
class TestAwsIamConformance(BaseIamConformance):
    """AWS IAM conformance tests.

    All test methods are inherited from :class:`BaseIamConformance`.
    Cassettes live at ``tests/iam/conformance/cassettes/aws/`` and are
    keyed by ``TestAwsIamConformance.<test_method>.yaml``.
    """

    pass
