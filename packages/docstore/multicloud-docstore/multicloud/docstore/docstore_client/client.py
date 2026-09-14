"""Public :class:`DocStoreClient` — the surface users touch.

Three-layer architecture:

1. **Client** (this file) — what users import; provider-agnostic API.
2. **Driver** (:class:`BaseDocStore` in ``driver.py``) — internal abstract
   contract; owns the fluent :class:`ActionList` and validation.
3. **Provider** (e.g. the AWS provider) — concrete impls that callers
   never reference directly. The right class is resolved from
   :data:`DOCSTORE_REGISTRY` at construction time based on ``provider``.

Every operation delegates to the underlying :class:`BaseDocStore` and
wraps native exceptions through :meth:`BaseDocStore.translate_error` so
callers only see the common
:class:`multicloud.common.exceptions.SubstrateSdkException` hierarchy.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from multicloud.docstore.docstore_client.driver import (
    DOCSTORE_REGISTRY,
    ActionList,
    BaseDocStore,
    CollectionOptions,
    Document,
)


class DocStoreClient:
    """Cloud-agnostic document store client.

    Resolves a provider-specific driver from :data:`DOCSTORE_REGISTRY` at
    construction time and forwards every operation to it. All arguments
    are keyword-only; only ``provider``, ``table_name``, and
    ``partition_key`` are required.

    :param provider: Provider id (e.g. ``"aws"``).
    :param table_name: Name of the underlying table / collection.
    :param partition_key: Attribute name that holds the partition key.
    :param sort_key: Optional attribute name for the sort key.
    :param revision_field: Attribute used for optimistic-concurrency
        revisions. ``None`` uses the driver default
        (``"DocstoreRevision"``).
    :param allow_scans: Whether scans (unindexed full-table reads) are
        permitted on this collection.
    :param max_outstanding_action_rpcs: Upper bound on in-flight backend
        RPCs when ``run_actions`` fans out. ``0`` uses the executor
        default.
    :param region: Provider region (e.g. ``"us-west-2"``).
    :param endpoint: Provider endpoint URI override.
    :param credentials_overrider: Optional provider-specific credentials
        callback.
    :raises KeyError: If ``provider`` is not registered.
    """

    def __init__(
        self,
        *,
        provider: str,
        table_name: str,
        partition_key: str,
        sort_key: Optional[str] = None,
        revision_field: Optional[str] = None,
        allow_scans: bool = True,
        max_outstanding_action_rpcs: int = 0,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        credentials_overrider: Optional[Callable[..., Any]] = None,
    ) -> None:
        options = CollectionOptions(
            table_name=table_name,
            partition_key=partition_key,
            sort_key=sort_key,
            allow_scans=allow_scans,
            revision_field=revision_field,
            max_outstanding_action_rpcs=max_outstanding_action_rpcs,
        )
        store_cls = DOCSTORE_REGISTRY.get(provider)
        kwargs: dict[str, Any] = {"collection_options": options}
        if region is not None:
            kwargs["region"] = region
        if endpoint is not None:
            kwargs["endpoint_url"] = endpoint
        if credentials_overrider is not None:
            kwargs["credentials_overrider"] = credentials_overrider
        self._store: BaseDocStore = store_cls(**kwargs)

    # ------------------------------------------------------------------
    # Fluent entry point
    # ------------------------------------------------------------------

    def actions(self) -> ActionList:
        """Return a fresh :class:`ActionList` bound to this client."""
        return self._store.actions()

    # ------------------------------------------------------------------
    # Single-action shortcuts
    # ------------------------------------------------------------------

    def create(self, document: Any) -> None:
        """Insert a new row for ``document``.

        Fails if a row with the same key already exists. The document
        must not carry the revision field; the driver stamps a fresh
        revision back onto ``document`` on success.

        :param document: A ``dict`` (wrapped automatically) or a
            :class:`Document` instance.
        :raises ResourceAlreadyExistsException: If the key already
            exists.
        :raises InvalidArgumentException: If the document carries the
            revision field, or the primary key shape is invalid.
        """
        self.actions().create(document).run()

    def replace(self, document: Any) -> None:
        """Overwrite an existing row with ``document``.

        Requires the row to already exist. When ``document`` carries the
        revision field, the write additionally asserts that the stored
        revision matches; the driver stamps a fresh revision back onto
        ``document`` on success.

        :param document: A ``dict`` (wrapped automatically) or a
            :class:`Document` instance.
        :raises ResourceNotFoundException: If the row does not exist or
            the revision precondition fails.
        """
        self.actions().replace(document).run()

    def put(self, document: Any) -> None:
        """Upsert ``document`` — insert or overwrite unconditionally.

        When ``document`` carries the revision field the write is
        promoted to a conditional REPLACE that requires the stored
        revision to match. The driver stamps a fresh revision back onto
        ``document`` on success in that case.

        :param document: A ``dict`` (wrapped automatically) or a
            :class:`Document` instance.
        :raises ResourceNotFoundException: If a revision was supplied
            and the stored revision does not match (or the row is
            absent).
        """
        self.actions().put(document).run()

    def delete(self, document: Any) -> None:
        """Delete the row identified by ``document``'s primary key.

        When ``document`` carries the revision field the delete is
        conditional on the stored revision matching.

        :param document: A ``dict`` (wrapped automatically) or a
            :class:`Document` carrying the primary key.
        :raises ResourceNotFoundException: If a revision was supplied
            and the stored revision does not match.
        """
        self.actions().delete(document).run()

    def get(self, document: Any, *field_paths: str) -> None:
        """Read a row by its primary key and merge fields into ``document``.

        The document's primary-key attributes are used as the lookup
        key; retrieved attributes are written back onto ``document``
        in place.

        :param document: A ``dict`` (wrapped automatically) or a
            :class:`Document` instance carrying the primary key.
        :param field_paths: Optional dotted projection paths (e.g.
            ``"a.b.c"``). Empty means "return the full item".
        """
        self.actions().get(document, *field_paths).run()

    # ------------------------------------------------------------------
    # Batch shortcuts
    # ------------------------------------------------------------------

    def batch_get(self, documents: Iterable[Any]) -> None:
        """Read many rows in a single :class:`ActionList` run.

        Each document must carry its primary key; retrieved attributes
        are merged back onto the same document instances in place.
        Projection is not supported here — use :meth:`actions` for
        per-row ``field_paths``.

        :param documents: Iterable of ``dict`` / :class:`Document` items.
        """
        action_list = self.actions()
        for document in documents:
            action_list.get(document)
        action_list.run()

    def batch_put(self, documents: Iterable[Any]) -> None:
        """Upsert many rows in a single :class:`ActionList` run.

        Each row is inserted if absent or overwritten if present, using
        the same revision-aware semantics as :meth:`put`.

        :param documents: Iterable of ``dict`` / :class:`Document` items.
        """
        action_list = self.actions()
        for document in documents:
            action_list.put(document)
        action_list.run()

    def batch_delete(self, documents: Iterable[Any]) -> None:
        """Delete many rows in a single :class:`ActionList` run.

        Each document must carry its primary key. When a document carries
        the revision field, that delete is conditional on the stored
        revision matching — same semantics as :meth:`delete`.

        :param documents: Iterable of ``dict`` / :class:`Document` items.
        """
        action_list = self.actions()
        for document in documents:
            action_list.delete(document)
        action_list.run()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "DocStoreClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def store(self) -> BaseDocStore:
        """Underlying driver instance (mostly for tests / introspection)."""
        return self._store


def wrap_document(document: Any) -> Document:
    """Ensure ``document`` is a :class:`Document`; wrap a raw dict / object."""
    return document if isinstance(document, Document) else Document(document)
