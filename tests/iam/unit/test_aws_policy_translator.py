"""Unit tests for AWS policy translator."""

import json

import pytest
from multicloud.common.exceptions import InvalidArgumentException
from multicloud.iam.iam_aws.aws_policy_translator import AwsPolicyTranslator
from multicloud.iam.iam_client.model import (
    Action,
    ComputeActions,
    ConditionOperator,
    Effect,
    IamActions,
    PolicyDocument,
    Statement,
    StorageActions,
)


class TestTranslateAction:
    def test_storage_get_object(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(StorageActions.GET_OBJECT)
            == "s3:GetObject"
        )

    def test_storage_put_object(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(StorageActions.PUT_OBJECT)
            == "s3:PutObject"
        )

    def test_storage_delete_object(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(StorageActions.DELETE_OBJECT)
            == "s3:DeleteObject"
        )

    def test_storage_list_bucket(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(StorageActions.LIST_BUCKET)
            == "s3:ListBucket"
        )

    def test_storage_get_bucket_location(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(StorageActions.GET_BUCKET_LOCATION)
            == "s3:GetBucketLocation"
        )

    def test_storage_create_bucket(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(StorageActions.CREATE_BUCKET)
            == "s3:CreateBucket"
        )

    def test_storage_delete_bucket(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(StorageActions.DELETE_BUCKET)
            == "s3:DeleteBucket"
        )

    def test_compute_create_instance(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(ComputeActions.CREATE_INSTANCE)
            == "ec2:RunInstances"
        )

    def test_compute_delete_instance(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(ComputeActions.DELETE_INSTANCE)
            == "ec2:TerminateInstances"
        )

    def test_compute_start_instance(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(ComputeActions.START_INSTANCE)
            == "ec2:StartInstances"
        )

    def test_compute_stop_instance(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(ComputeActions.STOP_INSTANCE)
            == "ec2:StopInstances"
        )

    def test_compute_describe_instances(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(ComputeActions.DESCRIBE_INSTANCES)
            == "ec2:DescribeInstances"
        )

    def test_compute_get_instance(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(ComputeActions.GET_INSTANCE)
            == "ec2:DescribeInstances"
        )

    def test_iam_assume_role(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(IamActions.ASSUME_ROLE)
            == "sts:AssumeRole"
        )

    def test_iam_create_role(self) -> None:
        assert (
            AwsPolicyTranslator._translate_action(IamActions.CREATE_ROLE)
            == "iam:CreateRole"
        )

    def test_storage_wildcard(self) -> None:
        assert AwsPolicyTranslator._translate_action(StorageActions.ALL) == "s3:*"

    def test_compute_wildcard(self) -> None:
        assert AwsPolicyTranslator._translate_action(ComputeActions.ALL) == "ec2:*"

    def test_iam_wildcard(self) -> None:
        assert AwsPolicyTranslator._translate_action(IamActions.ALL) == "iam:*"

    def test_unknown_action_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="Unknown action"):
            AwsPolicyTranslator._translate_action(Action.of("unknown:DoThing"))


class TestTranslateResource:
    def test_wildcard(self) -> None:
        assert AwsPolicyTranslator._translate_resource("*") == "*"

    def test_storage_uri(self) -> None:
        assert (
            AwsPolicyTranslator._translate_resource("storage://my-bucket/*")
            == "arn:aws:s3:::my-bucket/*"
        )

    def test_storage_uri_with_prefix(self) -> None:
        assert (
            AwsPolicyTranslator._translate_resource("storage://bucket/prefix/*")
            == "arn:aws:s3:::bucket/prefix/*"
        )

    def test_arn_passthrough(self) -> None:
        arn = "arn:aws:s3:::my-bucket/key.txt"
        assert AwsPolicyTranslator._translate_resource(arn) == arn

    def test_compute_uri(self) -> None:
        assert (
            AwsPolicyTranslator._translate_resource("compute://instance/i-12345")
            == "arn:aws:ec2:*:*:instance/i-12345"
        )

    def test_iam_uri(self) -> None:
        assert (
            AwsPolicyTranslator._translate_resource("iam://123456789012:role/MyRole")
            == "arn:aws:iam::123456789012:role/MyRole"
        )

    def test_unknown_format_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="Unknown resource format"):
            AwsPolicyTranslator._translate_resource("gcs://bucket/key")


class TestTranslateConditions:
    def test_string_equals(self) -> None:
        conditions = {"stringEquals": {"aws:RequestedRegion": "us-west-2"}}
        result = AwsPolicyTranslator._translate_conditions(conditions)
        assert result == {"StringEquals": {"aws:RequestedRegion": "us-west-2"}}

    def test_ip_address(self) -> None:
        conditions = {"ipAddress": {"aws:SourceIp": "10.0.0.0/8"}}
        result = AwsPolicyTranslator._translate_conditions(conditions)
        assert result == {"IpAddress": {"aws:SourceIp": "10.0.0.0/8"}}

    def test_multiple_operators(self) -> None:
        conditions = {
            "stringEquals": {"region": "us-west-2"},
            "bool": {"aws:SecureTransport": "true"},
        }
        result = AwsPolicyTranslator._translate_conditions(conditions)
        assert "StringEquals" in result
        assert "Bool" in result

    def test_empty_conditions(self) -> None:
        assert AwsPolicyTranslator._translate_conditions({}) == {}


class TestTranslatePrincipals:
    def test_aws_arn_principal(self) -> None:
        result = AwsPolicyTranslator._translate_principals(
            ("arn:aws:iam::123456789012:user/Bob",)
        )
        assert result == {"AWS": ["arn:aws:iam::123456789012:user/Bob"]}

    def test_service_principal(self) -> None:
        result = AwsPolicyTranslator._translate_principals(("lambda.amazonaws.com",))
        assert result == {"Service": ["lambda.amazonaws.com"]}

    def test_mixed_principals(self) -> None:
        result = AwsPolicyTranslator._translate_principals(
            ("arn:aws:iam::123:root", "s3.amazonaws.com")
        )
        assert "AWS" in result
        assert "Service" in result

    def test_empty_principals(self) -> None:
        result = AwsPolicyTranslator._translate_principals(())
        assert result == {}


class TestTranslateDocument:
    def test_full_document(self) -> None:
        doc = PolicyDocument(
            name="TestPolicy",
            version="2024-01-01",
            statements=(
                Statement(
                    sid="AllowRead",
                    effect=Effect.ALLOW,
                    actions=(StorageActions.GET_OBJECT, StorageActions.LIST_BUCKET),
                    principals=("arn:aws:iam::123:user/Bob",),
                    resources=("storage://my-bucket/*",),
                    conditions={"stringEquals": {"aws:RequestedRegion": "us-west-2"}},
                ),
            ),
        )
        result = json.loads(AwsPolicyTranslator.translate(doc))
        assert result["Version"] == "2012-10-17"
        assert len(result["Statement"]) == 1
        stmt = result["Statement"][0]
        assert stmt["Sid"] == "AllowRead"
        assert stmt["Effect"] == "Allow"
        assert stmt["Action"] == ["s3:GetObject", "s3:ListBucket"]
        assert stmt["Resource"] == ["arn:aws:s3:::my-bucket/*"]
        assert stmt["Principal"] == {"AWS": ["arn:aws:iam::123:user/Bob"]}
        assert stmt["Condition"] == {
            "StringEquals": {"aws:RequestedRegion": "us-west-2"}
        }

    def test_multi_statement(self) -> None:
        doc = PolicyDocument(
            name="MultiPolicy",
            statements=(
                Statement(effect=Effect.ALLOW, actions=(StorageActions.GET_OBJECT,)),
                Statement(effect=Effect.DENY, actions=(StorageActions.DELETE_OBJECT,)),
            ),
        )
        result = json.loads(AwsPolicyTranslator.translate(doc))
        assert len(result["Statement"]) == 2
        assert result["Statement"][0]["Effect"] == "Allow"
        assert result["Statement"][1]["Effect"] == "Deny"

    def test_minimal_statement_no_optional_fields(self) -> None:
        doc = PolicyDocument(
            name="Minimal",
            statements=(
                Statement(effect=Effect.ALLOW, actions=(StorageActions.GET_OBJECT,)),
            ),
        )
        result = json.loads(AwsPolicyTranslator.translate(doc))
        stmt = result["Statement"][0]
        assert "Sid" not in stmt
        assert "Principal" not in stmt
        assert "Resource" not in stmt
        assert "Condition" not in stmt
