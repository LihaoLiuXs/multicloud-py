"""Unit tests for :class:`GcpSts` — the GCP STS provider."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from multicloud.common.exceptions import (
    InvalidArgumentException,
    UnAuthorizedException,
    UnknownException,
    UnsupportedOperationException,
)
from multicloud.sts.sts_gcp.gcp_sts import _STS_TOKEN_ENDPOINT as _STS_ENDPOINT
from multicloud.sts.sts_gcp.gcp_sts import GcpSts


@pytest.fixture
def mock_credentials():
    creds = MagicMock()
    creds.service_account_email = "test-sa@my-project.iam.gserviceaccount.com"
    creds.token = "ya29.mock-access-token-abc123"
    creds.expiry = datetime(2026, 12, 1, 12, 0, 0)
    creds.valid = True
    return creds


@pytest.fixture
def gcp_sts(mock_credentials):
    return GcpSts(
        region="us-central1", credentials=mock_credentials, project="my-project"
    )


class TestGcpStsConstruction:

    def test_with_injected_credentials(self, mock_credentials):
        sts = GcpSts(
            region="us-central1", credentials=mock_credentials, project="test-project"
        )
        assert sts.region == "us-central1"
        assert sts.project == "test-project"
        assert sts.get_provider_id() == "gcp"

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.default")
    def test_loads_adc_when_no_credentials_injected(self, mock_default):
        creds = MagicMock()
        creds.valid = True
        mock_default.return_value = (creds, "auto-detected-project")
        sts = GcpSts(region="europe-west1")
        mock_default.assert_called_once()
        assert sts.project == "auto-detected-project"

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.default")
    def test_raises_when_no_credentials_available(self, mock_default):
        import google.auth.exceptions

        mock_default.side_effect = google.auth.exceptions.DefaultCredentialsError(
            "no creds"
        )
        with pytest.raises(
            UnAuthorizedException, match="Could not find default credentials"
        ):
            GcpSts()


class TestGcpStsGetCallerIdentity:

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.IDTokenCredentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_returns_jwt_as_cloud_resource_name(self, mock_req, mock_id_cls, gcp_sts):
        mock_id_creds = MagicMock()
        mock_id_creds.token = "eyJhbGciOiJSUzI1NiJ9.payload.sig"
        mock_id_cls.return_value = mock_id_creds

        identity = gcp_sts.get_caller_identity()
        assert identity.user_id == ""
        assert identity.cloud_resource_name == "eyJhbGciOiJSUzI1NiJ9.payload.sig"
        assert identity.account_id == ""
        mock_id_cls.assert_called_once_with(
            target_credentials=gcp_sts._credentials,
            target_audience="multicloud-python-sdk",
            include_email=True,
        )
        mock_id_creds.refresh.assert_called_once()

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.IDTokenCredentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_custom_audience(self, mock_req, mock_id_cls, gcp_sts):
        mock_id_creds = MagicMock()
        mock_id_creds.token = "jwt"
        mock_id_cls.return_value = mock_id_creds

        gcp_sts.get_caller_identity(aud="my-service")
        mock_id_cls.assert_called_once_with(
            target_credentials=gcp_sts._credentials,
            target_audience="my-service",
            include_email=True,
        )

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.IDTokenCredentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_refresh_failure_raises(self, mock_req, mock_id_cls, gcp_sts):
        import google.auth.exceptions

        mock_id_creds = MagicMock()
        mock_id_creds.refresh.side_effect = google.auth.exceptions.RefreshError("fail")
        mock_id_cls.return_value = mock_id_creds
        with pytest.raises(google.auth.exceptions.RefreshError):
            gcp_sts.get_caller_identity()

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.IDTokenCredentials"
    )
    def test_unsupported_credential_type(self, mock_id_cls, gcp_sts):
        mock_id_cls.side_effect = AttributeError("no sign_bytes")
        with pytest.raises(
            UnsupportedOperationException, match="service account or impersonated"
        ):
            gcp_sts.get_caller_identity()


class TestGcpStsGetAccessToken:

    def test_returns_token_as_security_token(self, gcp_sts):
        creds = gcp_sts.get_access_token()
        assert creds.access_key_id == ""
        assert creds.access_key_secret == ""
        assert creds.security_token == "ya29.mock-access-token-abc123"
        assert creds.expiration == datetime(2026, 12, 1, 12, 0, 0)

    def test_refreshes_expired_credentials(self, mock_credentials):
        mock_credentials.valid = False
        mock_credentials.token = None

        def refresh_side_effect(request):
            mock_credentials.valid = True
            mock_credentials.token = "ya29.refreshed"
            mock_credentials.expiry = datetime(2026, 12, 1, 13, 0, 0)

        mock_credentials.refresh.side_effect = refresh_side_effect
        sts = GcpSts(credentials=mock_credentials, project="proj")
        creds = sts.get_access_token()
        assert creds.security_token == "ya29.refreshed"

    def test_refresh_failure_raises(self, mock_credentials):
        import google.auth.exceptions

        mock_credentials.valid = False
        mock_credentials.refresh.side_effect = google.auth.exceptions.RefreshError(
            "fail"
        )
        sts = GcpSts(credentials=mock_credentials, project="proj")
        with pytest.raises(google.auth.exceptions.RefreshError):
            sts.get_access_token()

    def test_duration_over_one_hour_passes_through(self, gcp_sts):
        # The SDK does not cap duration_seconds; large values must not raise.
        creds = gcp_sts.get_access_token(duration_seconds=7200)
        assert creds.security_token == "ya29.mock-access-token-abc123"

    def test_duration_exactly_3600_succeeds(self, gcp_sts):
        creds = gcp_sts.get_access_token(duration_seconds=3600)
        assert creds.security_token == "ya29.mock-access-token-abc123"

    def test_duration_none_does_not_raise(self, gcp_sts):
        creds = gcp_sts.get_access_token(duration_seconds=None)
        assert creds.security_token == "ya29.mock-access-token-abc123"


class TestGcpStsGetAccessTokenAudience:

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.IDTokenCredentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_returns_id_token_as_security_token(self, mock_req, mock_id_cls, gcp_sts):
        mock_id_creds = MagicMock()
        mock_id_creds.token = "eyJhbGciOiJSUzI1NiJ9.audience-scoped.sig"
        mock_id_creds.expiry = datetime(2026, 12, 1, 13, 0, 0)
        mock_id_cls.return_value = mock_id_creds

        creds = gcp_sts.get_access_token(audience="https://my-service.example.com")

        assert creds.security_token == "eyJhbGciOiJSUzI1NiJ9.audience-scoped.sig"
        assert creds.access_key_id == ""
        assert creds.access_key_secret == ""
        mock_id_cls.assert_called_once_with(
            target_credentials=gcp_sts._credentials,
            target_audience="https://my-service.example.com",
            include_email=True,
        )
        mock_id_creds.refresh.assert_called_once()

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.IDTokenCredentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_uses_id_creds_expiry(self, mock_req, mock_id_cls, gcp_sts):
        mock_id_creds = MagicMock()
        mock_id_creds.token = "jwt"
        mock_id_creds.expiry = datetime(2026, 12, 1, 14, 0, 0)
        mock_id_cls.return_value = mock_id_creds

        creds = gcp_sts.get_access_token(audience="https://example.com")
        assert creds.expiration == datetime(2026, 12, 1, 14, 0, 0)

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.IDTokenCredentials"
    )
    def test_audience_with_unsupported_cred_type_raises(self, mock_id_cls, gcp_sts):
        mock_id_cls.side_effect = AttributeError("no sign_bytes")
        with pytest.raises(
            UnsupportedOperationException, match="audience requires service account"
        ):
            gcp_sts.get_access_token(audience="https://example.com")

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.IDTokenCredentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_audience_refresh_error_translated(self, mock_req, mock_id_cls, gcp_sts):
        import google.auth.exceptions

        mock_id_creds = MagicMock()
        mock_id_creds.refresh.side_effect = google.auth.exceptions.RefreshError(
            "denied"
        )
        mock_id_cls.return_value = mock_id_creds
        with pytest.raises(google.auth.exceptions.RefreshError):
            gcp_sts.get_access_token(audience="https://example.com")


class TestGcpStsGetAccessTokenScopes:

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_returns_scoped_token(self, mock_req, mock_imp_cls, gcp_sts):
        impersonated = MagicMock()
        impersonated.token = "ya29.scoped-access-token"
        impersonated.expiry = datetime(2026, 12, 1, 13, 0, 0)
        mock_imp_cls.return_value = impersonated

        scopes = ["https://www.googleapis.com/auth/bigquery"]
        creds = gcp_sts.get_access_token(scopes=scopes)

        assert creds.security_token == "ya29.scoped-access-token"
        assert creds.expiration == datetime(2026, 12, 1, 13, 0, 0)
        mock_imp_cls.assert_called_once_with(
            source_credentials=gcp_sts._credentials,
            target_principal="test-sa@my-project.iam.gserviceaccount.com",
            target_scopes=scopes,
            lifetime=3600,
        )
        impersonated.refresh.assert_called_once()

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_scopes_uses_duration_seconds_as_lifetime(
        self, mock_req, mock_imp_cls, gcp_sts
    ):
        impersonated = MagicMock()
        impersonated.token = "ya29.x"
        impersonated.expiry = None
        mock_imp_cls.return_value = impersonated

        gcp_sts.get_access_token(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            duration_seconds=1800,
        )
        call_kwargs = mock_imp_cls.call_args[1]
        assert call_kwargs["lifetime"] == 1800

    def test_scopes_without_sa_email_raises(self, mock_credentials):
        del mock_credentials.service_account_email
        sts = GcpSts(credentials=mock_credentials, project="p")
        with pytest.raises(
            UnsupportedOperationException, match="service account credentials"
        ):
            sts.get_access_token(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_scopes_refresh_error_translated(self, mock_req, mock_imp_cls, gcp_sts):
        import google.auth.exceptions

        imp = MagicMock()
        imp.refresh.side_effect = google.auth.exceptions.RefreshError("denied")
        mock_imp_cls.return_value = imp
        with pytest.raises(google.auth.exceptions.RefreshError):
            gcp_sts.get_access_token(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )


class TestGcpStsAssumeRole:

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_basic(self, mock_req, mock_imp_cls, gcp_sts):
        impersonated = MagicMock()
        impersonated.token = "ya29.impersonated-token"
        impersonated.expiry = datetime(2026, 12, 1, 13, 30, 0)
        mock_imp_cls.return_value = impersonated

        creds = gcp_sts.assume_role(
            "target-sa@other-project.iam.gserviceaccount.com",
            session_name="my-session",
            expiration=1800,
        )
        mock_imp_cls.assert_called_once_with(
            source_credentials=gcp_sts._credentials,
            target_principal="target-sa@other-project.iam.gserviceaccount.com",
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
            lifetime=1800,
        )
        impersonated.refresh.assert_called_once()
        assert creds.access_key_id == ""
        assert creds.access_key_secret == ""
        assert creds.security_token == "ya29.impersonated-token"
        assert creds.expiration == datetime(2026, 12, 1, 13, 30, 0)

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_refresh_error_translated(self, mock_req, mock_imp_cls, gcp_sts):
        import google.auth.exceptions

        imp = MagicMock()
        imp.refresh.side_effect = google.auth.exceptions.RefreshError("denied")
        mock_imp_cls.return_value = imp
        with pytest.raises(google.auth.exceptions.RefreshError):
            gcp_sts.assume_role("sa@proj.iam.gserviceaccount.com", session_name="s")

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_invalid_target_raises(self, mock_req, mock_imp_cls, gcp_sts):
        mock_imp_cls.side_effect = ValueError("invalid target_principal")
        with pytest.raises(ValueError):
            gcp_sts.assume_role("bad-email", session_name="s")

    def test_credential_scope_empty_permissions_rejected(self, gcp_sts):
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
            gcp_sts.assume_role(
                "sa@proj.iam.gserviceaccount.com",
                session_name="s",
                credential_scope=scope,
            )

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.downscoped")
    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_with_credential_scope(
        self, mock_req, mock_imp_cls, mock_downscoped, gcp_sts
    ):
        from multicloud.sts.sts_client.driver import (
            AvailabilityCondition,
            CredentialScope,
            ScopeRule,
        )

        impersonated = MagicMock()
        mock_imp_cls.return_value = impersonated

        downscoped_creds = MagicMock()
        downscoped_creds.token = "ya29.dr.downscoped-token"
        downscoped_creds.expiry = datetime(2026, 12, 1, 14, 0, 0)
        mock_downscoped.Credentials.return_value = downscoped_creds

        scope = CredentialScope(
            rules=(
                ScopeRule(
                    available_resource="storage://my-bucket",
                    available_permissions=("storage:GetObject",),
                    availability_condition=AvailabilityCondition(
                        resource_prefix="storage://my-bucket/docs/",
                        title="Limit to docs",
                    ),
                ),
            )
        )
        creds = gcp_sts.assume_role(
            "sa@proj.iam.gserviceaccount.com",
            session_name="s",
            credential_scope=scope,
        )

        mock_downscoped.AccessBoundaryRule.assert_called_once()
        rule_kwargs = mock_downscoped.AccessBoundaryRule.call_args[1]
        assert (
            rule_kwargs["available_resource"]
            == "//storage.googleapis.com/projects/_/buckets/my-bucket"
        )
        assert (
            "inRole:roles/storage.objectViewer" in rule_kwargs["available_permissions"]
        )

        mock_downscoped.Credentials.assert_called_once()
        assert creds.security_token == "ya29.dr.downscoped-token"


class TestGcpStsCredentialScopeHelpers:

    def test_convert_permission_to_gcp(self):
        from multicloud.sts.sts_gcp.gcp_sts import _convert_permission_to_gcp

        assert (
            _convert_permission_to_gcp("storage:GetObject")
            == "inRole:roles/storage.objectViewer"
        )
        assert (
            _convert_permission_to_gcp("storage:PutObject")
            == "inRole:roles/storage.objectCreator"
        )
        assert (
            _convert_permission_to_gcp("storage:DeleteObject")
            == "inRole:roles/storage.objectAdmin"
        )
        assert (
            _convert_permission_to_gcp("storage:ListBucket")
            == "inRole:roles/storage.objectViewer"
        )

    def test_build_gcp_prefix_expression(self):
        from multicloud.sts.sts_gcp.gcp_sts import _build_gcp_prefix_expression

        expr = _build_gcp_prefix_expression("storage://my-bucket/docs/")
        assert "projects/_/buckets/my-bucket/objects/docs/" in expr
        assert "objectListPrefix" in expr
        assert ".startsWith('docs/')" in expr

    def test_build_gcp_prefix_expression_invalid_scheme(self):
        from multicloud.common.exceptions import InvalidArgumentException
        from multicloud.sts.sts_gcp.gcp_sts import _build_gcp_prefix_expression

        with pytest.raises(InvalidArgumentException, match="storage://"):
            _build_gcp_prefix_expression("docstore://table/prefix/")

    def test_build_gcp_prefix_expression_no_slash(self):
        from multicloud.common.exceptions import InvalidArgumentException
        from multicloud.sts.sts_gcp.gcp_sts import _build_gcp_prefix_expression

        with pytest.raises(InvalidArgumentException, match="bucket name"):
            _build_gcp_prefix_expression("storage://bucketonly")

    def test_escape_cel_string_single_quote(self):
        from multicloud.sts.sts_gcp.gcp_sts import _escape_cel_string

        assert _escape_cel_string("doc's") == "doc\\'s"

    def test_escape_cel_string_backslash(self):
        from multicloud.sts.sts_gcp.gcp_sts import _escape_cel_string

        assert _escape_cel_string("path\\to") == "path\\\\to"

    def test_escape_cel_string_control_char_rejected(self):
        from multicloud.common.exceptions import InvalidArgumentException
        from multicloud.sts.sts_gcp.gcp_sts import _escape_cel_string

        with pytest.raises(InvalidArgumentException, match="control character"):
            _escape_cel_string("prefix\ninjection")

    def test_build_gcp_prefix_expression_escapes_quotes(self):
        from multicloud.sts.sts_gcp.gcp_sts import _build_gcp_prefix_expression

        expr = _build_gcp_prefix_expression("storage://bucket/it's-a-path/")
        assert "\\'" in expr
        assert "it\\'s-a-path/" in expr


class TestGcpStsGenerateServiceAccountToken:

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_basic(self, mock_req, mock_imp_cls, gcp_sts):
        impersonated = MagicMock()
        impersonated.token = "ya29.sa-impersonated-token"
        impersonated.expiry = datetime(2026, 12, 1, 14, 0, 0)
        mock_imp_cls.return_value = impersonated

        creds = gcp_sts.generate_service_account_token(
            "target-sa@other-project.iam.gserviceaccount.com",
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

        mock_imp_cls.assert_called_once_with(
            source_credentials=gcp_sts._credentials,
            target_principal="target-sa@other-project.iam.gserviceaccount.com",
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
            lifetime=3600,
        )
        impersonated.refresh.assert_called_once()
        assert creds.security_token == "ya29.sa-impersonated-token"
        assert creds.expiration == datetime(2026, 12, 1, 14, 0, 0)
        assert creds.access_key_id == ""
        assert creds.access_key_secret == ""

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_lifetime_above_3600(self, mock_req, mock_imp_cls, gcp_sts):
        """generate_service_account_token allows lifetime > 3600s."""
        impersonated = MagicMock()
        impersonated.token = "ya29.long-lived"
        impersonated.expiry = None
        mock_imp_cls.return_value = impersonated

        gcp_sts.generate_service_account_token(
            "target-sa@other-project.iam.gserviceaccount.com",
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            lifetime=7200,
        )
        call_kwargs = mock_imp_cls.call_args[1]
        assert call_kwargs["lifetime"] == 7200

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_multiple_scopes(self, mock_req, mock_imp_cls, gcp_sts):
        impersonated = MagicMock()
        impersonated.token = "ya29.multi-scope"
        impersonated.expiry = None
        mock_imp_cls.return_value = impersonated

        scopes = [
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/bigquery",
        ]
        gcp_sts.generate_service_account_token(
            "sa@proj.iam.gserviceaccount.com", scopes=scopes
        )
        assert mock_imp_cls.call_args[1]["target_scopes"] == scopes

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_refresh_error_translated(self, mock_req, mock_imp_cls, gcp_sts):
        import google.auth.exceptions

        imp = MagicMock()
        imp.refresh.side_effect = google.auth.exceptions.RefreshError(
            "permission denied"
        )
        mock_imp_cls.return_value = imp
        with pytest.raises(google.auth.exceptions.RefreshError):
            gcp_sts.generate_service_account_token(
                "target-sa@proj.iam.gserviceaccount.com",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

    @patch(
        "multicloud.sts.sts_gcp.gcp_sts.google.auth.impersonated_credentials.Credentials"
    )
    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.transport.requests.Request")
    def test_invalid_principal_raises(self, mock_req, mock_imp_cls, gcp_sts):
        mock_imp_cls.side_effect = ValueError("invalid email")
        with pytest.raises(ValueError):
            gcp_sts.generate_service_account_token(
                "not-an-email",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )


_WIF_PROVIDER = (
    "//iam.googleapis.com/projects/123/locations/global/"
    "workloadIdentityPools/my-pool/providers/my-provider"
)


def _mock_sts_client(mock_client_cls, *, response=None, side_effect=None):
    """Wire ``google.oauth2.sts.Client`` to return a stub whose
    ``exchange_token`` yields ``response`` (or raises ``side_effect``)."""
    client = MagicMock()
    if side_effect is not None:
        client.exchange_token.side_effect = side_effect
    else:
        client.exchange_token.return_value = response
    mock_client_cls.return_value = client
    return client


class TestGcpStsAssumeRoleWithWebIdentity:

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.oauth2.sts.Client")
    def test_basic_token_exchange(self, mock_client_cls, gcp_sts):
        client = _mock_sts_client(
            mock_client_cls,
            response={"access_token": "ya29.federated-token", "expires_in": 3600},
        )

        creds = gcp_sts.assume_role_with_web_identity(
            _WIF_PROVIDER, "external.oidc.jwt"
        )

        assert creds.access_key_id == ""
        assert creds.access_key_secret == ""
        assert creds.security_token == "ya29.federated-token"
        assert creds.expiration is not None  # derived from expires_in

        mock_client_cls.assert_called_once_with(token_exchange_endpoint=_STS_ENDPOINT)

        # Inspect the RFC 8693 token-exchange arguments.
        kwargs = client.exchange_token.call_args.kwargs
        assert kwargs["audience"] == _WIF_PROVIDER
        assert kwargs["subject_token"] == "external.oidc.jwt"
        assert kwargs["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
        assert kwargs["subject_token_type"] == "urn:ietf:params:oauth:token-type:jwt"
        assert (
            kwargs["requested_token_type"]
            == "urn:ietf:params:oauth:token-type:access_token"
        )
        assert kwargs["scopes"] == gcp_sts._scopes

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.oauth2.sts.Client")
    def test_uses_endpoint_override(self, mock_client_cls, mock_credentials):
        _mock_sts_client(mock_client_cls, response={"access_token": "ya29.x"})
        sts = GcpSts(
            credentials=mock_credentials,
            project="p",
            endpoint_url="https://sts.example.test/v1/token",
        )
        sts.assume_role_with_web_identity(_WIF_PROVIDER, "tok")
        mock_client_cls.assert_called_once_with(
            token_exchange_endpoint="https://sts.example.test/v1/token"
        )

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.oauth2.sts.Client")
    def test_empty_role_raises_before_call(self, mock_client_cls, gcp_sts):
        with pytest.raises(InvalidArgumentException):
            gcp_sts.assume_role_with_web_identity("", "tok")
        mock_client_cls.assert_not_called()

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.oauth2.sts.Client")
    def test_empty_token_raises_before_call(self, mock_client_cls, gcp_sts):
        with pytest.raises(InvalidArgumentException):
            gcp_sts.assume_role_with_web_identity(_WIF_PROVIDER, "")
        mock_client_cls.assert_not_called()

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.oauth2.sts.Client")
    def test_no_access_token_in_response_raises(self, mock_client_cls, gcp_sts):
        _mock_sts_client(mock_client_cls, response={"issued_token_type": "x"})
        with pytest.raises(UnAuthorizedException, match="no access_token"):
            gcp_sts.assume_role_with_web_identity(_WIF_PROVIDER, "tok")

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.oauth2.sts.Client")
    def test_oauth_error_translated_to_unauthorized(self, mock_client_cls, gcp_sts):
        import google.auth.exceptions

        _mock_sts_client(
            mock_client_cls,
            side_effect=google.auth.exceptions.OAuthError("invalid_grant"),
        )
        with pytest.raises(google.auth.exceptions.OAuthError):
            gcp_sts.assume_role_with_web_identity(_WIF_PROVIDER, "tok")

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.oauth2.sts.Client")
    def test_transport_error_translated_to_unknown(self, mock_client_cls, gcp_sts):
        import google.auth.exceptions

        _mock_sts_client(
            mock_client_cls,
            side_effect=google.auth.exceptions.TransportError("connection refused"),
        )
        with pytest.raises(google.auth.exceptions.TransportError):
            gcp_sts.assume_role_with_web_identity(_WIF_PROVIDER, "tok")

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.oauth2.sts.Client")
    def test_non_numeric_expires_in_translated(self, mock_client_cls, gcp_sts):
        # A malformed expires_in propagates as a raw ValueError; translation
        # happens at the client layer (StsClient), not the provider.
        _mock_sts_client(
            mock_client_cls,
            response={"access_token": "ya29.x", "expires_in": "not-a-number"},
        )
        with pytest.raises(ValueError):
            gcp_sts.assume_role_with_web_identity(_WIF_PROVIDER, "tok")


class TestGcpStsTranslateError:

    def test_already_translated(self, gcp_sts):
        exc = UnAuthorizedException("x")
        assert gcp_sts.translate_error(exc) is exc

    def test_refresh_error(self, gcp_sts):
        import google.auth.exceptions

        assert isinstance(
            gcp_sts.translate_error(google.auth.exceptions.RefreshError("x")),
            UnAuthorizedException,
        )

    def test_transport_error(self, gcp_sts):
        import google.auth.exceptions

        assert isinstance(
            gcp_sts.translate_error(google.auth.exceptions.TransportError("x")),
            UnknownException,
        )

    def test_value_error(self, gcp_sts):
        assert isinstance(
            gcp_sts.translate_error(ValueError("x")), InvalidArgumentException
        )

    def test_unknown(self, gcp_sts):
        assert isinstance(gcp_sts.translate_error(RuntimeError("x")), UnknownException)


class TestGcpStsLifecycle:

    def test_close(self, gcp_sts):
        gcp_sts.close()

    def test_context_manager(self, mock_credentials):
        with GcpSts(credentials=mock_credentials, project="p") as sts:
            assert sts.get_provider_id() == "gcp"


class TestGcpStsCredentialsOverrider:

    def test_session_override_creates_oauth2_credentials(self):
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
            StsCredentials,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                security_token="ya29.explicit-token",
            ),
        )
        sts = GcpSts(
            region="us-central1",
            credentials_overrider=overrider,
            project="my-project",
        )
        assert sts._credentials.token == "ya29.explicit-token"

    @patch("multicloud.gcp_common.gcp_credentials.google.auth.default")
    @patch(
        "multicloud.gcp_common.gcp_credentials.google.auth.impersonated_credentials.Credentials"
    )
    def test_assume_role_override_creates_impersonated_credentials(
        self, mock_imp_cls, mock_default
    ):
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        source_creds = MagicMock()
        mock_default.return_value = (source_creds, "detected-project")

        impersonated = MagicMock()
        mock_imp_cls.return_value = impersonated

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="target-sa@other-project.iam.gserviceaccount.com",
            duration_seconds=1800,
        )
        sts = GcpSts(
            region="us-central1",
            credentials_overrider=overrider,
            project="my-project",
        )
        mock_imp_cls.assert_called_once_with(
            source_credentials=source_creds,
            target_principal="target-sa@other-project.iam.gserviceaccount.com",
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
            lifetime=1800,
        )
        assert sts._credentials is impersonated

    @patch("multicloud.gcp_common.gcp_credentials.google.auth.default")
    @patch(
        "multicloud.gcp_common.gcp_credentials.google.auth.impersonated_credentials.Credentials"
    )
    def test_assume_role_override_default_duration(self, mock_imp_cls, mock_default):
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        source_creds = MagicMock()
        mock_default.return_value = (source_creds, "proj")
        mock_imp_cls.return_value = MagicMock()

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="sa@proj.iam.gserviceaccount.com",
        )
        GcpSts(credentials_overrider=overrider, project="p")
        call_kwargs = mock_imp_cls.call_args[1]
        assert "lifetime" not in call_kwargs

    @patch("multicloud.gcp_common.gcp_credentials.google.auth.default")
    def test_assume_role_override_no_base_creds_raises(self, mock_default):
        import google.auth.exceptions
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

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
            GcpSts(credentials_overrider=overrider, project="p")

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.default")
    def test_web_identity_override_falls_through_to_default(self, mock_default):
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        default_creds = MagicMock()
        default_creds.valid = True
        default_creds.token = "ya29.default-token"
        mock_default.return_value = (default_creds, "default-project")

        token_supplier = lambda: "my-oidc-jwt"
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="sa@proj.iam.gserviceaccount.com",
            web_identity_token_supplier=token_supplier,
        )
        sts = GcpSts(
            credentials_overrider=overrider,
            project="my-project",
        )
        assert sts._credentials is default_creds

    @patch("multicloud.sts.sts_gcp.gcp_sts.google.auth.default")
    def test_web_identity_override_no_default_creds_raises(self, mock_default):
        import google.auth.exceptions
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        mock_default.side_effect = google.auth.exceptions.DefaultCredentialsError(
            "nope"
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="sa@proj.iam.gserviceaccount.com",
            web_identity_token_supplier=lambda: "jwt",
        )
        with pytest.raises(
            UnAuthorizedException, match="Could not find default credentials"
        ):
            GcpSts(credentials_overrider=overrider, project="p")

    @patch("multicloud.sts.sts_gcp.gcp_sts.get_credentials")
    def test_credentials_take_precedence_over_overrider(
        self, mock_get_credentials, mock_credentials
    ):
        # If both ``credentials`` and ``credentials_overrider`` are passed,
        # the injected credentials win and the overrider is silently ignored.
        from multicloud.sts.sts_client.driver import (
            CredentialsOverrider,
            CredentialsType,
            StsCredentials,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token="tok"),
        )
        s = GcpSts(
            credentials=mock_credentials,
            credentials_overrider=overrider,
            project="p",
        )
        assert s._credentials is mock_credentials
        mock_get_credentials.assert_not_called()
