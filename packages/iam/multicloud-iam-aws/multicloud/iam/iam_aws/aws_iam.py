"""AWS IAM implementation of :class:`BaseIam`.

Translates boto3 IAM calls into the cloud-agnostic driver contract.

AWS identity concept: IAM Role
  - create_identity → createRole (with assume-role-policy-document)
  - get_identity    → getRole → returns ARN
  - delete_identity → deleteRole
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

try:
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError as exc:
    raise ImportError(
        "The AWS IAM provider requires boto3. Install with: "
        "pip install 'python-multicloud-sdk[aws]'"
    ) from exc

from multicloud.aws_common.aws_credentials import get_session
from multicloud.aws_common.error_mapping import AWS_THROTTLING_CODES
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceConflictException,
    ResourceExhaustedException,
    ResourceExistsException,
    ResourceNotFoundException,
    SubstrateSdkException,
    UnAuthorizedException,
    UnknownException,
)
from multicloud.iam.iam_aws.aws_policy_translator import AwsPolicyTranslator
from multicloud.iam.iam_client.driver import (
    IAM_REGISTRY,
    AttachInlinePolicyRequest,
    BaseIam,
    CreateIdentityRequest,
    CreateOptions,
    CredentialsOverrider,
    DeleteIdentityRequest,
    GetAttachedPoliciesRequest,
    GetIdentityRequest,
    GetInlinePolicyDetailsRequest,
    RemovePolicyRequest,
    TrustConfiguration,
)

POLICY_VERSION = "2012-10-17"
ALLOW = "Allow"
STS_ASSUME_ROLE = "sts:AssumeRole"
ARN_PREFIX = "arn:"
AWS_ACCOUNT_ID_REGEX_LEN = 12
ARN_AWS_IAM_PREFIX = "arn:aws:iam::"
ROOT_SUFFIX = ":root"
SERVICE_PRINCIPAL_SUFFIX = ".amazonaws.com"


#: IAM also reports a quota breach without the Exception suffix.
_THROTTLING_CODES = AWS_THROTTLING_CODES | {"LimitExceeded"}


@IAM_REGISTRY.register("aws")
class AwsIam(BaseIam):
    """AWS IAM-backed identity store — manages IAM Roles."""

    def __init__(
        self,
        region: Optional[str] = None,
        *,
        iam_client: Any = None,
        endpoint_url: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
    ) -> None:
        super().__init__(region=region, credentials_overrider=credentials_overrider)
        if iam_client is not None:
            # Caller-supplied client takes precedence over credentials_overrider.
            self._iam = iam_client
        else:
            # IAM is global; default to us-east-1 for the client while passing
            # the caller's raw region through to credential resolution.
            session = get_session(credentials_overrider, region)
            region_str = region or "us-east-1"
            client_kwargs: Dict[str, Any] = {"region_name": region_str}
            if endpoint_url:
                client_kwargs["endpoint_url"] = endpoint_url
            self._iam = session.client("iam", **client_kwargs)

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate AWS IAM exceptions to the common hierarchy."""
        if isinstance(exc, SubstrateSdkException):
            return exc
        if isinstance(exc, NoCredentialsError):
            return UnAuthorizedException(str(exc))
        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")

            # Checked before the status branches below: AWS reports throttling
            # with HTTP 400 on some services, which would otherwise be read as
            # an invalid argument.
            if code in _THROTTLING_CODES or status == 429:
                return ResourceExhaustedException(str(exc))
            if (
                code in ("NoSuchEntity", "NoSuchEntityException", "404")
                or status == 404
            ):
                return ResourceNotFoundException(str(exc))
            if code in ("EntityAlreadyExists", "EntityAlreadyExistsException"):
                return ResourceExistsException(str(exc))
            if code in ("AccessDenied", "Forbidden", "403") or status == 403:
                return UnAuthorizedException(str(exc))
            if code in ("InvalidInput", "MalformedPolicyDocument", "InvalidArgument"):
                return InvalidArgumentException(str(exc))
            if code in ("DeleteConflict", "DeleteConflictException"):
                return ResourceConflictException(str(exc))
            return UnknownException(str(exc))
        if isinstance(exc, BotoCoreError):
            return InvalidArgumentException(str(exc))
        return UnknownException(str(exc))

    def do_create_identity(self, request: CreateIdentityRequest) -> str:
        assume_role_policy = _build_assume_role_policy_document(
            request.tenant_id, request.trust_config
        )

        create_kwargs: Dict[str, Any] = {
            "RoleName": request.identity_name,
            "AssumeRolePolicyDocument": assume_role_policy,
            "Description": request.description or "",
        }

        if request.options:
            if request.options.path:
                create_kwargs["Path"] = request.options.path
            if request.options.max_session_duration is not None:
                create_kwargs["MaxSessionDuration"] = (
                    request.options.max_session_duration
                )
            if request.options.permission_boundary:
                create_kwargs["PermissionsBoundary"] = (
                    request.options.permission_boundary
                )

        try:
            response = self._iam.create_role(**create_kwargs)
            role = response.get("Role", {})
            return role.get("Arn", "")
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("EntityAlreadyExists", "EntityAlreadyExistsException"):
                return self._handle_already_exists(request, assume_role_policy)
            raise self.translate_error(exc) from exc
        except (BotoCoreError, NoCredentialsError) as exc:
            raise self.translate_error(exc) from exc

    def _handle_already_exists(
        self,
        request: CreateIdentityRequest,
        assume_role_policy: str,
    ) -> str:
        try:
            get_response = self._iam.get_role(RoleName=request.identity_name)
        except (ClientError, BotoCoreError, NoCredentialsError) as exc:
            raise self.translate_error(exc) from exc

        existing_role = get_response.get("Role", {})
        self._update_role_if_needed(
            existing_role, request.description, assume_role_policy, request.options
        )
        return existing_role.get("Arn", "")

    def _update_role_if_needed(
        self,
        existing_role: Dict[str, Any],
        new_description: Optional[str],
        new_assume_role_policy: str,
        options: Optional[CreateOptions],
    ) -> None:
        needs_update = False
        existing_description = existing_role.get("Description", "")
        target_description = new_description or ""
        if existing_description != target_description:
            needs_update = True

        if not needs_update and options:
            if (
                options.max_session_duration is not None
                and options.max_session_duration
                != existing_role.get("MaxSessionDuration")
            ):
                needs_update = True

        if needs_update:
            update_kwargs: Dict[str, Any] = {
                "RoleName": existing_role["RoleName"],
                "Description": target_description,
            }
            if options and options.max_session_duration is not None:
                update_kwargs["MaxSessionDuration"] = options.max_session_duration
            try:
                self._iam.update_role(**update_kwargs)
            except (ClientError, BotoCoreError, NoCredentialsError) as exc:
                raise self.translate_error(exc) from exc

        if _is_trust_policy_different(
            existing_role.get("AssumeRolePolicyDocument"), new_assume_role_policy
        ):
            try:
                self._iam.update_assume_role_policy(
                    RoleName=existing_role["RoleName"],
                    PolicyDocument=new_assume_role_policy,
                )
            except (ClientError, BotoCoreError, NoCredentialsError) as exc:
                raise self.translate_error(exc) from exc

    def do_get_identity(self, request: GetIdentityRequest) -> str:
        try:
            response = self._iam.get_role(RoleName=request.identity_name)
            role = response.get("Role", {})
            return role.get("Arn", "")
        except (ClientError, BotoCoreError, NoCredentialsError) as exc:
            raise self.translate_error(exc) from exc

    def do_delete_identity(self, request: DeleteIdentityRequest) -> None:
        try:
            self._iam.delete_role(RoleName=request.identity_name)
        except (ClientError, BotoCoreError, NoCredentialsError) as exc:
            raise self.translate_error(exc) from exc

    # Policy Management
    def do_attach_inline_policy(self, request: AttachInlinePolicyRequest) -> None:
        try:
            policy_json = AwsPolicyTranslator.translate(request.policy_document)
            self._iam.put_role_policy(
                RoleName=request.identity_name,
                PolicyName=request.policy_document.name,
                PolicyDocument=policy_json,
            )
        except (ClientError, BotoCoreError, NoCredentialsError) as exc:
            raise self.translate_error(exc) from exc

    def do_get_inline_policy_details(
        self, request: GetInlinePolicyDetailsRequest
    ) -> Optional[str]:
        try:
            response = self._iam.get_role_policy(
                RoleName=request.identity_name,
                PolicyName=request.policy_name,
            )
            policy_doc = response.get("PolicyDocument", "")
            if isinstance(policy_doc, str):
                return unquote(policy_doc)
            return json.dumps(policy_doc)
        except (ClientError, BotoCoreError, NoCredentialsError) as exc:
            raise self.translate_error(exc) from exc

    def do_get_attached_policies(
        self, request: GetAttachedPoliciesRequest
    ) -> List[str]:
        try:
            paginator = self._iam.get_paginator("list_role_policies")
            policy_names: List[str] = []
            for page in paginator.paginate(RoleName=request.identity_name):
                policy_names.extend(page.get("PolicyNames", []))
            return policy_names
        except (ClientError, BotoCoreError, NoCredentialsError) as exc:
            raise self.translate_error(exc) from exc

    def do_remove_policy(self, request: RemovePolicyRequest) -> None:
        try:
            self._iam.delete_role_policy(
                RoleName=request.identity_name,
                PolicyName=request.policy_name,
            )
        except (ClientError, BotoCoreError, NoCredentialsError) as exc:
            raise self.translate_error(exc) from exc

    def close(self) -> None:
        close_fn = getattr(self._iam, "close", None)
        if callable(close_fn):
            close_fn()


def _build_assume_role_policy_document(
    tenant_id: str,
    trust_config: Optional[TrustConfiguration],
) -> str:
    aws_principals: List[str] = []
    service_principals: List[str] = []

    if trust_config and trust_config.trusted_principals:
        for principal in trust_config.trusted_principals:
            if not principal or not principal.strip():
                continue
            if principal.startswith(ARN_PREFIX):
                aws_principals.append(principal)
            elif principal.isdigit() and len(principal) == AWS_ACCOUNT_ID_REGEX_LEN:
                aws_principals.append(ARN_AWS_IAM_PREFIX + principal + ROOT_SUFFIX)
            elif principal.endswith(SERVICE_PRINCIPAL_SUFFIX):
                service_principals.append(principal)
            else:
                aws_principals.append(principal)

    if not aws_principals and not service_principals:
        aws_principals.append(ARN_AWS_IAM_PREFIX + tenant_id + ROOT_SUFFIX)

    principal: Dict[str, Any] = {}
    if aws_principals:
        principal["AWS"] = aws_principals
    if service_principals:
        principal["Service"] = service_principals

    stmt: Dict[str, Any] = {
        "Effect": ALLOW,
        "Action": STS_ASSUME_ROLE,
        "Principal": principal,
    }

    if trust_config and trust_config.conditions:
        stmt["Condition"] = trust_config.conditions

    doc = {"Version": POLICY_VERSION, "Statement": [stmt]}
    return json.dumps(doc)


def _is_trust_policy_different(existing_policy: Any, new_policy: str) -> bool:
    if existing_policy is None and new_policy is None:
        return False
    if existing_policy is None or new_policy is None:
        return True
    try:
        if isinstance(existing_policy, str):
            existing_decoded = unquote(existing_policy)
            existing_json = json.loads(existing_decoded)
        else:
            existing_json = existing_policy
        new_json = json.loads(new_policy)
        return existing_json != new_json
    except (json.JSONDecodeError, ValueError):
        return True
