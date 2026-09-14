"""Shared VCR.py configuration for pubsub conformance tests.

Each provider's cassettes live in a sibling subdirectory (`cassettes/aws/`,
`cassettes/gcp/`, `cassettes/ali/`). The provider is detected from the
running test's class name so a single `vcr_config` fixture serves all
provider tests.
"""

import re
from pathlib import Path
from typing import Any, Dict

import pytest

from tests.pubsub.conformance.vcr_filters import (
    get_default_vcr_config,
    make_before_record_request,
)

CASSETTES_ROOT = Path(__file__).parent / "cassettes"

# Provider-specific extra headers — request IDs and other noise that's not a
# security risk but creates cassette diff churn if left in.
AWS_EXTRA_HEADERS = [
    "X-Amz-Request-Id",
    "x-amz-id-2",
    "x-amz-apigw-id",
]

GCP_EXTRA_HEADERS = [
    "x-goog-request-time",
    "x-goog-api-client",
]


def _scrub_aws_response_body(response):
    """Scrub OAuth tokens from AWS response bodies.

    AWS SQS may receive messages containing OAuth tokens from other producers.
    This scrubber provides defense-in-depth by removing common OAuth patterns.
    Handles both string and bytes bodies.
    """
    if "body" in response and "string" in response["body"]:
        body = response["body"]["string"]
        # Convert bytes to string if needed for regex
        if isinstance(body, bytes):
            body_str = body.decode("utf-8")
            is_bytes = True
        else:
            body_str = body
            is_bytes = False

        # Scrub common OAuth patterns
        body_str = re.sub(
            r'"access_token"\s*:\s*"[^"]+"', '"access_token":"REDACTED"', body_str
        )
        body_str = re.sub(
            r'"refresh_token"\s*:\s*"[^"]+"', '"refresh_token":"REDACTED"', body_str
        )
        body_str = re.sub(
            r'"client_secret"\s*:\s*"[^"]+"', '"client_secret":"REDACTED"', body_str
        )
        body_str = re.sub(
            r'"id_token"\s*:\s*"[^"]+"', '"id_token":"REDACTED"', body_str
        )

        # Convert back to bytes if original was bytes
        response["body"]["string"] = body_str.encode("utf-8") if is_bytes else body_str
    return response


def _provider_for(class_name: str) -> str:
    """Map a test class name to its provider id.

    ``TestAwsTopicConformance`` → ``"aws"``,
    ``TestGcpTopicConformance`` → ``"gcp"``,
    ``TestAliTopicConformance`` → ``"ali"``.
    """
    name = class_name.lower()
    if "aws" in name:
        return "aws"
    if "gcp" in name:
        return "gcp"
    if "ali" in name:
        return "ali"
    raise RuntimeError(
        f"Cannot determine provider for test class '{class_name}'. "
        "Class name must contain 'aws', 'gcp', or 'ali'."
    )


@pytest.fixture(scope="function")
def aws_sns_topic_name(request):
    """AWS SNS topic name for conformance tests.

    Creates a unique topic per test for isolation.
    Format: test-sns-{testMethodName}

    Example: test_send_batch_messages → test-sns-testSendBatchMessages
    """
    import os

    # Convert snake_case test name to camelCase suffix for AWS naming
    test_name = request.node.name
    # Remove 'test_' prefix if present
    name_without_prefix = test_name[5:] if test_name.startswith("test_") else test_name
    # Convert to camelCase: test_send_batch_messages → SendBatchMessages → testSendBatchMessages
    camel_suffix = "".join(word.capitalize() for word in name_without_prefix.split("_"))

    # Generate topic name: test-sns-{testMethodName}
    topic_name = f"test-sns-test{camel_suffix}"

    # Allow override via env var
    return os.environ.get("CONFORMANCE_AWS_SNS_TOPIC_NAME", topic_name)


@pytest.fixture(scope="function")
def aws_sqs_queue_name(request):
    """AWS SQS queue name for conformance tests.

    Creates a unique queue per test for isolation.
    Format: test-sqs-{testMethodName}

    Example: test_send_batch_messages → test-sqs-testSendBatchMessages
    """
    import os

    # Convert snake_case test name to camelCase suffix for AWS naming
    test_name = request.node.name
    # Remove 'test_' prefix if present
    name_without_prefix = test_name[5:] if test_name.startswith("test_") else test_name
    # Convert to camelCase: test_send_batch_messages → SendBatchMessages → testSendBatchMessages
    camel_suffix = "".join(word.capitalize() for word in name_without_prefix.split("_"))

    # Generate queue name: test-sqs-{testMethodName}
    queue_name = f"test-sqs-test{camel_suffix}"

    # Allow override via env var
    return os.environ.get("CONFORMANCE_AWS_SQS_QUEUE_NAME", queue_name)


@pytest.fixture(scope="module")
def aws_region():
    """AWS region for conformance tests."""
    import os

    return os.environ.get("CONFORMANCE_AWS_REGION", "us-west-2")


@pytest.fixture(scope="function", autouse=True)
def aws_credentials_for_replay(request, monkeypatch):
    """Provide dummy AWS credentials in replay mode.

    In CI replay mode, botocore attempts to discover credentials from
    environment, config files, and EC2 metadata. If none exist, the IMDS
    check causes VCR mismatches. Setting dummy credentials satisfies boto3's
    credential chain without triggering IMDS.

    Function-scoped with monkeypatch teardown so values do not leak into
    unrelated tests in the same pytest session.
    """
    import os

    record_mode = request.config.getoption("--record-mode") or "none"
    if record_mode == "none":
        # Only set if not already present, to avoid overriding real credentials.
        # monkeypatch restores env after each test function.
        if "AWS_ACCESS_KEY_ID" not in os.environ:
            monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        if "AWS_SECRET_ACCESS_KEY" not in os.environ:
            monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        if "AWS_SECURITY_TOKEN" not in os.environ:
            monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
        if "AWS_SESSION_TOKEN" not in os.environ:
            monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture(scope="function")
def gcp_topic_name(request):
    """GCP Pub/Sub topic name for conformance tests.

    Generates per-test topic names using camelCase naming convention:
    test_send_batch_messages → topic-testSendBatchMessages
    """
    import os

    # Convert snake_case test name to camelCase suffix
    # test_send_batch_messages → SendBatchMessages
    test_name = request.node.name
    # Remove 'test_' prefix if present
    name_without_prefix = test_name[5:] if test_name.startswith("test_") else test_name
    suffix = "".join(word.capitalize() for word in name_without_prefix.split("_"))

    project_id = os.environ.get("CONFORMANCE_GCP_PROJECT_ID", "example-project")
    return os.environ.get(
        "CONFORMANCE_GCP_TOPIC_NAME", f"projects/{project_id}/topics/topic-test{suffix}"
    )


@pytest.fixture(scope="function")
def gcp_subscription_name(request):
    """GCP Pub/Sub subscription name for conformance tests.

    Generates per-test subscription names using camelCase naming convention:
    test_send_batch_messages → sub-testSendBatchMessages
    """
    import os

    # Convert snake_case test name to camelCase suffix
    # test_send_batch_messages → SendBatchMessages
    test_name = request.node.name
    # Remove 'test_' prefix if present
    name_without_prefix = test_name[5:] if test_name.startswith("test_") else test_name
    suffix = "".join(word.capitalize() for word in name_without_prefix.split("_"))

    project_id = os.environ.get("CONFORMANCE_GCP_PROJECT_ID", "example-project")
    return os.environ.get(
        "CONFORMANCE_GCP_SUBSCRIPTION_NAME",
        f"projects/{project_id}/subscriptions/sub-test{suffix}",
    )


@pytest.fixture(scope="module")
def vcr_cassette_dir(request) -> str:
    """Override pytest-recording's default cassette directory.

    pytest-recording's built-in ``vcr_cassette_dir`` fixture resolves to
    ``<test_dir>/cassettes/<module_name>/`` (e.g.
    ``cassettes/test_aws_topic_conformance/``). Our layout organizes cassettes
    by *provider* instead (``cassettes/aws/``, ``cassettes/gcp/``).

    This override is **required** for ``--record-mode=rewrite`` to work.
    The rewrite logic in pytest-recording deletes the old cassette using the
    path from this fixture before re-recording. Without this override, the
    delete targets the wrong (non-existent) path, silently fails, and VCR
    replays the stale cassette instead of making a real HTTP call.

    The ``vcr_config`` fixture below also references ``cassette_library_dir``
    which must agree with this value — both are derived from the same
    ``CASSETTES_ROOT / provider`` computation to stay in sync.
    """
    test_class = getattr(request, "cls", None)
    if test_class is not None:
        class_name = test_class.__name__
    else:
        class_name = request.module.__name__

    provider = _provider_for(class_name)
    return str(CASSETTES_ROOT / provider)


@pytest.fixture
def vcr_config(request, vcr_cassette_dir) -> Dict[str, Any]:
    """Per-provider VCR.py configuration.

    Applies provider-specific header filters and body scrubbing rules.
    The cassette directory is sourced from the ``vcr_cassette_dir`` fixture
    (single source of truth) to guarantee that ``--record-mode=rewrite``
    deletes from the same path VCR reads/writes.
    """
    test_class = getattr(request, "cls", None)
    if test_class is not None:
        class_name = test_class.__name__
    else:
        class_name = request.module.__name__

    provider = _provider_for(class_name)

    # Get record mode from pytest config (--record-mode flag)
    record_mode = request.config.getoption("--record-mode") or "none"

    config = get_default_vcr_config(
        cassette_dir=vcr_cassette_dir, record_mode=record_mode
    )

    if provider == "aws":
        config["filter_headers"].extend(AWS_EXTRA_HEADERS)
        # boto3 stores BytesIO bodies that VCR's request-body scrubber can't
        # handle; AWS auth lives entirely in headers (already filtered).
        config["before_record_request"] = make_before_record_request(scrub_bodies=False)
        # Still scrub OAuth tokens from response bodies in case SQS receives
        # messages containing tokens from other producers.
        config["before_record_response"] = _scrub_aws_response_body
    elif provider == "gcp":
        config["filter_headers"].extend(GCP_EXTRA_HEADERS)
    # ali: no extra headers yet; impl is a placeholder.

    return config
