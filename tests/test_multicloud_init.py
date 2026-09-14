"""Unit tests for the multicloud namespace + common helpers.

``multicloud`` is a PEP 420 implicit namespace package — multiple
distributions (multicloud-common, multicloud-blob, multicloud-sts, ...)
contribute submodules under it. The cross-service helpers live in
``multicloud.common``.
"""

from __future__ import annotations

from multicloud.common import get_available_providers


class TestGetAvailableProviders:
    """Tests for ``multicloud.common.get_available_providers``."""

    def test_returns_list(self):
        result = get_available_providers()
        assert isinstance(result, list)

    def test_includes_aws_when_aws_provider_installed(self):
        """AWS provider should be discovered via entry points when an
        AWS provider distribution (e.g. multicloud-blob-aws or
        multicloud-sts-aws) is installed."""
        result = get_available_providers()
        assert "aws" in result
