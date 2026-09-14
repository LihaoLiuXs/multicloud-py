"""AWS STS implementation of :class:`BaseSts`.

Translates STS / boto3 errors into the cloud-agnostic exception hierarchy.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from multicloud.aws_common.aws_credentials import get_session
from multicloud.common.exceptions import (
    SubstrateSdkException,
    UnsupportedOperationException,
)
from multicloud.sts.sts_aws._aws_common import translate_aws_sts_error
from multicloud.sts.sts_client.driver import (
    STS_REGISTRY,
    BaseSts,
    CallerIdentity,
    CredentialScope,
    CredentialsOverrider,
    StsCredentials,
)

_RESOURCE_SCHEME = "storage://"
_PERMISSION_PREFIX = "storage:"
_IAM_POLICY_VERSION = "2012-10-17"


@STS_REGISTRY.register("aws")
class AwsSts(BaseSts):
    """AWS STS-backed credential service."""

    def __init__(
        self,
        region: Optional[str] = None,
        *,
        sts_client: Any = None,
        endpoint_url: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
    ) -> None:
        super().__init__(
            region=region,
            endpoint_url=endpoint_url,
            credentials_overrider=credentials_overrider,
        )

        if sts_client is not None:
            # Caller-supplied client takes precedence over credentials_overrider.
            self._sts = sts_client
            self._session = None
        else:
            self._session = get_session(credentials_overrider, region)
            kwargs: dict[str, Any] = {"region_name": region}
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            self._sts = self._session.client("sts", **kwargs)

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate AWS STS exceptions to common hierarchy."""
        return translate_aws_sts_error(exc)

    def do_get_caller_identity(self, *, aud: Optional[str] = None) -> CallerIdentity:
        """Call AWS STS GetCallerIdentity API.

        The ``aud`` parameter is ignored for AWS (not applicable).
        """
        resp = self._sts.get_caller_identity()
        return CallerIdentity(
            user_id=resp["UserId"],
            cloud_resource_name=resp["Arn"],
            account_id=resp["Account"],
        )

    def do_assume_role(
        self,
        role: str,
        *,
        session_name: str,
        expiration: int,
        external_id: Optional[str],
        policy: Optional[str],
        tags: Mapping[str, str],
        credential_scope: Optional[CredentialScope] = None,
    ) -> StsCredentials:
        """Call AWS STS AssumeRole API."""
        kwargs: dict[str, Any] = {
            "RoleArn": role,
            "RoleSessionName": session_name,
            "DurationSeconds": expiration,
        }
        if external_id:
            kwargs["ExternalId"] = external_id
        if credential_scope is not None:
            kwargs["Policy"] = self._convert_scope_to_policy(credential_scope)
        elif policy:
            kwargs["Policy"] = policy
        if tags:
            kwargs["Tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]

        resp = self._sts.assume_role(**kwargs)
        c = resp["Credentials"]
        return StsCredentials(
            access_key_id=c["AccessKeyId"],
            access_key_secret=c["SecretAccessKey"],
            security_token=c.get("SessionToken", ""),
            expiration=c.get("Expiration"),
        )

    @staticmethod
    def _convert_scope_to_policy(scope: CredentialScope) -> str:
        """Convert CredentialScope to AWS IAM Policy JSON."""
        statements = []
        for rule in scope.rules:
            resource = rule.available_resource
            bucket = ""
            if resource.startswith(_RESOURCE_SCHEME):
                bucket = resource[len(_RESOURCE_SCHEME) :]
                resource = f"arn:aws:s3:::{bucket}/*"

            actions = [
                p.replace(_PERMISSION_PREFIX, "s3:", 1)
                for p in rule.available_permissions
            ]

            if (
                rule.availability_condition
                and rule.availability_condition.resource_prefix
            ):
                prefix = rule.availability_condition.resource_prefix
                if prefix.startswith(_RESOURCE_SCHEME):
                    path = prefix[len(_RESOURCE_SCHEME) :]
                    if "/" in path:
                        path_prefix = path[path.index("/") + 1 :]
                        if path_prefix:
                            resource = f"arn:aws:s3:::{bucket}/{path_prefix}*"

            statement: dict[str, Any] = {
                "Effect": "Allow",
                "Action": actions,
                "Resource": resource,
            }

            statements.append(statement)

        return json.dumps({"Version": _IAM_POLICY_VERSION, "Statement": statements})

    def do_get_access_token(
        self,
        *,
        audience: Optional[str] = None,
        scopes: Optional[list[str]] = None,
        duration_seconds: Optional[int] = None,
    ) -> StsCredentials:
        """Call AWS STS GetSessionToken API to get fresh temporary credentials.

        ``audience`` and ``scopes`` are not applicable for AWS — AWS session
        tokens are scope-less and do not support audience targeting. Both
        parameters are accepted for API compatibility and silently ignored.
        """
        kwargs: dict[str, Any] = {}
        if duration_seconds is not None:
            kwargs["DurationSeconds"] = duration_seconds

        resp = self._sts.get_session_token(**kwargs)
        c = resp["Credentials"]
        return StsCredentials(
            access_key_id=c["AccessKeyId"],
            access_key_secret=c["SecretAccessKey"],
            security_token=c.get("SessionToken", ""),
            expiration=c.get("Expiration"),
        )

    def do_assume_role_with_web_identity(
        self,
        role: str,
        web_identity_token: str,
        *,
        session_name: str,
        expiration: int,
    ) -> StsCredentials:
        """Call AWS STS AssumeRoleWithWebIdentity API.

        Exchanges a web identity (OIDC) token for temporary credentials via
        an IAM role whose trust policy permits the token's issuer.
        """
        kwargs: dict[str, Any] = {
            "RoleArn": role,
            "RoleSessionName": session_name,
            "WebIdentityToken": web_identity_token,
            "DurationSeconds": expiration,
        }

        resp = self._sts.assume_role_with_web_identity(**kwargs)
        c = resp["Credentials"]
        return StsCredentials(
            access_key_id=c["AccessKeyId"],
            access_key_secret=c["SecretAccessKey"],
            security_token=c.get("SessionToken", ""),
            expiration=c.get("Expiration"),
        )

    def do_generate_service_account_token(
        self,
        target_principal: str,
        *,
        scopes: list[str],
        lifetime: int,
    ) -> StsCredentials:
        """Not supported for AWS.

        AWS has no equivalent to GCP's IAM Credentials
        ``generateAccessToken`` for service account impersonation.
        """
        raise UnsupportedOperationException(
            "generate_service_account_token is not supported for AWS. "
            "AWS does not have an equivalent to GCP's IAM Credentials "
            "generateAccessToken for service account impersonation."
        )

    def close(self) -> None:
        close = getattr(self._sts, "close", None)
        if callable(close):
            close()
