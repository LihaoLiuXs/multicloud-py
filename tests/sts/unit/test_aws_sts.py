"""Unit tests for :class:`AwsSts` — the AWS STS provider."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from multicloud.common.exceptions import (
    InvalidArgumentException,
    UnAuthorizedException,
    UnknownException,
    UnsupportedOperationException,
)
from multicloud.sts.sts_aws.aws_sts import AwsSts


@pytest.fixture
def mock_sts_client():
    return MagicMock()


@pytest.fixture
def sts(mock_sts_client):
    return AwsSts(region="us-east-1", sts_client=mock_sts_client)


class TestAwsStsConstruction:

    def test_with_injected_client(self, mock_sts_client):
        s = AwsSts(region="us-west-2", sts_client=mock_sts_client)
        assert s.region == "us-west-2"
        assert s.get_provider_id() == "aws"

    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_creates_session_when_no_injection(self, mock_session_cls):
        session = MagicMock()
        session.client.return_value = MagicMock()
        mock_session_cls.return_value = session
        AwsSts(region="eu-west-1")
        mock_session_cls.assert_called_once()
        session.client.assert_called_once_with("sts", region_name="eu-west-1")


class TestAwsStsGetCallerIdentity:

    def test_basic(self, sts, mock_sts_client):
        mock_sts_client.get_caller_identity.return_value = {
            "UserId": "AIDA12345",
            "Account": "111222333444",
            "Arn": "arn:aws:iam::111222333444:user/dev",
        }
        identity = sts.get_caller_identity()
        assert identity.user_id == "AIDA12345"
        assert identity.account_id == "111222333444"
        assert identity.cloud_resource_name == "arn:aws:iam::111222333444:user/dev"

    def test_error_translated(self, sts, mock_sts_client):
        mock_sts_client.get_caller_identity.side_effect = ClientError(
            {
                "Error": {"Code": "ExpiredToken"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "GetCallerIdentity",
        )
        with pytest.raises(ClientError):
            sts.get_caller_identity()

    def test_aud_ignored(self, sts, mock_sts_client):
        """AWS ignores the aud parameter."""
        mock_sts_client.get_caller_identity.return_value = {
            "UserId": "U",
            "Account": "A",
            "Arn": "arn:...",
        }
        identity = sts.get_caller_identity(aud="anything")
        assert identity.user_id == "U"


class TestAwsStsAssumeRole:

    def test_basic(self, sts, mock_sts_client):
        mock_sts_client.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "TEMP_AK",
                "SecretAccessKey": "TEMP_SK",
                "SessionToken": "TEMP_ST",
                "Expiration": datetime(2024, 12, 1),
            },
        }
        creds = sts.assume_role("arn:aws:iam::123:role/TestRole", session_name="sess")
        assert creds.access_key_id == "TEMP_AK"
        assert creds.access_key_secret == "TEMP_SK"
        assert creds.security_token == "TEMP_ST"
        assert creds.expiration == datetime(2024, 12, 1)

    def test_with_external_id_and_tags(self, sts, mock_sts_client):
        mock_sts_client.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
            },
        }
        sts.assume_role(
            "arn:aws:iam::123:role/R",
            session_name="s",
            expiration=7200,
            external_id="ext-123",
            policy='{"Version":"2012-10-17"}',
            tags={"team": "infra"},
        )
        call_kwargs = mock_sts_client.assume_role.call_args[1]
        assert call_kwargs["ExternalId"] == "ext-123"
        assert call_kwargs["Policy"] == '{"Version":"2012-10-17"}'
        assert call_kwargs["DurationSeconds"] == 7200
        assert {"Key": "team", "Value": "infra"} in call_kwargs["Tags"]

    def test_client_error_translated(self, sts, mock_sts_client):
        mock_sts_client.assume_role.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "AssumeRole",
        )
        with pytest.raises(ClientError):
            sts.assume_role("arn:aws:iam::123:role/R", session_name="s")

    def test_no_credentials_error(self, sts, mock_sts_client):
        mock_sts_client.assume_role.side_effect = NoCredentialsError()
        with pytest.raises(NoCredentialsError):
            sts.assume_role("arn:aws:iam::123:role/R", session_name="s")

    def test_credential_scope_empty_permissions_rejected(self, sts, mock_sts_client):
        """Empty permissions tuple is rejected at validation time."""
        from multicloud.sts.sts_client.driver import CredentialScope, ScopeRule

        scope = CredentialScope(
            rules=(
                ScopeRule(
                    available_resource="storage://my-bucket",
                    available_permissions=(),
                ),
            )
        )
        with pytest.raises(
            InvalidArgumentException, match="available_permissions must not be empty"
        ):
            sts.assume_role(
                "arn:aws:iam::123:role/R",
                session_name="s",
                credential_scope=scope,
            )

    def test_with_credential_scope(self, sts, mock_sts_client):
        import json

        from multicloud.sts.sts_client.driver import (
            AvailabilityCondition,
            CredentialScope,
            ScopeRule,
        )

        mock_sts_client.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
            },
        }
        scope = CredentialScope(
            rules=(
                ScopeRule(
                    available_resource="storage://my-bucket",
                    available_permissions=("storage:GetObject", "storage:PutObject"),
                    availability_condition=AvailabilityCondition(
                        resource_prefix="storage://my-bucket/docs/",
                    ),
                ),
            )
        )
        sts.assume_role(
            "arn:aws:iam::123:role/R",
            session_name="s",
            credential_scope=scope,
        )
        call_kwargs = mock_sts_client.assume_role.call_args[1]
        policy = json.loads(call_kwargs["Policy"])
        assert policy["Version"] == "2012-10-17"
        stmt = policy["Statement"][0]
        assert stmt["Effect"] == "Allow"
        assert stmt["Action"] == ["s3:GetObject", "s3:PutObject"]
        assert stmt["Resource"] == "arn:aws:s3:::my-bucket/docs/*"
        assert "Condition" not in stmt


class TestAwsStsGetAccessToken:

    def test_basic(self, sts, mock_sts_client):
        mock_sts_client.get_session_token.return_value = {
            "Credentials": {
                "AccessKeyId": "SESS_AK",
                "SecretAccessKey": "SESS_SK",
                "SessionToken": "SESS_ST",
                "Expiration": datetime(2024, 12, 1, 6, 0),
            },
        }
        creds = sts.get_access_token()
        assert creds.access_key_id == "SESS_AK"
        assert creds.security_token == "SESS_ST"
        mock_sts_client.get_session_token.assert_called_once_with()

    def test_with_duration(self, sts, mock_sts_client):
        mock_sts_client.get_session_token.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
            },
        }
        sts.get_access_token(duration_seconds=900)
        call_kwargs = mock_sts_client.get_session_token.call_args[1]
        assert call_kwargs["DurationSeconds"] == 900

    def test_audience_ignored(self, sts, mock_sts_client):
        """AWS ignores audience — still calls GetSessionToken without extra args."""
        mock_sts_client.get_session_token.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
            },
        }
        sts.get_access_token(audience="https://example.com")
        mock_sts_client.get_session_token.assert_called_once_with()

    def test_scopes_ignored(self, sts, mock_sts_client):
        """AWS ignores scopes — still calls GetSessionToken without extra args."""
        mock_sts_client.get_session_token.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
            },
        }
        sts.get_access_token(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        mock_sts_client.get_session_token.assert_called_once_with()

    def test_error_translated(self, sts, mock_sts_client):
        mock_sts_client.get_session_token.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "GetSessionToken",
        )
        with pytest.raises(ClientError):
            sts.get_access_token()


class TestAwsStsGenerateServiceAccountToken:

    def test_raises_unsupported(self, sts):
        with pytest.raises(
            UnsupportedOperationException, match="not supported for AWS"
        ):
            sts.generate_service_account_token(
                "sa@project.iam.gserviceaccount.com",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

    def test_raises_with_extended_lifetime(self, sts):
        with pytest.raises(UnsupportedOperationException):
            sts.generate_service_account_token(
                "sa@project.iam.gserviceaccount.com",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
                lifetime=7200,
            )


class TestAwsStsAssumeRoleWithWebIdentity:

    def test_basic(self, sts, mock_sts_client):
        mock_sts_client.assume_role_with_web_identity.return_value = {
            "Credentials": {
                "AccessKeyId": "ASIA_WI",
                "SecretAccessKey": "WI_SK",
                "SessionToken": "WI_ST",
                "Expiration": datetime(2026, 1, 1),
            },
        }
        creds = sts.assume_role_with_web_identity(
            "arn:aws:iam::123:role/WebRole",
            "oidc.jwt.token",
            session_name="federated",
            expiration=1800,
        )
        assert creds.access_key_id == "ASIA_WI"
        assert creds.access_key_secret == "WI_SK"
        assert creds.security_token == "WI_ST"
        assert creds.expiration == datetime(2026, 1, 1)
        call_kwargs = mock_sts_client.assume_role_with_web_identity.call_args[1]
        assert call_kwargs["RoleArn"] == "arn:aws:iam::123:role/WebRole"
        assert call_kwargs["RoleSessionName"] == "federated"
        assert call_kwargs["WebIdentityToken"] == "oidc.jwt.token"
        assert call_kwargs["DurationSeconds"] == 1800

    def test_default_session_name(self, sts, mock_sts_client):
        mock_sts_client.assume_role_with_web_identity.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "ST",
            },
        }
        sts.assume_role_with_web_identity("arn:aws:iam::123:role/R", "tok")
        call_kwargs = mock_sts_client.assume_role_with_web_identity.call_args[1]
        assert call_kwargs["RoleSessionName"] == "multicloud-web-identity"

    def test_empty_role_raises_before_call(self, sts, mock_sts_client):
        with pytest.raises(InvalidArgumentException):
            sts.assume_role_with_web_identity("", "tok")
        mock_sts_client.assume_role_with_web_identity.assert_not_called()

    def test_empty_token_raises_before_call(self, sts, mock_sts_client):
        with pytest.raises(InvalidArgumentException):
            sts.assume_role_with_web_identity("arn:aws:iam::123:role/R", "")
        mock_sts_client.assume_role_with_web_identity.assert_not_called()

    def test_error_translated(self, sts, mock_sts_client):
        mock_sts_client.assume_role_with_web_identity.side_effect = ClientError(
            {
                "Error": {"Code": "AccessDenied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "AssumeRoleWithWebIdentity",
        )
        with pytest.raises(ClientError):
            sts.assume_role_with_web_identity("arn:aws:iam::123:role/R", "tok")


class TestAwsStsTranslateError:

    def test_already_translated(self, sts):
        exc = UnAuthorizedException("already")
        assert sts.translate_error(exc) is exc

    def test_no_credentials(self, sts):
        assert isinstance(
            sts.translate_error(NoCredentialsError()), UnAuthorizedException
        )

    def test_expired_token(self, sts):
        exc = ClientError(
            {
                "Error": {"Code": "ExpiredToken"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "AssumeRole",
        )
        assert isinstance(sts.translate_error(exc), UnAuthorizedException)

    def test_invalid_duration(self, sts):
        exc = ClientError(
            {
                "Error": {"Code": "InvalidDuration"},
                "ResponseMetadata": {"HTTPStatusCode": 400},
            },
            "AssumeRole",
        )
        assert isinstance(sts.translate_error(exc), InvalidArgumentException)

    def test_unknown_client_error(self, sts):
        exc = ClientError(
            {
                "Error": {"Code": "InternalFailure"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "AssumeRole",
        )
        assert isinstance(sts.translate_error(exc), UnknownException)

    def test_botocore_error(self, sts):
        assert isinstance(
            sts.translate_error(BotoCoreError()), InvalidArgumentException
        )

    def test_unknown_exception(self, sts):
        assert isinstance(sts.translate_error(RuntimeError("x")), UnknownException)


class TestAwsStsLifecycle:

    def test_close_calls_underlying_client(self, sts, mock_sts_client):
        mock_sts_client.close = MagicMock()
        sts.close()
        mock_sts_client.close.assert_called_once()

    def test_close_noop_when_no_close_method(self):
        client = MagicMock(spec=[])
        s = AwsSts(sts_client=client)
        s.close()


class TestAwsStsCredentialsOverrider:

    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_session_override_uses_explicit_credentials(self, mock_session_cls):
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
                access_key_id="OVERRIDE_AK",
                access_key_secret="OVERRIDE_SK",
                security_token="OVERRIDE_ST",
            ),
        )
        AwsSts(region="us-east-1", credentials_overrider=overrider)
        mock_session_cls.assert_called_once_with(
            aws_access_key_id="OVERRIDE_AK",
            aws_secret_access_key="OVERRIDE_SK",
            aws_session_token="OVERRIDE_ST",
            region_name="us-east-1",
        )

    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_session_override_empty_token_passes_none(self, mock_session_cls):
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
                access_key_id="AK",
                access_key_secret="SK",
                security_token="",
            ),
        )
        AwsSts(region="us-west-2", credentials_overrider=overrider)
        mock_session_cls.assert_called_once_with(
            aws_access_key_id="AK",
            aws_secret_access_key="SK",
            aws_session_token=None,
            region_name="us-west-2",
        )

    @patch("multicloud.aws_common.aws_credentials.AssumeRoleCredentialFetcher")
    @patch("multicloud.aws_common.aws_credentials.DeferredRefreshableCredentials")
    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_assume_role_override_builds_fetcher(
        self, mock_session_cls, mock_deferred_cls, mock_fetcher_cls
    ):
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="arn:aws:iam::999:role/CrossAccount",
            session_name="test-session",
            duration_seconds=1800,
        )
        AwsSts(region="us-east-1", credentials_overrider=overrider)

        # The AssumeRole call itself is deferred — botocore makes it on first
        # credential use, not at session construction. Verify the fetcher
        # was built with the right role / session name / duration.
        mock_fetcher_cls.assert_called_once()
        kwargs = mock_fetcher_cls.call_args.kwargs
        assert kwargs["role_arn"] == "arn:aws:iam::999:role/CrossAccount"
        assert kwargs["extra_args"]["RoleSessionName"] == "test-session"
        assert kwargs["extra_args"]["DurationSeconds"] == 1800

    @patch("multicloud.aws_common.aws_credentials.AssumeRoleCredentialFetcher")
    @patch("multicloud.aws_common.aws_credentials.DeferredRefreshableCredentials")
    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_assume_role_override_default_session_name(
        self, mock_session_cls, mock_deferred_cls, mock_fetcher_cls
    ):
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="arn:aws:iam::999:role/R",
        )
        AwsSts(region="us-east-1", credentials_overrider=overrider)

        name = mock_fetcher_cls.call_args.kwargs["extra_args"]["RoleSessionName"]
        assert name.startswith("multicloud-")

    @patch(
        "multicloud.aws_common.aws_credentials."
        "AssumeRoleWithWebIdentityCredentialFetcher"
    )
    @patch("multicloud.aws_common.aws_credentials.DeferredRefreshableCredentials")
    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_web_identity_override_builds_fetcher(
        self, mock_session_cls, mock_deferred_cls, mock_fetcher_cls
    ):
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        token_supplier = lambda: "my-jwt-token"
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="arn:aws:iam::999:role/WebRole",
            web_identity_token_supplier=token_supplier,
            session_name="web-session",
            duration_seconds=900,
        )
        AwsSts(region="us-west-2", credentials_overrider=overrider)

        # AssumeRoleWithWebIdentity is deferred to first credential use.
        # Verify the fetcher was wired with the supplier as token loader.
        mock_fetcher_cls.assert_called_once()
        kwargs = mock_fetcher_cls.call_args.kwargs
        assert kwargs["role_arn"] == "arn:aws:iam::999:role/WebRole"
        assert kwargs["web_identity_token_loader"] is token_supplier
        assert kwargs["extra_args"]["RoleSessionName"] == "web-session"
        assert kwargs["extra_args"]["DurationSeconds"] == 900

    @patch("multicloud.sts.sts_aws.aws_sts.get_session")
    def test_sts_client_takes_precedence_over_overrider(self, mock_get_session):
        # If both ``sts_client`` and ``credentials_overrider`` are passed,
        # the injected client wins and the overrider is silently ignored.
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
            StsCredentials,
        )

        injected = MagicMock()
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                access_key_id="AK", access_key_secret="SK"
            ),
        )
        s = AwsSts(sts_client=injected, credentials_overrider=overrider)
        assert s._sts is injected
        mock_get_session.assert_not_called()
