"""VCR.py filter module for IAM conformance tests.

IAM conformance tests must RECORD calls to the identity-management
endpoints (``iam.amazonaws.com`` for AWS; ``iam.googleapis.com`` and
``cloudresourcemanager.googleapis.com`` for GCP). Only the OAuth
token-refresh hosts are ignored, since they are pre-auth steps and not
the IAM calls under test.

This module:

  - Does NOT ignore IAM hosts
  - Scrubs security headers (Authorization, tokens)
  - Scrubs sensitive response body fields (access/session tokens, secrets)
  - Filters stability headers (timestamps, nonces)

Filters are organized in three tiers:
- SECURITY: Must be filtered to prevent credential leakage
- STABILITY: Should be filtered to prevent replay failures
- NOISE: May be filtered to reduce cassette diff noise

Identity ARNs, service-account emails, and 12-digit account ids are
test-environment identifiers needed for path-based cassette matching —
they are intentionally NOT scrubbed.
"""

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import parse_qs, urlencode

SECURITY_HEADERS = [
    # All providers
    "Authorization",
    "Cookie",
    "Set-Cookie",
    # AWS signing / session
    "X-Amz-Security-Token",
    "x-amz-security-token",
    "X-Amz-Credential",
    # GCP
    "x-goog-api-key",
    # Alibaba Cloud
    "x-acs-security-token",
]

STABILITY_HEADERS = [
    # AWS signing (timestamps and content hashes change per request)
    "X-Amz-Date",
    "x-amz-date",
    "X-Amz-Content-Sha256",
    "x-amz-content-sha256",
    # GCP (client metadata and routing params)
    "x-goog-api-client",
    "x-goog-request-params",
    # Alibaba Cloud
    "x-acs-date",
    "x-acs-signature-nonce",
    "x-acs-content-sha256",
    # All providers (client version strings)
    "User-Agent",
    "Accept-Encoding",
]

IAM_IGNORE_HOSTS: Set[str] = {
    # GCP OAuth token endpoint (pre-auth refresh, not the IAM call itself)
    "oauth2.googleapis.com",
    "accounts.google.com",
}

_SENSITIVE_REQUEST_FIELDS: Set[str] = {
    "client_secret",
    "refresh_token",
    "access_token",
    "assertion",
    "password",
    "client_assertion",
}

_SENSITIVE_RESPONSE_FIELDS: Set[str] = {
    "AccessKeyId",
    "SecretAccessKey",
    "SessionToken",
    "access_token",
    "refresh_token",
    "secret",
    "accessToken",
    "refreshToken",
    "privateKeyData",
    "privateKey",
}

# A project getIamPolicy response returns the project's *entire* IAM binding
# set — hundreds of unrelated teammate members. Those members are not secrets
# (so the credential scanner stays quiet) but they are teammate PII that has no
# business living permanently in recorded cassettes. The conformance policy
# tests only ever look for the test identity's own member in a binding
# (the provider matches ``member in binding.members``), so every other member
# is non-asserted noise. We keep only members tied to the conformance test
# identity and drop the rest; bindings left with no members are dropped too.
#
# The base name is kept in sync with the GCP harness via the same env var
# (``CONFORMANCE_GCP_TEST_IDENTITY_NAME``); the default matches the harness
# default so the scrub works without any env configuration in replay/CI.
#
# A project getIamPolicy response is the only VCR-recorded response that
# carries an IAM ``bindings`` array, so the scrub is gated on that body shape
# (the response callback does not see the request URI) rather than on host.
_GCP_TEST_IDENTITY_BASE_NAME = os.environ.get(
    "CONFORMANCE_GCP_TEST_IDENTITY_NAME", "mcp-conf-sa"
)


def _host_from_uri(uri: str) -> str:
    """Extract hostname from a URI string."""
    try:
        after_scheme = uri.split("://", 1)[1] if "://" in uri else uri
        host_port = after_scheme.split("/", 1)[0]
        host = host_port.split(":")[0]
        return host
    except (IndexError, AttributeError):
        return ""


def _scrub_json_fields(body_str: str, fields: Set[str]) -> str:
    """Scrub sensitive fields from a JSON body string."""
    try:
        data = json.loads(body_str)
        if isinstance(data, dict):
            changed = _scrub_dict_recursive(data, fields)
            if changed:
                return json.dumps(data, separators=(",", ":"))
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return body_str


def _scrub_dict_recursive(d: dict, fields: Set[str]) -> bool:
    """Recursively scrub sensitive fields in a dict. Returns True if modified."""
    changed = False
    for key in list(d.keys()):
        if key in fields and isinstance(d[key], str):
            d[key] = f"FILTERED_{key}"
            changed = True
        elif isinstance(d[key], dict):
            if _scrub_dict_recursive(d[key], fields):
                changed = True
        elif isinstance(d[key], list):
            for item in d[key]:
                if isinstance(item, dict) and _scrub_dict_recursive(item, fields):
                    changed = True
    return changed


def _scrub_form_body(body_str: str) -> str:
    """Scrub sensitive fields from a form-encoded body (AWS uses form-encoded)."""
    try:
        params = parse_qs(body_str, keep_blank_values=True)
        changed = False
        for key in _SENSITIVE_REQUEST_FIELDS:
            if key in params:
                params[key] = [f"FILTERED_{key}"]
                changed = True
        if changed:
            return urlencode(params, doseq=True)
    except Exception:
        pass
    return body_str


def _is_test_identity_member(member: str, base_name: str) -> bool:
    """Return True if ``member`` references the conformance test identity.

    Members look like ``serviceAccount:<base>-pol-attach@<project>...`` or
    ``deleted:serviceAccount:<base>-pol-attach@<project>...?uid=...``. The
    conformance tests provision identities named ``<base>`` plus a per-test
    suffix, so a substring match on ``<base>@`` / ``<base>-`` after the
    ``serviceAccount:`` prefix uniquely identifies them.
    """
    return f"{base_name}@" in member or f"{base_name}-" in member


def _prune_policy_bindings(policy: dict, base_name: str) -> None:
    """In-place: keep only test-identity members in each binding of ``policy``.

    Drops every non-test-identity member and any binding left empty.
    """
    bindings = policy.get("bindings")
    if not isinstance(bindings, list):
        return
    kept_bindings: List[Any] = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        members = binding.get("members")
        if not isinstance(members, list):
            kept_bindings.append(binding)
            continue
        retained = [
            m
            for m in members
            if isinstance(m, str) and _is_test_identity_member(m, base_name)
        ]
        if retained:
            binding["members"] = retained
            kept_bindings.append(binding)
    policy["bindings"] = kept_bindings


def _scrub_project_policy_members(body_str: str, base_name: str) -> str:
    """Strip non-asserted IAM members from a project IAM-policy body.

    Handles both shapes the project-policy operations carry:
      - the ``getIamPolicy`` response, whose top level is the policy
        (``{"bindings": [...]}``); and
      - the ``setIamPolicy`` request, which nests the policy under a
        ``"policy"`` key (``{"policy": {"bindings": [...]}}``).

    Keeps only members tied to the conformance test identity in each binding
    and drops bindings left with no members. Returns the body unchanged if it
    is not a JSON IAM policy (so non-policy bodies pass through untouched).
    """
    try:
        data = json.loads(body_str)
    except (json.JSONDecodeError, ValueError, TypeError):
        return body_str
    if not isinstance(data, dict):
        return body_str

    if isinstance(data.get("bindings"), list):
        _prune_policy_bindings(data, base_name)
    elif isinstance(data.get("policy"), dict) and isinstance(
        data["policy"].get("bindings"), list
    ):
        _prune_policy_bindings(data["policy"], base_name)
    else:
        return body_str
    return json.dumps(data, separators=(",", ":"))


def _scrub_xml_tokens(body_str: str) -> str:
    """Scrub credential values from AWS XML responses.

    AWS identity responses do not normally embed long-term credentials,
    but any session-token-like elements are scrubbed defensively.
    """
    patterns = [
        (r"(<AccessKeyId>)[^<]+(</AccessKeyId>)", r"\1FILTERED_AccessKeyId\2"),
        (
            r"(<SecretAccessKey>)[^<]+(</SecretAccessKey>)",
            r"\1FILTERED_SecretAccessKey\2",
        ),
        (r"(<SessionToken>)[^<]+(</SessionToken>)", r"\1FILTERED_SessionToken\2"),
    ]
    for pattern, replacement in patterns:
        body_str = re.sub(pattern, replacement, body_str)
    return body_str


def make_before_record_request(
    ignore_hosts: Optional[Set[str]] = None,
) -> Callable:
    """Create a before_record_request callback for VCR.py.

    Filters out requests to OAuth/pre-auth hosts that should not be
    recorded, and scrubs sensitive fields from request bodies. Identity
    names / emails in URIs are intentionally NOT scrubbed — they are
    test-environment identifiers (not secrets) and are needed for
    path-based cassette matching during replay.

    Args:
        ignore_hosts: Set of hostnames to skip recording entirely.
            Defaults to IAM_IGNORE_HOSTS (minimal set for IAM tests).

    Returns:
        A callback function suitable for VCR.py's before_record_request.

    .. note::
        TODO: Consider extracting shared VCR filter logic into a common
        module as more services adopt conformance tests. Tracked for
        follow-up.
    """
    if ignore_hosts is None:
        ignore_hosts = IAM_IGNORE_HOSTS

    def _callback(request):
        host = _host_from_uri(request.uri or "")
        if host in ignore_hosts:
            return None

        body = getattr(request, "body", None)
        if body:
            if isinstance(body, bytes):
                body_str = body.decode("utf-8", errors="replace")
                scrubbed = _scrub_form_body(body_str)
                scrubbed = _scrub_json_fields(scrubbed, _SENSITIVE_REQUEST_FIELDS)
                # setIamPolicy echoes the full project policy in its request
                # body; strip non-asserted teammate members from it too.
                scrubbed = _scrub_project_policy_members(
                    scrubbed, _GCP_TEST_IDENTITY_BASE_NAME
                )
                if scrubbed != body_str:
                    request.body = scrubbed.encode("utf-8")
            elif isinstance(body, str):
                scrubbed = _scrub_form_body(body)
                scrubbed = _scrub_json_fields(scrubbed, _SENSITIVE_REQUEST_FIELDS)
                # setIamPolicy echoes the full project policy in its request
                # body; strip non-asserted teammate members from it too.
                scrubbed = _scrub_project_policy_members(
                    scrubbed, _GCP_TEST_IDENTITY_BASE_NAME
                )
                if scrubbed != body:
                    request.body = scrubbed

        return request

    return _callback


def make_before_record_response() -> Callable:
    """Create a before_record_response callback for VCR.py.

    Scrubs sensitive credential data from response bodies. Handles both
    JSON (GCP) and XML (AWS) response formats.
    """

    def _callback(response):
        body = response.get("body", {})
        body_content = body.get("string", "")
        if not body_content:
            return response

        if isinstance(body_content, bytes):
            body_str = body_content.decode("utf-8", errors="replace")
            was_bytes = True
        else:
            body_str = body_content
            was_bytes = False

        original = body_str

        # Try XML scrubbing (AWS responses)
        body_str = _scrub_xml_tokens(body_str)

        # Try JSON scrubbing (GCP responses)
        body_str = _scrub_json_fields(body_str, _SENSITIVE_RESPONSE_FIELDS)

        # Strip non-asserted project IAM members (teammate PII) from the
        # project getIamPolicy response, keeping only the test identity's
        # members — the only members the policy tests assert on.
        body_str = _scrub_project_policy_members(body_str, _GCP_TEST_IDENTITY_BASE_NAME)

        if body_str != original:
            body["string"] = body_str.encode("utf-8") if was_bytes else body_str

        return response

    return _callback


def get_default_vcr_config(
    cassette_dir: str,
    **overrides: Any,
) -> Dict[str, Any]:
    """Build a complete VCR.py configuration dict for IAM tests.

    Key difference from blob config: does NOT ignore IAM endpoints.
    IAM tests need to record the actual identity-management API calls.

    Args:
        cassette_dir: Path to the directory where cassettes will be stored.
        **overrides: Additional keys to merge into the config.

    Returns:
        A configuration dict suitable for returning from a vcr_config fixture.
    """
    config: Dict[str, Any] = {
        "cassette_library_dir": cassette_dir,
        "filter_headers": list(SECURITY_HEADERS + STABILITY_HEADERS),
        "before_record_request": make_before_record_request(),
        "before_record_response": make_before_record_response(),
        # "query" is intentionally omitted: IAM operations are POST-based
        # with form-encoded (AWS) or JSON (GCP) bodies, not query-string
        # parameters. Including "query" would cause spurious mismatches from
        # AWS signing params or GCP client metadata that vary across runs.
        "match_on": ["method", "scheme", "host", "port", "path"],
        "decode_compressed_response": True,
    }
    config.update(overrides)
    return config
