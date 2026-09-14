"""Unit tests for GCP policy translator."""

import pytest
from multicloud.common.exceptions import InvalidArgumentException
from multicloud.iam.iam_client.model import (
    Action,
    ComputeActions,
    Effect,
    IamActions,
    Statement,
    StorageActions,
)
from multicloud.iam.iam_gcp.gcp_policy_translator import GcpPolicyTranslator


class TestTranslateActionToRole:
    def test_storage_get_object(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(StorageActions.GET_OBJECT)
            == "roles/storage.objectViewer"
        )

    def test_storage_put_object(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(StorageActions.PUT_OBJECT)
            == "roles/storage.objectCreator"
        )

    def test_storage_delete_object(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(StorageActions.DELETE_OBJECT)
            == "roles/storage.objectAdmin"
        )

    def test_storage_list_bucket(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(StorageActions.LIST_BUCKET)
            == "roles/storage.objectViewer"
        )

    def test_storage_create_bucket(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(StorageActions.CREATE_BUCKET)
            == "roles/storage.admin"
        )

    def test_compute_create_instance(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(
                ComputeActions.CREATE_INSTANCE
            )
            == "roles/compute.instanceAdmin.v1"
        )

    def test_compute_describe_instances(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(
                ComputeActions.DESCRIBE_INSTANCES
            )
            == "roles/compute.viewer"
        )

    def test_iam_assume_role(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(IamActions.ASSUME_ROLE)
            == "roles/iam.serviceAccountUser"
        )

    def test_iam_get_role(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(IamActions.GET_ROLE)
            == "roles/iam.serviceAccountViewer"
        )

    def test_storage_wildcard(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(StorageActions.ALL)
            == "roles/storage.admin"
        )

    def test_compute_wildcard(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(ComputeActions.ALL)
            == "roles/compute.admin"
        )

    def test_iam_wildcard(self) -> None:
        assert (
            GcpPolicyTranslator._translate_action_to_role(IamActions.ALL)
            == "roles/iam.serviceAccountAdmin"
        )

    def test_unknown_action_raises(self) -> None:
        with pytest.raises(InvalidArgumentException, match="Unknown action"):
            GcpPolicyTranslator._translate_action_to_role(Action.of("unknown:DoThing"))


class TestTranslateActionsToRoles:
    def test_single_action(self) -> None:
        stmt = Statement(effect=Effect.ALLOW, actions=(StorageActions.GET_OBJECT,))
        roles = GcpPolicyTranslator.translate_actions_to_roles(stmt)
        assert roles == ["roles/storage.objectViewer"]

    def test_deduplication(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT, StorageActions.LIST_BUCKET),
        )
        roles = GcpPolicyTranslator.translate_actions_to_roles(stmt)
        assert roles == ["roles/storage.objectViewer"]

    def test_multiple_roles(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT, StorageActions.DELETE_OBJECT),
        )
        roles = GcpPolicyTranslator.translate_actions_to_roles(stmt)
        assert "roles/storage.objectViewer" in roles
        assert "roles/storage.objectAdmin" in roles
        assert len(roles) == 2

    def test_conditions_rejected(self) -> None:
        stmt = Statement(
            effect=Effect.ALLOW,
            actions=(StorageActions.GET_OBJECT,),
            conditions={"stringEquals": {"region": "us-west-2"}},
        )
        with pytest.raises(InvalidArgumentException, match="[Cc]onditions"):
            GcpPolicyTranslator.translate_actions_to_roles(stmt)

    def test_effect_required(self) -> None:
        stmt = Statement(actions=(StorageActions.GET_OBJECT,))
        with pytest.raises(InvalidArgumentException, match="[Ee]ffect"):
            GcpPolicyTranslator.translate_actions_to_roles(stmt)
