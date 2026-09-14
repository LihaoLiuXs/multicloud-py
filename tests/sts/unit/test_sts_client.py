"""Unit tests for :class:`StsClient` — the user-facing STS client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from multicloud.common.exceptions import SubstrateSdkException, UnAuthorizedException
from multicloud.sts.sts_aws import AwsSts
from multicloud.sts.sts_client import StsClient
from multicloud.sts.sts_gcp.gcp_sts import GcpSts


class TestStsClientConstruction:

    def test_aws_creates_aws_sts_internally(self):
        client = StsClient(provider="aws", region="us-west-2")
        assert client.provider == "aws"
        assert client.region == "us-west-2"
        assert isinstance(client._sts, AwsSts)

    def test_unknown_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="azure"):
            StsClient(provider="azure")

    def test_provider_is_keyword_only(self):
        with pytest.raises(TypeError):
            StsClient("aws")  # type: ignore[misc]

    def test_endpoint_forwarded_to_aws_provider(self):
        """endpoint kwarg is translated to endpoint_url for the provider."""
        client = StsClient(
            provider="aws", region="us-west-2", endpoint="http://localhost:4566"
        )
        assert isinstance(client._sts, AwsSts)
        # boto3 stores the endpoint on the client's meta
        assert "localhost:4566" in client._sts._sts.meta.endpoint_url

    def test_endpoint_forwarded_to_gcp_provider(self):
        """endpoint kwarg must not crash GcpSts (regression test).

        Previously GcpSts.__init__ did not accept endpoint_url, causing
        a TypeError when StsClient passed it through.
        """
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.token = "ya29.test"
        with patch(
            "multicloud.sts.sts_gcp.gcp_sts.google.auth.default"
        ) as mock_default:
            mock_default.return_value = (mock_creds, "test-project")
            client = StsClient(
                provider="gcp", region="us-central1", endpoint="https://custom-endpoint"
            )
        assert isinstance(client._sts, GcpSts)
        assert client.provider == "gcp"

    def test_gcp_construction_without_endpoint(self):
        """GCP still works fine when endpoint is omitted (common case)."""
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.token = "ya29.test"
        with patch(
            "multicloud.sts.sts_gcp.gcp_sts.google.auth.default"
        ) as mock_default:
            mock_default.return_value = (mock_creds, "test-project")
            client = StsClient(provider="gcp", region="us-central1")
        assert isinstance(client._sts, GcpSts)
        assert client.region == "us-central1"


class TestStsClientLifecycle:

    def test_context_manager(self):
        with StsClient(provider="aws", region="us-west-2") as c:
            assert c.provider == "aws"


class TestStsClientErrorTranslation:
    """StsClient is the single translation point: native errors become SubstrateSdkException."""

    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_aws_get_caller_identity_translates_client_error(self, mock_session_cls):
        boto_client = MagicMock()
        boto_client.get_caller_identity.side_effect = ClientError(
            {
                "Error": {"Code": "ExpiredToken"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "GetCallerIdentity",
        )
        session = MagicMock()
        session.client.return_value = boto_client
        mock_session_cls.return_value = session

        client = StsClient(provider="aws", region="us-east-1")
        with pytest.raises(UnAuthorizedException):
            client.get_caller_identity()

    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_aws_assume_role_translates_no_credentials_error(self, mock_session_cls):
        boto_client = MagicMock()
        boto_client.assume_role.side_effect = NoCredentialsError()
        session = MagicMock()
        session.client.return_value = boto_client
        mock_session_cls.return_value = session

        client = StsClient(provider="aws", region="us-east-1")
        with pytest.raises(SubstrateSdkException):
            client.assume_role("arn:aws:iam::123:role/R", session_name="s")

    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_aws_get_access_token_translates_client_error(self, mock_session_cls):
        boto_client = MagicMock()
        boto_client.get_session_token.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "GetSessionToken",
        )
        session = MagicMock()
        session.client.return_value = boto_client
        mock_session_cls.return_value = session

        client = StsClient(provider="aws", region="us-east-1")
        with pytest.raises(SubstrateSdkException):
            client.get_access_token()

    def test_gcp_get_caller_identity_translates_refresh_error(self):
        import google.auth.exceptions

        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.token = "ya29.test"

        with (
            patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.default") as mock_default,
            patch(
                "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.IDTokenCredentials"
            ) as mock_id_cls,
            patch(
                "multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request"
            ),
        ):
            mock_default.return_value = (mock_creds, "test-project")
            id_creds = MagicMock()
            id_creds.refresh.side_effect = google.auth.exceptions.RefreshError("denied")
            mock_id_cls.return_value = id_creds

            client = StsClient(provider="gcp", region="us-central1")
            with pytest.raises(UnAuthorizedException):
                client.get_caller_identity()

    def test_gcp_assume_role_translates_refresh_error(self):
        import google.auth.exceptions

        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.token = "ya29.test"

        with (
            patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.default") as mock_default,
            patch(
                "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
            ) as mock_imp_cls,
            patch(
                "multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request"
            ),
        ):
            mock_default.return_value = (mock_creds, "test-project")
            imp = MagicMock()
            imp.refresh.side_effect = google.auth.exceptions.RefreshError(
                "access denied"
            )
            mock_imp_cls.return_value = imp

            client = StsClient(provider="gcp", region="us-central1")
            with pytest.raises(UnAuthorizedException):
                client.assume_role("sa@proj.iam.gserviceaccount.com", session_name="s")
