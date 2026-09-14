"""Shared GCS helpers used by both the sync and async GCP blob stores.

The sync (:mod:`gcp_blob_store`) and async (:mod:`async_blob.gcp_async_blob_store`)
providers enforce the same write-precondition policy — reject ETag-only CAS,
map create-if-absent / ``if_version_match`` onto a generation match — but apply
it to different request shapes: the ``google-cloud-storage`` blob handle vs
JSON-API query parameters. The policy lives here once; each store adapts the
returned generation onto its own wire format.
"""

from __future__ import annotations

from typing import Optional

from multicloud.common.exceptions import (
    InvalidArgumentException,
    UnsupportedOperationException,
)

_IF_MATCH_UNSUPPORTED = (
    "GCS does not support conditional overwrite by ETag (if_match) "
    "on upload; use if_version_match with the object's version id, "
    "or create_if_absent for create-if-absent semantics"
)


def resolve_generation_match(
    *,
    create_if_absent: bool,
    if_match: Optional[str],
    if_version_match: Optional[str],
) -> Optional[int]:
    """Return the GCS generation to match, or None if the write is unconditional.

    ``create_if_absent`` maps to generation ``0`` (succeed only when no live
    object exists). ``if_version_match`` maps to that generation (succeed only
    when the live object's generation matches — the version id GCS returns is
    the generation) and must be a positive integer; ``0`` (create-if-absent)
    and negative values are rejected. When both are set, ``if_version_match``
    wins.

    GCS has no ETag-based conditional overwrite. A portable caller may set both
    tokens so each provider enforces with the one it supports; GCS then enforces
    via ``if_version_match`` and ignores the redundant ``if_match``. ``if_match``
    with no ``if_version_match`` cannot be expressed and raises
    UnsupportedOperationException.
    """
    if if_match is not None and if_version_match is None:
        raise UnsupportedOperationException(_IF_MATCH_UNSUPPORTED)
    if if_version_match is not None:
        try:
            generation = int(if_version_match)
        except ValueError as exc:
            raise InvalidArgumentException(
                f"if_version_match must be a numeric GCS generation; "
                f"got {if_version_match!r}"
            ) from exc
        if generation <= 0:
            raise InvalidArgumentException(
                f"if_version_match must be a positive GCS generation; "
                f"got {if_version_match!r}"
            )
        return generation
    if create_if_absent:
        return 0
    return None
