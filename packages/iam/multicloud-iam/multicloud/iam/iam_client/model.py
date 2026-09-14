"""Policy primitive types — cloud-agnostic building blocks for IAM policies.

These types are composed by PolicyDocument and consumed by provider-specific
policy translators. All types are immutable and safe to use as constants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, List, Mapping, Optional, Tuple

from multicloud.common.exceptions import InvalidArgumentException


@dataclass(frozen=True)
class Action:
    """Substrate-neutral action in 'service:operation' format."""

    service: str
    operation: str

    @staticmethod
    def of(action: str) -> "Action":
        """Parse 'service:operation' format string."""
        if not action or not action.strip():
            raise InvalidArgumentException("Action string must not be null or empty")
        action = action.strip()
        colon_idx = action.find(":")
        if colon_idx <= 0 or colon_idx >= len(action) - 1:
            raise InvalidArgumentException(
                f"Action must be in 'service:operation' format, got: '{action}'"
            )
        if action.find(":", colon_idx + 1) != -1:
            raise InvalidArgumentException(
                f"Action must contain exactly one ':', got: '{action}'"
            )
        return Action(service=action[:colon_idx], operation=action[colon_idx + 1 :])

    @staticmethod
    def wildcard(service: str) -> "Action":
        """Create wildcard action 'service:*'."""
        if not service or not service.strip():
            raise InvalidArgumentException("Service must not be null or empty")
        return Action(service=service.strip(), operation="*")

    @property
    def is_wildcard(self) -> bool:
        return self.operation == "*"

    def to_action_string(self) -> str:
        return f"{self.service}:{self.operation}"

    def __str__(self) -> str:
        return self.to_action_string()


class Effect(str, Enum):
    """Policy effect — whether a statement grants or denies access."""

    ALLOW = "Allow"
    DENY = "Deny"


class ConditionOperator(str, Enum):
    """Condition operators for policy conditions."""

    # String operators
    STRING_EQUALS = "stringEquals"
    STRING_NOT_EQUALS = "stringNotEquals"
    STRING_LIKE = "stringLike"
    STRING_NOT_LIKE = "stringNotLike"

    # Numeric operators
    NUMERIC_EQUALS = "numericEquals"
    NUMERIC_NOT_EQUALS = "numericNotEquals"
    NUMERIC_LESS_THAN = "numericLessThan"
    NUMERIC_LESS_THAN_EQUALS = "numericLessThanEquals"
    NUMERIC_GREATER_THAN = "numericGreaterThan"
    NUMERIC_GREATER_THAN_EQUALS = "numericGreaterThanEquals"

    # Date operators
    DATE_EQUALS = "dateEquals"
    DATE_NOT_EQUALS = "dateNotEquals"
    DATE_LESS_THAN = "dateLessThan"
    DATE_LESS_THAN_EQUALS = "dateLessThanEquals"
    DATE_GREATER_THAN = "dateGreaterThan"
    DATE_GREATER_THAN_EQUALS = "dateGreaterThanEquals"

    # Other operators
    BOOL = "bool"
    IP_ADDRESS = "ipAddress"
    NOT_IP_ADDRESS = "notIpAddress"


class StorageActions:
    """Predefined storage service actions."""

    GET_OBJECT = Action.of("storage:GetObject")
    PUT_OBJECT = Action.of("storage:PutObject")
    DELETE_OBJECT = Action.of("storage:DeleteObject")
    LIST_BUCKET = Action.of("storage:ListBucket")
    GET_BUCKET_LOCATION = Action.of("storage:GetBucketLocation")
    CREATE_BUCKET = Action.of("storage:CreateBucket")
    DELETE_BUCKET = Action.of("storage:DeleteBucket")
    ALL = Action.wildcard("storage")


class ComputeActions:
    """Predefined compute service actions."""

    CREATE_INSTANCE = Action.of("compute:CreateInstance")
    DELETE_INSTANCE = Action.of("compute:DeleteInstance")
    START_INSTANCE = Action.of("compute:StartInstance")
    STOP_INSTANCE = Action.of("compute:StopInstance")
    DESCRIBE_INSTANCES = Action.of("compute:DescribeInstances")
    GET_INSTANCE = Action.of("compute:GetInstance")
    ALL = Action.wildcard("compute")


class IamActions:
    """Predefined IAM service actions."""

    ASSUME_ROLE = Action.of("iam:AssumeRole")
    CREATE_ROLE = Action.of("iam:CreateRole")
    DELETE_ROLE = Action.of("iam:DeleteRole")
    GET_ROLE = Action.of("iam:GetRole")
    ATTACH_ROLE_POLICY = Action.of("iam:AttachRolePolicy")
    DETACH_ROLE_POLICY = Action.of("iam:DetachRolePolicy")
    PUT_ROLE_POLICY = Action.of("iam:PutRolePolicy")
    GET_ROLE_POLICY = Action.of("iam:GetRolePolicy")
    ALL = Action.wildcard("iam")


@dataclass(frozen=True)
class Statement:
    """Single policy statement — an effect applied to actions on resources."""

    sid: Optional[str] = None
    effect: Optional[Effect] = None
    principals: Tuple[str, ...] = ()
    actions: Tuple[Action, ...] = ()
    resources: Tuple[str, ...] = ()
    conditions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        frozen = MappingProxyType(
            {k: MappingProxyType(v) for k, v in self.conditions.items()}
        )
        object.__setattr__(self, "conditions", frozen)

    @property
    def actions_as_strings(self) -> List[str]:
        return [a.to_action_string() for a in self.actions]

    @property
    def conditions_as_strings(self) -> Mapping[str, Mapping[str, Any]]:
        return self.conditions


@dataclass(frozen=True)
class PolicyDocument:
    """Cloud-agnostic policy document — a named collection of statements."""

    name: Optional[str] = None
    version: Optional[str] = None
    statements: Tuple[Statement, ...] = ()
