"""GCP IAM implementation of :class:`BaseIam`.

Translates Google Cloud IAM SDK calls into the cloud-agnostic driver contract.

GCP identity concept: Service Account
  - create_identity → createServiceAccount (+ optional trust bindings)
  - get_identity    → getServiceAccount → returns email
  - delete_identity → deleteServiceAccount
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

try:
    from google.api_core import exceptions as gcp_exceptions
    from google.cloud.iam_admin_v1 import (
        CreateServiceAccountRequest,
        DeleteServiceAccountRequest,
        GetServiceAccountRequest,
        IAMClient,
        ServiceAccount,
    )
    from google.cloud.resourcemanager_v3 import ProjectsClient
    from google.iam.v1 import iam_policy_pb2, policy_pb2
except ImportError as exc:
    raise ImportError(
        "The GCP IAM provider requires google-cloud-iam and "
        "google-cloud-resource-manager. Install with: "
        "pip install 'python-multicloud-sdk[gcp]'"
    ) from exc

from multicloud.common.exceptions import (
    DeadlineExceededException,
    InvalidArgumentException,
    ResourceConflictException,
    ResourceExhaustedException,
    ResourceExistsException,
    ResourceNotFoundException,
    SubstrateSdkException,
    UnAuthorizedException,
    UnknownException,
)
from multicloud.gcp_common.gcp_credentials import get_credentials
from multicloud.iam.iam_client.driver import (
    IAM_REGISTRY,
    AttachInlinePolicyRequest,
    BaseIam,
    CreateIdentityRequest,
    CredentialsOverrider,
    DeleteIdentityRequest,
    GetAttachedPoliciesRequest,
    GetIdentityRequest,
    GetInlinePolicyDetailsRequest,
    RemovePolicyRequest,
)
from multicloud.iam.iam_gcp.gcp_policy_translator import GcpPolicyTranslator


@IAM_REGISTRY.register("gcp")
class GcpIam(BaseIam):
    """GCP IAM-backed identity store — manages Service Accounts."""

    def __init__(
        self,
        region: Optional[str] = None,
        *,
        iam_client: Any = None,
        projects_client: Any = None,
        endpoint_url: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
    ) -> None:
        super().__init__(region=region, credentials_overrider=credentials_overrider)
        # Resolve the overrider once, but only if a client actually needs
        # building — a fully-injected caller bypasses credential resolution.
        # Each client then independently prefers an injected instance, then the
        # resolved credentials, then defaults, so injecting only one client
        # still honors the overrider for the other.
        needs_build = iam_client is None or projects_client is None
        resolved = (
            get_credentials(credentials_overrider)
            if needs_build and credentials_overrider is not None
            else None
        )

        if iam_client is not None:
            self._iam_client = iam_client
        elif resolved is not None:
            self._iam_client = IAMClient(credentials=resolved)
        else:
            self._iam_client = IAMClient()

        if projects_client is not None:
            self._projects_client = projects_client
        elif resolved is not None:
            self._projects_client = ProjectsClient(credentials=resolved)
        else:
            self._projects_client = ProjectsClient()

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate GCP IAM exceptions to the common hierarchy."""
        if isinstance(exc, SubstrateSdkException):
            return exc
        if isinstance(exc, gcp_exceptions.NotFound):
            return ResourceNotFoundException(str(exc))
        if isinstance(exc, gcp_exceptions.AlreadyExists):
            return ResourceExistsException(str(exc))
        if isinstance(exc, (gcp_exceptions.Unauthorized, gcp_exceptions.Forbidden)):
            return UnAuthorizedException(str(exc))
        if isinstance(exc, gcp_exceptions.BadRequest):
            return InvalidArgumentException(str(exc))
        # 429 means the request was rate limited; 504 is reported as a deadline.
        # 503 falls through to unknown: an unavailable backend is not the caller
        # exceeding a quota.
        # ResourceExhausted and DeadlineExceeded are the gRPC-flavoured
        # subclasses of these, so each check covers both.
        if isinstance(exc, gcp_exceptions.TooManyRequests):
            return ResourceExhaustedException(str(exc))
        if isinstance(exc, gcp_exceptions.GatewayTimeout):
            return DeadlineExceededException(str(exc))
        if isinstance(
            exc, (gcp_exceptions.FailedPrecondition, gcp_exceptions.Conflict)
        ):
            return ResourceConflictException(str(exc))
        return UnknownException(str(exc))

    def do_create_identity(self, request: CreateIdentityRequest) -> str:
        tenant_id = request.tenant_id
        project_name = (
            tenant_id if tenant_id.startswith("projects/") else f"projects/{tenant_id}"
        )

        service_account = ServiceAccount(
            display_name=request.identity_name,
            description=request.description or "",
        )

        create_request = CreateServiceAccountRequest(
            name=project_name,
            account_id=request.identity_name,
            service_account=service_account,
        )

        try:
            created = self._iam_client.create_service_account(request=create_request)
        except gcp_exceptions.AlreadyExists:
            created = self._get_service_account(request.identity_name, tenant_id)
        except gcp_exceptions.GoogleAPIError as exc:
            raise self.translate_error(exc) from exc

        service_account_email = str(created.email)

        if request.trust_config and request.trust_config.trusted_principals:
            self._set_trust_bindings(
                created.name,
                request.trust_config.trusted_principals,
            )

        return service_account_email

    def _set_trust_bindings(
        self,
        service_account_resource_name: str,
        trusted_principals: List[str],
    ) -> None:
        try:
            get_request = iam_policy_pb2.GetIamPolicyRequest(
                resource=service_account_resource_name
            )
            policy = self._iam_client.get_iam_policy(request=get_request)

            for principal in trusted_principals:
                member = _format_principal_as_member(principal)
                existing_binding = None
                for b in policy.bindings:
                    if b.role == "roles/iam.serviceAccountTokenCreator":
                        existing_binding = b
                        break
                if existing_binding is not None:
                    if member not in existing_binding.members:
                        existing_binding.members.append(member)
                else:
                    binding = policy_pb2.Binding(
                        role="roles/iam.serviceAccountTokenCreator",
                        members=[member],
                    )
                    policy.bindings.append(binding)

            set_request = iam_policy_pb2.SetIamPolicyRequest(
                resource=service_account_resource_name,
                policy=policy,
            )
            self._iam_client.set_iam_policy(request=set_request)
        except gcp_exceptions.GoogleAPIError as exc:
            raise self.translate_error(exc) from exc

    def do_get_identity(self, request: GetIdentityRequest) -> str:
        sa = self._get_service_account(request.identity_name, request.tenant_id)
        return str(sa.email)

    def do_delete_identity(self, request: DeleteIdentityRequest) -> None:
        resource_name = _get_service_account_resource_name(
            request.identity_name, request.tenant_id
        )
        delete_request = DeleteServiceAccountRequest(name=resource_name)
        try:
            self._iam_client.delete_service_account(request=delete_request)
        except gcp_exceptions.GoogleAPIError as exc:
            raise self.translate_error(exc) from exc

    # Policy Management
    def _policy_context(
        self, identity_name: str, tenant_id: str
    ) -> tuple[str, str, str]:
        """Return (project, resource_name, member) for policy operations."""
        project = (
            tenant_id if tenant_id.startswith("projects/") else f"projects/{tenant_id}"
        )
        resource_name = _get_service_account_resource_name(identity_name, tenant_id)
        member = f"serviceAccount:{resource_name.split('/serviceAccounts/')[1]}"
        return project, resource_name, member

    def do_attach_inline_policy(self, request: AttachInlinePolicyRequest) -> None:
        try:
            project, _, member = self._policy_context(
                request.identity_name, request.tenant_id
            )

            get_request = iam_policy_pb2.GetIamPolicyRequest(resource=project)
            policy = self._projects_client.get_iam_policy(request=get_request)
            if policy is None:
                policy = policy_pb2.Policy()
            bindings = list(policy.bindings) if policy.bindings else []
            changed = False

            for statement in request.policy_document.statements:
                # GCP IAM v1 bindings only support ALLOW. Deny policies require
                # the IAM v2 PoliciesClient and are managed separately.
                if statement.effect and statement.effect.value == "Deny":
                    continue
                roles = GcpPolicyTranslator.translate_actions_to_roles(statement)
                for role in roles:
                    binding = next((b for b in bindings if b.role == role), None)
                    if binding is None:
                        new_binding = policy_pb2.Binding(role=role, members=[member])
                        bindings.append(new_binding)
                        changed = True
                    elif member not in binding.members:
                        binding.members.append(member)
                        changed = True

            if changed:
                del policy.bindings[:]
                policy.bindings.extend(bindings)
                set_request = iam_policy_pb2.SetIamPolicyRequest(
                    resource=project, policy=policy
                )
                self._projects_client.set_iam_policy(request=set_request)
        except gcp_exceptions.GoogleAPIError as exc:
            raise self.translate_error(exc) from exc

    def do_get_inline_policy_details(
        self, request: GetInlinePolicyDetailsRequest
    ) -> Optional[str]:
        try:
            project, _, member = self._policy_context(
                request.identity_name, request.tenant_id
            )

            get_request = iam_policy_pb2.GetIamPolicyRequest(resource=project)
            policy = self._projects_client.get_iam_policy(request=get_request)
            if policy is None:
                return None
            for binding in policy.bindings or []:
                if binding.role == request.policy_name and member in binding.members:
                    return json.dumps({"role": binding.role, "member": member})
            return None
        except gcp_exceptions.GoogleAPIError as exc:
            raise self.translate_error(exc) from exc

    def do_get_attached_policies(
        self, request: GetAttachedPoliciesRequest
    ) -> List[str]:
        try:
            project, _, member = self._policy_context(
                request.identity_name, request.tenant_id
            )

            get_request = iam_policy_pb2.GetIamPolicyRequest(resource=project)
            policy = self._projects_client.get_iam_policy(request=get_request)
            if policy is None:
                return []
            roles: List[str] = []
            for binding in policy.bindings or []:
                if member in binding.members:
                    roles.append(binding.role)
            return roles
        except gcp_exceptions.GoogleAPIError as exc:
            raise self.translate_error(exc) from exc

    def do_remove_policy(self, request: RemovePolicyRequest) -> None:
        try:
            project, _, member = self._policy_context(
                request.identity_name, request.tenant_id
            )

            get_request = iam_policy_pb2.GetIamPolicyRequest(resource=project)
            policy = self._projects_client.get_iam_policy(request=get_request)
            if policy is None:
                return
            bindings = list(policy.bindings) if policy.bindings else []
            changed = False
            remove_idx: Optional[int] = None
            for i, binding in enumerate(bindings):
                if binding.role == request.policy_name and member in binding.members:
                    binding.members.remove(member)
                    changed = True
                    if not binding.members:
                        remove_idx = i
                    break

            if remove_idx is not None:
                bindings.pop(remove_idx)

            if changed:
                del policy.bindings[:]
                policy.bindings.extend(bindings)
                set_request = iam_policy_pb2.SetIamPolicyRequest(
                    resource=project, policy=policy
                )
                self._projects_client.set_iam_policy(request=set_request)
        except gcp_exceptions.GoogleAPIError as exc:
            raise self.translate_error(exc) from exc

    def _get_service_account(self, identity_name: str, tenant_id: str) -> Any:
        resource_name = _get_service_account_resource_name(identity_name, tenant_id)
        get_request = GetServiceAccountRequest(name=resource_name)
        try:
            return self._iam_client.get_service_account(request=get_request)
        except gcp_exceptions.GoogleAPIError as exc:
            raise self.translate_error(exc) from exc

    def close(self) -> None:
        for client in (self._iam_client, self._projects_client):
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass


def _get_service_account_resource_name(identity_name: str, tenant_id: str) -> str:
    project_name = (
        tenant_id if tenant_id.startswith("projects/") else f"projects/{tenant_id}"
    )
    project_id = project_name.removeprefix("projects/")
    if "@" in identity_name:
        service_account_email = identity_name
    else:
        service_account_email = f"{identity_name}@{project_id}.iam.gserviceaccount.com"
    return f"{project_name}/serviceAccounts/{service_account_email}"


def _format_principal_as_member(principal: str) -> str:
    if ":" in principal:
        return principal
    return f"serviceAccount:{principal}"
