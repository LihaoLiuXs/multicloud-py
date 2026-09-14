"""Shared VCR.py configuration for STS conformance tests.

Each provider's cassettes live in a sibling subdirectory (`cassettes/aws/`,
`cassettes/gcp/`). The provider is detected from the running test's class
name so a single `vcr_config` fixture serves all provider tests.
"""

from pathlib import Path
from typing import Any, Dict

import pytest

from tests.sts.conformance.vcr_filters import get_default_vcr_config

CASSETTES_ROOT = Path(__file__).parent / "cassettes"

# Provider-specific extra headers — request IDs and other noise that
# creates cassette diff churn if left in.
AWS_EXTRA_HEADERS = [
    "X-Amz-Request-Id",
    "x-amz-id-2",
    "amz-sdk-invocation-id",
    "amz-sdk-request",
]

GCP_EXTRA_HEADERS = [
    "x-goog-generation",
    "x-goog-metageneration",
    "x-guploader-uploadid",
]


def _provider_for(class_name: str) -> str:
    """Map a test class name to its provider id.

    ``TestAwsStsConformance`` -> ``"aws"``,
    ``TestGcpStsConformance`` -> ``"gcp"``,
    ``TestAliStsConformance`` -> ``"ali"``.
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


@pytest.fixture(scope="module")
def vcr_cassette_dir(request) -> str:
    """Override pytest-recording's default cassette directory.

    Organizes cassettes by provider: ``cassettes/aws/``, ``cassettes/gcp/``.
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
    """Per-provider VCR.py configuration for STS tests.

    Applies provider-specific header filters.
    """
    test_class = getattr(request, "cls", None)
    if test_class is not None:
        class_name = test_class.__name__
    else:
        class_name = request.module.__name__

    provider = _provider_for(class_name)

    config = get_default_vcr_config(cassette_dir=vcr_cassette_dir)

    if provider == "aws":
        config["filter_headers"].extend(AWS_EXTRA_HEADERS)
    elif provider == "gcp":
        config["filter_headers"].extend(GCP_EXTRA_HEADERS)

    return config
