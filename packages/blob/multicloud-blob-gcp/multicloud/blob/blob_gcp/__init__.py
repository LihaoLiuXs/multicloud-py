"""GCP Cloud Storage implementation of the blob storage driver.

The provider class registers itself with ``BLOB_REGISTRY`` via the
``@BLOB_REGISTRY.register("gcp")`` decorator at import time. Loaded
automatically by the ``multicloud.blob.providers`` entry-point
mechanism whenever ``multicloud-blob-gcp`` is installed.
"""

from multicloud.blob.blob_gcp.gcp_blob_store import GcpBlobStore

__all__ = ["GcpBlobStore"]
