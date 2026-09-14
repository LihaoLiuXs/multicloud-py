"""IAM Identity Lifecycle — provider-agnostic E2E example.

Same code runs against AWS or GCP. Only --provider and cloud-neutral
arguments change.

Usage:
    python-multicloud-sdk-e2e iam --provider aws --tenant-id 123456789012
    python-multicloud-sdk-e2e iam --provider gcp --tenant-id my-project
"""

from __future__ import annotations

import functools
import os
from typing import Optional

from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
    StsCredentials,
)
from multicloud.iam.iam_client import (
    CreateOptions,
    Effect,
    IamClient,
    PolicyDocument,
    Statement,
    StorageActions,
    TrustConfiguration,
)
from multicloud_e2e.common.console_urls import console_url
from multicloud_e2e.common.harness import (
    lifecycle_test,
    pause,
    provider_call_annotation,
    sdk_call_annotation,
    section_manual_verify,
    section_provider_verify,
    section_sdk_call,
    step_fail,
    step_link,
    step_start,
    step_success,
    step_verify_timed,
    timed_call,
    unique_name,
)
from multicloud_e2e.iam.verifiers import get_iam_verifier


def _build_overrider(
    provider: str,
    creds_type: str,
    assume_role_target: Optional[str],
    web_identity_token_file: Optional[str],
    session_name: Optional[str],
    duration_seconds: Optional[int],
) -> CredentialsOverrider:
    """Build a CredentialsOverrider from CLI flags.

    Secrets are read from the environment — never from CLI arguments. The
    SESSION source is provider-specific so an AWS session left in the shell
    cannot leak into a GCP run (and vice versa):
      - AWS SESSION: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN.
      - GCP SESSION: MULTICLOUD_E2E_BEARER_TOKEN (OAuth2 bearer token, e.g.
        `gcloud auth print-access-token`).
      - WEB_IDENTITY: token read from --web-identity-token-file at call time.
    """
    if creds_type == "session":
        if provider == "gcp":
            bearer = os.environ.get("MULTICLOUD_E2E_BEARER_TOKEN", "")
            if not bearer:
                raise ValueError(
                    "GCP SESSION requires MULTICLOUD_E2E_BEARER_TOKEN "
                    "(e.g. export MULTICLOUD_E2E_BEARER_TOKEN="
                    '"$(gcloud auth print-access-token)")'
                )
            session_creds = StsCredentials(security_token=bearer)
        else:
            access_key_id = os.environ.get("AWS_ACCESS_KEY_ID", "")
            access_key_secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
            if not access_key_id or not access_key_secret:
                raise ValueError(
                    "AWS SESSION requires AWS_ACCESS_KEY_ID and "
                    "AWS_SECRET_ACCESS_KEY in the environment"
                )
            session_creds = StsCredentials(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                security_token=os.environ.get("AWS_SESSION_TOKEN", ""),
            )
        return CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=session_creds,
        )

    if creds_type == "assume-role":
        if not assume_role_target:
            raise ValueError(
                "--assume-role-target is required for --creds-type assume-role"
            )
        return CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role=assume_role_target,
            session_name=session_name,
            duration_seconds=duration_seconds,
        )

    if creds_type == "web-identity":
        if not assume_role_target:
            raise ValueError(
                "--assume-role-target is required for --creds-type web-identity"
            )
        if not web_identity_token_file:
            raise ValueError(
                "--web-identity-token-file is required for --creds-type web-identity"
            )
        token_file = web_identity_token_file

        def _read_token() -> str:
            with open(token_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()

        return CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role=assume_role_target,
            web_identity_token_supplier=_read_token,
            session_name=session_name,
            duration_seconds=duration_seconds,
        )

    raise ValueError(f"Unknown creds_type: {creds_type}")


def _whoami(
    provider: str, region: Optional[str], overrider: CredentialsOverrider
) -> str:
    """Resolve the effective principal after applying the overrider.

    Proves the override took effect rather than merely that the client
    constructed. Returns a principal string (AWS caller ARN or GCP account).
    """
    if provider == "aws":
        from multicloud.aws_common.aws_credentials import get_session

        session = get_session(overrider, region)
        return str(session.client("sts").get_caller_identity()["Arn"])

    # GCP: resolve the credentials the overrider yields, then read the
    # principal off the resolved credentials object.
    from multicloud.gcp_common.gcp_credentials import get_credentials

    resolved = get_credentials(overrider)
    if resolved is None:
        # WEB_IDENTITY is unsupported on GCP — get_credentials returns None
        # and the client falls through to Application Default Credentials.
        import google.auth

        default_creds, _ = google.auth.default()
        principal = getattr(default_creds, "service_account_email", None) or getattr(
            default_creds, "signer_email", "<default ADC principal>"
        )
        return f"{principal} (override no-op: WEB_IDENTITY unsupported on GCP)"

    # Impersonated (ASSUME_ROLE) credentials expose the target principal.
    target = getattr(resolved, "service_account_email", None) or getattr(
        resolved, "_target_principal", None
    )
    if target:
        return str(target)

    # SESSION wraps a raw OAuth2 bearer token that carries no principal
    # attribute. Validate it against Google's tokeninfo endpoint so the proof
    # fails loudly on an invalid/expired/wrong-cloud token instead of silently
    # succeeding — this is what distinguishes a real override from a no-op.
    import json
    import ssl
    import urllib.request

    token = getattr(resolved, "token", None)
    if not token:
        raise RuntimeError("resolved SESSION credentials carry no token")

    # Use certifi's CA bundle — the framework Python build does not trust the
    # macOS system keychain, which otherwise yields CERTIFICATE_VERIFY_FAILED.
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()

    url = "https://oauth2.googleapis.com/tokeninfo?access_token=" + token
    with urllib.request.urlopen(url, context=ctx) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    if "error" in info or "error_description" in info:
        raise RuntimeError(f"bearer token rejected by tokeninfo: {info}")
    principal = info.get("email") or info.get("azp") or info.get("sub")
    return f"{principal} (validated OAuth2 bearer token)"


def run(
    provider: str,
    identity_name: Optional[str],
    tenant_id: str,
    region: Optional[str],
    trusted_principal: Optional[str] = None,
    creds_type: Optional[str] = None,
    assume_role_target: Optional[str] = None,
    web_identity_token_file: Optional[str] = None,
    session_name: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    interactive: bool = True,
    do_cleanup: bool = True,
    verbose: bool = False,
) -> None:
    """Run IAM identity lifecycle: create → get → delete."""
    if not identity_name:
        prefix = "mc-e2e-test-" if provider == "gcp" else "multicloud-e2e-test-"
        identity_name = unique_name(prefix)

    # The verifier is an independent cross-check: it uses raw provider SDK
    # clients built from the ambient default credentials, NOT the overrider.
    # That independence is deliberate (a bug in the SDK's own credential
    # resolution can't mask itself during verification). Consequence: for a
    # cross-*principal* override, create can succeed while verify/cleanup run
    # under the default identity — a resource may be left behind and need
    # manual cleanup. Same-account overrides (the validated cases) are fine.
    verifier = get_iam_verifier(provider, region)
    provider_label = provider.upper()

    overrider = (
        _build_overrider(
            provider,
            creds_type,
            assume_role_target,
            web_identity_token_file,
            session_name,
            duration_seconds,
        )
        if creds_type
        else None
    )

    with lifecycle_test(
        f"IAM Identity Lifecycle — {provider_label}",
        f"Provider: {provider} | Tenant: {tenant_id} | Region: {region or 'default'}",
        interactive=interactive,
    ) as (cleanup, results, call_log):

        # -- STEP 1: CREATE IDENTITY --
        trust_config = TrustConfiguration(
            trusted_principals=[trusted_principal] if trusted_principal else [],
        )
        step_start(1, "CREATE IDENTITY", f"Name: {identity_name}")
        results["total"] += 1

        try:
            if overrider is not None:
                section_sdk_call()
                sdk_call_annotation(
                    f"CredentialsOverrider(credentials_type={overrider.credentials_type.name})"
                )
                try:
                    principal = _whoami(provider, region, overrider)
                    step_success(f"Override active — effective principal: {principal}")
                except Exception as who_exc:
                    step_fail(f"Could not resolve overridden principal: {who_exc}")

            client = IamClient(
                provider=provider, region=region, credentials_overrider=overrider
            )

            if do_cleanup:
                cleanup.register(
                    functools.partial(verifier.cleanup, identity_name, tenant_id)
                )

            section_sdk_call()
            sdk_call_annotation(
                f"IamClient.create_identity({identity_name}, tenant_id={tenant_id}, ...)"
            )
            with timed_call("IamClient.create_identity()") as t:
                t.value = client.create_identity(
                    identity_name,
                    description=f"E2E test identity ({provider})",
                    tenant_id=tenant_id,
                    region=region,
                    trust_config=trust_config,
                    options=CreateOptions(),
                )

            identity_id = t.value
            step_success(f"SDK returned: {identity_id}", elapsed=t.elapsed)
            call_log.record(
                "CREATE",
                "IamClient.create_identity()",
                verifier.api_names["create"],
                t.elapsed,
                True,
            )

            section_provider_verify(provider_label)
            provider_call_annotation(
                provider_label, verifier.api_names["verify_exists"], "verifying"
            )
            verified, v_elapsed = step_verify_timed(
                "identity exists, ID matches",
                f"{provider_label} SDK: get identity",
                lambda: verifier.verify_exists(identity_name, tenant_id, identity_id),
            )
            call_log.record(
                "CREATE",
                "",
                verifier.api_names["verify_exists"],
                v_elapsed,
                verified,
                is_verify=True,
            )

            section_manual_verify()
            url = console_url(
                provider,
                "iam_identity",
                identity_name=identity_name,
                tenant_id=tenant_id,
            )
            step_link(url)
            pause(interactive)

            if verified:
                results["passed"] += 1

        except Exception as exc:
            step_fail(f"CREATE failed: {exc}")
            call_log.record("CREATE", "IamClient.create_identity()", "", 0, False)
            return

        # -- STEP 2: VERIFY TRUST BINDING (conditional) --
        if trusted_principal:
            step_start(2, "VERIFY TRUST BINDING", f"Principal: {trusted_principal}")
            results["total"] += 1

            try:
                section_provider_verify(provider_label)
                provider_call_annotation(
                    provider_label, "verify_trust_config()", "verifying trust binding"
                )
                verified, v_elapsed = step_verify_timed(
                    f"trust binding contains {trusted_principal}",
                    f"{provider_label} SDK: get policy",
                    lambda: verifier.verify_trust_config(
                        identity_name, tenant_id, trusted_principal
                    ),
                )
                call_log.record(
                    "TRUST",
                    "",
                    "verify_trust_config()",
                    v_elapsed,
                    verified,
                    is_verify=True,
                )

                if verified:
                    results["passed"] += 1

            except Exception as exc:
                step_fail(f"TRUST VERIFY failed: {exc}")
                call_log.record("TRUST", "", "verify_trust_config()", 0, False)

        # -- STEP 3: IDEMPOTENT RE-CREATE --
        step_start(
            3 if trusted_principal else 2,
            "IDEMPOTENT RE-CREATE",
            "Same name, expect same ID",
        )
        results["total"] += 1

        try:
            section_sdk_call()
            sdk_call_annotation(
                f"IamClient.create_identity({identity_name}, ...) [second call]"
            )
            with timed_call("IamClient.create_identity() [re-create]") as t:
                t.value = client.create_identity(
                    identity_name,
                    description=f"E2E test identity ({provider}) - re-create",
                    tenant_id=tenant_id,
                    region=region,
                    trust_config=trust_config,
                    options=CreateOptions(),
                )

            second_id = t.value
            if second_id == identity_id:
                step_success(
                    f"Idempotent: same ID returned ({second_id})", elapsed=t.elapsed
                )
                results["passed"] += 1
            else:
                step_fail(f"ID mismatch! First: {identity_id}, Second: {second_id}")

            call_log.record(
                "IDEMPOTENT",
                "IamClient.create_identity() [re-create]",
                verifier.api_names["create"],
                t.elapsed,
                second_id == identity_id,
            )

        except Exception as exc:
            step_fail(f"IDEMPOTENT RE-CREATE failed: {exc}")
            call_log.record("IDEMPOTENT", "IamClient.create_identity()", "", 0, False)

        # -- STEP 4: GET IDENTITY --
        next_step = 4 if trusted_principal else 3
        step_start(next_step, "GET IDENTITY")
        results["total"] += 1

        try:
            section_sdk_call()
            sdk_call_annotation(
                f"IamClient.get_identity({identity_name}, tenant_id={tenant_id})"
            )
            with timed_call("IamClient.get_identity()") as t:
                t.value = client.get_identity(
                    identity_name, tenant_id=tenant_id, region=region
                )

            retrieved_id = t.value
            step_success(f"SDK returned: {retrieved_id}", elapsed=t.elapsed)
            call_log.record(
                "GET",
                "IamClient.get_identity()",
                verifier.api_names["get"],
                t.elapsed,
                True,
            )

            section_provider_verify(provider_label)
            provider_call_annotation(
                provider_label, verifier.api_names["verify_exists"], "verifying"
            )
            verified, v_elapsed = step_verify_timed(
                "ID matches independent lookup",
                f"{provider_label} SDK: get identity",
                lambda: verifier.verify_exists(identity_name, tenant_id, retrieved_id),
            )
            call_log.record(
                "GET",
                "",
                verifier.api_names["verify_exists"],
                v_elapsed,
                verified,
                is_verify=True,
            )

            if verified:
                results["passed"] += 1

        except Exception as exc:
            step_fail(f"GET failed: {exc}")
            call_log.record("GET", "IamClient.get_identity()", "", 0, False)

        # -- STEP: ATTACH INLINE POLICY --
        policy_step = next_step + 1
        step_start(policy_step, "ATTACH INLINE POLICY")
        results["total"] += 1

        test_policy = PolicyDocument(
            name="ReadBucketPolicy",
            statements=(
                Statement(
                    sid="AllowStorageRead",
                    effect=Effect.ALLOW,
                    actions=(StorageActions.GET_OBJECT, StorageActions.LIST_BUCKET),
                    resources=("storage://mc-e2e-test-bucket/*",),
                ),
            ),
        )

        attached_policy_name = test_policy.name or "ReadBucketPolicy"

        try:
            section_sdk_call()
            sdk_call_annotation(
                "IamClient.attach_inline_policy()",
                identity_name=identity_name,
                policy_name=test_policy.name,
                statements=[s.actions_as_strings for s in test_policy.statements],
                tenant_id=tenant_id,
            )
            with timed_call("IamClient.attach_inline_policy()") as t:
                client.attach_inline_policy(
                    identity_name,
                    policy_document=test_policy,
                    tenant_id=tenant_id,
                    region=region,
                )

            step_success("Policy attached", elapsed=t.elapsed)
            call_log.record(
                "ATTACH POLICY",
                "IamClient.attach_inline_policy()",
                "putRolePolicy / setIamPolicy",
                t.elapsed,
                True,
            )

            section_manual_verify()
            url = console_url(
                provider,
                "iam_policy",
                identity_name=identity_name,
                tenant_id=tenant_id,
            )
            step_link(url)
            pause(interactive)

            results["passed"] += 1

        except Exception as exc:
            step_fail(f"ATTACH POLICY failed: {exc}")
            call_log.record(
                "ATTACH POLICY", "IamClient.attach_inline_policy()", "", 0, False
            )

        # -- STEP: GET ATTACHED POLICIES --
        list_step = policy_step + 1
        step_start(list_step, "GET ATTACHED POLICIES")
        results["total"] += 1

        policies: list = []
        try:
            section_sdk_call()
            sdk_call_annotation(
                "IamClient.get_attached_policies()",
                identity_name=identity_name,
                tenant_id=tenant_id,
            )
            with timed_call("IamClient.get_attached_policies()") as t:
                t.value = client.get_attached_policies(
                    identity_name, tenant_id=tenant_id, region=region
                )

            policies = t.value
            step_success(
                f"Found {len(policies)} policies: {policies}", elapsed=t.elapsed
            )
            call_log.record(
                "LIST POLICIES",
                "IamClient.get_attached_policies()",
                "listRolePolicies / getIamPolicy",
                t.elapsed,
                len(policies) > 0,
            )

            if policies:
                attached_policy_name = policies[0]
                results["passed"] += 1

        except Exception as exc:
            step_fail(f"LIST POLICIES failed: {exc}")
            call_log.record(
                "LIST POLICIES", "IamClient.get_attached_policies()", "", 0, False
            )

        # -- STEP: VERIFY POLICY ATTACHED --
        verify_step = list_step + 1
        step_start(verify_step, "VERIFY POLICY ATTACHED")
        results["total"] += 1

        try:
            section_provider_verify(provider_label)
            provider_call_annotation(
                provider_label, "verify_policy_attached()", "verifying"
            )
            verified, v_elapsed = step_verify_timed(
                "policy is attached to identity",
                f"{provider_label} SDK: get policy",
                lambda: verifier.verify_policy_attached(
                    identity_name, attached_policy_name, tenant_id
                ),
            )
            call_log.record(
                "VERIFY ATTACH",
                "",
                "verify_policy_attached()",
                v_elapsed,
                verified,
                is_verify=True,
            )

            if verified:
                results["passed"] += 1

        except Exception as exc:
            step_fail(f"VERIFY ATTACH failed: {exc}")
            call_log.record("VERIFY ATTACH", "verify_policy_attached()", "", 0, False)

        # -- STEP: GET INLINE POLICY DETAILS --
        details_step = verify_step + 1
        step_start(details_step, "GET INLINE POLICY DETAILS")
        results["total"] += 1

        try:
            section_sdk_call()
            sdk_call_annotation(
                "IamClient.get_inline_policy_details()",
                identity_name=identity_name,
                policy_name=attached_policy_name,
                tenant_id=tenant_id,
            )
            with timed_call("IamClient.get_inline_policy_details()") as t:
                t.value = client.get_inline_policy_details(
                    identity_name,
                    policy_name=attached_policy_name,
                    tenant_id=tenant_id,
                    region=region,
                )

            details = t.value
            has_details = details is not None and len(details) > 0
            step_success(
                (
                    f"Policy details: {details[:100]}..."
                    if details and len(details) > 100
                    else f"Policy details: {details}"
                ),
                elapsed=t.elapsed,
            )
            call_log.record(
                "GET DETAILS",
                "IamClient.get_inline_policy_details()",
                "getRolePolicy / getIamPolicy",
                t.elapsed,
                has_details,
            )

            if has_details:
                results["passed"] += 1

        except Exception as exc:
            step_fail(f"GET DETAILS failed: {exc}")
            call_log.record(
                "GET DETAILS", "IamClient.get_inline_policy_details()", "", 0, False
            )

        # -- STEP: REMOVE POLICY --
        remove_step = details_step + 1
        step_start(remove_step, "REMOVE POLICY")
        results["total"] += 1

        try:
            section_sdk_call()
            sdk_call_annotation(
                "IamClient.remove_policy()",
                identity_name=identity_name,
                policy_name=attached_policy_name,
                tenant_id=tenant_id,
            )
            with timed_call("IamClient.remove_policy()") as t:
                client.remove_policy(
                    identity_name,
                    policy_name=attached_policy_name,
                    tenant_id=tenant_id,
                    region=region,
                )

            step_success("Policy removed", elapsed=t.elapsed)
            call_log.record(
                "REMOVE POLICY",
                "IamClient.remove_policy()",
                "deleteRolePolicy / setIamPolicy",
                t.elapsed,
                True,
            )

            section_provider_verify(provider_label)
            provider_call_annotation(
                provider_label, "verify_policy_removed()", "verifying"
            )
            verified, v_elapsed = step_verify_timed(
                "policy is no longer attached",
                f"{provider_label} SDK: get policy [expect not found]",
                lambda: verifier.verify_policy_removed(
                    identity_name, attached_policy_name, tenant_id
                ),
            )
            call_log.record(
                "REMOVE POLICY",
                "",
                "verify_policy_removed()",
                v_elapsed,
                verified,
                is_verify=True,
            )

            section_manual_verify()
            url = console_url(
                provider,
                "iam_policy",
                identity_name=identity_name,
                tenant_id=tenant_id,
            )
            step_link(url)
            pause(interactive)

            if verified:
                results["passed"] += 1

        except Exception as exc:
            step_fail(f"REMOVE POLICY failed: {exc}")
            call_log.record("REMOVE POLICY", "IamClient.remove_policy()", "", 0, False)

        # -- STEP: DELETE IDENTITY --
        delete_step = remove_step + 1
        step_start(delete_step, "DELETE IDENTITY")
        results["total"] += 1

        try:
            section_sdk_call()
            sdk_call_annotation(
                f"IamClient.delete_identity({identity_name}, tenant_id={tenant_id})"
            )
            with timed_call("IamClient.delete_identity()") as t:
                client.delete_identity(
                    identity_name, tenant_id=tenant_id, region=region
                )

            step_success("SDK completed without error", elapsed=t.elapsed)
            call_log.record(
                "DELETE",
                "IamClient.delete_identity()",
                verifier.api_names["delete"],
                t.elapsed,
                True,
            )

            section_provider_verify(provider_label)
            provider_call_annotation(
                provider_label, verifier.api_names["verify_deleted"], "verifying"
            )
            verified, v_elapsed = step_verify_timed(
                "identity no longer exists",
                f"{provider_label} SDK: get identity [expect not found]",
                lambda: verifier.verify_deleted(identity_name, tenant_id),
            )
            call_log.record(
                "DELETE",
                "",
                verifier.api_names["verify_deleted"],
                v_elapsed,
                verified,
                is_verify=True,
            )

            section_manual_verify()
            url = console_url(
                provider,
                "iam_identity_list",
                identity_name=identity_name,
                tenant_id=tenant_id,
            )
            step_link(url)
            pause(interactive)

            if verified:
                results["passed"] += 1
                cleanup._actions.clear()

        except Exception as exc:
            step_fail(f"DELETE failed: {exc}")
            call_log.record("DELETE", "IamClient.delete_identity()", "", 0, False)

        client.close()
