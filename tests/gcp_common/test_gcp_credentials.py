"""Unit tests for multicloud.gcp_common.gcp_credentials."""

from __future__ import annotations

import importlib
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
    StsCredentials,
)
from multicloud.common.exceptions import (
    InvalidArgumentException,
    UnAuthorizedException,
)
from multicloud.gcp_common.gcp_credentials import (
    DEFAULT_SCOPES,
    get_credentials,
    get_token,
)


class TestGetCredentialsSession:

    def test_session_creates_oauth2_credentials(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                security_token="ya29.test-token",
            ),
        )
        result = get_credentials(overrider)
        assert result.token == "ya29.test-token"

    def test_session_credential_is_non_refreshable(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                security_token="ya29.test-token",
            ),
        )
        result = get_credentials(overrider)
        assert result.token_uri is None
        assert result.refresh_token is None
        assert result.client_id is None
        assert result.client_secret is None

    def test_empty_security_token_raises(self):
        # The bearer token IS the credential on GCP; an empty one would build a
        # credential that fails opaquely at request time, so it is rejected.
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token=""),
        )
        with pytest.raises(InvalidArgumentException, match="security_token required"):
            get_credentials(overrider)


class TestGetCredentialsAssumeRole:

    @patch("multicloud.gcp_common.gcp_credentials.google.auth.default")
    @patch(
        "multicloud.gcp_common.gcp_credentials.google.auth.impersonated_credentials.Credentials"
    )
    def test_custom_scopes(self, mock_imp_cls, mock_default):
        mock_default.return_value = (MagicMock(), "proj")
        mock_imp_cls.return_value = MagicMock()

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="sa@proj.iam.gserviceaccount.com",
        )
        get_credentials(
            overrider, scopes=["https://www.googleapis.com/auth/devstorage.read_only"]
        )

        call_kwargs = mock_imp_cls.call_args[1]
        assert call_kwargs["target_scopes"] == [
            "https://www.googleapis.com/auth/devstorage.read_only"
        ]

    @patch("multicloud.gcp_common.gcp_credentials.google.auth.default")
    def test_no_default_creds_raises(self, mock_default):
        import google.auth.exceptions

        mock_default.side_effect = google.auth.exceptions.DefaultCredentialsError(
            "no creds"
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="sa@proj.iam.gserviceaccount.com",
        )
        with pytest.raises(
            UnAuthorizedException, match="Could not find default credentials"
        ):
            get_credentials(overrider)


class TestGetCredentialsWebIdentity:

    def test_returns_none(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="sa@proj.iam.gserviceaccount.com",
            web_identity_token_supplier=lambda: "jwt",
        )
        assert get_credentials(overrider) is None

    def test_warns_before_falling_back_to_adc(self, caplog):
        # An explicitly-requested WIF override that can't be honored must
        # not silently degrade to ADC — it warns so the identity swap is
        # debuggable in production.
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="sa@proj.iam.gserviceaccount.com",
            web_identity_token_supplier=lambda: "jwt",
        )
        with caplog.at_level(
            logging.WARNING, logger="multicloud.gcp_common.gcp_credentials"
        ):
            assert get_credentials(overrider) is None
        assert any(
            record.levelno == logging.WARNING
            and "ASSUME_ROLE_WEB_IDENTITY" in record.message
            for record in caplog.records
        )


class TestGetTokenSession:
    """SESSION override — a static-bearer token stand-in for gcloud-aio."""

    @pytest.mark.asyncio
    async def test_session_returns_static_bearer_token(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                security_token="ya29.test-token",
            ),
        )
        token = get_token(overrider)
        # The stand-in exposes the async get()/close() surface Storage needs.
        assert await token.get() == "ya29.test-token"
        # close() is a no-op for the static stand-in and must not raise.
        assert await token.close() is None

    def test_session_without_credentials_raises(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token="tok"),
        )
        object.__setattr__(overrider, "session_credentials", None)
        with pytest.raises(InvalidArgumentException, match="session_credentials"):
            get_token(overrider)

    def test_empty_security_token_raises(self):
        # The bearer token IS the credential on GCP; an empty one would build a
        # static token that fails opaquely at request time, so it is rejected.
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token=""),
        )
        with pytest.raises(InvalidArgumentException, match="security_token required"):
            get_token(overrider)


class TestGetTokenAssumeRole:
    """ASSUME_ROLE override — an impersonation Token from gcloud-aio."""

    @pytest.mark.asyncio
    @patch("gcloud.aio.auth.Token")
    async def test_builds_token_with_principal_and_scopes(self, mock_token_cls):
        mock_token_cls.return_value = MagicMock()
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="sa@proj.iam.gserviceaccount.com",
        )
        result = get_token(
            overrider,
            scopes=["https://www.googleapis.com/auth/devstorage.read_only"],
        )

        assert result is mock_token_cls.return_value
        mock_token_cls.assert_called_once_with(
            target_principal="sa@proj.iam.gserviceaccount.com",
            scopes=["https://www.googleapis.com/auth/devstorage.read_only"],
        )

    @pytest.mark.asyncio
    @patch("gcloud.aio.auth.Token")
    async def test_defaults_to_cloud_platform_scope(self, mock_token_cls):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="sa@proj.iam.gserviceaccount.com",
        )
        get_token(overrider)
        scopes = mock_token_cls.call_args.kwargs["scopes"]
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]

    @pytest.mark.asyncio
    @patch("gcloud.aio.auth.Token")
    async def test_duration_seconds_sets_token_ttl(self, mock_token_cls):
        # The token type takes no lifetime kwarg; duration is applied as a
        # per-instance TTL so it matches the sync lifetime behavior.
        mock_token_cls.return_value = SimpleNamespace()
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="sa@proj.iam.gserviceaccount.com",
            duration_seconds=900,
        )
        token = get_token(overrider)
        assert token.default_token_ttl == 900

    @pytest.mark.asyncio
    @patch("gcloud.aio.auth.Token")
    async def test_no_duration_leaves_default_ttl_untouched(self, mock_token_cls):
        # Without duration_seconds the TTL is left at the token's own default.
        mock_token_cls.return_value = SimpleNamespace()
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="sa@proj.iam.gserviceaccount.com",
        )
        token = get_token(overrider)
        assert not hasattr(token, "default_token_ttl")

    def test_real_token_exposes_default_token_ttl(self):
        # get_token() honors duration_seconds by setting default_token_ttl on
        # the real gcloud-aio Token. The tests above mock Token, so they would
        # not catch an upstream rename/removal of that attribute — duration
        # would then be silently dropped in production. This smoke test builds a
        # real Token (offline: no network until a token is fetched) and asserts
        # the attribute exists with the documented 3600s default, so any
        # upstream change fails loudly here.
        from gcloud.aio.auth import Token

        token = Token(
            target_principal="sa@proj.iam.gserviceaccount.com",
            scopes=DEFAULT_SCOPES,
        )
        assert hasattr(token, "default_token_ttl")
        assert token.default_token_ttl == 3600


class TestGetTokenFallthrough:
    """None and unsupported types fall through to ADC (return None)."""

    def test_none_overrider_returns_none(self):
        assert get_token(None) is None

    def test_web_identity_returns_none(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="sa@proj.iam.gserviceaccount.com",
            web_identity_token_supplier=lambda: "jwt",
        )
        assert get_token(overrider) is None

    def test_web_identity_warns_before_falling_back_to_adc(self, caplog):
        # An explicitly-requested WIF override that can't be honored on the
        # async path must warn rather than silently authenticate as the
        # ambient ADC identity.
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="sa@proj.iam.gserviceaccount.com",
            web_identity_token_supplier=lambda: "jwt",
        )
        with caplog.at_level(
            logging.WARNING, logger="multicloud.gcp_common.gcp_credentials"
        ):
            assert get_token(overrider) is None
        assert any(
            record.levelno == logging.WARNING
            and "ASSUME_ROLE_WEB_IDENTITY" in record.message
            for record in caplog.records
        )

    def test_none_overrider_does_not_warn(self, caplog):
        # The default ADC path (no overrider) is legitimate and must stay
        # silent — only an explicit, unhonorable override warns.
        with caplog.at_level(
            logging.WARNING, logger="multicloud.gcp_common.gcp_credentials"
        ):
            assert get_token(None) is None
        assert not caplog.records


class TestGetTokenImportFailure:
    """Missing gcloud-aio surfaces an actionable install hint."""

    def test_missing_gcloud_aio_raises_actionable_error(self):
        original = sys.modules.get("gcloud.aio.auth")
        sys.modules["gcloud.aio.auth"] = None  # type: ignore[assignment]
        try:
            overrider = CredentialsOverrider(
                credentials_type=CredentialsType.ASSUME_ROLE,
                role="sa@proj.iam.gserviceaccount.com",
            )
            with pytest.raises(ImportError, match="multicloud-py-blob-gcp\\[async\\]"):
                get_token(overrider)
        finally:
            if original is not None:
                sys.modules["gcloud.aio.auth"] = original
            else:
                sys.modules.pop("gcloud.aio.auth", None)


class TestModuleImportFailure:

    def test_missing_google_auth_raises_actionable_error(self):
        original = sys.modules.get("google.auth")
        sys.modules["google.auth"] = None  # type: ignore[assignment]
        sys.modules.pop("multicloud.gcp_common.gcp_credentials", None)
        try:
            with pytest.raises(ImportError, match="python-multicloud-sdk\\[gcp\\]"):
                importlib.import_module("multicloud.gcp_common.gcp_credentials")
        finally:
            if original is not None:
                sys.modules["google.auth"] = original
            else:
                sys.modules.pop("google.auth", None)
            sys.modules.pop("multicloud.gcp_common.gcp_credentials", None)
            importlib.import_module("multicloud.gcp_common.gcp_credentials")
