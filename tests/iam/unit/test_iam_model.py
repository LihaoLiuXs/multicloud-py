"""Unit tests for IAM policy primitive types."""

import dataclasses

import pytest
from multicloud.common.exceptions import InvalidArgumentException
from multicloud.iam.iam_client.model import (
    Action,
    ComputeActions,
    ConditionOperator,
    Effect,
    IamActions,
    StorageActions,
)


class TestActionOf:
    def test_valid_action(self) -> None:
        action = Action.of("storage:GetObject")
        assert action.service == "storage"
        assert action.operation == "GetObject"

    def test_strips_whitespace(self) -> None:
        action = Action.of("  storage:GetObject  ")
        assert action.service == "storage"
        assert action.operation == "GetObject"

    def test_empty_string_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="must not be null or empty"):
            Action.of("")

    def test_none_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="must not be null or empty"):
            Action.of(None)  # type: ignore[arg-type]

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="must not be null or empty"):
            Action.of("   ")

    def test_no_colon_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="service:operation"):
            Action.of("storageGetObject")

    def test_empty_service_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="service:operation"):
            Action.of(":GetObject")

    def test_empty_operation_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="service:operation"):
            Action.of("storage:")

    def test_multiple_colons_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="exactly one ':'"):
            Action.of("a:b:c")


class TestActionWildcard:
    def test_creates_star_operation(self) -> None:
        action = Action.wildcard("storage")
        assert action.service == "storage"
        assert action.operation == "*"

    def test_strips_whitespace(self) -> None:
        action = Action.wildcard("  compute  ")
        assert action.service == "compute"

    def test_empty_service_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="must not be null or empty"):
            Action.wildcard("")

    def test_none_service_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="must not be null or empty"):
            Action.wildcard(None)  # type: ignore[arg-type]


class TestActionProperties:
    def test_is_wildcard_true(self) -> None:
        assert Action.wildcard("storage").is_wildcard is True

    def test_is_wildcard_false(self) -> None:
        assert Action.of("storage:GetObject").is_wildcard is False

    def test_to_action_string(self) -> None:
        assert Action.of("iam:AssumeRole").to_action_string() == "iam:AssumeRole"

    def test_str_delegates_to_action_string(self) -> None:
        action = Action.of("compute:StartInstance")
        assert str(action) == action.to_action_string()

    def test_equality(self) -> None:
        a1 = Action.of("storage:GetObject")
        a2 = Action.of("storage:GetObject")
        assert a1 == a2

    def test_hash_usable_in_set(self) -> None:
        a1 = Action.of("storage:GetObject")
        a2 = Action.of("storage:GetObject")
        assert len({a1, a2}) == 1

    def test_frozen_immutability(self) -> None:
        action = Action.of("storage:GetObject")
        with pytest.raises(dataclasses.FrozenInstanceError):
            action.service = "other"  # type: ignore[misc]


class TestEffect:
    def test_allow_value(self) -> None:
        assert Effect.ALLOW.value == "Allow"

    def test_deny_value(self) -> None:
        assert Effect.DENY.value == "Deny"

    def test_string_comparison(self) -> None:
        assert Effect.ALLOW == "Allow"
        assert Effect.DENY == "Deny"

    def test_exactly_two_members(self) -> None:
        assert len(Effect) == 2


class TestConditionOperator:
    def test_total_operator_count(self) -> None:
        assert len(ConditionOperator) == 19

    def test_string_operators_values(self) -> None:
        assert ConditionOperator.STRING_EQUALS.value == "stringEquals"
        assert ConditionOperator.STRING_NOT_EQUALS.value == "stringNotEquals"
        assert ConditionOperator.STRING_LIKE.value == "stringLike"
        assert ConditionOperator.STRING_NOT_LIKE.value == "stringNotLike"

    def test_numeric_operators_values(self) -> None:
        assert ConditionOperator.NUMERIC_EQUALS.value == "numericEquals"
        assert ConditionOperator.NUMERIC_NOT_EQUALS.value == "numericNotEquals"
        assert ConditionOperator.NUMERIC_LESS_THAN.value == "numericLessThan"
        assert (
            ConditionOperator.NUMERIC_LESS_THAN_EQUALS.value == "numericLessThanEquals"
        )
        assert ConditionOperator.NUMERIC_GREATER_THAN.value == "numericGreaterThan"
        assert (
            ConditionOperator.NUMERIC_GREATER_THAN_EQUALS.value
            == "numericGreaterThanEquals"
        )

    def test_date_operators_values(self) -> None:
        assert ConditionOperator.DATE_EQUALS.value == "dateEquals"
        assert ConditionOperator.DATE_NOT_EQUALS.value == "dateNotEquals"
        assert ConditionOperator.DATE_LESS_THAN.value == "dateLessThan"
        assert ConditionOperator.DATE_LESS_THAN_EQUALS.value == "dateLessThanEquals"
        assert ConditionOperator.DATE_GREATER_THAN.value == "dateGreaterThan"
        assert (
            ConditionOperator.DATE_GREATER_THAN_EQUALS.value == "dateGreaterThanEquals"
        )

    def test_other_operators_values(self) -> None:
        assert ConditionOperator.BOOL.value == "bool"
        assert ConditionOperator.IP_ADDRESS.value == "ipAddress"
        assert ConditionOperator.NOT_IP_ADDRESS.value == "notIpAddress"

    def test_string_comparison(self) -> None:
        assert ConditionOperator.STRING_EQUALS == "stringEquals"
        assert ConditionOperator.IP_ADDRESS == "ipAddress"


class TestStorageActions:
    def test_each_constant_value(self) -> None:
        assert StorageActions.GET_OBJECT == Action.of("storage:GetObject")
        assert StorageActions.PUT_OBJECT == Action.of("storage:PutObject")
        assert StorageActions.DELETE_OBJECT == Action.of("storage:DeleteObject")
        assert StorageActions.LIST_BUCKET == Action.of("storage:ListBucket")
        assert StorageActions.GET_BUCKET_LOCATION == Action.of(
            "storage:GetBucketLocation"
        )
        assert StorageActions.CREATE_BUCKET == Action.of("storage:CreateBucket")
        assert StorageActions.DELETE_BUCKET == Action.of("storage:DeleteBucket")

    def test_all_is_storage_wildcard(self) -> None:
        assert StorageActions.ALL == Action.wildcard("storage")
        assert StorageActions.ALL.is_wildcard is True

    def test_all_have_storage_service(self) -> None:
        constants = [
            StorageActions.GET_OBJECT,
            StorageActions.PUT_OBJECT,
            StorageActions.DELETE_OBJECT,
            StorageActions.LIST_BUCKET,
            StorageActions.GET_BUCKET_LOCATION,
            StorageActions.CREATE_BUCKET,
            StorageActions.DELETE_BUCKET,
            StorageActions.ALL,
        ]
        for action in constants:
            assert action.service == "storage"


class TestComputeActions:
    def test_each_constant_value(self) -> None:
        assert ComputeActions.CREATE_INSTANCE == Action.of("compute:CreateInstance")
        assert ComputeActions.DELETE_INSTANCE == Action.of("compute:DeleteInstance")
        assert ComputeActions.START_INSTANCE == Action.of("compute:StartInstance")
        assert ComputeActions.STOP_INSTANCE == Action.of("compute:StopInstance")
        assert ComputeActions.DESCRIBE_INSTANCES == Action.of(
            "compute:DescribeInstances"
        )
        assert ComputeActions.GET_INSTANCE == Action.of("compute:GetInstance")

    def test_all_is_compute_wildcard(self) -> None:
        assert ComputeActions.ALL == Action.wildcard("compute")
        assert ComputeActions.ALL.is_wildcard is True

    def test_all_have_compute_service(self) -> None:
        constants = [
            ComputeActions.CREATE_INSTANCE,
            ComputeActions.DELETE_INSTANCE,
            ComputeActions.START_INSTANCE,
            ComputeActions.STOP_INSTANCE,
            ComputeActions.DESCRIBE_INSTANCES,
            ComputeActions.GET_INSTANCE,
            ComputeActions.ALL,
        ]
        for action in constants:
            assert action.service == "compute"


class TestIamActions:
    def test_each_constant_value(self) -> None:
        assert IamActions.ASSUME_ROLE == Action.of("iam:AssumeRole")
        assert IamActions.CREATE_ROLE == Action.of("iam:CreateRole")
        assert IamActions.DELETE_ROLE == Action.of("iam:DeleteRole")
        assert IamActions.GET_ROLE == Action.of("iam:GetRole")
        assert IamActions.ATTACH_ROLE_POLICY == Action.of("iam:AttachRolePolicy")
        assert IamActions.DETACH_ROLE_POLICY == Action.of("iam:DetachRolePolicy")
        assert IamActions.PUT_ROLE_POLICY == Action.of("iam:PutRolePolicy")
        assert IamActions.GET_ROLE_POLICY == Action.of("iam:GetRolePolicy")

    def test_all_is_iam_wildcard(self) -> None:
        assert IamActions.ALL == Action.wildcard("iam")
        assert IamActions.ALL.is_wildcard is True

    def test_all_have_iam_service(self) -> None:
        constants = [
            IamActions.ASSUME_ROLE,
            IamActions.CREATE_ROLE,
            IamActions.DELETE_ROLE,
            IamActions.GET_ROLE,
            IamActions.ATTACH_ROLE_POLICY,
            IamActions.DETACH_ROLE_POLICY,
            IamActions.PUT_ROLE_POLICY,
            IamActions.GET_ROLE_POLICY,
            IamActions.ALL,
        ]
        for action in constants:
            assert action.service == "iam"
