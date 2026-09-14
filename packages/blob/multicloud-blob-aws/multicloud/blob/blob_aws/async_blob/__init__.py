"""AWS async blob provider package.

Registers ``AwsAsyncBlobStore`` with ``ASYNC_BLOB_REGISTRY`` via the
``@ASYNC_BLOB_REGISTRY.register("aws")`` decorator at import time.
This module is loaded automatically by the ``multicloud.blob.async_providers``
entry-point mechanism whenever ``multicloud-blob-aws[async]`` is installed.
"""

from .aws_async_blob_store import AwsAsyncBlobStore

__all__ = ["AwsAsyncBlobStore"]
