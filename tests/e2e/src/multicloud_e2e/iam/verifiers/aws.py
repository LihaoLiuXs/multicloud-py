"""AWS IAM verifier — uses raw boto3 to independently verify resources."""

import json
from typing import Any
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError
from multicloud_e2e.common.verifier import ServiceVerifier


class AwsIamVerifier(ServiceVerifier):
    def __init__(self, region: str = None):
        self._iam = boto3.client("iam", region_name=region or "us-east-1")

    @property
    def api_names(self) -> dict:
        return {
            "create": "iam.create_role()",
            "get": "iam.get_role()",
            "delete": "iam.delete_role()",
            "verify_exists": "iam.get_role()",
            "verify_deleted": "iam.get_role() [expect NoSuchEntity]",
        }

    def verify_exists(
        self, identity_name: str, tenant_id: str, expected_id: str
    ) -> bool:
        response = self._iam.get_role(RoleName=identity_name)
        return response["Role"]["Arn"] == expected_id

    def verify_deleted(self, identity_name: str, tenant_id: str) -> bool:
        try:
            self._iam.get_role(RoleName=identity_name)
            return False
        except ClientError as exc:
            return exc.response.get("Error", {}).get("Code") == "NoSuchEntity"

    def verify_trust_config(
        self, identity_name: str, tenant_id: str, expected_principal: str
    ) -> bool:
        response = self._iam.get_role(RoleName=identity_name)
        policy_str = response["Role"].get("AssumeRolePolicyDocument", "")
        if isinstance(policy_str, str):
            policy_doc = json.loads(unquote(policy_str))
        else:
            policy_doc = policy_str
        for stmt in policy_doc.get("Statement", []):
            principals = stmt.get("Principal", {})
            aws_principals = principals.get("AWS", [])
            if isinstance(aws_principals, str):
                aws_principals = [aws_principals]
            service_principals = principals.get("Service", [])
            if isinstance(service_principals, str):
                service_principals = [service_principals]
            if (
                expected_principal in aws_principals
                or expected_principal in service_principals
            ):
                return True
        return False

    def verify_policy_attached(
        self, identity_name: str, policy_name: str, tenant_id: str
    ) -> bool:
        try:
            self._iam.get_role_policy(RoleName=identity_name, PolicyName=policy_name)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
                return False
            raise

    def verify_policy_removed(
        self, identity_name: str, policy_name: str, tenant_id: str
    ) -> bool:
        try:
            self._iam.get_role_policy(RoleName=identity_name, PolicyName=policy_name)
            return False
        except ClientError as exc:
            return exc.response.get("Error", {}).get("Code") == "NoSuchEntity"

    def cleanup(self, identity_name: str, tenant_id: str) -> None:
        try:
            paginator = self._iam.get_paginator("list_role_policies")
            for page in paginator.paginate(RoleName=identity_name):
                for policy_name in page.get("PolicyNames", []):
                    self._iam.delete_role_policy(
                        RoleName=identity_name, PolicyName=policy_name
                    )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                raise
        try:
            self._iam.delete_role(RoleName=identity_name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                raise
