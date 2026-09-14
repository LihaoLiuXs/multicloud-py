"""Unit tests for PolicyDocument and Statement model types."""

import dataclasses

import pytest
from multicloud.common.exceptions import InvalidArgumentException
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


class TestStatementConstruction:
    def test_minimal_valid_statement(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
        )
        assert stmt.effect == Effect.ALLOW
        assert stmt.actions == (StorageActions.GET_OBJECT,)
        assert stmt.sid is None
        assert stmt.principals == ()
        assert stmt.resources == ()
        assert stmt.conditions == {}

    def test_full_statement(self) -> None:
        stmt = Statement(
            sid="FullAccess",
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT, StorageActions.PUT_OBJECT),
            principals=("arn:aws:iam::123:user/Bob",),
            resources=("storage://my-bucket/*",),
            conditions={"stringEquals": {"region": "us-west-2"}},
        )
        assert stmt.sid == "FullAccess"
        assert stmt.effect == Effect.ALLOW
        assert len(stmt.actions) == 2
        assert stmt.principals == ("arn:aws:iam::123:user/Bob",)
        assert stmt.resources == ("storage://my-bucket/*",)
        assert stmt.conditions == {"stringEquals": {"region": "us-west-2"}}

    def test_multiple_actions(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(
                StorageActions.GET_OBJECT,
                StorageActions.PUT_OBJECT,
                StorageActions.DELETE_OBJECT,
            ),
        )
        assert len(stmt.actions) == 3

    def test_multiple_principals(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
            principals=("user1", "user2", "user3"),
        )
        assert stmt.principals == ("user1", "user2", "user3")

    def test_multiple_resources(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
            resources=("storage://a/*", "storage://b/*"),
        )
        assert stmt.resources == ("storage://a/*", "storage://b/*")

    def test_condition_single_operator(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
            conditions={"stringEquals": {"region": "us-west-2"}},
        )
        assert stmt.conditions == {"stringEquals": {"region": "us-west-2"}}

    def test_condition_multiple_keys_same_operator(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
            conditions={"stringEquals": {"region": "us-west-2", "env": "prod"}},
        )
        assert stmt.conditions["stringEquals"]["region"] == "us-west-2"
        assert stmt.conditions["stringEquals"]["env"] == "prod"

    def test_condition_multiple_operators(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
            conditions={
                "stringEquals": {"region": "us-west-2"},
                "ipAddress": {"aws:SourceIp": "10.0.0.0/8"},
            },
        )
        assert "stringEquals" in stmt.conditions
        assert "ipAddress" in stmt.conditions

    def test_deny_effect(self) -> None:
        stmt = Statement(
            effect=Effect.DENY,
            actions=(StorageActions.DELETE_OBJECT,),
            resources=("storage://protected/*",),
        )
        assert stmt.effect == Effect.DENY


class TestStatementImmutability:
    def test_frozen_fields(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            stmt.sid = "changed"  # type: ignore[misc]

    def test_principals_tuple_not_mutable(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
            principals=("user1",),
        )
        assert isinstance(stmt.principals, tuple)
        with pytest.raises(AttributeError):
            stmt.principals.append("user2")  # type: ignore[attr-defined]

    def test_actions_tuple_not_mutable(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
        )
        assert isinstance(stmt.actions, tuple)
        with pytest.raises(AttributeError):
            stmt.actions.append(StorageActions.PUT_OBJECT)  # type: ignore[attr-defined]

    def test_resources_tuple_not_mutable(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
            resources=("storage://b/*",),
        )
        assert isinstance(stmt.resources, tuple)
        with pytest.raises(AttributeError):
            stmt.resources.append("other")  # type: ignore[attr-defined]

    def test_conditions_deeply_frozen(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
            conditions={"stringEquals": {"region": "us-west-2"}},
        )
        with pytest.raises(TypeError):
            stmt.conditions["newKey"] = {"a": "b"}  # type: ignore[index]
        with pytest.raises(TypeError):
            stmt.conditions["stringEquals"]["newField"] = "x"  # type: ignore[index]


class TestStatementProperties:
    def test_actions_as_strings(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT, StorageActions.PUT_OBJECT),
        )
        assert stmt.actions_as_strings == [
            "storage:GetObject",
            "storage:PutObject",
        ]

    def test_actions_as_strings_empty(self) -> None:
        stmt = Statement(effect=Effect.ALLOW)
        assert stmt.actions_as_strings == []

    def test_conditions_as_strings(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
            conditions={"stringEquals": {"region": "us-west-2"}},
        )
        result = stmt.conditions_as_strings
        assert result == {"stringEquals": {"region": "us-west-2"}}

    def test_conditions_as_strings_empty(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
        )
        assert stmt.conditions_as_strings == {}


class TestPolicyDocumentConstruction:
    def _make_stmt(self) -> Statement:
        return Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
        )

    def test_minimal_valid_document(self) -> None:
        doc = PolicyDocument(statements=(self._make_stmt(),))
        assert len(doc.statements) == 1
        assert doc.name is None
        assert doc.version is None

    def test_full_document(self) -> None:
        stmt1 = self._make_stmt()
        stmt2 = Statement(
            effect=Effect.DENY,
            actions=(StorageActions.DELETE_OBJECT,),
        )
        doc = PolicyDocument(
            name="TestPolicy",
            version="2024-01-01",
            statements=(stmt1, stmt2),
        )
        assert doc.name == "TestPolicy"
        assert doc.version == "2024-01-01"
        assert len(doc.statements) == 2

    def test_multiple_statements(self) -> None:
        stmt = self._make_stmt()
        doc = PolicyDocument(statements=(stmt, stmt, stmt))
        assert len(doc.statements) == 3

    def test_name_and_version_optional(self) -> None:
        doc = PolicyDocument(statements=(self._make_stmt(),))
        assert doc.name is None
        assert doc.version is None


class TestPolicyDocumentImmutability:
    def test_frozen_fields(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
        )
        doc = PolicyDocument(statements=(stmt,))
        with pytest.raises(dataclasses.FrozenInstanceError):
            doc.name = "changed"  # type: ignore[misc]

    def test_statements_tuple_not_mutable(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
        )
        doc = PolicyDocument(statements=(stmt,))
        assert isinstance(doc.statements, tuple)
        with pytest.raises(AttributeError):
            doc.statements.append(stmt)  # type: ignore[attr-defined]


class TestPolicyDocumentIntegration:
    def test_full_construction(self) -> None:
        policy = PolicyDocument(
            name="BucketReadPolicy",
            version="2024-01-01",
            statements=(
                Statement(
                    sid="AllowRead",
                    effect=Effect.ALLOW,
                    actions=(StorageActions.GET_OBJECT, StorageActions.LIST_BUCKET),
                    principals=("arn:aws:iam::123456789012:user/Reader",),
                    resources=("storage://my-bucket/*",),
                    conditions={"stringEquals": {"aws:RequestedRegion": "us-west-2"}},
                ),
            ),
        )
        assert policy.name == "BucketReadPolicy"
        assert policy.version == "2024-01-01"
        assert len(policy.statements) == 1
        stmt = policy.statements[0]
        assert stmt.sid == "AllowRead"
        assert stmt.effect == Effect.ALLOW
        assert len(stmt.actions) == 2
        assert stmt.principals == ("arn:aws:iam::123456789012:user/Reader",)
        assert stmt.resources == ("storage://my-bucket/*",)
        assert stmt.conditions == {"stringEquals": {"aws:RequestedRegion": "us-west-2"}}

    def test_multi_statement_policy(self) -> None:
        policy = PolicyDocument(
            name="MixedPolicy",
            statements=(
                Statement(
                    sid="AllowRead",
                    effect=Effect.ALLOW,
                    actions=(StorageActions.GET_OBJECT,),
                ),
                Statement(
                    sid="DenyDelete",
                    effect=Effect.DENY,
                    actions=(StorageActions.DELETE_OBJECT,),
                    resources=("storage://protected-bucket/*",),
                ),
            ),
        )
        assert len(policy.statements) == 2
        assert policy.statements[0].effect == Effect.ALLOW
        assert policy.statements[1].effect == Effect.DENY
        assert policy.statements[1].resources == ("storage://protected-bucket/*",)

    def test_round_trip_actions_as_strings(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(
                StorageActions.GET_OBJECT,
                ComputeActions.CREATE_INSTANCE,
                IamActions.ASSUME_ROLE,
            ),
        )
        assert stmt.actions_as_strings == [
            "storage:GetObject",
            "compute:CreateInstance",
            "iam:AssumeRole",
        ]
