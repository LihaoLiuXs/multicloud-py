"""Unit tests for iam_aws/aws_iam.py — AWS IAM provider."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceConflictException,
    ResourceExistsException,
    ResourceNotFoundException,
    UnAuthorizedException,
    UnknownException,
)
from multicloud.iam.iam_aws.aws_iam import (
    AwsIam,
    _build_assume_role_policy_document,
)
from multicloud.iam.iam_client.driver import (
    CreateIdentityRequest,
    CreateOptions,
    DeleteIdentityRequest,
    GetIdentityRequest,
    TrustConfiguration,
)


@pytest.fixture
def mock_iam_client():
    return MagicMock()


@pytest.fixture
def aws_store(mock_iam_client):
    return AwsIam(region="us-west-2", iam_client=mock_iam_client)


class TestAwsIamCreateIdentity:
    def test_create_identity_basic(self, aws_store, mock_iam_client):
        mock_iam_client.create_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::123456789012:role/TestRole",
                "RoleName": "TestRole",
            }
        }

        result = aws_store.create_identity(
            "TestRole",
            description="Test role",
            tenant_id="123456789012",
            region="us-west-2",
            trust_config=TrustConfiguration(),
            options=CreateOptions(),
        )

        assert result == "arn:aws:iam::123456789012:role/TestRole"
        mock_iam_client.create_role.assert_called_once()
        call_kwargs = mock_iam_client.create_role.call_args[1]
        assert call_kwargs["RoleName"] == "TestRole"
        assert call_kwargs["Description"] == "Test role"

    def test_create_identity_with_options(self, aws_store, mock_iam_client):
        mock_iam_client.create_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::123:role/TestRole"}
        }
        opts = CreateOptions(
            path="/service-roles/",
            max_session_duration=3600,
            permission_boundary="arn:aws:iam::123:policy/Bound",
        )

        aws_store.create_identity(
            "TestRole",
            description="desc",
            tenant_id="123",
            region="us-west-2",
            trust_config=TrustConfiguration(),
            options=opts,
        )

        call_kwargs = mock_iam_client.create_role.call_args[1]
        assert call_kwargs["Path"] == "/service-roles/"
        assert call_kwargs["MaxSessionDuration"] == 3600
        assert call_kwargs["PermissionsBoundary"] == "arn:aws:iam::123:policy/Bound"

    def test_create_identity_already_exists_returns_existing_arn(
        self, aws_store, mock_iam_client
    ):
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "EntityAlreadyExists", "Message": "exists"}}
        mock_iam_client.create_role.side_effect = ClientError(
            error_response, "CreateRole"
        )
        mock_iam_client.get_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::123:role/TestRole",
                "RoleName": "TestRole",
                "Description": "Test role",
                "MaxSessionDuration": 3600,
                "AssumeRolePolicyDocument": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Principal": {"AWS": ["arn:aws:iam::123:root"]},
                            }
                        ],
                    }
                ),
            }
        }

        result = aws_store.create_identity(
            "TestRole",
            description="Test role",
            tenant_id="123",
            region="us-west-2",
            trust_config=TrustConfiguration(),
            options=CreateOptions(),
        )

        assert result == "arn:aws:iam::123:role/TestRole"

    def test_create_identity_with_trust_config(self, aws_store, mock_iam_client):
        mock_iam_client.create_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::123:role/TestRole"}
        }
        tc = TrustConfiguration(
            trusted_principals=["arn:aws:iam::999999999999:root"],
            conditions={"StringEquals": {"sts:ExternalId": "ext-id"}},
        )

        aws_store.create_identity(
            "TestRole",
            description="desc",
            tenant_id="123",
            region="us-west-2",
            trust_config=tc,
            options=CreateOptions(),
        )

        call_kwargs = mock_iam_client.create_role.call_args[1]
        doc = json.loads(call_kwargs["AssumeRolePolicyDocument"])
        assert doc["Statement"][0]["Principal"]["AWS"] == [
            "arn:aws:iam::999999999999:root"
        ]
        assert (
            doc["Statement"][0]["Condition"]["StringEquals"]["sts:ExternalId"]
            == "ext-id"
        )


class TestAwsIamGetIdentity:
    def test_get_identity_returns_arn(self, aws_store, mock_iam_client):
        mock_iam_client.get_role.return_value = {
            "Role": {"Arn": "arn:aws:iam::123:role/TestRole"}
        }

        result = aws_store.get_identity("TestRole", tenant_id="123")

        assert result == "arn:aws:iam::123:role/TestRole"
        mock_iam_client.get_role.assert_called_once_with(RoleName="TestRole")

    def test_get_identity_not_found(self, aws_store, mock_iam_client):
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "NoSuchEntity", "Message": "not found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_iam_client.get_role.side_effect = ClientError(error_response, "GetRole")

        with pytest.raises(ResourceNotFoundException):
            aws_store.get_identity("NonExistent", tenant_id="123")


class TestAwsIamDeleteIdentity:
    def test_delete_identity_success(self, aws_store, mock_iam_client):
        mock_iam_client.delete_role.return_value = {}

        aws_store.delete_identity("TestRole", tenant_id="123")

        mock_iam_client.delete_role.assert_called_once_with(RoleName="TestRole")

    def test_delete_identity_not_found(self, aws_store, mock_iam_client):
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "NoSuchEntity", "Message": "not found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        mock_iam_client.delete_role.side_effect = ClientError(
            error_response, "DeleteRole"
        )

        with pytest.raises(ResourceNotFoundException):
            aws_store.delete_identity("NonExistent", tenant_id="123")


class TestAwsIamClose:
    def test_close_calls_underlying_client_close(self, aws_store, mock_iam_client):
        mock_iam_client.close = MagicMock()
        aws_store.close()
        mock_iam_client.close.assert_called_once()

    def test_close_no_op_when_client_has_no_close(self):
        client = MagicMock(spec=[])
        store = AwsIam(region="us-west-2", iam_client=client)
        store.close()


class TestAwsIamIdempotentUpdate:
    """Tests for _handle_already_exists and _update_role_if_needed paths."""

    def test_existing_role_updated_when_description_differs(
        self, aws_store, mock_iam_client
    ):
        from botocore.exceptions import ClientError

        mock_iam_client.create_role.side_effect = ClientError(
            {"Error": {"Code": "EntityAlreadyExists"}, "ResponseMetadata": {}},
            "CreateRole",
        )
        mock_iam_client.get_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::123:role/R",
                "RoleName": "R",
                "Description": "old desc",
                "MaxSessionDuration": 3600,
                "AssumeRolePolicyDocument": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Principal": {"AWS": ["arn:aws:iam::123:root"]},
                            }
                        ],
                    }
                ),
            }
        }
        mock_iam_client.update_role.return_value = {}

        result = aws_store.create_identity(
            "R",
            description="new desc",
            tenant_id="123",
            region="us-west-2",
            trust_config=TrustConfiguration(),
            options=CreateOptions(),
        )

        assert result == "arn:aws:iam::123:role/R"
        mock_iam_client.update_role.assert_called_once()
        call_kwargs = mock_iam_client.update_role.call_args[1]
        assert call_kwargs["Description"] == "new desc"

    def test_existing_role_updated_when_max_session_duration_differs(
        self, aws_store, mock_iam_client
    ):
        from botocore.exceptions import ClientError

        mock_iam_client.create_role.side_effect = ClientError(
            {"Error": {"Code": "EntityAlreadyExists"}, "ResponseMetadata": {}},
            "CreateRole",
        )
        mock_iam_client.get_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::123:role/R",
                "RoleName": "R",
                "Description": "desc",
                "MaxSessionDuration": 3600,
                "AssumeRolePolicyDocument": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Principal": {"AWS": ["arn:aws:iam::123:root"]},
                            }
                        ],
                    }
                ),
            }
        }
        mock_iam_client.update_role.return_value = {}

        aws_store.create_identity(
            "R",
            description="desc",
            tenant_id="123",
            region="us-west-2",
            trust_config=TrustConfiguration(),
            options=CreateOptions(max_session_duration=7200),
        )

        mock_iam_client.update_role.assert_called_once()
        call_kwargs = mock_iam_client.update_role.call_args[1]
        assert call_kwargs["MaxSessionDuration"] == 7200

    def test_existing_role_trust_policy_updated_when_different(
        self, aws_store, mock_iam_client
    ):
        from botocore.exceptions import ClientError

        mock_iam_client.create_role.side_effect = ClientError(
            {"Error": {"Code": "EntityAlreadyExists"}, "ResponseMetadata": {}},
            "CreateRole",
        )
        mock_iam_client.get_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::123:role/R",
                "RoleName": "R",
                "Description": "desc",
                "MaxSessionDuration": 3600,
                "AssumeRolePolicyDocument": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Principal": {"AWS": ["arn:aws:iam::123:root"]},
                            }
                        ],
                    }
                ),
            }
        }
        mock_iam_client.update_assume_role_policy.return_value = {}

        aws_store.create_identity(
            "R",
            description="desc",
            tenant_id="123",
            region="us-west-2",
            trust_config=TrustConfiguration(
                trusted_principals=["arn:aws:iam::999:root"]
            ),
            options=CreateOptions(),
        )

        mock_iam_client.update_assume_role_policy.assert_called_once()

    def test_update_role_error_translated(self, aws_store, mock_iam_client):
        from botocore.exceptions import ClientError

        mock_iam_client.create_role.side_effect = ClientError(
            {"Error": {"Code": "EntityAlreadyExists"}, "ResponseMetadata": {}},
            "CreateRole",
        )
        mock_iam_client.get_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::123:role/R",
                "RoleName": "R",
                "Description": "old",
                "AssumeRolePolicyDocument": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Principal": {"AWS": ["arn:aws:iam::123:root"]},
                            }
                        ],
                    }
                ),
            }
        }
        mock_iam_client.update_role.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {}},
            "UpdateRole",
        )

        with pytest.raises(UnAuthorizedException):
            aws_store.create_identity(
                "R",
                description="new",
                tenant_id="123",
                region="us-west-2",
            )

    def test_update_trust_policy_error_translated(self, aws_store, mock_iam_client):
        from botocore.exceptions import ClientError

        mock_iam_client.create_role.side_effect = ClientError(
            {"Error": {"Code": "EntityAlreadyExists"}, "ResponseMetadata": {}},
            "CreateRole",
        )
        mock_iam_client.get_role.return_value = {
            "Role": {
                "Arn": "arn:aws:iam::123:role/R",
                "RoleName": "R",
                "Description": "desc",
                "AssumeRolePolicyDocument": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Principal": {"AWS": ["arn:aws:iam::123:root"]},
                            }
                        ],
                    }
                ),
            }
        }
        mock_iam_client.update_assume_role_policy.side_effect = ClientError(
            {"Error": {"Code": "MalformedPolicyDocument"}, "ResponseMetadata": {}},
            "UpdateAssumeRolePolicy",
        )

        with pytest.raises(InvalidArgumentException):
            aws_store.create_identity(
                "R",
                description="desc",
                tenant_id="123",
                region="us-west-2",
                trust_config=TrustConfiguration(
                    trusted_principals=["arn:aws:iam::999:root"]
                ),
            )

    def test_get_role_error_during_already_exists(self, aws_store, mock_iam_client):
        from botocore.exceptions import ClientError

        mock_iam_client.create_role.side_effect = ClientError(
            {"Error": {"Code": "EntityAlreadyExists"}, "ResponseMetadata": {}},
            "CreateRole",
        )
        mock_iam_client.get_role.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {}},
            "GetRole",
        )

        with pytest.raises(UnAuthorizedException):
            aws_store.create_identity("R", description="d", tenant_id="123")


class TestBuildAssumeRolePolicyDocument:
    def test_no_trust_config_defaults_to_same_account_root(self):
        doc_str = _build_assume_role_policy_document(
            "123456789012", TrustConfiguration()
        )
        doc = json.loads(doc_str)

        assert doc["Version"] == "2012-10-17"
        assert doc["Statement"][0]["Effect"] == "Allow"
        assert doc["Statement"][0]["Action"] == "sts:AssumeRole"
        assert doc["Statement"][0]["Principal"]["AWS"] == [
            "arn:aws:iam::123456789012:root"
        ]

    def test_arn_principal(self):
        tc = TrustConfiguration(trusted_principals=["arn:aws:iam::999:root"])
        doc = json.loads(_build_assume_role_policy_document("123", tc))
        assert doc["Statement"][0]["Principal"]["AWS"] == ["arn:aws:iam::999:root"]

    def test_account_id_principal(self):
        tc = TrustConfiguration(trusted_principals=["999999999999"])
        doc = json.loads(_build_assume_role_policy_document("123", tc))
        assert doc["Statement"][0]["Principal"]["AWS"] == [
            "arn:aws:iam::999999999999:root"
        ]

    def test_service_principal(self):
        tc = TrustConfiguration(trusted_principals=["ec2.amazonaws.com"])
        doc = json.loads(_build_assume_role_policy_document("123", tc))
        assert doc["Statement"][0]["Principal"]["Service"] == ["ec2.amazonaws.com"]

    def test_mixed_principals(self):
        tc = TrustConfiguration(
            trusted_principals=["arn:aws:iam::999:root", "lambda.amazonaws.com"]
        )
        doc = json.loads(_build_assume_role_policy_document("123", tc))
        assert "AWS" in doc["Statement"][0]["Principal"]
        assert "Service" in doc["Statement"][0]["Principal"]

    def test_conditions_included(self):
        tc = TrustConfiguration(
            trusted_principals=["arn:aws:iam::999:root"],
            conditions={"StringEquals": {"sts:ExternalId": "ext-123"}},
        )
        doc = json.loads(_build_assume_role_policy_document("123", tc))
        assert (
            doc["Statement"][0]["Condition"]["StringEquals"]["sts:ExternalId"]
            == "ext-123"
        )

    def test_empty_and_whitespace_principals_ignored(self):
        tc = TrustConfiguration(trusted_principals=["", "  ", "arn:aws:iam::999:root"])
        doc = json.loads(_build_assume_role_policy_document("123", tc))
        assert doc["Statement"][0]["Principal"]["AWS"] == ["arn:aws:iam::999:root"]

    def test_none_trust_config_defaults_to_same_account(self):
        doc = json.loads(_build_assume_role_policy_document("123456789012", None))
        assert doc["Statement"][0]["Principal"]["AWS"] == [
            "arn:aws:iam::123456789012:root"
        ]

    def test_unrecognized_principal_treated_as_aws(self):
        tc = TrustConfiguration(trusted_principals=["some-custom-principal"])
        doc = json.loads(_build_assume_role_policy_document("123", tc))
        assert doc["Statement"][0]["Principal"]["AWS"] == ["some-custom-principal"]


class TestIsTrustPolicyDifferent:
    """Tests for the _is_trust_policy_different helper."""

    def test_both_none_returns_false(self):
        from multicloud.iam.iam_aws.aws_iam import _is_trust_policy_different

        assert _is_trust_policy_different(None, None) is False

    def test_existing_none_new_present_returns_true(self):
        from multicloud.iam.iam_aws.aws_iam import _is_trust_policy_different

        assert _is_trust_policy_different(None, '{"Version":"2012-10-17"}') is True

    def test_existing_present_new_none_returns_true(self):
        from multicloud.iam.iam_aws.aws_iam import _is_trust_policy_different

        assert _is_trust_policy_different('{"Version":"2012-10-17"}', None) is True

    def test_same_policy_returns_false(self):
        from multicloud.iam.iam_aws.aws_iam import _is_trust_policy_different

        policy = '{"Version":"2012-10-17","Statement":[]}'
        assert _is_trust_policy_different(policy, policy) is False

    def test_different_policy_returns_true(self):
        from multicloud.iam.iam_aws.aws_iam import _is_trust_policy_different

        old = '{"Version":"2012-10-17","Statement":[]}'
        new = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow"}]}'
        assert _is_trust_policy_different(old, new) is True

    def test_url_encoded_existing_policy(self):
        from multicloud.iam.iam_aws.aws_iam import _is_trust_policy_different

        raw = '{"Version":"2012-10-17"}'
        encoded = "%7B%22Version%22%3A%222012-10-17%22%7D"
        assert _is_trust_policy_different(encoded, raw) is False

    def test_dict_existing_policy(self):
        from multicloud.iam.iam_aws.aws_iam import _is_trust_policy_different

        existing_dict = {"Version": "2012-10-17", "Statement": []}
        new_str = '{"Version":"2012-10-17","Statement":[]}'
        assert _is_trust_policy_different(existing_dict, new_str) is False

    def test_malformed_json_returns_true(self):
        from multicloud.iam.iam_aws.aws_iam import _is_trust_policy_different

        assert (
            _is_trust_policy_different("not-json{", '{"Version":"2012-10-17"}') is True
        )


class TestTranslateError:
    def test_already_translated_passthrough(self):
        exc = ResourceNotFoundException("already translated")
        result = AwsIam().translate_error(exc)
        assert result is exc

    def test_botocore_error(self):
        from botocore.exceptions import BotoCoreError

        exc = BotoCoreError()
        assert isinstance(AwsIam().translate_error(exc), InvalidArgumentException)

    def test_non_aws_exception(self):
        exc = RuntimeError("something unexpected")
        assert isinstance(AwsIam().translate_error(exc), UnknownException)

    def test_no_such_entity(self):
        from botocore.exceptions import ClientError

        exc = ClientError(
            {
                "Error": {"Code": "NoSuchEntity"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "GetRole",
        )
        assert isinstance(AwsIam().translate_error(exc), ResourceNotFoundException)

    def test_entity_already_exists(self):
        from botocore.exceptions import ClientError

        exc = ClientError(
            {"Error": {"Code": "EntityAlreadyExists"}, "ResponseMetadata": {}},
            "CreateRole",
        )
        assert isinstance(AwsIam().translate_error(exc), ResourceExistsException)

    def test_access_denied(self):
        from botocore.exceptions import ClientError

        exc = ClientError(
            {
                "Error": {"Code": "AccessDenied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "GetRole",
        )
        assert isinstance(AwsIam().translate_error(exc), UnAuthorizedException)

    def test_invalid_input(self):
        from botocore.exceptions import ClientError

        exc = ClientError(
            {"Error": {"Code": "InvalidInput"}, "ResponseMetadata": {}},
            "CreateRole",
        )
        assert isinstance(AwsIam().translate_error(exc), InvalidArgumentException)

    def test_no_credentials(self):
        from botocore.exceptions import NoCredentialsError

        exc = NoCredentialsError()
        assert isinstance(AwsIam().translate_error(exc), UnAuthorizedException)

    def test_delete_conflict(self):
        from botocore.exceptions import ClientError

        exc = ClientError(
            {"Error": {"Code": "DeleteConflict"}, "ResponseMetadata": {}},
            "DeleteRole",
        )
        assert isinstance(AwsIam().translate_error(exc), ResourceConflictException)

    def test_unknown_error(self):
        from botocore.exceptions import ClientError

        exc = ClientError(
            {"Error": {"Code": "ServiceFailure"}, "ResponseMetadata": {}},
            "Unknown",
        )
        assert isinstance(AwsIam().translate_error(exc), UnknownException)


class TestAwsIamAttachInlinePolicy:
    def test_attach_inline_policy_success(self, aws_store, mock_iam_client):
        from multicloud.iam.iam_client.model import (
            Effect,
            PolicyDocument,
            Statement,
            StorageActions,
        )

        mock_iam_client.put_role_policy.return_value = {}
        doc = PolicyDocument(
            name="TestPolicy",
            statements=(
                Statement(
                    effect=Effect.ALLOW,
                    actions=(StorageActions.GET_OBJECT,),
                    resources=("storage://bucket/*",),
                ),
            ),
        )

        aws_store.attach_inline_policy("TestRole", policy_document=doc, tenant_id="123")

        mock_iam_client.put_role_policy.assert_called_once()
        call_kwargs = mock_iam_client.put_role_policy.call_args[1]
        assert call_kwargs["RoleName"] == "TestRole"
        assert call_kwargs["PolicyName"] == "TestPolicy"
        assert "s3:GetObject" in call_kwargs["PolicyDocument"]

    def test_attach_inline_policy_error(self, aws_store, mock_iam_client):
        from botocore.exceptions import ClientError
        from multicloud.iam.iam_client.model import (
            Effect,
            PolicyDocument,
            Statement,
            StorageActions,
        )

        mock_iam_client.put_role_policy.side_effect = ClientError(
            {"Error": {"Code": "NoSuchEntity"}, "ResponseMetadata": {}},
            "PutRolePolicy",
        )
        doc = PolicyDocument(
            name="P",
            statements=(
                Statement(effect=Effect.ALLOW, actions=(StorageActions.GET_OBJECT,)),
            ),
        )

        with pytest.raises(ResourceNotFoundException):
            aws_store.attach_inline_policy(
                "BadRole", policy_document=doc, tenant_id="123"
            )


class TestAwsIamGetInlinePolicyDetails:
    def test_get_inline_policy_details_success(self, aws_store, mock_iam_client):
        mock_iam_client.get_role_policy.return_value = {
            "PolicyDocument": '{"Version":"2012-10-17","Statement":[]}'
        }

        result = aws_store.get_inline_policy_details(
            "TestRole", policy_name="TestPolicy", tenant_id="123"
        )

        assert "2012-10-17" in result
        mock_iam_client.get_role_policy.assert_called_once_with(
            RoleName="TestRole", PolicyName="TestPolicy"
        )

    def test_get_inline_policy_details_url_decodes(self, aws_store, mock_iam_client):
        mock_iam_client.get_role_policy.return_value = {
            "PolicyDocument": "%7B%22Version%22%3A%222012-10-17%22%7D"
        }

        result = aws_store.get_inline_policy_details(
            "TestRole", policy_name="P", tenant_id="123"
        )

        assert '"Version"' in result
        assert "%7B" not in result

    def test_get_inline_policy_details_dict_response(self, aws_store, mock_iam_client):
        mock_iam_client.get_role_policy.return_value = {
            "PolicyDocument": {"Version": "2012-10-17", "Statement": []}
        }

        result = aws_store.get_inline_policy_details(
            "TestRole", policy_name="P", tenant_id="123"
        )

        assert "2012-10-17" in result


class TestAwsIamGetAttachedPolicies:
    def test_get_attached_policies_success(self, aws_store, mock_iam_client):
        paginator_mock = MagicMock()
        paginator_mock.paginate.return_value = [
            {"PolicyNames": ["Policy1", "Policy2"]},
            {"PolicyNames": ["Policy3"]},
        ]
        mock_iam_client.get_paginator.return_value = paginator_mock

        result = aws_store.get_attached_policies("TestRole", tenant_id="123")

        assert result == ["Policy1", "Policy2", "Policy3"]
        mock_iam_client.get_paginator.assert_called_once_with("list_role_policies")


class TestAwsIamRemovePolicy:
    def test_remove_policy_success(self, aws_store, mock_iam_client):
        mock_iam_client.delete_role_policy.return_value = {}

        aws_store.remove_policy("TestRole", policy_name="TestPolicy", tenant_id="123")

        mock_iam_client.delete_role_policy.assert_called_once_with(
            RoleName="TestRole", PolicyName="TestPolicy"
        )

    def test_remove_policy_not_found(self, aws_store, mock_iam_client):
        from botocore.exceptions import ClientError

        mock_iam_client.delete_role_policy.side_effect = ClientError(
            {"Error": {"Code": "NoSuchEntity"}, "ResponseMetadata": {}},
            "DeleteRolePolicy",
        )

        with pytest.raises(ResourceNotFoundException):
            aws_store.remove_policy("TestRole", policy_name="P", tenant_id="123")


class TestAwsIamCredentialsOverrider:
    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_session_override_builds_session(self, mock_session_cls):
        from multicloud.common.credentials import StsCredentials
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
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
        AwsIam(region="us-east-1", credentials_overrider=overrider)

        mock_session_cls.assert_called_once_with(
            aws_access_key_id="OVERRIDE_AK",
            aws_secret_access_key="OVERRIDE_SK",
            aws_session_token="OVERRIDE_ST",
            region_name="us-east-1",
        )
        session.client.assert_called_once_with("iam", region_name="us-east-1")

    @patch("multicloud.aws_common.aws_credentials.boto3.Session")
    def test_session_override_empty_token_passes_none(self, mock_session_cls):
        from multicloud.common.credentials import StsCredentials
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
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
        AwsIam(region="us-west-2", credentials_overrider=overrider)

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
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="arn:aws:iam::999:role/CrossAccount",
            session_name="test-session",
            duration_seconds=1800,
        )
        AwsIam(region="us-east-1", credentials_overrider=overrider)

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
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="arn:aws:iam::999:role/R",
        )
        AwsIam(region="us-east-1", credentials_overrider=overrider)

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
        from multicloud.iam.iam_client.driver import (
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
        AwsIam(region="us-west-2", credentials_overrider=overrider)

        mock_fetcher_cls.assert_called_once()
        kwargs = mock_fetcher_cls.call_args.kwargs
        assert kwargs["role_arn"] == "arn:aws:iam::999:role/WebRole"
        assert kwargs["web_identity_token_loader"] is token_supplier

    @patch("multicloud.iam.iam_aws.aws_iam.get_session")
    def test_no_overrider_uses_default_session(self, mock_get_session):
        session = MagicMock()
        session.client.return_value = MagicMock()
        mock_get_session.return_value = session

        AwsIam(region="eu-west-1")

        mock_get_session.assert_called_once_with(None, "eu-west-1")
        session.client.assert_called_once_with("iam", region_name="eu-west-1")

    @patch("multicloud.iam.iam_aws.aws_iam.get_session")
    def test_no_region_defaults_to_us_east_1(self, mock_get_session):
        session = MagicMock()
        session.client.return_value = MagicMock()
        mock_get_session.return_value = session

        AwsIam()

        # Raw region (None) flows into get_session; the client build keeps the
        # global-IAM default of us-east-1.
        mock_get_session.assert_called_once_with(None, None)
        session.client.assert_called_once_with("iam", region_name="us-east-1")

    @patch("multicloud.iam.iam_aws.aws_iam.get_session")
    def test_endpoint_url_passed_to_client(self, mock_get_session):
        session = MagicMock()
        session.client.return_value = MagicMock()
        mock_get_session.return_value = session

        AwsIam(region="us-west-2", endpoint_url="http://localhost:4566")

        session.client.assert_called_once_with(
            "iam",
            region_name="us-west-2",
            endpoint_url="http://localhost:4566",
        )

    @patch("multicloud.iam.iam_aws.aws_iam.get_session")
    def test_injected_client_bypasses_session(self, mock_get_session):
        fake = MagicMock()
        store = AwsIam(region="us-east-1", iam_client=fake)

        assert store._iam is fake
        mock_get_session.assert_not_called()
