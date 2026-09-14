"""Cloud-agnostic document store driver contract.

Contains the abstract types that a provider (e.g. the AWS provider) must
implement, along with the value types (``Action``, ``ActionKind``,
``Document``, ``CollectionOptions``), the fluent ``ActionList`` builder,
and shared ``util`` helpers used across providers.

Provider packages register a concrete subclass of :class:`BaseDocStore`
with :data:`DOCSTORE_REGISTRY`; :class:`DocStoreClient` in ``client.py``
resolves them by id at construction time.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
)
from multicloud.common.provider import Provider, ProviderRegistry

DOCSTORE_REGISTRY = ProviderRegistry(
    service_name="docstore",
    entry_point_group="multicloud.docstore.providers",
)


DEFAULT_REVISION_FIELD = "DocstoreRevision"


class ActionKind(Enum):
    """The kind of write / read that an :class:`Action` represents."""

    ACTION_KIND_CREATE = "create"
    ACTION_KIND_REPLACE = "replace"
    ACTION_KIND_PUT = "put"
    ACTION_KIND_GET = "get"
    ACTION_KIND_DELETE = "delete"
    ACTION_KIND_UPDATE = "update"


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


class Document:
    """Wraps either a ``dict`` or a plain object with attributes.

    Provides the operations the driver needs on the caller's data:
    reading a top-level field, setting a top-level field, walking nested
    dotted paths, and enumerating field names for encoding.

    A ``Document`` never represents an atomic value on its own — it is
    always a container of named fields.
    """

    def __init__(self, source: Any) -> None:
        if source is None:
            raise InvalidArgumentException("document source must not be None")
        self._source = source

    @property
    def source(self) -> Any:
        """Underlying wrapped object (dict or arbitrary object)."""
        return self._source

    @property
    def is_map(self) -> bool:
        return isinstance(self._source, dict)

    def has_field(self, field_name: str) -> bool:
        if self.is_map:
            return field_name in self._source
        return hasattr(self._source, field_name)

    def get_field(self, field_name: str) -> Any:
        if self.is_map:
            return self._source.get(field_name)
        return getattr(self._source, field_name, None)

    def set_field(self, field_name: str, value: Any) -> None:
        if self.is_map:
            self._source[field_name] = value
        else:
            setattr(self._source, field_name, value)

    def field_names(self) -> List[str]:
        if self.is_map:
            return list(self._source.keys())
        return [name for name in vars(self._source).keys() if not name.startswith("_")]

    def get(self, field_path: Sequence[str]) -> Any:
        """Read a nested field by dotted-path parts.

        Traverses ``dict`` values by key and objects by attribute; a
        missing segment short-circuits to ``None`` instead of raising.

        :param field_path: Path segments; ``["a", "b", "c"]`` walks
            ``source["a"]["b"]["c"]``.
        :return: The leaf value, or ``None`` if any segment is missing.
        """
        current: Any = self._source
        for part in field_path:
            if isinstance(current, dict):
                current = current.get(part)
            elif current is None:
                return None
            else:
                current = getattr(current, part, None)
        return current

    def set(self, field_path: Sequence[str], value: Any) -> None:
        """Write a value at a nested dotted path, creating intermediates.

        Any missing intermediate is replaced with a fresh ``dict`` so
        the write always succeeds when ``field_path`` is non-empty. On
        object sources, intermediates are set via ``setattr``.

        :param field_path: Path segments; must be non-empty.
        :param value: Leaf value to store at the last segment.
        :raises InvalidArgumentException: If ``field_path`` is empty.
        """
        if not field_path:
            raise InvalidArgumentException("fieldPath should not be empty")
        cursor: Any = self._source
        for part in field_path[:-1]:
            if isinstance(cursor, dict):
                next_val = cursor.get(part)
                if not isinstance(next_val, (dict,)):
                    next_val = {}
                    cursor[part] = next_val
                cursor = next_val
            else:
                next_val = getattr(cursor, part, None)
                if not isinstance(next_val, dict):
                    next_val = {}
                    setattr(cursor, part, next_val)
                cursor = next_val
        leaf = field_path[-1]
        if isinstance(cursor, dict):
            cursor[leaf] = value
        else:
            setattr(cursor, leaf, value)


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------


@dataclass
class Action:
    """A single pending operation on a document.

    A value type produced by the fluent :class:`ActionList` and consumed
    by the driver's :meth:`BaseDocStore.run_actions`.

    ``key`` and ``index`` are populated by
    :meth:`BaseDocStore._to_driver_action` and used by
    :func:`group_actions` to keep the caller-supplied order.
    """

    kind: ActionKind
    document: Document
    field_paths: Optional[List[List[str]]] = None
    mods: Optional[List[Any]] = None
    in_atomic_write: bool = False
    key: Any = None
    index: int = 0


# ---------------------------------------------------------------------------
# CollectionOptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionOptions:
    """Immutable configuration for a document collection (table).

    ``table_name`` names the underlying table or collection,
    ``partition_key`` / ``sort_key`` name the primary key attributes,
    ``allow_scans`` gates full-table scans on the read path,
    ``revision_field`` is the attribute used for optimistic concurrency
    control (empty ⇒ use :data:`DEFAULT_REVISION_FIELD`), and
    ``max_outstanding_action_rpcs`` bounds the internal thread pool that
    services :meth:`BaseDocStore.run_actions` (``0`` ⇒ pick a sensible
    default).
    """

    table_name: str
    partition_key: str
    sort_key: Optional[str] = None
    allow_scans: bool = True
    revision_field: Optional[str] = None
    max_outstanding_action_rpcs: int = 0


# ---------------------------------------------------------------------------
# Util helpers
# ---------------------------------------------------------------------------


def serialize_object(obj: Any) -> str:
    """Serialize an object into a stable string for key comparison.

    Used as the dedup key across an action list — two actions targeting
    the same key must produce the same string here even if the caller
    passed a fresh instance.
    """

    def _default(o: Any) -> Any:
        if isinstance(o, (list, tuple)):
            return list(o)
        if hasattr(o, "__dict__"):
            return o.__dict__
        return str(o)

    try:
        return json.dumps(obj, default=_default, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise InvalidArgumentException(f"Unable to serialize object: {exc}") from exc


def validate_field_path(field_path: str) -> None:
    """Validate that ``field_path`` is non-empty and has no empty segments."""
    if not field_path:
        raise InvalidArgumentException("fieldPath should not be null or empty")
    parts = field_path.split(".")
    for part in parts:
        if not part:
            raise InvalidArgumentException("fieldPath part should not be empty")


def group_actions(
    actions: Sequence[Action],
) -> Tuple[List[Action], List[Action], List[Action], List[Action], List[Action]]:
    """Split an action list into five buckets, preserving user-provided order.

    Returns ``(before_gets, get_list, write_list, atomic_write_list,
    after_gets)``.

    Any GET targeting the same key as a pending write becomes an
    ``after_gets`` (must run *after* the write); any GET whose key is
    later written by a non-atomic write is promoted to ``before_gets``.
    Actions with a ``None`` key stream through to the write bucket
    directly (they will be assigned a key later on the CREATE path).
    """
    bgets: Dict[str, Action] = {}
    agets: Dict[str, Action] = {}
    cgets: Dict[str, Action] = {}
    writes: Dict[str, Action] = {}
    atomic_writes: Dict[str, Action] = {}

    null_keys: List[Action] = []
    atomic_null_keys: List[Action] = []

    for action in actions:
        key_str = serialize_object(action.key)
        if action.key is None:
            if action.in_atomic_write:
                atomic_null_keys.append(action)
            else:
                null_keys.append(action)
            continue

        if action.kind == ActionKind.ACTION_KIND_GET:
            if key_str in writes or key_str in atomic_writes:
                agets[key_str] = action
            else:
                cgets[key_str] = action
            continue

        # It's a write.
        if key_str in cgets:
            bgets[key_str] = cgets.pop(key_str)
        if action.in_atomic_write:
            atomic_writes[key_str] = action
        else:
            writes[key_str] = action

    def _sorted(values: Iterable[Action]) -> List[Action]:
        return sorted(values, key=lambda a: a.index)

    before_gets = _sorted(bgets.values())
    get_list = _sorted(cgets.values())
    write_list = _sorted(list(writes.values()) + null_keys)
    atomic_write_list = _sorted(list(atomic_writes.values()) + atomic_null_keys)
    after_gets = _sorted(agets.values())
    return before_gets, get_list, write_list, atomic_write_list, after_gets


def group_by_field_path(gets: Sequence[Action]) -> List[List[Action]]:
    """Group GET actions with identical field-path sets into batches.

    Backend batch-get APIs typically accept only a single projection
    per request, so gets that project different fields cannot ride in
    the same batch.
    """
    groups: List[List[Action]] = []
    seen: Dict[int, bool] = {}
    while len(seen) < len(gets):
        current: List[Action] = []
        for a in gets:
            if id(a) in seen:
                continue
            if not current or _fps_equal(current[0].field_paths, a.field_paths):
                current.append(a)
                seen[id(a)] = True
        groups.append(current)
    return groups


def _fps_equal(
    fps1: Optional[List[List[str]]], fps2: Optional[List[List[str]]]
) -> bool:
    if fps1 is None and fps2 is None:
        return True
    if fps1 is None or fps2 is None:
        return False
    if len(fps1) != len(fps2):
        return False
    for a, b in zip(fps1, fps2):
        if a != b:
            return False
    return True


# ---------------------------------------------------------------------------
# BaseDocStore — the provider contract
# ---------------------------------------------------------------------------


class BaseDocStore(Provider, ABC):
    """Abstract driver a provider (e.g. the AWS provider) implements.

    Owns the collection options, the internal executor used to fan
    action lists out to their backend, and the CREATE-path helpers that
    are agnostic of the backing store (key extraction, validation,
    executor sizing).
    """

    def __init__(
        self,
        collection_options: CollectionOptions,
        *,
        credentials_overrider: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._collection_options = collection_options
        self._credentials_overrider = credentials_overrider
        self._closed = False
        self._executor = self._build_executor(
            collection_options.max_outstanding_action_rpcs
        )

    # ------------------------------------------------------------------
    # Executor lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _build_executor(max_outstanding: int) -> ThreadPoolExecutor:
        # Fall back to a generous ceiling when the caller did not
        # request an explicit RPC bound.
        workers = max_outstanding if max_outstanding >= 1 else 32
        return ThreadPoolExecutor(max_workers=workers)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def collection_options(self) -> CollectionOptions:
        return self._collection_options

    @property
    def executor(self) -> ThreadPoolExecutor:
        return self._executor

    @property
    def credentials_overrider(self) -> Optional[Callable[..., Any]]:
        return self._credentials_overrider

    @property
    def revision_field(self) -> str:
        rf = self._collection_options.revision_field
        return rf if rf else DEFAULT_REVISION_FIELD

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def check_closed(self) -> None:
        """Raise if the store has been closed."""
        if self._closed:
            raise InvalidArgumentException("docstore is closed")

    def actions(self) -> "ActionList":
        """Return a new fluent :class:`ActionList` bound to this store."""
        return ActionList(self)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False)

    def __enter__(self) -> "BaseDocStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Key extraction & driver-action shaping
    # ------------------------------------------------------------------

    def get_key(self, document: Document) -> Any:
        """Extract the primary key from ``document``.

        Returns ``None`` when the partition key is missing so the CREATE
        path can decide whether to synthesize a UUID. On a table with a
        sort key, both parts must be present or the result is ``None``.

        :param document: The document being written or read.
        :return: The raw partition-key value when no sort key is
            configured, a ``(pk, sk)`` tuple when one is, or ``None``
            when a required part is missing.
        """
        pk_name = self._collection_options.partition_key
        pk_val = document.get_field(pk_name)
        if pk_val is None:
            return None
        sk_name = self._collection_options.sort_key
        if not sk_name:
            return pk_val
        sk_val = document.get_field(sk_name)
        if sk_val is None:
            return None
        return (pk_val, sk_val)

    def _to_driver_action(self, action: Action, index: int) -> None:
        """Validate and finalize a single pending action.

        Stamps ``action.index`` for stable ordering, extracts and stores
        the primary key on ``action.key``, and enforces the per-kind
        invariants: CREATE may omit the key (the provider will
        synthesize one) but must not carry the revision field; every
        other kind requires the key. A PUT that carries a revision is
        promoted to REPLACE so the write goes through the conditional
        precondition path.

        :param action: The action to shape; mutated in place.
        :param index: Position in the caller's :class:`ActionList`.
        :raises InvalidArgumentException: If the shape check fails —
            missing key on a non-CREATE action, revision field set on
            CREATE, or an empty field-path segment.
        """
        action.index = index
        key = self.get_key(action.document)
        action.key = key

        if action.kind != ActionKind.ACTION_KIND_CREATE and key is None:
            raise InvalidArgumentException(
                f"missing key field for {action.kind.name} action"
            )

        if action.kind == ActionKind.ACTION_KIND_CREATE and action.document.has_field(
            self.revision_field
        ):
            raise InvalidArgumentException(
                f"revision field {self.revision_field!r} must not be set for CREATE"
            )

        if action.field_paths:
            for fp in action.field_paths:
                for segment in fp:
                    validate_field_path(segment)

        # A PUT that carries a revision is promoted to REPLACE so the
        # write goes through the conditional-precondition path.
        if action.kind == ActionKind.ACTION_KIND_PUT and action.document.has_field(
            self.revision_field
        ):
            action.kind = ActionKind.ACTION_KIND_REPLACE

    # ------------------------------------------------------------------
    # Provider-supplied
    # ------------------------------------------------------------------

    @abstractmethod
    def run_actions(
        self,
        actions: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]] = None,
    ) -> None:
        """Execute a driver-shaped list of actions."""

    # ------------------------------------------------------------------
    # GET batching (shared across providers)
    # ------------------------------------------------------------------

    def _run_gets(
        self,
        actions: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]],
        batch_size: int,
        consistent_read: bool = False,
    ) -> None:
        """Fan a list of GET actions out to the provider's batch API.

        Groups actions by identical projection field-paths (a single
        backend batch cannot mix projections), splits each group into
        chunks of at most ``batch_size``, and submits one
        :meth:`_batch_get` call per chunk to the shared executor. Every
        submission runs on its own worker thread; :func:`wait_for_all`
        blocks until they finish and re-raises the first failure.

        :param actions: Driver-shaped GET actions to execute.
        :param before_do: Optional hook forwarded to each
            :meth:`_batch_get` call and invoked once per batch.
        :param batch_size: Maximum actions per backend call.
        :param consistent_read: If True, request a strongly-consistent
            read from backends that expose the knob. Only callers that
            need read-your-writes semantics should set this; providers
            whose reads are already strongly consistent may ignore it.
            Default: False.
        """
        if not actions:
            return
        groups = group_by_field_path(list(actions))
        futures: List[Future[Any]] = []
        for group in groups:
            n = (len(group) + batch_size - 1) // batch_size
            for index in range(n):
                start = index * batch_size
                end = min((index + 1) * batch_size, len(group)) - 1
                futures.append(
                    self._executor.submit(
                        self._batch_get,
                        group,
                        before_do,
                        start,
                        end,
                        consistent_read,
                    )
                )
        wait_for_all(futures)

    @abstractmethod
    def _batch_get(
        self,
        gets: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]],
        start: int,
        end: int,
        consistent_read: bool = False,
    ) -> None:
        """Execute one backend batch-get for ``gets[start..end]`` (inclusive).

        ``gets`` is a homogeneous field-path group produced by
        :func:`group_by_field_path`; the implementation should read only
        the ``[start, end]`` inclusive slice. Missing documents are
        silently ignored — the caller's document is left untouched.
        ``before_do`` (when supplied) MUST be invoked exactly once per
        batch immediately before the backend RPC. ``consistent_read``,
        when True, requests a strongly-consistent read from backends
        that expose the knob; providers whose reads are already
        strongly consistent may ignore it.
        """


# ---------------------------------------------------------------------------
# ActionList
# ---------------------------------------------------------------------------


class ActionList:
    """Fluent builder for a batch of document operations.

    Users call ``.create(doc)`` (etc.) any number of times, then
    ``.run()``. ``run()`` first shapes each pending action via
    :meth:`BaseDocStore._to_driver_action` (which assigns keys and
    validates), then dispatches the whole batch through
    :meth:`BaseDocStore.run_actions`.
    """

    def __init__(self, docstore: BaseDocStore) -> None:
        self._docstore = docstore
        self._actions: List[Action] = []
        self._enable_atomic_writes = False
        self._before_do: Optional[Callable[[Callable[[Any], bool]], None]] = None

    # -- builder methods -------------------------------------------------

    def create(self, document: Any) -> "ActionList":
        """Queue a CREATE action for ``document``.

        CREATE inserts a new row and fails with
        :class:`ResourceAlreadyExistsException` at :meth:`run` time if
        the key already exists. The document must not carry the
        revision field; the driver stamps a fresh revision on success.

        :param document: A ``dict`` (wrapped automatically) or a
            :class:`Document`.
        :return: This :class:`ActionList` for fluent chaining.
        """
        doc = document if isinstance(document, Document) else Document(document)
        self._actions.append(
            Action(
                kind=ActionKind.ACTION_KIND_CREATE,
                document=doc,
                in_atomic_write=self._enable_atomic_writes,
            )
        )
        return self

    def replace(self, document: Any) -> "ActionList":
        """Queue a REPLACE action for ``document``.

        REPLACE requires the row to already exist. If ``document``
        carries a revision, the write additionally asserts that the
        stored revision matches; the driver stamps a fresh revision on
        success.

        :param document: A ``dict`` (wrapped automatically) or a
            :class:`Document`.
        :return: This :class:`ActionList` for fluent chaining.
        """
        doc = document if isinstance(document, Document) else Document(document)
        self._actions.append(
            Action(
                kind=ActionKind.ACTION_KIND_REPLACE,
                document=doc,
                in_atomic_write=self._enable_atomic_writes,
            )
        )
        return self

    def put(self, document: Any) -> "ActionList":
        """Queue a PUT (upsert) action for ``document``.

        PUT is an unconditional upsert. When ``document`` carries the
        revision field the action is promoted to REPLACE by
        :meth:`BaseDocStore._to_driver_action`, adding a
        matching-revision precondition.

        :param document: A ``dict`` (wrapped automatically) or a
            :class:`Document`.
        :return: This :class:`ActionList` for fluent chaining.
        """
        doc = document if isinstance(document, Document) else Document(document)
        self._actions.append(
            Action(
                kind=ActionKind.ACTION_KIND_PUT,
                document=doc,
                in_atomic_write=self._enable_atomic_writes,
            )
        )
        return self

    def delete(self, document: Any) -> "ActionList":
        """Queue a DELETE action for ``document``.

        The document's primary-key attributes are used as the target
        key. When ``document`` carries the revision field the write
        additionally asserts that the stored revision matches.

        :param document: A ``dict`` (wrapped automatically) or a
            :class:`Document` carrying the primary key.
        :return: This :class:`ActionList` for fluent chaining.
        """
        doc = document if isinstance(document, Document) else Document(document)
        self._actions.append(
            Action(
                kind=ActionKind.ACTION_KIND_DELETE,
                document=doc,
                in_atomic_write=self._enable_atomic_writes,
            )
        )
        return self

    def get(self, document: Any, *field_paths: str) -> "ActionList":
        """Queue a GET action for ``document`` with optional field projection.

        The document's primary-key attributes are used as the lookup
        key; retrieved attributes are merged back into it in place at
        :meth:`run` time.

        :param document: A ``dict`` (wrapped automatically) or a
            :class:`Document` carrying the primary key.
        :param field_paths: Optional dotted projection paths (e.g.
            ``"a.b.c"``). Empty means "return the full item".
        :return: This :class:`ActionList` for fluent chaining.
        """
        doc = document if isinstance(document, Document) else Document(document)
        fps: Optional[List[List[str]]] = None
        if field_paths:
            fps = [fp.split(".") for fp in field_paths]
        self._actions.append(
            Action(
                kind=ActionKind.ACTION_KIND_GET,
                document=doc,
                field_paths=fps,
                in_atomic_write=False,
            )
        )
        return self

    def enable_atomic_writes(self) -> "ActionList":
        self._enable_atomic_writes = True
        return self

    def before_do(
        self, callback: Callable[[Callable[[Any], bool]], None]
    ) -> "ActionList":
        self._before_do = callback
        return self

    # -- terminal --------------------------------------------------------

    def run(self) -> None:
        """Validate every queued action and dispatch the batch.

        First shapes each pending action via
        :meth:`BaseDocStore._to_driver_action` (assigns keys, promotes
        PUT-with-revision to REPLACE, rejects duplicates), then hands
        the whole list to :meth:`BaseDocStore.run_actions`. Any
        provider-side error surfaces as a
        :class:`SubstrateSdkException`; unexpected exceptions are
        re-raised as :class:`InvalidArgumentException`.

        :raises SubstrateSdkException: Any translated backend error.
        """
        try:
            self._docstore.check_closed()
            self._to_driver_actions()
            self._docstore.run_actions(self._actions, self._before_do)
        except SubstrateSdkException:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise InvalidArgumentException(str(exc)) from exc

    def _to_driver_actions(self) -> None:
        """Assign ``index`` + ``key`` on each action; reject duplicates.

        Two actions with the same ``(key, kind)`` in one batch raise
        :class:`InvalidArgumentException` — the batch is refused, not
        silently collapsed.
        """
        seen: Dict[Tuple[str, ActionKind], bool] = {}
        for i, action in enumerate(self._actions):
            self._docstore._to_driver_action(action, i)
            if action.key is None:
                continue
            marker = (serialize_object(action.key), action.kind)
            if marker in seen:
                raise InvalidArgumentException(
                    f"duplicate action for key {action.key!r} kind {action.kind.name}"
                )
            seen[marker] = True


# ---------------------------------------------------------------------------
# Convenience: submit-and-forget helper used by providers
# ---------------------------------------------------------------------------


def wait_for_all(futures: Sequence[Future[Any]]) -> None:
    """Block until every future completes; re-raise the first exception.

    The caller should only see one exception, and it should be one that
    already passed through the provider's ``translate_error``.
    """
    first_exc: Optional[BaseException] = None
    for f in futures:
        try:
            f.result()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if first_exc is None:
                first_exc = exc
    if first_exc is not None:
        raise first_exc
