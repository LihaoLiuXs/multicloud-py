"""AWS S3 implementation of the blob storage driver.

The provider class registers itself with ``BLOB_REGISTRY`` via the
``@BLOB_REGISTRY.register("aws")`` decorator at import time. This module
is loaded automatically by the ``multicloud.blob.providers`` entry-point
mechanism whenever ``multicloud-blob-aws`` is installed.
"""

from multicloud.blob.blob_aws.aws_blob_store import AwsBlobStore

__all__ = ["AwsBlobStore"]
