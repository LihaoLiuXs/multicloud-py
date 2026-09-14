"""Unit tests for the STS driver layer (BaseSts, StsValidator, value objects)."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Optional

import pytest
from multicloud.common.exceptions import InvalidArgumentException, SubstrateSdkException
from multicloud.sts.sts_client.driver import (
    STS_REGISTRY,
    AvailabilityCondition,
    BaseSts,
    CallerIdentity,
    CredentialScope,
    CredentialsOverrider,
    CredentialsType,
    ScopeRule,
    StsCredentials,
    StsValidator,
)


class _FakeSts(BaseSts):
    """Minimal concrete implementation for testing BaseSts."""

    provider_id = "fake-sts"

    def __init__(self, region: Optional[str] = None, **kwargs):
        super().__init__(region=region)
        self.assume_role_calls = []
        self.get_identity_calls = 0
        self.get_token_calls: list = []
        self.generate_sa_token_calls: list = []
        self.web_identity_calls: list = []

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        return SubstrateSdkException(str(exc))

    def do_get_caller_identity(self, *, aud: Optional[str] = None) -> CallerIdentity:
        self.get_identity_calls += 1
        return CallerIdentity(
            user_id="SAMPLE_USER_ID",
            cloud_resource_name="cloud://account-123456/user/testuser",
            account_id="123456",
        )

    def do_assume_role(
        self,
        role: str,
        *,
        session_name: str,
        expiration: int,
        external_id: Optional[str],
        policy: Optional[str],
        tags: Mapping[str, str],
        credential_scope: Optional[CredentialScope] = None,
    ) -> StsCredentials:
        self.assume_role_calls.append(role)
        return StsCredentials(
            access_key_id="TEMP_AKID",
            access_key_secret="TEMP_SECRET",
            security_token="TEMP_TOKEN",
        )

    def do_get_access_token(
        self,
        *,
        audience: Optional[str] = None,
        scopes: Optional[list[str]] = None,
        duration_seconds: Optional[int] = None,
    ) -> StsCredentials:
        self.get_token_calls.append(
            {
                "audience": audience,
                "scopes": scopes,
                "duration_seconds": duration_seconds,
            }
        )
        return StsCredentials(
            access_key_id="AKID",
            access_key_secret="SECRET",
            security_token="TOKEN",
        )

    def do_generate_service_account_token(
        self,
        target_principal: str,
        *,
        scopes: list[str],
        lifetime: int,
    ) -> StsCredentials:
        self.generate_sa_token_calls.append(
            {
                "target_principal": target_principal,
                "scopes": scopes,
                "lifetime": lifetime,
            }
        )
        return StsCredentials(security_token="SA_TOKEN")

    def do_assume_role_with_web_identity(
        self,
        role: str,
        web_identity_token: str,
        *,
        session_name: str,
        expiration: int,
    ) -> StsCredentials:
        self.web_identity_calls.append((role, web_identity_token))
        return StsCredentials(
            access_key_id="WI_AKID",
            access_key_secret="WI_SECRET",
            security_token="WI_TOKEN",
        )


class TestStsValidator:
    """Tests for STS input validation."""

    def setup_method(self):
        self.validator = StsValidator()

    def test_validate_assume_role_empty_role_raises(self):
        with pytest.raises(InvalidArgumentException, match="role is required"):
            self.validator.validate_assume_role("", 3600)

    def test_validate_assume_role_zero_expiration_raises(self):
        with pytest.raises(InvalidArgumentException, match="must be positive"):
            self.validator.validate_assume_role("cross-account-role", 0)

    def test_validate_assume_role_negative_expiration_raises(self):
        with pytest.raises(InvalidArgumentException, match="must be positive"):
            self.validator.validate_assume_role("cross-account-role", -100)

    def test_validate_assume_role_valid_passes(self):
        self.validator.validate_assume_role("cross-account-role", 3600)

    def test_validate_get_access_token_audience_and_scopes_raises(self):
        with pytest.raises(InvalidArgumentException, match="mutually exclusive"):
            self.validator.validate_get_access_token(
                audience="https://example.com",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

    def test_validate_get_access_token_audience_only_passes(self):
        self.validator.validate_get_access_token(
            audience="https://example.com", scopes=None
        )

    def test_validate_get_access_token_scopes_only_passes(self):
        self.validator.validate_get_access_token(
            audience=None,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )

    def test_validate_get_access_token_neither_passes(self):
        self.validator.validate_get_access_token(audience=None, scopes=None)

    def test_validate_generate_service_account_token_empty_principal_raises(self):
        with pytest.raises(
            InvalidArgumentException, match="target_principal is required"
        ):
            self.validator.validate_generate_service_account_token(
                "", ["https://www.googleapis.com/auth/cloud-platform"]
            )

    def test_validate_generate_service_account_token_empty_scopes_raises(self):
        with pytest.raises(InvalidArgumentException, match="scopes must be non-empty"):
            self.validator.validate_generate_service_account_token(
                "sa@project.iam.gserviceaccount.com", []
            )

    def test_validate_generate_service_account_token_valid_passes(self):
        self.validator.validate_generate_service_account_token(
            "sa@project.iam.gserviceaccount.com",
            ["https://www.googleapis.com/auth/cloud-platform"],
        )

    def test_validate_web_identity_empty_role_raises(self):
        with pytest.raises(InvalidArgumentException, match="role is required"):
            self.validator.validate_assume_role_with_web_identity("", "tok")

    def test_validate_web_identity_empty_token_raises(self):
        with pytest.raises(
            InvalidArgumentException, match="web_identity_token is required"
        ):
            self.validator.validate_assume_role_with_web_identity(
                "arn:aws:iam::123:role/R", ""
            )

    def test_validate_web_identity_valid_passes(self):
        self.validator.validate_assume_role_with_web_identity(
            "arn:aws:iam::123:role/R", "tok"
        )


class TestBaseSts:
    """Tests for the BaseSts abstract base + validation wiring."""

    def test_init_stores_region(self):
        sts = _FakeSts(region="ap-southeast-1")
        assert sts.region == "ap-southeast-1"

    def test_init_region_defaults_to_none(self):
        sts = _FakeSts()
        assert sts.region is None

    def test_get_caller_identity_delegates(self):
        sts = _FakeSts()
        identity = sts.get_caller_identity()
        assert identity.user_id == "SAMPLE_USER_ID"
        assert identity.cloud_resource_name == "cloud://account-123456/user/testuser"
        assert identity.account_id == "123456"
        assert sts.get_identity_calls == 1

    def test_get_caller_identity_with_aud(self):
        sts = _FakeSts()
        identity = sts.get_caller_identity(aud="my-service")
        assert sts.get_identity_calls == 1

    def test_assume_role_validates_then_delegates(self):
        sts = _FakeSts()
        creds = sts.assume_role("cross-account-role", session_name="s")
        assert creds.access_key_id == "TEMP_AKID"
        assert creds.security_token == "TEMP_TOKEN"
        assert len(sts.assume_role_calls) == 1

    def test_assume_role_empty_role_raises(self):
        sts = _FakeSts()
        with pytest.raises(InvalidArgumentException):
            sts.assume_role("")

    def test_assume_role_zero_expiration_raises(self):
        sts = _FakeSts()
        with pytest.raises(InvalidArgumentException):
            sts.assume_role("cross-account-role", expiration=0)

    def test_get_access_token_delegates(self):
        sts = _FakeSts()
        creds = sts.get_access_token()
        assert creds.security_token == "TOKEN"
        assert len(sts.get_token_calls) == 1

    def test_get_access_token_with_duration(self):
        sts = _FakeSts()
        sts.get_access_token(duration_seconds=900)
        assert sts.get_token_calls[0]["duration_seconds"] == 900

    def test_get_access_token_with_audience(self):
        sts = _FakeSts()
        sts.get_access_token(audience="https://my-service.example.com")
        assert sts.get_token_calls[0]["audience"] == "https://my-service.example.com"
        assert sts.get_token_calls[0]["scopes"] is None

    def test_get_access_token_with_scopes(self):
        sts = _FakeSts()
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        sts.get_access_token(scopes=scopes)
        assert sts.get_token_calls[0]["scopes"] == scopes
        assert sts.get_token_calls[0]["audience"] is None

    def test_get_access_token_audience_and_scopes_raises_before_delegate(self):
        sts = _FakeSts()
        with pytest.raises(InvalidArgumentException, match="mutually exclusive"):
            sts.get_access_token(
                audience="https://example.com",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        assert len(sts.get_token_calls) == 0

    def test_generate_service_account_token_delegates(self):
        sts = _FakeSts()
        creds = sts.generate_service_account_token(
            "sa@proj.iam.gserviceaccount.com",
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        assert creds.security_token == "SA_TOKEN"
        assert len(sts.generate_sa_token_calls) == 1
        call = sts.generate_sa_token_calls[0]
        assert call["target_principal"] == "sa@proj.iam.gserviceaccount.com"
        assert call["lifetime"] == 3600

    def test_generate_service_account_token_custom_lifetime(self):
        sts = _FakeSts()
        sts.generate_service_account_token(
            "sa@proj.iam.gserviceaccount.com",
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            lifetime=7200,
        )
        assert sts.generate_sa_token_calls[0]["lifetime"] == 7200

    def test_generate_service_account_token_empty_principal_raises(self):
        sts = _FakeSts()
        with pytest.raises(
            InvalidArgumentException, match="target_principal is required"
        ):
            sts.generate_service_account_token(
                "",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        assert len(sts.generate_sa_token_calls) == 0

    def test_generate_service_account_token_empty_scopes_raises(self):
        sts = _FakeSts()
        with pytest.raises(InvalidArgumentException, match="scopes must be non-empty"):
            sts.generate_service_account_token(
                "sa@proj.iam.gserviceaccount.com",
                scopes=[],
            )
        assert len(sts.generate_sa_token_calls) == 0

    def test_assume_role_with_web_identity_validates_then_delegates(self):
        sts = _FakeSts()
        creds = sts.assume_role_with_web_identity(
            "arn:aws:iam::123:role/R", "oidc-token", session_name="s"
        )
        assert creds.security_token == "WI_TOKEN"
        assert sts.web_identity_calls == [("arn:aws:iam::123:role/R", "oidc-token")]

    def test_assume_role_with_web_identity_empty_role_raises(self):
        sts = _FakeSts()
        with pytest.raises(InvalidArgumentException):
            sts.assume_role_with_web_identity("", "oidc-token")

    def test_assume_role_with_web_identity_empty_token_raises(self):
        sts = _FakeSts()
        with pytest.raises(InvalidArgumentException):
            sts.assume_role_with_web_identity("arn:aws:iam::123:role/R", "")

    def test_close_is_noop_by_default(self):
        sts = _FakeSts()
        sts.close()  # should not raise

    def test_context_manager_protocol(self):
        with _FakeSts(region="us-west-2") as sts:
            assert sts.region == "us-west-2"

    def test_provider_error_propagates_untranslated(self):
        """Driver does not catch do_* errors; raw exceptions propagate to the client."""

        class _ErroringSts(_FakeSts):
            def do_get_caller_identity(self, *, aud=None):
                raise RuntimeError("boom")

        sts = _ErroringSts()
        with pytest.raises(RuntimeError, match="boom"):
            sts.get_caller_identity()

    def test_generate_service_account_token_error_propagates_untranslated(self):
        """Raw errors from do_generate_service_account_token propagate untranslated."""

        class _ErroringSts(_FakeSts):
            def do_generate_service_account_token(
                self, target_principal, *, scopes, lifetime
            ):
                raise OSError("connection refused")

        sts = _ErroringSts()
        with pytest.raises(OSError, match="connection refused"):
            sts.generate_service_account_token(
                "sa@proj.iam.gserviceaccount.com",
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

    def test_domain_exception_propagates_through_driver(self):
        """A SubstrateSdkException raised by do_* propagates unchanged through the driver."""

        class _PassThroughSts(_FakeSts):
            def translate_error(self, exc):
                if isinstance(exc, SubstrateSdkException):
                    return exc
                return SubstrateSdkException(str(exc))

            def do_get_access_token(
                self,
                *,
                audience=None,
                scopes=None,
                duration_seconds=None,
            ):
                raise InvalidArgumentException("already mapped")

        sts = _PassThroughSts()
        with pytest.raises(InvalidArgumentException, match="already mapped"):
            sts.get_access_token()


class TestStsDataclasses:
    """Tests for STS-related frozen dataclasses."""

    def test_sts_credentials_fields(self):
        creds = StsCredentials(
            access_key_id="AK",
            access_key_secret="SK",
            security_token="ST",
            expiration=datetime(2024, 1, 1, 12, 0, 0),
        )
        assert creds.access_key_id == "AK"
        assert creds.access_key_secret == "SK"
        assert creds.security_token == "ST"
        assert creds.expiration == datetime(2024, 1, 1, 12, 0, 0)

    def test_sts_credentials_defaults(self):
        creds = StsCredentials()
        assert creds.access_key_id == ""
        assert creds.access_key_secret == ""
        assert creds.security_token == ""
        assert creds.expiration is None

    def test_caller_identity_fields(self):
        identity = CallerIdentity(
            user_id="UID",
            cloud_resource_name="cloud://account-123/user/dev",
            account_id="123",
        )
        assert identity.user_id == "UID"
        assert identity.cloud_resource_name == "cloud://account-123/user/dev"
        assert identity.account_id == "123"

    def test_caller_identity_defaults(self):
        identity = CallerIdentity()
        assert identity.user_id == ""
        assert identity.cloud_resource_name == ""
        assert identity.account_id == ""

    def test_frozen_immutability(self):
        creds = StsCredentials(access_key_id="A")
        with pytest.raises(AttributeError):
            creds.access_key_id = "B"  # type: ignore[misc]


class TestCredentialsType:
    def test_all_types_are_defined(self):
        names = {t.name for t in CredentialsType}
        assert names == {"SESSION", "ASSUME_ROLE", "ASSUME_ROLE_WEB_IDENTITY"}


class TestCredentialsOverrider:
    def test_session_with_credentials(self):
        creds = StsCredentials(
            access_key_id="AK",
            access_key_secret="SK",
            security_token="ST",
        )
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=creds,
        )
        assert overrider.credentials_type == CredentialsType.SESSION
        assert overrider.session_credentials is creds
        assert overrider.role is None
        assert overrider.duration_seconds is None

    def test_assume_role_with_all_fields(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="cross-account-role",
            session_name="my-session",
            duration_seconds=7200,
        )
        assert overrider.credentials_type == CredentialsType.ASSUME_ROLE
        assert overrider.role == "cross-account-role"
        assert overrider.session_name == "my-session"
        assert overrider.duration_seconds == 7200

    def test_web_identity_with_supplier(self):
        supplier = lambda: "jwt-token-value"
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="cross-account-role",
            web_identity_token_supplier=supplier,
            session_name="web-session",
            duration_seconds=3600,
        )
        assert overrider.credentials_type == CredentialsType.ASSUME_ROLE_WEB_IDENTITY
        assert overrider.role == "cross-account-role"
        assert overrider.web_identity_token_supplier is supplier
        assert overrider.web_identity_token_supplier() == "jwt-token-value"

    def test_frozen(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                access_key_id="a", access_key_secret="b"
            ),
        )
        with pytest.raises(AttributeError):
            overrider.credentials_type = CredentialsType.ASSUME_ROLE  # type: ignore[misc]

    # error-path tests for __post_init__ validation
    def test_session_missing_credentials_raises(self):
        with pytest.raises(
            InvalidArgumentException, match="session_credentials required"
        ):
            CredentialsOverrider(credentials_type=CredentialsType.SESSION)

    def test_assume_role_missing_role_raises(self):
        with pytest.raises(InvalidArgumentException, match="role required"):
            CredentialsOverrider(credentials_type=CredentialsType.ASSUME_ROLE)

    def test_web_identity_missing_role_raises(self):
        with pytest.raises(InvalidArgumentException, match="role required"):
            CredentialsOverrider(
                credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
                web_identity_token_supplier=lambda: "tok",
            )

    def test_web_identity_missing_supplier_raises(self):
        with pytest.raises(
            InvalidArgumentException, match="web_identity_token_supplier required"
        ):
            CredentialsOverrider(
                credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
                role="cross-account-role",
            )

    def test_duration_seconds_zero_raises(self):
        with pytest.raises(
            InvalidArgumentException, match="duration_seconds must be positive"
        ):
            CredentialsOverrider(
                credentials_type=CredentialsType.ASSUME_ROLE,
                role="cross-account-role",
                duration_seconds=0,
            )

    def test_duration_seconds_negative_raises(self):
        with pytest.raises(
            InvalidArgumentException, match="duration_seconds must be positive"
        ):
            CredentialsOverrider(
                credentials_type=CredentialsType.ASSUME_ROLE,
                role="cross-account-role",
                duration_seconds=-1,
            )


class TestCredentialScope:
    def test_assume_role_accepts_credential_scope(self):
        sts = _FakeSts()
        scope = CredentialScope(
            rules=(
                ScopeRule(
                    available_resource="storage://my-bucket",
                    available_permissions=("storage:GetObject",),
                ),
            )
        )
        creds = sts.assume_role(
            "arn:aws:iam::123:role/R",
            session_name="sess",
            credential_scope=scope,
        )
        assert creds.access_key_id == "TEMP_AKID"

    def test_empty_permissions_rejected(self):
        sts = _FakeSts()
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
                session_name="sess",
                credential_scope=scope,
            )

    def test_frozen_immutability(self):
        scope = CredentialScope(rules=())
        with pytest.raises(AttributeError):
            scope.rules = ()  # type: ignore[misc]

        rule = ScopeRule(
            available_resource="storage://b",
            available_permissions=("storage:GetObject",),
        )
        with pytest.raises(AttributeError):
            rule.available_resource = "other"  # type: ignore[misc]

        cond = AvailabilityCondition(resource_prefix="storage://b/p/")
        with pytest.raises(AttributeError):
            cond.resource_prefix = "other"  # type: ignore[misc]
