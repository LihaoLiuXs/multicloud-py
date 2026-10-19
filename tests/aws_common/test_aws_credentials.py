"""Unit tests for multicloud.aws_common.aws_credentials."""

from __future__ import annotations

import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from multicloud.aws_common.aws_credentials import get_aio_session, get_session
from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
    StsCredentials,
)
from multicloud.common.exceptions import InvalidArgumentException


class TestGetSessionSession:
    """SESSION override path — static credentials wrapped in a boto3 Session."""

    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_session_credentials_forwarded(self, mock_session_cls):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                access_key_id="AK",
                access_key_secret="SK",
                security_token="ST",
            ),
        )
        get_session(overrider, region="us-east-1")
        mock_session_cls.assert_called_once_with(
            aws_access_key_id="AK",
            aws_secret_access_key="SK",
            aws_session_token="ST",
            region_name="us-east-1",
        )


class TestGetSessionAssumeRole:
    """ASSUME_ROLE override path — botocore AssumeRoleCredentialFetcher."""

    @patch("multicloud.aws_common.aws_credentials.AssumeRoleCredentialFetcher")
    @patch("multicloud.aws_common.aws_credentials.DeferredRefreshableCredentials")
    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_builds_fetcher_with_role_and_session_name(
        self, mock_session_cls, mock_deferred_cls, mock_fetcher_cls
    ):
        # boto3.Session() returns a real-ish object whose .get_credentials()
        # and ._session.create_client are passed to the fetcher.
        source_session = MagicMock()
        mock_session_cls.return_value = source_session

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="arn:aws:iam::123:role/R",
            session_name="my-session",
            duration_seconds=900,
        )
        get_session(overrider, region="us-east-1")

        # Fetcher built with the right role, session name, and duration.
        mock_fetcher_cls.assert_called_once()
        kwargs = mock_fetcher_cls.call_args.kwargs
        assert kwargs["role_arn"] == "arn:aws:iam::123:role/R"
        assert kwargs["extra_args"]["RoleSessionName"] == "my-session"
        assert kwargs["extra_args"]["DurationSeconds"] == 900
        # Source credentials & client creator forwarded from the source session.
        assert (
            kwargs["source_credentials"] is source_session.get_credentials.return_value
        )
        assert kwargs["client_creator"] is source_session._session.create_client

        # Deferred-refreshable wired to the fetcher's fetch_credentials.
        mock_deferred_cls.assert_called_once()
        deferred_kwargs = mock_deferred_cls.call_args.kwargs
        assert deferred_kwargs["method"] == "sts-assume-role"
        assert (
            deferred_kwargs["refresh_using"]
            is mock_fetcher_cls.return_value.fetch_credentials
        )

    @patch("multicloud.aws_common.aws_credentials.AssumeRoleCredentialFetcher")
    @patch("multicloud.aws_common.aws_credentials.DeferredRefreshableCredentials")
    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_without_duration_omits_param(
        self, mock_session_cls, mock_deferred_cls, mock_fetcher_cls
    ):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="arn:aws:iam::123:role/R",
        )
        get_session(overrider, region="us-east-1")
        extra_args = mock_fetcher_cls.call_args.kwargs["extra_args"]
        assert "DurationSeconds" not in extra_args

    @patch("multicloud.aws_common.aws_credentials.AssumeRoleCredentialFetcher")
    @patch("multicloud.aws_common.aws_credentials.DeferredRefreshableCredentials")
    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_default_session_name_is_multicloud_prefixed(
        self, mock_session_cls, mock_deferred_cls, mock_fetcher_cls
    ):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="arn:aws:iam::123:role/R",
        )
        get_session(overrider)
        name = mock_fetcher_cls.call_args.kwargs["extra_args"]["RoleSessionName"]
        assert name.startswith("multicloud-")


class TestGetSessionWebIdentity:
    """ASSUME_ROLE_WEB_IDENTITY override path."""

    @patch(
        "multicloud.aws_common.aws_credentials."
        "AssumeRoleWithWebIdentityCredentialFetcher"
    )
    @patch("multicloud.aws_common.aws_credentials.DeferredRefreshableCredentials")
    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_builds_fetcher_with_token_loader(
        self, mock_session_cls, mock_deferred_cls, mock_fetcher_cls
    ):
        token_supplier = lambda: "fake-jwt"  # noqa: E731
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="arn:aws:iam::123:role/R",
            web_identity_token_supplier=token_supplier,
            duration_seconds=1800,
        )
        get_session(overrider, region="us-east-1")

        mock_fetcher_cls.assert_called_once()
        kwargs = mock_fetcher_cls.call_args.kwargs
        assert kwargs["role_arn"] == "arn:aws:iam::123:role/R"
        # The supplier is forwarded as the token loader — botocore calls
        # it on each refresh to obtain a fresh JWT.
        assert kwargs["web_identity_token_loader"] is token_supplier
        assert kwargs["extra_args"]["DurationSeconds"] == 1800

        deferred_kwargs = mock_deferred_cls.call_args.kwargs
        assert deferred_kwargs["method"] == "sts-assume-role-with-web-identity"


class TestGetSessionUnsupported:

    def test_unsupported_type_raises(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="r",
        )
        object.__setattr__(overrider, "credentials_type", "UNKNOWN")
        with pytest.raises(InvalidArgumentException, match="Unsupported"):
            get_session(overrider)


class TestGetSessionDefaultPath:

    def test_none_overrider_returns_default_session(self):
        with patch(
            "multicloud.aws_common.aws_credentials.boto3.Session"
        ) as mock_session_cls:
            mock_session_cls.return_value = MagicMock()
            get_session(None)
            mock_session_cls.assert_called_once_with()


class TestGetAioSessionSession:
    """SESSION override path — static credentials set on an aiobotocore session."""

    @pytest.mark.asyncio
    @patch("aiobotocore.session.get_session")
    async def test_session_credentials_forwarded(self, mock_get_session):
        session = MagicMock()
        mock_get_session.return_value = session

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                access_key_id="AK",
                access_key_secret="SK",
                security_token="ST",
            ),
        )
        result = await get_aio_session(overrider, region="us-east-1")

        assert result is session
        session.set_config_variable.assert_called_once_with("region", "us-east-1")
        session.set_credentials.assert_called_once_with("AK", "SK", "ST")

    @pytest.mark.asyncio
    @patch("aiobotocore.session.get_session")
    async def test_empty_security_token_becomes_none(self, mock_get_session):
        session = MagicMock()
        mock_get_session.return_value = session

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                access_key_id="AK",
                access_key_secret="SK",
            ),
        )
        await get_aio_session(overrider)
        session.set_credentials.assert_called_once_with("AK", "SK", None)


class TestGetAioSessionAssumeRole:
    """ASSUME_ROLE override path — aiobotocore AioAssumeRoleCredentialFetcher."""

    @pytest.mark.asyncio
    @patch("aiobotocore.credentials.AioDeferredRefreshableCredentials")
    @patch("aiobotocore.credentials.AioAssumeRoleCredentialFetcher")
    @patch("aiobotocore.session.get_session")
    async def test_builds_fetcher_with_role_and_session_name(
        self, mock_get_session, mock_fetcher_cls, mock_deferred_cls
    ):
        session = MagicMock()
        session.get_credentials = AsyncMock(return_value="source-creds")
        mock_get_session.return_value = session

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="arn:aws:iam::123:role/R",
            session_name="my-session",
            duration_seconds=900,
        )
        result = await get_aio_session(overrider, region="us-east-1")

        assert result is session
        # Fetcher built with the right role, session name, and duration.
        mock_fetcher_cls.assert_called_once()
        kwargs = mock_fetcher_cls.call_args.kwargs
        assert kwargs["role_arn"] == "arn:aws:iam::123:role/R"
        assert kwargs["extra_args"]["RoleSessionName"] == "my-session"
        assert kwargs["extra_args"]["DurationSeconds"] == 900
        # Source credentials resolved asynchronously and client creator forwarded.
        assert kwargs["source_credentials"] == "source-creds"
        assert kwargs["client_creator"] is session.create_client

        # Deferred-refreshable wired to the fetcher and injected on the session.
        mock_deferred_cls.assert_called_once()
        deferred_kwargs = mock_deferred_cls.call_args.kwargs
        assert deferred_kwargs["method"] == "sts-assume-role"
        assert (
            deferred_kwargs["refresh_using"]
            is mock_fetcher_cls.return_value.fetch_credentials
        )
        assert session._credentials is mock_deferred_cls.return_value

    @pytest.mark.asyncio
    @patch("aiobotocore.credentials.AioDeferredRefreshableCredentials")
    @patch("aiobotocore.credentials.AioAssumeRoleCredentialFetcher")
    @patch("aiobotocore.session.get_session")
    async def test_without_duration_omits_param(
        self, mock_get_session, mock_fetcher_cls, mock_deferred_cls
    ):
        session = MagicMock()
        session.get_credentials = AsyncMock(return_value="source-creds")
        mock_get_session.return_value = session

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="arn:aws:iam::123:role/R",
        )
        await get_aio_session(overrider)
        extra_args = mock_fetcher_cls.call_args.kwargs["extra_args"]
        assert "DurationSeconds" not in extra_args
        assert extra_args["RoleSessionName"].startswith("multicloud-")


class TestGetAioSessionWebIdentity:
    """ASSUME_ROLE_WEB_IDENTITY override path."""

    @pytest.mark.asyncio
    @patch("aiobotocore.credentials.AioDeferredRefreshableCredentials")
    @patch("aiobotocore.credentials.AioAssumeRoleWithWebIdentityCredentialFetcher")
    @patch("aiobotocore.session.get_session")
    async def test_builds_fetcher_with_token_loader(
        self, mock_get_session, mock_fetcher_cls, mock_deferred_cls
    ):
        session = MagicMock()
        mock_get_session.return_value = session
        token_supplier = lambda: "fake-jwt"  # noqa: E731

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="arn:aws:iam::123:role/R",
            web_identity_token_supplier=token_supplier,
            duration_seconds=1800,
        )
        result = await get_aio_session(overrider, region="us-east-1")

        assert result is session
        mock_fetcher_cls.assert_called_once()
        kwargs = mock_fetcher_cls.call_args.kwargs
        assert kwargs["role_arn"] == "arn:aws:iam::123:role/R"
        # The supplier is forwarded as the token loader for each refresh.
        assert kwargs["web_identity_token_loader"] is token_supplier
        assert kwargs["client_creator"] is session.create_client
        assert kwargs["extra_args"]["DurationSeconds"] == 1800

        deferred_kwargs = mock_deferred_cls.call_args.kwargs
        assert deferred_kwargs["method"] == "sts-assume-role-with-web-identity"
        assert session._credentials is mock_deferred_cls.return_value


class TestGetAioSessionDefaultAndUnsupported:

    @pytest.mark.asyncio
    @patch("aiobotocore.session.get_session")
    async def test_none_overrider_returns_default_session(self, mock_get_session):
        session = MagicMock()
        mock_get_session.return_value = session

        result = await get_aio_session(None, region="us-west-2")

        assert result is session
        session.set_config_variable.assert_called_once_with("region", "us-west-2")
        session.set_credentials.assert_not_called()

    @pytest.mark.asyncio
    @patch("aiobotocore.session.get_session")
    async def test_unsupported_type_raises(self, mock_get_session):
        mock_get_session.return_value = MagicMock()
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="r",
        )
        object.__setattr__(overrider, "credentials_type", "UNKNOWN")
        with pytest.raises(InvalidArgumentException, match="Unsupported"):
            await get_aio_session(overrider)


class TestGetAioSessionImportFailure:
    """Missing aiobotocore surfaces an actionable install hint."""

    @pytest.mark.asyncio
    async def test_missing_aiobotocore_raises_actionable_error(self):
        # The helper imports aiobotocore lazily; simulate it being absent.
        original = sys.modules.get("aiobotocore.credentials")
        sys.modules["aiobotocore.credentials"] = None  # type: ignore[assignment]
        try:
            overrider = CredentialsOverrider(
                credentials_type=CredentialsType.SESSION,
                session_credentials=StsCredentials(access_key_id="AK"),
            )
            with pytest.raises(ImportError, match="multicloud-py-blob-aws\\[async\\]"):
                await get_aio_session(overrider)
        finally:
            if original is not None:
                sys.modules["aiobotocore.credentials"] = original
            else:
                sys.modules.pop("aiobotocore.credentials", None)


class TestModuleImportFailure:
    """Verify that missing boto3 surfaces an actionable error message."""

    def test_missing_boto3_raises_actionable_error(self):
        # Snapshot every aws_common / downstream-AWS module so we can rebuild
        # the import graph after the reload. Without this, downstream callers
        # (aws_sts, aws_blob_store) keep function-globals pointing at the
        # *first* module instance and lose patchability for the rest of the
        # test run.
        aws_modules = [
            name
            for name in list(sys.modules)
            if name == "multicloud.aws_common.aws_credentials"
            or name == "multicloud.sts.sts_aws.aws_sts"
            or name == "multicloud.blob.blob_aws.aws_blob_store"
        ]
        snapshots = {name: sys.modules[name] for name in aws_modules}

        original_boto3 = sys.modules.get("boto3")
        sys.modules["boto3"] = None  # type: ignore[assignment]
        for name in aws_modules:
            sys.modules.pop(name, None)
        try:
            with pytest.raises(ImportError, match="python-multicloud-sdk\\[aws\\]"):
                importlib.import_module("multicloud.aws_common.aws_credentials")
        finally:
            if original_boto3 is not None:
                sys.modules["boto3"] = original_boto3
            else:
                sys.modules.pop("boto3", None)
            # Restore the *exact* original module objects so downstream
            # callers keep their bindings.
            for name, mod in snapshots.items():
                sys.modules[name] = mod
