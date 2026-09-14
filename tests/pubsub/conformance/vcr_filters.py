"""VCR.py scrubbers and filters for pubsub conformance tests.

Strips secrets (Authorization headers, credentials, API keys, OAuth tokens) and
provider-specific noise (request IDs, timestamps, generations) from recorded
HTTP cassettes.

Usage in conftest.py::

    from tests.pubsub.conformance.vcr_filters import get_default_vcr_config

    @pytest.fixture
    def vcr_config():
        return get_default_vcr_config(cassette_dir="cassettes/aws")
"""

import re
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

DEFAULT_FILTER_HEADERS = [
    "authorization",
    "x-amz-security-token",
    "x-amz-credential",
    "x-goog-iam-authority-selector",
    "x-goog-iam-authorization-token",
    "x-goog-api-key",
    "cookie",
    "set-cookie",
]

# OAuth endpoints to ignore entirely (don't record token exchanges)
OAUTH_HOSTS = {"oauth2.googleapis.com", "accounts.google.com", "www.googleapis.com"}


def _scrub_form_body(body: bytes) -> bytes:
    """Scrub OAuth credentials from form-encoded request bodies.

    GCP OAuth token refresh requests contain:
    - client_secret (long-lived app credential)
    - refresh_token (long-lived user credential)
    - access_token (short-lived but still sensitive)
    """
    try:
        body_str = body.decode("utf-8")
        params = parse_qs(body_str, keep_blank_values=True)

        # Scrub OAuth fields
        if "client_secret" in params:
            params["client_secret"] = ["REDACTED"]
        if "refresh_token" in params:
            params["refresh_token"] = ["REDACTED"]
        if "access_token" in params:
            params["access_token"] = ["REDACTED"]
        if "id_token" in params:
            params["id_token"] = ["REDACTED"]

        # Reconstruct body
        return urlencode(params, doseq=True).encode("utf-8")
    except Exception:
        # If parsing fails, return original (better than crashing)
        return body


def _scrub_json_body(body: bytes) -> bytes:
    """Scrub secrets from JSON request/response bodies."""
    try:
        import json

        data = json.loads(body)

        # Scrub OAuth/auth fields
        if isinstance(data, dict):
            for key in ["access_token", "refresh_token", "id_token", "client_secret"]:
                if key in data:
                    data[key] = "REDACTED"

        return json.dumps(data).encode("utf-8")
    except Exception:
        return body


def _scrub_request_body(body: Optional[bytes]) -> Optional[bytes]:
    """Scrub sensitive data from request bodies."""
    if not body:
        return body

    # Detect content type and scrub accordingly
    if b"client_secret=" in body or b"refresh_token=" in body:
        return _scrub_form_body(body)
    elif body.startswith(b"{"):
        return _scrub_json_body(body)

    return body


def _scrub_response_body(body: Optional[bytes]) -> Optional[bytes]:
    """Scrub sensitive data from response bodies."""
    if not body:
        return body

    # Scrub JSON responses (OAuth token responses)
    if body.startswith(b"{"):
        return _scrub_json_body(body)

    return body


def _should_ignore_request(request) -> bool:
    """Return True if this request should be ignored entirely (not recorded)."""
    try:
        parsed = urlparse(request.uri)
        return parsed.hostname in OAUTH_HOSTS
    except Exception:
        return False


def make_before_record_request(scrub_bodies: bool = True):
    """Factory for a VCR before_record_request hook.

    Args:
        scrub_bodies: If True, scrub request bodies. Set to False for
            providers like AWS where body scrubbing breaks BytesIO bodies.

    Returns:
        A before_record_request callback compatible with VCR.py.
    """

    def before_record_request(request):
        """Scrub sensitive data from the request before recording."""
        # Ignore OAuth token exchanges entirely
        if _should_ignore_request(request):
            return None

        if scrub_bodies and request.body:
            request.body = _scrub_request_body(request.body)
        return request

    return before_record_request


def before_record_response(response):
    """Scrub sensitive data from the response before recording."""
    if response.get("body"):
        if isinstance(response["body"], dict):
            body_str = response["body"].get("string")
            if body_str:
                response["body"]["string"] = _scrub_response_body(body_str)
    return response


def get_default_vcr_config(
    cassette_dir: str, record_mode: str = "none"
) -> Dict[str, Any]:
    """Return the default VCR.py config for pubsub conformance tests.

    Args:
        cassette_dir: Path to the cassette directory.
        record_mode: VCR record mode (none/once/new_episodes/all/rewrite).

    Returns:
        A dict of VCR config options.
    """
    return {
        "cassette_library_dir": cassette_dir,
        "record_mode": record_mode,
        "match_on": ["method", "scheme", "host", "port", "path", "query"],
        "filter_headers": list(DEFAULT_FILTER_HEADERS),
        "before_record_request": make_before_record_request(scrub_bodies=True),
        "before_record_response": before_record_response,
        "decode_compressed_response": True,
    }
