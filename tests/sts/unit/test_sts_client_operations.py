"""Unit tests for StsClient operation forwarding."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from multicloud.common.exceptions import UnsupportedOperationException
from multicloud.sts.sts_client import StsClient
from multicloud.sts.sts_client.driver import CallerIdentity, StsCredentials


@pytest.fixture
def mock_sts_driver():
    """Patch the provider's underlying SDK session so StsClient uses mocked I/O."""
    with patch(
        "multicloud.aws_common.aws_credentials.boto3.Session"
    ) as mock_session_cls:
        session = MagicMock()
        sts_client = MagicMock()
        session.client.return_value = sts_client
        mock_session_cls.return_value = session
        yield session, sts_client


class TestStsClientOperations:

    def test_get_caller_identity(self, mock_sts_driver):
        _, sts_client = mock_sts_driver
        sts_client.get_caller_identity.return_value = {
            "UserId": "AIDACKCEVSQ6C2EXAMPLE",
            "Account": "123456789012",
            "Arn": "arn:aws:iam::123456789012:user/devops-bot",
        }
        client = StsClient(provider="aws", region="us-east-1")
        result = client.get_caller_identity()
        assert result.user_id == "AIDACKCEVSQ6C2EXAMPLE"
        assert result.account_id == "123456789012"
        assert result.cloud_resource_name == "arn:aws:iam::123456789012:user/devops-bot"

    def test_assume_role_returns_flat_credentials(self, mock_sts_driver):
        _, sts_client = mock_sts_driver
        sts_client.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "ASIATEMP123",
                "SecretAccessKey": "tempSecret456",
                "SessionToken": "sessToken789",
                "Expiration": datetime(2026, 6, 5, 23, 0, 0),
            },
        }
        client = StsClient(provider="aws", region="us-east-1")
        result = client.assume_role(
            "arn:aws:iam::123456789012:role/MyRole",
            session_name="my-session",
        )
        assert isinstance(result, StsCredentials)
        assert result.access_key_id == "ASIATEMP123"
        assert result.access_key_secret == "tempSecret456"
        assert result.security_token == "sessToken789"
        # Verify the correct provider call was made
        sts_client.assume_role.assert_called_once_with(
            RoleArn="arn:aws:iam::123456789012:role/MyRole",
            RoleSessionName="my-session",
            DurationSeconds=3600,
        )

    def test_get_access_token(self, mock_sts_driver):
        _, sts_client = mock_sts_driver
        sts_client.get_session_token.return_value = {
            "Credentials": {
                "AccessKeyId": "SESS_AK",
                "SecretAccessKey": "SESS_SK",
                "SessionToken": "SESS_TOKEN",
                "Expiration": datetime(2026, 6, 6, 0, 0, 0),
            },
        }
        client = StsClient(provider="aws", region="us-east-1")
        result = client.get_access_token()
        assert isinstance(result, StsCredentials)
        assert result.access_key_id == "SESS_AK"
        assert result.security_token == "SESS_TOKEN"

    def test_get_access_token_audience_forwarded_to_aws(self, mock_sts_driver):
        """audience is forwarded to the driver; AWS ignores it (scope-less)."""
        _, sts_client = mock_sts_driver
        sts_client.get_session_token.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
            },
        }
        client = StsClient(provider="aws", region="us-east-1")
        result = client.get_access_token(audience="https://example.com")
        assert result.security_token == "ST"
        sts_client.get_session_token.assert_called_once_with()

    def test_get_access_token_audience_and_scopes_raises(self, mock_sts_driver):
        """audience + scopes raises InvalidArgumentException at the driver level."""
        from multicloud.common.exceptions import InvalidArgumentException

        client = StsClient(provider="aws", region="us-east-1")
        with pytest.raises(InvalidArgumentException, match="mutually exclusive"):
            client.get_access_token(
                audience="https://example.com",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

    def test_generate_service_account_token_raises_for_aws(self, mock_sts_driver):
        """generate_service_account_token is not supported for AWS."""
        client = StsClient(provider="aws", region="us-east-1")
        with pytest.raises(
            UnsupportedOperationException, match="not supported for AWS"
        ):
            client.generate_service_account_token(
                "sa@project.iam.gserviceaccount.com",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

    def test_assume_role_with_web_identity(self, mock_sts_driver):
        _, sts_client = mock_sts_driver
        sts_client.assume_role_with_web_identity.return_value = {
            "Credentials": {
                "AccessKeyId": "ASIA_WI",
                "SecretAccessKey": "WI_SK",
                "SessionToken": "WI_ST",
                "Expiration": datetime(2026, 6, 6, 1, 0, 0),
            },
        }
        client = StsClient(provider="aws", region="us-east-1")
        result = client.assume_role_with_web_identity(
            "arn:aws:iam::123456789012:role/WebRole",
            "oidc.jwt.token",
            session_name="federated",
        )
        assert isinstance(result, StsCredentials)
        assert result.access_key_id == "ASIA_WI"
        assert result.security_token == "WI_ST"
        sts_client.assume_role_with_web_identity.assert_called_once_with(
            RoleArn="arn:aws:iam::123456789012:role/WebRole",
            RoleSessionName="federated",
            WebIdentityToken="oidc.jwt.token",
            DurationSeconds=3600,
        )

    def test_close_idempotent(self, mock_sts_driver):
        _, sts_client = mock_sts_driver
        sts_client.close = MagicMock()
        client = StsClient(provider="aws", region="us-east-1")
        client.close()
        client.close()
        assert sts_client.close.call_count == 2


class TestStsClientCredentialsOverrider:

    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_credentials_overrider_passed_to_provider(self, mock_session_cls):
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
            StsCredentials,
        )

        session = MagicMock()
        session.client.return_value = MagicMock()
        mock_session_cls.return_value = session

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                access_key_id="CLIENT_AK",
                access_key_secret="CLIENT_SK",
                security_token="CLIENT_ST",
            ),
        )
        client = StsClient(
            provider="aws",
            region="us-east-1",
            credentials_overrider=overrider,
        )
        mock_session_cls.assert_called_once_with(
            aws_access_key_id="CLIENT_AK",
            aws_secret_access_key="CLIENT_SK",
            aws_session_token="CLIENT_ST",
            region_name="us-east-1",
        )
        assert client.provider == "aws"
