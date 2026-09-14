"""Cloud-agnostic document store client package.

Public entry point — :class:`DocStoreClient`::

    from multicloud.docstore.docstore_client import DocStoreClient

    with DocStoreClient(
        provider="aws",
        table_name="my-table",
        partition_key="pk",
        region="us-west-2",
    ) as client:
        client.actions().create({"pk": "alice", "name": "Alice"}).run()

Provider classes (``AwsDocStore``, ...) live in sibling distributions
(``multicloud-docstore-aws``, ...) and are discovered at runtime via
setuptools entry points.
"""

from multicloud.docstore.docstore_client.client import DocStoreClient
from multicloud.docstore.docstore_client.driver import (
    DEFAULT_REVISION_FIELD,
    DOCSTORE_REGISTRY,
    Action,
    ActionKind,
    ActionList,
    BaseDocStore,
    CollectionOptions,
    Document,
    group_actions,
    group_by_field_path,
    serialize_object,
    validate_field_path,
)

__all__ = [
    "DocStoreClient",
    "DOCSTORE_REGISTRY",
    "DEFAULT_REVISION_FIELD",
    "Action",
    "ActionKind",
    "ActionList",
    "BaseDocStore",
    "CollectionOptions",
    "Document",
    "group_actions",
    "group_by_field_path",
    "serialize_object",
    "validate_field_path",
]
