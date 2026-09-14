"""GCP IAM verifier — uses raw google-cloud to independently verify resources."""

from typing import Any

from google.api_core import exceptions as gcp_exceptions
from google.cloud.iam_admin_v1 import (
    DeleteServiceAccountRequest,
    GetServiceAccountRequest,
)
from google.cloud.iam_admin_v1 import IAMClient as RawIAMClient
from google.cloud.resourcemanager_v3 import ProjectsClient
from google.iam.v1 import iam_policy_pb2
from multicloud_e2e.common.verifier import ServiceVerifier


class GcpIamVerifier(ServiceVerifier):
    def __init__(self, region: str = None):
        self._iam = RawIAMClient()
        self._projects = ProjectsClient()

    @property
    def api_names(self) -> dict:
        return {
            "create": "IAMClient.create_service_account()",
            "get": "IAMClient.get_service_account()",
            "delete": "IAMClient.delete_service_account()",
            "verify_exists": "IAMClient.get_service_account()",
            "verify_deleted": "IAMClient.get_service_account() [expect NotFound]",
        }

    def verify_exists(
        self, identity_name: str, tenant_id: str, expected_id: str
    ) -> bool:
        resource_name = self._resource_name(identity_name, tenant_id)
        request = GetServiceAccountRequest(name=resource_name)
        sa = self._iam.get_service_account(request=request)
        return sa.email == expected_id

    def verify_deleted(self, identity_name: str, tenant_id: str) -> bool:
        resource_name = self._resource_name(identity_name, tenant_id)
        request = GetServiceAccountRequest(name=resource_name)
        try:
            sa = self._iam.get_service_account(request=request)
            if hasattr(sa, "disabled") and sa.disabled:
                return True
            return False
        except gcp_exceptions.NotFound:
            return True
        except gcp_exceptions.PermissionDenied:
            return True

    def verify_trust_config(
        self, identity_name: str, tenant_id: str, expected_principal: str
    ) -> bool:
        resource_name = self._resource_name(identity_name, tenant_id)
        request = iam_policy_pb2.GetIamPolicyRequest(resource=resource_name)
        policy = self._iam.get_iam_policy(request=request)
        candidates = {expected_principal}
        if ":" not in expected_principal:
            candidates.add(f"serviceAccount:{expected_principal}")
        for binding in policy.bindings:
            if "serviceAccountTokenCreator" in binding.role:
                if candidates & set(binding.members):
                    return True
        return False

    def verify_policy_attached(
        self, identity_name: str, role_name: str, tenant_id: str
    ) -> bool:
        project = (
            tenant_id if tenant_id.startswith("projects/") else f"projects/{tenant_id}"
        )
        resource_name = self._resource_name(identity_name, tenant_id)
        member = f"serviceAccount:{resource_name.split('/serviceAccounts/')[1]}"
        request = iam_policy_pb2.GetIamPolicyRequest(resource=project)
        policy = self._projects.get_iam_policy(request=request)
        for binding in policy.bindings:
            if binding.role == role_name and member in binding.members:
                return True
        return False

    def verify_policy_removed(
        self, identity_name: str, role_name: str, tenant_id: str
    ) -> bool:
        project = (
            tenant_id if tenant_id.startswith("projects/") else f"projects/{tenant_id}"
        )
        resource_name = self._resource_name(identity_name, tenant_id)
        member = f"serviceAccount:{resource_name.split('/serviceAccounts/')[1]}"
        request = iam_policy_pb2.GetIamPolicyRequest(resource=project)
        policy = self._projects.get_iam_policy(request=request)
        for binding in policy.bindings:
            if binding.role == role_name and member in binding.members:
                return False
        return True

    def cleanup(self, identity_name: str, tenant_id: str) -> None:
        resource_name = self._resource_name(identity_name, tenant_id)
        try:
            request = DeleteServiceAccountRequest(name=resource_name)
            self._iam.delete_service_account(request=request)
        except gcp_exceptions.NotFound:
            pass

    def _resource_name(self, identity_name: str, tenant_id: str) -> str:
        project = (
            tenant_id if tenant_id.startswith("projects/") else f"projects/{tenant_id}"
        )
        project_id = project.removeprefix("projects/")
        if "@" in identity_name:
            email = identity_name
        else:
            email = f"{identity_name}@{project_id}.iam.gserviceaccount.com"
        return f"{project}/serviceAccounts/{email}"
