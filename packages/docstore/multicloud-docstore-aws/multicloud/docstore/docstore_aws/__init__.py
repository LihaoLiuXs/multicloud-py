"""AWS DynamoDB provider for :mod:`multicloud.docstore`.

Importing this module has the side effect of registering
:class:`AwsDocStore` in
:data:`multicloud.docstore.docstore_client.driver.DOCSTORE_REGISTRY`
via its ``@DOCSTORE_REGISTRY.register("aws")`` decorator.

The client package (:mod:`multicloud.docstore.docstore_client`) imports
this module lazily through the ``multicloud.docstore.providers`` setuptools
entry point declared in this distribution's ``pyproject.toml``.
"""

from multicloud.docstore.docstore_aws.aws_docstore import AwsDocStore

__all__ = ["AwsDocStore"]
