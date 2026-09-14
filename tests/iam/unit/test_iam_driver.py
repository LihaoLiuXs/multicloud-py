"""Unit tests for iam_client/driver.py — dataclasses, validator, ABC."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
from multicloud.common.exceptions import InvalidArgumentException
from multicloud.iam.iam_client.driver import (
    BaseIam,
    CreateIdentityRequest,
    CreateOptions,
    DeleteIdentityRequest,
    GetIdentityRequest,
    IamValidator,
    TrustConfiguration,
)


class TestDataclasses:
    def test_create_identity_request_defaults(self):
        req = CreateIdentityRequest(identity_name="role1", tenant_id="123")
        assert req.identity_name == "role1"
        assert req.tenant_id == "123"
        assert req.description is None
        assert req.region is None
        assert req.trust_config is None
        assert req.options is None

    def test_create_identity_request_full(self):
        tc = TrustConfiguration(trusted_principals=["trusted-principal-1"])
        opts = CreateOptions(path="/svc/", max_session_duration=3600)
        req = CreateIdentityRequest(
            identity_name="role1",
            description="test role",
            tenant_id="123",
            region="us-west-2",
            trust_config=tc,
            options=opts,
        )
        assert req.description == "test role"
        assert req.trust_config.trusted_principals == ["trusted-principal-1"]
        assert req.options.max_session_duration == 3600

    def test_get_identity_request(self):
        req = GetIdentityRequest(identity_name="role1", tenant_id="123")
        assert req.identity_name == "role1"
        assert req.region is None

    def test_delete_identity_request(self):
        req = DeleteIdentityRequest(
            identity_name="role1", tenant_id="123", region="us-east-1"
        )
        assert req.region == "us-east-1"

    def test_trust_configuration_defaults(self):
        tc = TrustConfiguration()
        assert tc.trusted_principals == []
        assert tc.conditions == {}

    def test_create_options_defaults(self):
        opts = CreateOptions()
        assert opts.path is None
        assert opts.max_session_duration is None
        assert opts.permission_boundary is None

    def test_create_options_full(self):
        opts = CreateOptions(
            path="/service-roles/",
            max_session_duration=7200,
            permission_boundary="boundary-policy-id",
        )
        assert opts.path == "/service-roles/"
        assert opts.max_session_duration == 7200


class TestTrustConfigurationConstruction:
    def test_with_principals(self):
        tc = TrustConfiguration(
            trusted_principals=["principal-1", "principal-2"],
        )
        assert len(tc.trusted_principals) == 2

    def test_with_conditions(self):
        tc = TrustConfiguration(
            trusted_principals=["principal-1"],
            conditions={"StringEquals": {"key": "value"}},
        )
        assert tc.conditions == {"StringEquals": {"key": "value"}}

    def test_with_multiple_conditions(self):
        tc = TrustConfiguration(
            trusted_principals=["principal-1"],
            conditions={
                "StringEquals": {"key1": "val1", "key2": "val2"},
                "IpAddress": {"source-ip": "10.0.0.0/8"},
            },
        )
        assert tc.conditions["StringEquals"] == {"key1": "val1", "key2": "val2"}
        assert tc.conditions["IpAddress"] == {"source-ip": "10.0.0.0/8"}


class TestIamValidator:
    def setup_method(self):
        self.v = IamValidator()

    def test_validate_create_identity_blank_name(self):
        with pytest.raises(InvalidArgumentException, match="identity_name"):
            self.v.validate_create_identity("", "tenant")

    def test_validate_create_identity_blank_tenant(self):
        with pytest.raises(InvalidArgumentException, match="tenant_id"):
            self.v.validate_create_identity("role", "")

    def test_validate_create_identity_ok(self):
        self.v.validate_create_identity("role", "tenant")

    def test_validate_get_identity_blank_name(self):
        with pytest.raises(InvalidArgumentException, match="identity_name"):
            self.v.validate_get_identity("", "tenant")

    def test_validate_get_identity_blank_tenant(self):
        with pytest.raises(InvalidArgumentException, match="tenant_id"):
            self.v.validate_get_identity("role", "")

    def test_validate_get_identity_ok(self):
        self.v.validate_get_identity("role", "tenant")

    def test_validate_delete_identity_blank_name(self):
        with pytest.raises(InvalidArgumentException, match="identity_name"):
            self.v.validate_delete_identity("", "tenant")

    def test_validate_delete_identity_blank_tenant(self):
        with pytest.raises(InvalidArgumentException, match="tenant_id"):
            self.v.validate_delete_identity("role", "")

    def test_validate_delete_identity_ok(self):
        self.v.validate_delete_identity("role", "tenant")


class StubIamStore(BaseIam):
    """Minimal concrete implementation that records calls."""

    def __init__(self, region=None, credentials_overrider=None):
        super().__init__(region=region, credentials_overrider=credentials_overrider)
        self.calls: Dict[str, list] = {}

    def translate_error(self, exc):
        from multicloud.common.exceptions import UnknownException

        return UnknownException(str(exc))

    def do_create_identity(self, request):
        self.calls.setdefault("do_create_identity", []).append(request)
        return "stub-identity-id"

    def do_get_identity(self, request):
        self.calls.setdefault("do_get_identity", []).append(request)
        return "stub-arn"

    def do_delete_identity(self, request):
        self.calls.setdefault("do_delete_identity", []).append(request)

    def do_attach_inline_policy(self, request):
        self.calls.setdefault("do_attach_inline_policy", []).append(request)

    def do_get_inline_policy_details(self, request):
        self.calls.setdefault("do_get_inline_policy_details", []).append(request)
        return '{"policy": "details"}'

    def do_get_attached_policies(self, request):
        self.calls.setdefault("do_get_attached_policies", []).append(request)
        return ["policy-1"]

    def do_remove_policy(self, request):
        self.calls.setdefault("do_remove_policy", []).append(request)


class TestBaseIamTemplate:
    def setup_method(self):
        self.store = StubIamStore(region="us-west-2")

    def test_create_identity_delegates(self):
        result = self.store.create_identity(
            "role1",
            description="desc",
            tenant_id="tenant",
            region="us-west-2",
            trust_config=TrustConfiguration(),
            options=CreateOptions(),
        )
        assert result == "stub-identity-id"
        assert "do_create_identity" in self.store.calls
        req = self.store.calls["do_create_identity"][0]
        assert req.identity_name == "role1"
        assert req.description == "desc"
        assert req.tenant_id == "tenant"

    def test_create_identity_with_none_trust_config_and_options(self):
        result = self.store.create_identity(
            "role1",
            tenant_id="tenant",
        )
        assert result == "stub-identity-id"
        req = self.store.calls["do_create_identity"][0]
        assert req.trust_config is None
        assert req.options is None

    def test_create_identity_validates(self):
        with pytest.raises(InvalidArgumentException):
            self.store.create_identity(
                "",
                description="desc",
                tenant_id="tenant",
                region=None,
            )

    def test_get_identity_delegates(self):
        result = self.store.get_identity("role1", tenant_id="tenant")
        assert result == "stub-arn"
        assert "do_get_identity" in self.store.calls

    def test_get_identity_validates(self):
        with pytest.raises(InvalidArgumentException):
            self.store.get_identity("", tenant_id="tenant")

    def test_delete_identity_delegates(self):
        self.store.delete_identity("role1", tenant_id="tenant")
        assert "do_delete_identity" in self.store.calls

    def test_delete_identity_validates(self):
        with pytest.raises(InvalidArgumentException):
            self.store.delete_identity("role1", tenant_id="")

    def test_context_manager(self):
        with StubIamStore() as s:
            s.create_identity("r", tenant_id="t")

    def test_region_property(self):
        assert self.store.region == "us-west-2"
        s = StubIamStore()
        assert s.region is None

    def test_credentials_overrider_stored(self):
        from multicloud.common.credentials import StsCredentials
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                access_key_id="AK",
                access_key_secret="SK",
            ),
        )
        s = StubIamStore(credentials_overrider=overrider)
        assert s._credentials_overrider is overrider

    def test_credentials_overrider_defaults_none(self):
        assert self.store._credentials_overrider is None

    # -- Policy management template-method tests --

    def test_attach_inline_policy_delegates(self):
        from multicloud.iam.iam_client.model import (
            Effect,
            PolicyDocument,
            Statement,
            StorageActions,
        )

        doc = PolicyDocument(
            name="P",
            statements=(
                Statement(effect=Effect.ALLOW, actions=(StorageActions.GET_OBJECT,)),
            ),
        )
        self.store.attach_inline_policy(
            "role1", policy_document=doc, tenant_id="tenant"
        )
        assert "do_attach_inline_policy" in self.store.calls
        req = self.store.calls["do_attach_inline_policy"][0]
        assert req.identity_name == "role1"
        assert req.policy_document.name == "P"

    def test_attach_inline_policy_validates_identity(self):
        from multicloud.iam.iam_client.model import (
            Effect,
            PolicyDocument,
            Statement,
            StorageActions,
        )

        doc = PolicyDocument(
            name="P",
            statements=(
                Statement(effect=Effect.ALLOW, actions=(StorageActions.GET_OBJECT,)),
            ),
        )
        with pytest.raises(InvalidArgumentException, match="identity_name"):
            self.store.attach_inline_policy("", policy_document=doc, tenant_id="t")

    def test_attach_inline_policy_validates_policy_document_none(self):
        with pytest.raises(InvalidArgumentException, match="policy_document"):
            self.store.attach_inline_policy(
                "role1", policy_document=None, tenant_id="t"
            )

    def test_attach_inline_policy_validates_empty_statements(self):
        from multicloud.iam.iam_client.model import PolicyDocument

        doc = PolicyDocument(name="P")
        with pytest.raises(InvalidArgumentException, match="at least one statement"):
            self.store.attach_inline_policy("role1", policy_document=doc, tenant_id="t")

    def test_attach_inline_policy_validates_tenant(self):
        from multicloud.iam.iam_client.model import (
            Effect,
            PolicyDocument,
            Statement,
            StorageActions,
        )

        doc = PolicyDocument(
            name="P",
            statements=(
                Statement(effect=Effect.ALLOW, actions=(StorageActions.GET_OBJECT,)),
            ),
        )
        with pytest.raises(InvalidArgumentException, match="tenant_id"):
            self.store.attach_inline_policy("role1", policy_document=doc, tenant_id="")

    def test_get_inline_policy_details_delegates(self):
        result = self.store.get_inline_policy_details(
            "role1", policy_name="P", tenant_id="tenant"
        )
        assert result == '{"policy": "details"}'
        assert "do_get_inline_policy_details" in self.store.calls

    def test_get_inline_policy_details_validates(self):
        with pytest.raises(InvalidArgumentException, match="identity_name"):
            self.store.get_inline_policy_details("", policy_name="P", tenant_id="t")
        with pytest.raises(InvalidArgumentException, match="policy_name"):
            self.store.get_inline_policy_details("r", policy_name="", tenant_id="t")
        with pytest.raises(InvalidArgumentException, match="tenant_id"):
            self.store.get_inline_policy_details("r", policy_name="P", tenant_id="")

    def test_get_attached_policies_delegates(self):
        result = self.store.get_attached_policies("role1", tenant_id="tenant")
        assert result == ["policy-1"]
        assert "do_get_attached_policies" in self.store.calls

    def test_get_attached_policies_validates(self):
        with pytest.raises(InvalidArgumentException, match="identity_name"):
            self.store.get_attached_policies("", tenant_id="t")
        with pytest.raises(InvalidArgumentException, match="tenant_id"):
            self.store.get_attached_policies("r", tenant_id="")

    def test_remove_policy_delegates(self):
        self.store.remove_policy("role1", policy_name="P", tenant_id="tenant")
        assert "do_remove_policy" in self.store.calls
        req = self.store.calls["do_remove_policy"][0]
        assert req.identity_name == "role1"
        assert req.policy_name == "P"

    def test_remove_policy_validates(self):
        with pytest.raises(InvalidArgumentException, match="identity_name"):
            self.store.remove_policy("", policy_name="P", tenant_id="t")
        with pytest.raises(InvalidArgumentException, match="policy_name"):
            self.store.remove_policy("r", policy_name="", tenant_id="t")
        with pytest.raises(InvalidArgumentException, match="tenant_id"):
            self.store.remove_policy("r", policy_name="P", tenant_id="")
