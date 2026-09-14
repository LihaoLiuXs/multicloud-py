"""Translate substrate-neutral actions to GCP IAM roles."""

from __future__ import annotations

from typing import Dict, List

from multicloud.common.exceptions import InvalidArgumentException
from multicloud.iam.iam_client.model import Action, Statement


class GcpPolicyTranslator:
    """Translate substrate-neutral actions to GCP IAM predefined roles."""

    ACTION_TO_ROLE_MAP: Dict[str, str] = {
        "storage:GetObject": "roles/storage.objectViewer",
        "storage:PutObject": "roles/storage.objectCreator",
        "storage:DeleteObject": "roles/storage.objectAdmin",
        "storage:ListBucket": "roles/storage.objectViewer",
        "storage:GetBucketLocation": "roles/storage.objectViewer",
        "storage:CreateBucket": "roles/storage.admin",
        "storage:DeleteBucket": "roles/storage.admin",
        "compute:CreateInstance": "roles/compute.instanceAdmin.v1",
        "compute:DeleteInstance": "roles/compute.instanceAdmin.v1",
        "compute:StartInstance": "roles/compute.instanceAdmin.v1",
        "compute:StopInstance": "roles/compute.instanceAdmin.v1",
        "compute:DescribeInstances": "roles/compute.viewer",
        "compute:GetInstance": "roles/compute.viewer",
        "iam:AssumeRole": "roles/iam.serviceAccountUser",
        "iam:CreateRole": "roles/iam.serviceAccountAdmin",
        "iam:DeleteRole": "roles/iam.serviceAccountAdmin",
        "iam:GetRole": "roles/iam.serviceAccountViewer",
        "iam:AttachRolePolicy": "roles/iam.serviceAccountAdmin",
        "iam:DetachRolePolicy": "roles/iam.serviceAccountAdmin",
        "iam:PutRolePolicy": "roles/iam.serviceAccountAdmin",
        "iam:GetRolePolicy": "roles/iam.serviceAccountViewer",
    }

    SERVICE_WILDCARD_MAP: Dict[str, str] = {
        "storage": "roles/storage.admin",
        "compute": "roles/compute.admin",
        "iam": "roles/iam.serviceAccountAdmin",
    }

    @classmethod
    def translate_actions_to_roles(cls, statement: Statement) -> List[str]:
        if not statement.effect:
            raise InvalidArgumentException("Statement effect is required")
        if statement.conditions:
            raise InvalidArgumentException(
                "Conditions are not supported in GCP policy translation"
            )
        seen: set[str] = set()
        roles: List[str] = []
        for action in statement.actions:
            role = cls._translate_action_to_role(action)
            if role not in seen:
                seen.add(role)
                roles.append(role)
        return roles

    @classmethod
    def _translate_action_to_role(cls, action: Action) -> str:
        if action.is_wildcard:
            role = cls.SERVICE_WILDCARD_MAP.get(action.service)
            if role is None:
                raise InvalidArgumentException(
                    f"Unknown action service for wildcard: '{action.service}'"
                )
            return role
        action_str = action.to_action_string()
        role = cls.ACTION_TO_ROLE_MAP.get(action_str)
        if role is None:
            raise InvalidArgumentException(f"Unknown action: '{action_str}'")
        return role
