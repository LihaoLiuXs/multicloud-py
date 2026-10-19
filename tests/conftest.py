"""
Pytest fixtures for testing the Python Multi-Cloud SDK.

This module provides shared fixtures that can be used across all tests.
"""

import os
import uuid
from typing import Any, Callable, Dict

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-tag every test under a ``conformance/`` directory.

    Lets CI exclude conformance tests with ``-m "not conformance"``
    without each new conformance file having to remember the marker.
    Adding a new service (e.g. ``tests/docstore/conformance/``) just
    works — no CI change needed.
    """
    conformance = pytest.mark.conformance
    for item in items:
        if "/conformance/" in item.nodeid:
            item.add_marker(conformance)


# Skip cloud provider tests if credentials are not available
skip_aws_tests = pytest.mark.skipif(
    os.environ.get("AWS_ACCESS_KEY_ID") is None
    or os.environ.get("AWS_SECRET_ACCESS_KEY") is None,
    reason="AWS credentials not available",
)

skip_gcp_tests = pytest.mark.skipif(
    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") is None,
    reason="GCP credentials not available",
)

skip_ali_tests = pytest.mark.skipif(
    os.environ.get("ALI_ACCESS_KEY_ID") is None
    or os.environ.get("ALI_ACCESS_KEY_SECRET") is None,
    reason="Ali credentials not available",
)


def generate_resource_name(prefix: str) -> str:
    """Generate a unique resource name for testing."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def aws_credentials() -> Dict[str, Any]:
    """Get AWS credentials from environment variables."""
    return {
        "access_key_id": os.environ.get("AWS_ACCESS_KEY_ID"),
        "secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY"),
        "region": os.environ.get("AWS_REGION", "us-east-1"),
    }


@pytest.fixture
def gcp_credentials() -> Dict[str, Any]:
    """Get GCP credentials from environment variables."""
    return {
        "credentials_file": os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        "project": os.environ.get("GOOGLE_CLOUD_PROJECT"),
    }


@pytest.fixture
def ali_credentials() -> Dict[str, Any]:
    """Get Ali credentials from environment variables."""
    return {
        "access_key_id": os.environ.get("ALI_ACCESS_KEY_ID"),
        "access_key_secret": os.environ.get("ALI_ACCESS_KEY_SECRET"),
        "region": os.environ.get("ALI_REGION", "cn-hangzhou"),
    }


@pytest.fixture
def test_bucket_name() -> str:
    """Generate a unique bucket name for testing."""
    return generate_resource_name("multicloud-test-bucket")


@pytest.fixture
def test_blob_name() -> str:
    """Generate a unique blob name for testing."""
    return f"test-blob-{uuid.uuid4().hex[:8]}.txt"


@pytest.fixture
def test_blob_content() -> bytes:
    """Generate test content for a blob."""
    return b"This is test content for the blob."


_GCS_BUCKET_METADATA_ENV_VAR = "DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA"

#: Names of the only two methods in google-cloud-storage's bucket-metadata
#: cache that start threads. Stubbed for the session; the originals are kept so
#: tests can exercise the real behavior.
_CACHE_THREAD_METHODS = ("get_or_queue_fetch", "check_and_evict")

#: Real implementations, captured before stubbing.
REAL_BUCKET_METADATA_CACHE_METHODS: dict = {}


@pytest.fixture(autouse=True, scope="session")
def disable_gcs_bucket_metadata_prefetch():
    """Stop google-cloud-storage issuing requests on background threads.

    Its bucket-metadata cache exists only to decorate OpenTelemetry spans. On a
    cache miss it starts a daemon thread to fetch the bucket, and on a 404
    another to check the bucket still exists. Both issue real HTTP on no
    schedule, so under VCR a late request lands in whichever cassette is open --
    exhausting an interaction another test needed, or persisting a stray one.

    The env var alone is not enough: it gates the cache-miss read, but the 404
    eviction path and every write that populates the cache are ungated. So the
    two thread-starting methods are stubbed as well, which also means a release
    renaming the env var cannot silently reintroduce the flakiness.

    Both the stubs and the env var are undone on teardown, so the mutation does
    not outlive the session.
    """
    from google.cloud.storage._bucket_metadata_cache import BucketMetadataCache

    previous_env = os.environ.get(_GCS_BUCKET_METADATA_ENV_VAR)
    os.environ[_GCS_BUCKET_METADATA_ENV_VAR] = "true"

    originals = {
        name: getattr(BucketMetadataCache, name) for name in _CACHE_THREAD_METHODS
    }
    REAL_BUCKET_METADATA_CACHE_METHODS.update(originals)
    for name in originals:
        setattr(BucketMetadataCache, name, lambda self, bucket_name: None)
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(BucketMetadataCache, name, original)
        REAL_BUCKET_METADATA_CACHE_METHODS.clear()
        if previous_env is None:
            os.environ.pop(_GCS_BUCKET_METADATA_ENV_VAR, None)
        else:
            os.environ[_GCS_BUCKET_METADATA_ENV_VAR] = previous_env


@pytest.fixture
def real_bucket_metadata_cache():
    """Put the real cache methods back for one test, then re-stub.

    Needed because the session fixture above replaces them, so a test that
    asserts on the vendor's own behavior would otherwise be asserting on the
    stub.
    """
    from google.cloud.storage._bucket_metadata_cache import BucketMetadataCache

    if not REAL_BUCKET_METADATA_CACHE_METHODS:
        pytest.skip("session fixture has not captured the real methods")

    stubs = {name: getattr(BucketMetadataCache, name) for name in _CACHE_THREAD_METHODS}
    for name, original in REAL_BUCKET_METADATA_CACHE_METHODS.items():
        setattr(BucketMetadataCache, name, original)
    try:
        yield BucketMetadataCache
    finally:
        for name, stub in stubs.items():
            setattr(BucketMetadataCache, name, stub)


@pytest.fixture(autouse=True, scope="session")
def aws_replay_credentials_for_conformance(request) -> None:
    """Set dummy AWS credentials for ALL tests in replay mode (session-wide).

    In ``--record-mode=none``, botocore may probe IMDS when credentials are
    absent, which produces unmatched HTTP traffic under VCR replay. This
    fixture sets dummy credentials once at session start to prevent IMDS
    calls across all tests, not just conformance tests.

    Session-scoped to ensure env vars are set before module-scoped fixtures
    (like AwsStsHarness) initialize boto3 clients. This fixes the fixture
    scope mismatch where function-scoped credentials weren't available when
    module-scoped fixtures ran.

    Note: Session-scoped fixtures cannot access request.node, so we cannot
    scope this to conformance-only tests. All tests in replay mode get these
    dummy credentials, which is acceptable for CI one-shot runs.

    Note: This sets os.environ directly (not monkeypatch), so changes persist
    for the entire pytest process. This is acceptable for CI.
    """
    record_mode = request.config.getoption("--record-mode", default="none") or "none"
    if record_mode != "none":
        return

    env_defaults = {
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_SECURITY_TOKEN": "testing",
    }
    for key, value in env_defaults.items():
        if os.environ.get(key) is None:
            os.environ[key] = value
