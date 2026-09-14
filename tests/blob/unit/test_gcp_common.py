"""Unit tests for shared GCS write-precondition policy."""

from __future__ import annotations

import pytest
from multicloud.blob.blob_gcp._gcp_common import resolve_generation_match
from multicloud.common.exceptions import (
    InvalidArgumentException,
    UnsupportedOperationException,
)


class TestResolveGenerationMatch:
    """resolve_generation_match maps agnostic write options to a generation."""

    def test_unconditional_write_returns_none(self):
        assert (
            resolve_generation_match(
                create_if_absent=False, if_match=None, if_version_match=None
            )
            is None
        )

    def test_create_if_absent_maps_to_generation_zero(self):
        assert (
            resolve_generation_match(
                create_if_absent=True, if_match=None, if_version_match=None
            )
            == 0
        )

    def test_if_version_match_maps_to_generation(self):
        assert (
            resolve_generation_match(
                create_if_absent=False, if_match=None, if_version_match="42"
            )
            == 42
        )

    def test_if_version_match_non_numeric_is_invalid_argument(self):
        with pytest.raises(InvalidArgumentException, match="numeric GCS generation"):
            resolve_generation_match(
                create_if_absent=False, if_match=None, if_version_match="not-a-number"
            )

    def test_if_version_match_zero_is_invalid_argument(self):
        # Generation 0 is create-if-absent semantics, not a live version id.
        with pytest.raises(InvalidArgumentException, match="positive GCS generation"):
            resolve_generation_match(
                create_if_absent=False, if_match=None, if_version_match="0"
            )

    def test_if_version_match_negative_is_invalid_argument(self):
        with pytest.raises(InvalidArgumentException, match="positive GCS generation"):
            resolve_generation_match(
                create_if_absent=False, if_match=None, if_version_match="-1"
            )

    def test_if_match_alone_is_unsupported(self):
        with pytest.raises(UnsupportedOperationException, match="if_match"):
            resolve_generation_match(
                create_if_absent=False, if_match="etag-abc", if_version_match=None
            )

    def test_if_match_with_if_version_match_uses_generation(self):
        assert (
            resolve_generation_match(
                create_if_absent=False, if_match="etag-abc", if_version_match="42"
            )
            == 42
        )

    def test_if_version_match_wins_over_create_if_absent(self):
        assert (
            resolve_generation_match(
                create_if_absent=True, if_match=None, if_version_match="7"
            )
            == 7
        )
