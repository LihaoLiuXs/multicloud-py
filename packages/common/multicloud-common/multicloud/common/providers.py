"""Cross-service helpers for enumerating installed providers.

Replaces the previous top-level ``multicloud.get_available_providers()``
helper. Walks setuptools entry points so the answer reflects what is
actually installed in the current environment, not what is on disk.
"""

from __future__ import annotations

import importlib.metadata
from typing import List, Set

# Service entry-point groups currently shipped by the SDK. Each
# provider distribution registers itself under exactly one of these.
_SERVICE_GROUPS = (
    "multicloud.blob.providers",
    "multicloud.sts.providers",
)


def get_available_providers() -> List[str]:
    """Return a sorted list of provider ids installed in this environment.

    A provider id appears in the result if any installed distribution
    advertises an entry point for it under any service group. The id
    itself (``"aws"``, ``"gcp"``, ``"alibaba"``) is the entry-point name.
    """
    seen: Set[str] = set()
    eps = importlib.metadata.entry_points()
    for group in _SERVICE_GROUPS:
        for ep in eps.select(group=group):
            seen.add(ep.name)
    return sorted(seen)
