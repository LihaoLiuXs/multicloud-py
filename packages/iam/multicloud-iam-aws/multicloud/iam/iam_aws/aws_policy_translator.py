"""Translate substrate-neutral PolicyDocument to AWS IAM JSON."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from multicloud.common.exceptions import InvalidArgumentException
from multicloud.iam.iam_client.model import Action, PolicyDocument, Statement


class AwsPolicyTranslator:
    """Translate substrate-neutral PolicyDocument to AWS IAM policy JSON."""

    AWS_POLICY_VERSION = "2012-10-17"

    ACTION_MAP: Dict[str, str] = {
        "storage:GetObject": "s3:GetObject",
        "storage:PutObject": "s3:PutObject",
        "storage:DeleteObject": "s3:DeleteObject",
        "storage:ListBucket": "s3:ListBucket",
        "storage:GetBucketLocation": "s3:GetBucketLocation",
        "storage:CreateBucket": "s3:CreateBucket",
        "storage:DeleteBucket": "s3:DeleteBucket",
        "compute:CreateInstance": "ec2:RunInstances",
        "compute:DeleteInstance": "ec2:TerminateInstances",
        "compute:StartInstance": "ec2:StartInstances",
        "compute:StopInstance": "ec2:StopInstances",
        "compute:DescribeInstances": "ec2:DescribeInstances",
        "compute:GetInstance": "ec2:DescribeInstances",
        "iam:AssumeRole": "sts:AssumeRole",
        "iam:CreateRole": "iam:CreateRole",
        "iam:DeleteRole": "iam:DeleteRole",
        "iam:GetRole": "iam:GetRole",
        "iam:AttachRolePolicy": "iam:AttachRolePolicy",
        "iam:DetachRolePolicy": "iam:DetachRolePolicy",
        "iam:PutRolePolicy": "iam:PutRolePolicy",
        "iam:GetRolePolicy": "iam:GetRolePolicy",
    }

    SERVICE_MAP: Dict[str, str] = {
        "storage": "s3",
        "compute": "ec2",
        "iam": "iam",
    }

    CONDITION_MAP: Dict[str, str] = {
        "stringEquals": "StringEquals",
        "stringNotEquals": "StringNotEquals",
        "stringLike": "StringLike",
        "stringNotLike": "StringNotLike",
        "numericEquals": "NumericEquals",
        "numericNotEquals": "NumericNotEquals",
        "numericLessThan": "NumericLessThan",
        "numericLessThanEquals": "NumericLessThanEquals",
        "numericGreaterThan": "NumericGreaterThan",
        "numericGreaterThanEquals": "NumericGreaterThanEquals",
        "dateEquals": "DateEquals",
        "dateNotEquals": "DateNotEquals",
        "dateLessThan": "DateLessThan",
        "dateLessThanEquals": "DateLessThanEquals",
        "dateGreaterThan": "DateGreaterThan",
        "dateGreaterThanEquals": "DateGreaterThanEquals",
        "bool": "Bool",
        "ipAddress": "IpAddress",
        "notIpAddress": "NotIpAddress",
    }

    @classmethod
    def translate(cls, policy_document: PolicyDocument) -> str:
        policy = {
            "Version": cls.AWS_POLICY_VERSION,
            "Statement": [
                cls._translate_statement(stmt) for stmt in policy_document.statements
            ],
        }
        return json.dumps(policy)

    @classmethod
    def _translate_statement(cls, statement: Statement) -> Dict[str, Any]:
        stmt: Dict[str, Any] = {}
        if statement.sid:
            stmt["Sid"] = statement.sid
        if not statement.effect:
            raise InvalidArgumentException("Statement effect is required")
        stmt["Effect"] = statement.effect.value
        stmt["Action"] = [cls._translate_action(a) for a in statement.actions]
        if statement.principals:
            stmt["Principal"] = cls._translate_principals(statement.principals)
        if statement.resources:
            stmt["Resource"] = [cls._translate_resource(r) for r in statement.resources]
        if statement.conditions:
            stmt["Condition"] = cls._translate_conditions(statement.conditions)
        return stmt

    @classmethod
    def _translate_action(cls, action: Action) -> str:
        if action.is_wildcard:
            aws_service = cls.SERVICE_MAP.get(action.service)
            if aws_service is None:
                raise InvalidArgumentException(
                    f"Unknown action service for wildcard: '{action.service}'"
                )
            return f"{aws_service}:*"
        action_str = action.to_action_string()
        aws_action = cls.ACTION_MAP.get(action_str)
        if aws_action is None:
            raise InvalidArgumentException(f"Unknown action: '{action_str}'")
        return aws_action

    @classmethod
    def _translate_resource(cls, resource: str) -> str:
        if resource == "*":
            return "*"
        if resource.startswith("storage://"):
            path = resource[len("storage://") :]
            return f"arn:aws:s3:::{path}"
        if resource.startswith("compute://"):
            path = resource[len("compute://") :]
            return f"arn:aws:ec2:*:*:{path}"
        if resource.startswith("iam://"):
            path = resource[len("iam://") :]
            return f"arn:aws:iam::{path}"
        if resource.startswith("arn:"):
            return resource
        raise InvalidArgumentException(f"Unknown resource format: '{resource}'")

    @classmethod
    def _translate_principals(cls, principals: Tuple[str, ...]) -> Dict[str, List[str]]:
        aws_principals: List[str] = []
        service_principals: List[str] = []
        for p in principals:
            if p.endswith(".amazonaws.com"):
                service_principals.append(p)
            else:
                aws_principals.append(p)
        result: Dict[str, List[str]] = {}
        if aws_principals:
            result["AWS"] = aws_principals
        if service_principals:
            result["Service"] = service_principals
        return result

    @classmethod
    def _translate_conditions(cls, conditions: Any) -> Dict[str, Dict[str, Any]]:
        if not conditions:
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for op_key, values in conditions.items():
            aws_op = cls.CONDITION_MAP.get(op_key, op_key)
            result[aws_op] = dict(values)
        return result
