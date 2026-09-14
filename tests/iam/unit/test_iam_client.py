"""Unit tests for iam_client/client.py — IamClient (direct kwargs, no builder)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from multicloud.iam.iam_client.client import IamClient
from multicloud.iam.iam_client.driver import (
    IAM_REGISTRY,
    BaseIam,
    CreateOptions,
    TrustConfiguration,
)


class TestIamClient:
    def setup_method(self):
        self.mock_store = MagicMock(spec=BaseIam)
        self.mock_store.get_provider_id.return_value = "test"
        self.mock_store.region = "us-west-2"
        # Construct client by injecting store directly (internal)
        self.client = IamClient.__new__(IamClient)
        self.client._iam = self.mock_store

    def test_create_identity_delegates(self):
        self.mock_store.create_identity.return_value = "test-arn"
        tc = TrustConfiguration()
        opts = CreateOptions()

        result = self.client.create_identity(
            "role1",
            description="desc",
            tenant_id="tenant",
            region="us-west-2",
            trust_config=tc,
            options=opts,
        )

        assert result == "test-arn"
        self.mock_store.create_identity.assert_called_once_with(
            "role1",
            description="desc",
            tenant_id="tenant",
            region="us-west-2",
            trust_config=tc,
            options=opts,
        )

    def test_get_identity_delegates(self):
        self.mock_store.get_identity.return_value = "provider-identity-id-123"

        result = self.client.get_identity(
            "role1", tenant_id="tenant", region="us-west-2"
        )

        assert result == "provider-identity-id-123"
        self.mock_store.get_identity.assert_called_once_with(
            "role1", tenant_id="tenant", region="us-west-2"
        )

    def test_delete_identity_delegates(self):
        self.client.delete_identity("role1", tenant_id="tenant", region="us-west-2")

        self.mock_store.delete_identity.assert_called_once_with(
            "role1", tenant_id="tenant", region="us-west-2"
        )

    def test_provider_property(self):
        assert self.client.provider == "test"

    def test_region_property(self):
        assert self.client.region == "us-west-2"

    def test_close_delegates(self):
        self.client.close()
        self.mock_store.close.assert_called_once()

    def test_context_manager(self):
        with self.client:
            pass
        self.mock_store.close.assert_called_once()

    # -- Policy management forwarding --

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
        self.client.attach_inline_policy(
            "role1", policy_document=doc, tenant_id="t", region="r"
        )

        self.mock_store.attach_inline_policy.assert_called_once_with(
            "role1", policy_document=doc, tenant_id="t", region="r"
        )

    def test_get_inline_policy_details_delegates(self):
        self.mock_store.get_inline_policy_details.return_value = '{"json": true}'

        result = self.client.get_inline_policy_details(
            "role1", policy_name="P", tenant_id="t", region="r"
        )

        assert result == '{"json": true}'
        self.mock_store.get_inline_policy_details.assert_called_once_with(
            "role1", policy_name="P", tenant_id="t", region="r"
        )

    def test_get_attached_policies_delegates(self):
        self.mock_store.get_attached_policies.return_value = ["P1", "P2"]

        result = self.client.get_attached_policies("role1", tenant_id="t", region="r")

        assert result == ["P1", "P2"]
        self.mock_store.get_attached_policies.assert_called_once_with(
            "role1", tenant_id="t", region="r"
        )

    def test_remove_policy_delegates(self):
        self.client.remove_policy("role1", policy_name="P", tenant_id="t", region="r")

        self.mock_store.remove_policy.assert_called_once_with(
            "role1", policy_name="P", tenant_id="t", region="r"
        )


class TestIamClientConstruction:
    def test_direct_kwargs_construction(self):
        mock_cls = MagicMock()
        mock_store = MagicMock(spec=BaseIam)
        mock_cls.return_value = mock_store

        with patch.object(IAM_REGISTRY, "get", return_value=mock_cls) as mock_get:
            client = IamClient(
                provider="aws", region="us-east-1", endpoint="http://localhost"
            )

        mock_get.assert_called_once_with("aws")
        mock_cls.assert_called_once_with(
            region="us-east-1", endpoint_url="http://localhost"
        )
        assert isinstance(client, IamClient)

    def test_construction_no_endpoint(self):
        mock_cls = MagicMock()
        mock_store = MagicMock(spec=BaseIam)
        mock_cls.return_value = mock_store

        with patch.object(IAM_REGISTRY, "get", return_value=mock_cls):
            IamClient(provider="aws", region="us-east-1")

        mock_cls.assert_called_once_with(region="us-east-1")

    def test_construction_with_properties(self):
        mock_cls = MagicMock()
        mock_store = MagicMock(spec=BaseIam)
        mock_cls.return_value = mock_store

        with patch.object(IAM_REGISTRY, "get", return_value=mock_cls):
            IamClient(
                provider="aws", region="us-west-2", properties={"iam_client": "mock"}
            )

        mock_cls.assert_called_once_with(region="us-west-2", iam_client="mock")

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="No provider registered"):
            IamClient(provider="nonexistent")


class TestIamClientCredentialsOverrider:
    def test_credentials_overrider_forwarded_to_provider(self):
        from multicloud.common.credentials import StsCredentials
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        mock_cls = MagicMock()
        mock_cls.return_value = MagicMock(spec=BaseIam)
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                access_key_id="AK",
                access_key_secret="SK",
                security_token="ST",
            ),
        )

        with patch.object(IAM_REGISTRY, "get", return_value=mock_cls):
            IamClient(
                provider="aws",
                region="us-east-1",
                credentials_overrider=overrider,
            )

        mock_cls.assert_called_once_with(
            region="us-east-1", credentials_overrider=overrider
        )

    def test_no_overrider_not_forwarded(self):
        mock_cls = MagicMock()
        mock_cls.return_value = MagicMock(spec=BaseIam)

        with patch.object(IAM_REGISTRY, "get", return_value=mock_cls):
            IamClient(provider="aws", region="us-east-1")

        assert "credentials_overrider" not in mock_cls.call_args.kwargs
