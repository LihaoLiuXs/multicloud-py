"""GCP async blob provider package.

Registers ``GcpAsyncBlobStore`` with ``ASYNC_BLOB_REGISTRY`` via the
``@ASYNC_BLOB_REGISTRY.register("gcp")`` decorator at import time.
This module is loaded automatically by the ``multicloud.blob.async_providers``
entry-point mechanism whenever ``multicloud-blob-gcp[async]`` is installed.
"""

from .gcp_async_blob_store import GcpAsyncBlobStore

__all__ = ["GcpAsyncBlobStore"]
