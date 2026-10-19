"""VCR.py filter module for STS conformance tests.

Unlike the blob conformance filters which IGNORE STS endpoints
(since blob tests don't need STS traffic), STS conformance tests
must RECORD calls to STS endpoints. This module provides
STS-specific filtering that:

  - Does NOT ignore STS hosts (sts.amazonaws.com, iamcredentials.googleapis.com)
  - Scrubs security headers (Authorization, tokens)
  - Scrubs sensitive response body fields (access tokens, session tokens)
  - Filters stability headers (timestamps, nonces)

Filters are organized in three tiers:
- SECURITY: Must be filtered to prevent credential leakage
- STABILITY: Should be filtered to prevent replay failures
- NOISE: May be filtered to reduce cassette diff noise
"""

import json
import re
from typing import Any, Callable, Dict, Optional, Set
from urllib.parse import parse_qs, urlencode

SECURITY_HEADERS = [
    # All providers
    "Authorization",
    "Cookie",
    "Set-Cookie",
    # AWS STS / signing
    "X-Amz-Security-Token",
    "x-amz-security-token",
    "X-Amz-Credential",
    # GCP
    "x-goog-api-key",
    # Ali
    "x-acs-security-token",
    "x-oss-security-token",
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
    # Ali
    "x-acs-date",
    "x-acs-signature-nonce",
    "x-acs-content-sha256",
    # All providers (client version strings)
    "User-Agent",
    "Accept-Encoding",
]

STS_IGNORE_HOSTS: Set[str] = {
    # GCP OAuth token endpoint (pre-auth refresh, not the STS call itself)
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
    # AWS AssumeRoleWithWebIdentity — the OIDC JWT in the form body
    "WebIdentityToken",
    # GCP Workload Identity Federation — the subject token in the JSON body
    "subjectToken",
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
    # NOTE: "token" and "id_token" are intentionally NOT scrubbed here.
    # GCP's generateIdToken returns {"token": "<JWT>"} which google-auth
    # must decode client-side during VCR replay. Scrubbing it breaks replay.
    # The JWT is an identity proof (not a reusable credential) and expires
    # within 1 hour, so the risk is minimal.
}

# Pattern for AWS account IDs in ARNs (12-digit number)
_AWS_ACCOUNT_PATTERN = re.compile(r"(\d{12})")


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
    return changed


def _scrub_form_body(body_str: str) -> str:
    """Scrub sensitive fields from form-encoded body (AWS STS uses form-encoded)."""
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


def _scrub_xml_tokens(body_str: str) -> str:
    """Scrub token values from AWS STS XML responses.

    AWS STS returns XML responses with credential data like:
    <AccessKeyId>ASIA...</AccessKeyId>
    <SecretAccessKey>...</SecretAccessKey>
    <SessionToken>...</SessionToken>
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
    recorded. Service account emails in GCP URIs are intentionally NOT
    scrubbed here — they are test-environment identifiers (not secrets)
    and are needed for path-based cassette matching during replay.

    Args:
        ignore_hosts: Set of hostnames to skip recording entirely.
            Defaults to STS_IGNORE_HOSTS (minimal set for STS tests).

    Returns:
        A callback function suitable for VCR.py's before_record_request.

    .. note::
        TODO: Consider extracting shared VCR filter logic into a common
        module (e.g. ``tests/conftest_vcr.py``) as more services adopt
        conformance tests. Tracked for follow-up.
    """
    if ignore_hosts is None:
        ignore_hosts = STS_IGNORE_HOSTS

    def _callback(request):
        host = _host_from_uri(request.uri or "")
        if host in ignore_hosts:
            return None
        return request

    return _callback


def make_before_record_response() -> Callable:
    """Create a before_record_response callback for VCR.py.

    Scrubs sensitive credential data from response bodies.
    Handles both JSON (GCP) and XML (AWS) response formats.
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

        # Try XML scrubbing (AWS STS responses)
        body_str = _scrub_xml_tokens(body_str)

        # Try JSON scrubbing (GCP responses)
        body_str = _scrub_json_fields(body_str, _SENSITIVE_RESPONSE_FIELDS)

        if body_str != original:
            body["string"] = body_str.encode("utf-8") if was_bytes else body_str

        return response

    return _callback


def get_default_vcr_config(
    cassette_dir: str,
    **overrides: Any,
) -> Dict[str, Any]:
    """Build a complete VCR.py configuration dict for STS tests.

    Key difference from blob config: does NOT ignore STS endpoints.
    STS tests need to record the actual STS API calls.

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
        # "query" is intentionally omitted: STS operations are POST-based
        # with form-encoded or JSON bodies, not query-string parameters.
        # Including "query" would cause spurious mismatches from AWS signing
        # params or GCP client metadata that vary across runs.
        "match_on": ["method", "scheme", "host", "port", "path"],
        "decode_compressed_response": True,
    }
    config.update(overrides)
    return config
