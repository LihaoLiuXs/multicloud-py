"""GCP Firestore implementation of :class:`BaseDocStore`.

Owns the write / read paths against Firestore:

* :meth:`run_actions` — five-bucket dispatch (before-gets, writes,
  atomic writes, gets, after-gets). Before-gets run inline so writes
  can see their result; writes, atomic writes, and gets are fanned out
  through the store's internal thread pool and awaited together;
  after-gets run last so they can see the post-write state. Atomic
  writes commit through the same ``CommitRequest`` path as non-atomic
  writes — a single ``commit()`` is already all-or-nothing for the
  writes it carries.
* :meth:`_run_writes_batched` — non-atomic writes are prepared
  serially, then committed via a single low-level V1 ``CommitRequest``.
  The ``WriteResult.update_time`` values returned by the server become
  the fresh revisions stamped back onto the caller's documents by
  :meth:`_update_revision`.
* :meth:`_run_atomic_writes` — atomic writes take the same commit path.
* :meth:`_batch_get` — serves one chunk of GET actions (dispatched
  by the base class ``_run_gets``) with a single low-level V1
  ``BatchGetDocuments`` RPC. Missing documents are silently ignored.
* :meth:`translate_error` — surface ``google.api_core.exceptions``
  types as the common :class:`SubstrateSdkException` hierarchy.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from google.api_core import exceptions as gax  # type: ignore[import-not-found]
    from google.api_core.client_options import (
        ClientOptions,  # type: ignore[import-not-found]
    )
    from google.cloud import firestore  # type: ignore[import-not-found]
    from google.cloud.firestore_v1 import (
        _helpers as fs_helpers,  # type: ignore[import-not-found]
    )
    from google.cloud.firestore_v1.services.firestore import (
        FirestoreClient as _V1FirestoreClient,  # type: ignore[import-not-found]
    )
    from google.cloud.firestore_v1.types import (
        common as fs_common,  # type: ignore[import-not-found]
    )
    from google.cloud.firestore_v1.types import document as fs_document
    from google.cloud.firestore_v1.types import firestore as fs_types
    from google.cloud.firestore_v1.types import write as fs_write
    from google.protobuf import message as fs_proto_message
except ImportError as _exc:
    raise ImportError(
        "GCP Firestore dependencies not found. Install with: "
        "pip install multicloud-py-docstore-gcp-firestore"
    ) from _exc


def _build_firestore_api(
    *,
    endpoint_url: Optional[str],
    credentials: Optional[Any],
) -> Any:
    """Construct the public low-level V1 ``FirestoreClient``.

    Handles emulator, endpoint override, and credentials so the
    low-level client targets the same backend as the high-level
    ``firestore.Client``.

    Args:
        endpoint_url: Explicit endpoint override; wrapped in
            ``ClientOptions(api_endpoint=...)`` when set.
        credentials: Pre-resolved credentials to pass through; when
            ``None`` the library falls back to application-default
            discovery (or ``AnonymousCredentials`` on the emulator path).

    Returns:
        A fresh ``google.cloud.firestore_v1.services.firestore.FirestoreClient``.
    """
    emulator_host = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if emulator_host:
        import grpc  # type: ignore[import-not-found]
        from google.auth.credentials import (  # type: ignore[import-not-found]
            AnonymousCredentials,
        )
        from google.cloud.firestore_v1.services.firestore.transports.grpc import (  # type: ignore[import-not-found]
            FirestoreGrpcTransport,
        )

        channel = grpc.insecure_channel(emulator_host)
        transport = FirestoreGrpcTransport(
            host=emulator_host,
            channel=channel,
            credentials=AnonymousCredentials(),
        )
        return _V1FirestoreClient(transport=transport)

    api_kwargs: Dict[str, Any] = {}
    if endpoint_url:
        api_kwargs["client_options"] = ClientOptions(api_endpoint=endpoint_url)
    if credentials is not None:
        api_kwargs["credentials"] = credentials
    return _V1FirestoreClient(**api_kwargs)


from multicloud.common.credentials import CredentialsOverrider
from multicloud.common.exceptions import (
    DeadlineExceededException,
    FailedPreconditionException,
    InvalidArgumentException,
    ResourceAlreadyExistsException,
    ResourceExhaustedException,
    ResourceNotFoundException,
    SubstrateSdkException,
    TransactionFailedException,
    UnAuthorizedException,
    UnknownException,
    UnsupportedOperationException,
)
from multicloud.docstore.docstore_client.driver import (
    DOCSTORE_REGISTRY,
    Action,
    ActionKind,
    BaseDocStore,
    CollectionOptions,
    Document,
    group_actions,
    wait_for_all,
)
from multicloud.gcp_common.gcp_credentials import get_credentials

_logger = logging.getLogger(__name__)

# Firestore full-path collection form:
#   projects/<project>/databases/<database>/documents/<collection>...
_FULL_COLLECTION_PATH = re.compile(r"^(projects/[^/]+/databases/[^/]+)/documents/(.+)$")


# ---------------------------------------------------------------------------
# WriteOperation — value type for a prepared Firestore write
# ---------------------------------------------------------------------------


@dataclass
class WriteOperation:
    """A prepared write ready to be committed.

    * ``action`` — the source Action (kind + document).
    * ``write_item`` — the low-level V1 ``Write`` protobuf that goes
      into a ``CommitRequest``.
    * ``new_partition_key`` — always ``None`` for Firestore. Kept on
      the dataclass for shape uniformity with the driver contract;
      Firestore rejects a CREATE with a missing partition key rather
      than synthesizing one.
    * ``new_revision`` — always ``None`` on this path. Firestore derives
      revisions from the server's ``WriteResult.update_time`` on commit
      rather than stamping them at prepare time; the field is kept for
      shape uniformity with the driver contract.
    """

    action: Action
    write_item: Any
    new_partition_key: Optional[str] = None
    new_revision: Optional[str] = None


# ---------------------------------------------------------------------------
# FirestoreDocStore
# ---------------------------------------------------------------------------


@DOCSTORE_REGISTRY.register("gcp-firestore")
class FirestoreDocStore(BaseDocStore):
    """Firestore-backed :class:`BaseDocStore`."""

    def __init__(
        self,
        collection_options: CollectionOptions,
        *,
        project: Optional[str] = None,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
        firestore_client: Any = None,
    ) -> None:
        super().__init__(
            collection_options=collection_options,
            credentials_overrider=credentials_overrider,
        )

        # If the table name is a full Firestore path
        # ``projects/<project>/databases/<database>/documents/<coll>``, extract
        # the collection id and database id so the constructed client and
        # any doc-refs address the right database.
        parsed_project, database_id, collection_path = _parse_table_name(
            collection_options.table_name, project
        )
        self._project = parsed_project
        self._database_id = database_id
        self._collection_path = collection_path

        if firestore_client is not None:
            self._firestore = firestore_client
            # Aligned with the caller's client so RPCs route to the same
            # backend. This escape hatch reads one private attribute; the
            # owned-construction path below avoids that entirely.
            self._firestore_api = firestore_client._firestore_api
            self._owns_client = False
            self._rpc_metadata = self._build_rpc_metadata()
            return

        client_kwargs: Dict[str, Any] = {}
        if self._project is not None:
            client_kwargs["project"] = self._project
        if self._database_id is not None:
            client_kwargs["database"] = self._database_id
        if endpoint_url:
            client_kwargs["client_options"] = ClientOptions(api_endpoint=endpoint_url)
        creds: Optional[Any] = None
        if credentials_overrider is not None:
            creds = get_credentials(credentials_overrider)
            if creds is not None:
                client_kwargs["credentials"] = creds

        self._firestore = firestore.Client(**client_kwargs)
        self._firestore_api = _build_firestore_api(
            endpoint_url=endpoint_url, credentials=creds
        )
        self._owns_client = True
        self._rpc_metadata = self._build_rpc_metadata()

    def _build_rpc_metadata(self) -> List[Tuple[str, str]]:
        """Build the ``google-cloud-resource-prefix`` metadata header.

        Every V1 RPC needs a resource-prefix header that names the target
        database so requests route to the correct Firestore instance.

        Returns:
            The gRPC metadata list carrying the resource-prefix header.
        """
        project = self._project or self._firestore.project
        if not project:
            raise InvalidArgumentException(
                "firestore project could not be resolved from constructor "
                "kwargs or the underlying client; set 'project' explicitly "
                "or ensure application-default credentials expose one"
            )
        return [
            (
                "google-cloud-resource-prefix",
                f"projects/{project}/databases/{self._database_id}",
            )
        ]

    # ------------------------------------------------------------------
    # Public: exception translation
    # ------------------------------------------------------------------

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate a google-api-core exception into the common hierarchy.

        Walks the cause chain up to five levels deep so a wrapped GAX
        exception is still recognized. Already-translated
        :class:`SubstrateSdkException` instances pass through unchanged.

        :param exc: The exception raised by the Firestore layer.
        :return: The equivalent :class:`SubstrateSdkException`.
        """
        if isinstance(exc, SubstrateSdkException):
            return exc

        current: Optional[BaseException] = exc
        depth = 0
        while current is not None and depth < 5:
            translated = self._map_client_error(current, exc)
            if translated is not None:
                return translated
            current = current.__cause__ or current.__context__
            depth += 1

        return UnknownException(str(exc))

    @staticmethod
    def _map_client_error(
        current: BaseException, original: Exception
    ) -> Optional[SubstrateSdkException]:
        if isinstance(current, gax.NotFound):
            return ResourceNotFoundException(str(original))
        if isinstance(current, gax.AlreadyExists):
            return ResourceAlreadyExistsException(str(original))
        if isinstance(current, gax.FailedPrecondition):
            return FailedPreconditionException(str(original))
        if isinstance(current, gax.Aborted):
            return TransactionFailedException(str(original))
        if isinstance(
            current, (gax.PermissionDenied, gax.Unauthenticated, gax.Forbidden)
        ):
            return UnAuthorizedException(str(original))
        if isinstance(
            current,
            (gax.ResourceExhausted, gax.TooManyRequests, gax.ServiceUnavailable),
        ):
            return ResourceExhaustedException(str(original))
        if isinstance(current, gax.DeadlineExceeded):
            return DeadlineExceededException(str(original))
        if isinstance(current, gax.InvalidArgument):
            return InvalidArgumentException(str(original))
        if isinstance(current, gax.OutOfRange):
            return InvalidArgumentException(str(original))
        if isinstance(current, gax.Cancelled):
            return TransactionFailedException(str(original))
        if isinstance(current, gax.MethodNotImplemented):
            return UnsupportedOperationException(str(original))
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the Firestore clients we own, then delegate upward.

        Only closes ``self._firestore`` / ``self._firestore_api`` when
        this store constructed them; caller-injected clients are not
        ours to close.
        """
        if self._owns_client:
            for client in (self._firestore, self._firestore_api):
                close_fn = getattr(client, "close", None)
                if close_fn is None:
                    continue
                try:
                    close_fn()
                except Exception:  # noqa: BLE001 — close must not raise
                    _logger.warning(
                        "Firestore client close() failed; continuing shutdown",
                        exc_info=True,
                    )
        super().close()

    # ------------------------------------------------------------------
    # Public: runActions dispatch
    # ------------------------------------------------------------------

    def run_actions(
        self,
        actions: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]] = None,
    ) -> None:
        """Execute a batch of driver-shaped actions against Firestore.

        Splits the batch into five ordered buckets (before-gets, writes,
        atomic writes, gets, after-gets). Before-gets run inline so
        writes can see their result; writes, atomic writes, and gets are
        fanned out through the store's internal thread pool and awaited
        together; after-gets run last so they can see the post-write
        state. Atomic writes commit through a single Firestore
        transaction; every other write commits through a
        :class:`WriteBatch`.

        :param actions: Actions previously shaped by
            :meth:`ActionList.run`.
        :param before_do: Optional per-action hook invoked immediately
            before each backend call.
        :raises SubstrateSdkException: Any Firestore error surfaces as a
            translated exception from :meth:`translate_error`.
        """
        before_gets, get_list, write_list, atomic_write_list, after_gets = (
            group_actions(actions)
        )

        if before_gets:
            self._run_gets(before_gets, before_do, batch_size=100)

        futures: List[Future[Any]] = []
        if write_list:
            futures.append(
                self._executor.submit(self._run_writes_batched, write_list, before_do)
            )
        if atomic_write_list:
            futures.append(
                self._executor.submit(
                    self._run_atomic_writes, atomic_write_list, before_do
                )
            )
        if get_list:
            futures.append(
                self._executor.submit(self._run_gets, get_list, before_do, 100)
            )

        try:
            wait_for_all(futures)
        except SubstrateSdkException:
            raise
        except Exception as exc:
            raise self.translate_error(exc) from exc

        if after_gets:
            self._run_gets(after_gets, before_do, batch_size=100)

    # ------------------------------------------------------------------
    # Writes — non-atomic batch commit
    # ------------------------------------------------------------------

    def _run_writes_batched(
        self,
        actions: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]],
    ) -> None:
        """Prepare each action, commit them in a single ``CommitRequest``.

        A single Firestore ``commit`` call is atomic on the server for
        the writes it carries. That's slightly stricter than the
        driver's non-atomic write contract, which is fine: strictness
        never violates a looser contract.

        :raises SubstrateSdkException: The first translated backend
            error, or a precondition-classified exception.
        """
        if not actions:
            return

        prepared: List[WriteOperation] = []
        for action in actions:
            if before_do is not None:
                before_do(lambda _cls: True)
            prepared.append(self._action_to_write(action))

        self._commit_prepared(prepared, atomic=False)

    def _run_atomic_writes(
        self,
        actions: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]],
    ) -> None:
        """Execute an atomic write group via a single ``CommitRequest``.

        Firestore commits are all-or-nothing for the writes inside a
        single ``CommitRequest``; that's the same guarantee an explicit
        transaction would give when the transaction contains no reads.
        The driver's atomic-write bucket carries only writes (before-
        and after-gets are handled outside this call), so committing
        them together suffices for true all-or-nothing semantics.

        :raises TransactionFailedException: The commit was aborted (e.g.
            contention on a document already touched by another
            transaction).
        :raises SubstrateSdkException: Any other translated backend
            error, including per-action precondition classification.
        """
        if not actions:
            return

        prepared: List[WriteOperation] = []
        for action in actions:
            if before_do is not None:
                before_do(lambda _cls: True)
            prepared.append(self._action_to_write(action))

        self._commit_prepared(prepared, atomic=True)

    def _commit_prepared(
        self,
        write_operations: Sequence[WriteOperation],
        atomic: bool,
    ) -> None:
        """Send a single ``CommitRequest`` and stamp revisions on success."""
        writes = [op.write_item for op in write_operations]
        commit_request = fs_types.CommitRequest(
            database=self._database_path(),
            writes=writes,
        )
        try:
            response = self._firestore_api.commit(
                request=commit_request,
                metadata=self._rpc_metadata,
            )
        except gax.GoogleAPICallError as exc:
            raise self._classify_write_error(
                write_operations, exc, atomic=atomic
            ) from exc

        write_results = list(response.write_results)
        self._update_revision(write_operations, write_results)

    def _update_revision(
        self,
        write_operations: Sequence[WriteOperation],
        results: Sequence[Any],
    ) -> None:
        """Stamp update_time from each WriteResult onto the caller's Document.

        A shorter results list is tolerated: the tail entries are simply
        left with their existing revision when Firestore returns fewer
        results than writes committed.
        """
        for i, op in enumerate(write_operations):
            if i >= len(results):
                break
            update_time = getattr(results[i], "update_time", None)
            if update_time is None:
                continue
            if op.action.document.has_field(self.revision_field):
                op.action.document.set_field(self.revision_field, update_time)

    def _classify_write_error(
        self,
        write_operations: Sequence[WriteOperation],
        exc: gax.GoogleAPICallError,
        atomic: bool,
    ) -> SubstrateSdkException:
        """Map a batch-commit failure to a precondition-aware exception.

        Firestore returns ``FailedPrecondition`` for both "must exist"
        and "must-not-exist" mismatches. Dispatch on the first action's
        kind — CREATE precondition failure →
        :class:`ResourceAlreadyExistsException`, non-CREATE precondition
        or bare ``NotFound`` → :class:`ResourceNotFoundException`. The
        same rule applies to atomic multi-action batches: even though
        the failing write could be any of them, the first action's kind
        is the actionable signal callers get in every other SDK.

        :class:`TransactionFailedException` is reserved for the generic
        catch-all — an atomic batch whose failure is not a precondition
        mismatch (deadline exceeded, unavailable, etc.). Non-atomic
        non-precondition failures fall through to
        :meth:`translate_error`.
        """
        is_precondition = isinstance(exc, gax.FailedPrecondition) or (
            isinstance(exc, gax.Aborted) and "conflict" in str(exc).lower()
        )
        if is_precondition or isinstance(exc, gax.AlreadyExists):
            first_kind = write_operations[0].action.kind
            if first_kind == ActionKind.ACTION_KIND_CREATE:
                return ResourceAlreadyExistsException(str(exc))
            return ResourceNotFoundException(str(exc))
        if len(write_operations) == 1 and isinstance(exc, gax.NotFound):
            return ResourceNotFoundException(str(exc))
        if atomic:
            return TransactionFailedException(
                f"Atomic write failed - all operations rolled back: {exc}"
            )
        return self.translate_error(exc)

    # ------------------------------------------------------------------
    # Gets
    # ------------------------------------------------------------------

    def _batch_get(
        self,
        gets: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]],
        start: int,
        end: int,
        consistent_read: bool = False,
    ) -> None:
        """Execute one BatchGetDocuments RPC for ``gets[start..end]``.

        Both ``start`` and ``end`` are inclusive. Builds the list of
        fully-qualified document paths and a reverse lookup from path to
        Action so each response can be dispatched back to the caller's
        Document. When the first action in the batch carries
        ``field_paths``, a ``DocumentMask`` is attached that also
        includes the collection's partition key (and sort key, if any)
        so keys are always returned. Missing documents in the response
        stream are silently ignored.

        ``consistent_read`` is accepted for driver-signature parity and
        ignored — Firestore reads are always strongly consistent.
        """
        del consistent_read
        if not gets or start > end or start < 0 or end >= len(gets):
            return

        document_paths: List[str] = []
        doc_path_to_action: Dict[str, Action] = {}
        for i in range(start, end + 1):
            action = gets[i]
            document_path = self._document_path(action.document)
            document_paths.append(document_path)
            doc_path_to_action[document_path] = action

        if before_do is not None:
            before_do(lambda _obj: True)

        try:
            request_kwargs: Dict[str, Any] = {
                "database": self._database_path(),
                "documents": document_paths,
            }
            # Add field mask if specific fields are requested.
            first = gets[start]
            if first.field_paths:
                field_paths: set[str] = {".".join(fp) for fp in first.field_paths}
                # Ensure key fields are included so responses stay dispatchable.
                field_paths.add(self._collection_options.partition_key)
                sort_key = self._collection_options.sort_key
                if sort_key:
                    field_paths.add(sort_key)
                request_kwargs["mask"] = fs_common.DocumentMask(
                    field_paths=list(field_paths)
                )

            request = fs_types.BatchGetDocumentsRequest(**request_kwargs)
            response_iter = self._firestore_api.batch_get_documents(
                request=request,
                metadata=self._rpc_metadata,
            )
            for response in response_iter:
                # ``result`` is a proto oneof of {found, missing}; only
                # ``found`` responses carry a Document to decode.
                which = fs_types.BatchGetDocumentsResponse.pb(response).WhichOneof(
                    "result"
                )
                if which != "found":
                    # Missing documents are silently ignored.
                    continue
                found_doc = response.found
                action = doc_path_to_action.get(found_doc.name)
                if action is not None:
                    # Decode document fields into the action's document.
                    self._decode_doc(found_doc, action.document)
        except SubstrateSdkException:
            raise
        except Exception as exc:
            raise self.translate_error(exc) from exc

    def _decode_doc(self, fs_doc: Any, doc: Document) -> None:
        """Decode a V1 protobuf Document into ``doc`` in place.

        Populates every field returned in the batch response and, when
        the caller's Document already carries the revision field, stamps
        the server's ``update_time`` back as the fresh revision so the
        caller can turn around and use it for optimistic writes.
        """
        decoded = fs_helpers.decode_dict(fs_doc.fields, self._firestore)
        for attr_name, attr_value in decoded.items():
            doc.set_field(attr_name, attr_value)
        if (
            self.revision_field
            and doc.has_field(self.revision_field)
            and fs_doc.update_time is not None
        ):
            doc.set_field(self.revision_field, fs_doc.update_time)

    # ------------------------------------------------------------------
    # Write preparation
    # ------------------------------------------------------------------

    def _action_to_write(self, action: Action) -> WriteOperation:
        """Dispatch on ``action.kind`` and build a :class:`WriteOperation`.

        * CREATE / PUT / REPLACE — full-document overwrite via
          :meth:`_create_put_write`; they differ only in the precondition attached.
        * DELETE — :meth:`_create_delete_write`.
        * UPDATE — :meth:`_create_update_write`, which sets an update mask so
          untouched fields survive.
        """
        if action.kind in (
            ActionKind.ACTION_KIND_CREATE,
            ActionKind.ACTION_KIND_PUT,
            ActionKind.ACTION_KIND_REPLACE,
        ):
            return self._create_put_write(action)
        if action.kind == ActionKind.ACTION_KIND_DELETE:
            return self._create_delete_write(action)
        if action.kind == ActionKind.ACTION_KIND_UPDATE:
            return self._create_update_write(action)
        raise SubstrateSdkException(
            f"Action kind {action.kind.name} not implemented on this port."
        )

    def _create_put_write(self, action: Action) -> WriteOperation:
        """Build a full-document V1 ``Write`` for CREATE / PUT / REPLACE.

        Encodes every field on the document into the V1 protobuf
        ``Document`` shape, sets that document as the write's ``update``
        payload, then attaches the kind-appropriate precondition from
        :meth:`_build_precondition`.
        """
        doc = action.document
        document_path = self._document_path(doc)
        encoded_fields = fs_helpers.encode_dict(_snapshot_doc(doc))
        fs_doc = fs_document.Document(name=document_path, fields=encoded_fields)

        write = fs_write.Write(update=fs_doc)
        precondition = self._build_precondition(action)
        if precondition is not None:
            write.current_document = precondition

        return WriteOperation(action, write_item=write)

    def _create_delete_write(self, action: Action) -> WriteOperation:
        """Build a V1 ``Write`` for DELETE.

        Sets the write's ``delete`` field to the fully-qualified
        document path and attaches the kind-appropriate precondition
        from :meth:`_build_precondition`.
        """
        doc = action.document
        write = fs_write.Write(delete=self._document_path(doc))
        precondition = self._build_precondition(action)
        if precondition is not None:
            write.current_document = precondition
        return WriteOperation(action, write_item=write)

    def _create_update_write(self, action: Action) -> WriteOperation:
        """Build a V1 ``Write`` for UPDATE with an update mask.

        The payload document carries only the fields being modified;
        the update mask lists every path in ``mods`` so Firestore
        leaves untouched fields alone. A ``None`` value in ``mods``
        becomes a mask-only entry (no field on the payload document)
        which Firestore interprets as a field delete.
        """
        doc = action.document
        mods = action.mods or {}

        # An update with an empty mask is degenerate; fall back to a
        # full-document PUT so at least the caller-supplied fields land.
        if not mods:
            return self._create_put_write(action)

        fields: Dict[str, Any] = {}
        field_paths: List[str] = []
        for field_path, value in mods.items():
            field_paths.append(field_path)
            if value is not None:
                fields[field_path] = value

        encoded_fields = fs_helpers.encode_dict(fields)
        fs_doc = fs_document.Document(
            name=self._document_path(doc),
            fields=encoded_fields,
        )
        update_mask = fs_common.DocumentMask(field_paths=field_paths)

        write = fs_write.Write(update=fs_doc, update_mask=update_mask)
        precondition = self._build_precondition(action)
        if precondition is not None:
            write.current_document = precondition

        return WriteOperation(action, write_item=write)

    def _build_precondition(self, action: Action) -> Optional[Any]:
        """Return the V1 ``Precondition`` for a write, or ``None``.

        The precondition is selected from the action kind × has-revision
        matrix:

        * CREATE → ``exists=False`` — the document must not already exist.
        * REPLACE / UPDATE with revision →
          ``update_time = <revision>`` — the document must exist and
          its stored update-time must match.
        * REPLACE / UPDATE without revision → ``exists=True`` — the
          document must exist.
        * PUT / DELETE with revision → ``update_time = <revision>``.
        * PUT / DELETE without revision → ``None`` — unconditional.
        * GET → ``None``.
        """
        doc = action.document
        kind = action.kind
        if kind == ActionKind.ACTION_KIND_CREATE:
            return fs_common.Precondition(exists=False)
        if kind in (
            ActionKind.ACTION_KIND_REPLACE,
            ActionKind.ACTION_KIND_UPDATE,
        ):
            revision_precondition = self._build_revision_precondition(doc)
            if revision_precondition is not None:
                return revision_precondition
            return fs_common.Precondition(exists=True)
        if kind in (ActionKind.ACTION_KIND_PUT, ActionKind.ACTION_KIND_DELETE):
            return self._build_revision_precondition(doc)
        return None

    def _build_revision_precondition(self, doc: Document) -> Optional[Any]:
        """Return an ``update_time`` precondition when the doc carries one.

        Only builds a precondition when the revision field is populated
        with a value the underlying protobuf ``Timestamp`` will accept —
        ``DatetimeWithNanoseconds`` from a prior GET or a raw
        ``google.protobuf.Timestamp``.

        A caller-supplied value that protobuf can't marshal (raw string,
        wrong numeric range, unencodable proto) is treated as a stale
        revision and rejected up front with
        :class:`FailedPreconditionException`. This is a deliberately
        strict, fail-fast choice: silently dropping the precondition for
        a malformed revision would degrade a conditional write into an
        unconditional overwrite/delete and lose optimistic-concurrency
        protection without any signal to the caller. Surfacing the bad
        revision loudly lets the caller see and fix the concurrency
        break.

        The exception filter covers the full raise set of the proto-plus
        marshaling path: ``TypeError`` (wrong type),
        ``ValueError``/``OverflowError`` (out-of-range values), and the
        protobuf base class ``google.protobuf.message.Error`` (encode /
        decode failures, e.g. ``EncodeError``). Anything else is a
        genuine bug and propagates.
        """
        revision = self._read_revision(doc)
        if revision is None:
            return None
        try:
            return fs_common.Precondition(update_time=revision)
        except (
            TypeError,
            ValueError,
            OverflowError,
            fs_proto_message.Error,
        ) as exc:
            raise FailedPreconditionException(
                f"invalid {self.revision_field!r} revision "
                f"({type(revision).__name__}): {exc}"
            ) from exc

    def _read_revision(self, doc: Document) -> Optional[Any]:
        """Return the revision value on ``doc`` if present, else ``None``.

        Single entry point for pulling the caller-supplied revision off
        a Document so precondition builders don't duplicate the
        has-field + read pair.
        """
        if not doc.has_field(self.revision_field):
            return None
        return doc.get_field(self.revision_field)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _database_path(self) -> str:
        """Return ``projects/<p>/databases/<db>`` for this store."""
        project = self._project or self._firestore.project
        database = self._database_id or "(default)"
        return f"projects/{project}/databases/{database}"

    def _document_path(self, document: Document) -> str:
        """Return the fully-qualified V1 document path for ``document``."""
        return (
            f"{self._database_path()}/documents/{self._collection_path}/"
            f"{self._document_id_for(document)}"
        )

    def _doc_ref_for(self, document: Document) -> Any:
        """Resolve a Firestore ``DocumentReference`` for ``document``.

        Used by the GET path, which stays on the high-level client
        because ``DocumentReference.get(field_paths=...)`` already
        exposes everything we need. ``Client.document`` accepts a
        slash-separated path so a subcollection ``collection_path``
        resolves to the intended nested doc rather than a same-named
        top-level collection.
        """
        return self._firestore.document(
            f"{self._collection_path}/{self._document_id_for(document)}"
        )

    def get_key(self, document: Document) -> str:
        """Return the Firestore document id for ``document``.

        Overrides :meth:`BaseDocStore.get_key` to enforce Firestore's
        contract that the partition-key field must be present and
        non-empty — Firestore never synthesizes a document id. A
        missing or empty partition key is rejected up front so a
        CREATE can't reach the commit path with an invalid key.
        """
        options = self._collection_options
        pk_val = document.get_field(options.partition_key)
        if pk_val is None or (isinstance(pk_val, str) and pk_val == ""):
            raise InvalidArgumentException(
                f"partition key {options.partition_key!r} cannot be null "
                f"or empty for Firestore"
            )
        return self._document_id_for(document)

    def _document_id_for(self, document: Document) -> str:
        """Build the Firestore document id from a Document's primary key.

        Composite keys concatenate ``partition_key`` and ``sort_key`` with
        a ``:`` separator. Each component is encoded so distinct
        ``(type, value)`` pairs map to distinct ids (see
        :func:`_encode_component`).
        """
        options = self._collection_options
        pk_val = document.get_field(options.partition_key)
        if pk_val is None:
            raise InvalidArgumentException(
                f"missing partition key {options.partition_key!r}"
            )
        pk_str = _encode_component(pk_val)
        sk_name = options.sort_key
        if not sk_name:
            return pk_str
        sk_val = document.get_field(sk_name)
        if sk_val is None:
            raise InvalidArgumentException(f"missing sort key {sk_name!r}")
        return f"{pk_str}:{_encode_component(sk_val)}"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _encode_component(component: Any) -> str:
    """Encode one key component into a reversible, type-preserving fragment.

    Document ids concatenate components with ``:``. To keep distinct
    SDK-level keys mapping to distinct Firestore document ids the
    encoder is injective across ``(type, value)`` pairs:

    * A one-character type tag prevents ``1`` (int) from aliasing
      ``"1"`` (str).
    * ``%`` is escaped FIRST so the ``%XX`` sequences emitted for
      ``/`` and ``:`` cannot be forged by a literal in the input --
      otherwise ``a/b`` would collide with the literal ``a%2Fb``.
    * ``:`` is escaped so composite ids parse back unambiguously --
      otherwise ``("a:b","c")`` and ``("a","b:c")`` would both
      encode to ``a:b:c``.
    """
    if isinstance(component, bool):
        tag, raw = "b", "1" if component else "0"
    elif isinstance(component, int):
        tag, raw = "i", str(component)
    elif isinstance(component, str):
        tag, raw = "s", component
    else:
        raise InvalidArgumentException(
            f"unsupported key component type: {type(component).__name__}"
        )
    escaped = raw.replace("%", "%25").replace("/", "%2F").replace(":", "%3A")
    return f"{tag}{escaped}"


def _parse_table_name(
    table_name: str, project_hint: Optional[str]
) -> tuple[Optional[str], Optional[str], str]:
    """Return ``(project, database, collection_path)`` from a table-name option.

    ``table_name`` must be a full Firestore path — top-level
    (``projects/<p>/databases/<db>/documents/<coll>``) or nested
    (``projects/<p>/databases/<db>/documents/<coll>/<doc>/<subColl>``).
    The project + database are extracted and the
    ``<coll>[/<doc>/<subColl>]...`` tail is preserved verbatim so
    subcollections address the intended document, not a same-named
    top-level collection.

    A bare collection id (e.g. ``"orders"``) is rejected with
    :class:`InvalidArgumentException`. Silently inferring project +
    database from ambient client defaults would mask configuration
    mistakes — a typo or missing env var would silently retarget writes
    at whatever project the environment happened to pick up. Requiring
    the full path forces the caller to be explicit and keeps the same
    ``table_name`` portable across language SDKs.
    """
    match = _FULL_COLLECTION_PATH.match(table_name)
    if match is None:
        raise InvalidArgumentException(
            f"table_name must be a full Firestore path "
            f"'projects/<p>/databases/<db>/documents/<coll>[/<doc>/<sub>...]', "
            f"got {table_name!r}"
        )
    # Everything after ``documents/`` is the collection path — for a
    # subcollection this includes intermediate ``<coll>/<doc>/`` segments
    # that must be preserved, not collapsed to the leaf.
    collection_path = match.group(2)
    prefix = match.group(1).split("/")
    # prefix == ["projects", "<p>", "databases", "<db>"]
    parsed_project = prefix[1] if len(prefix) >= 2 else project_hint
    database_id = prefix[3] if len(prefix) >= 4 else None
    return parsed_project, database_id, collection_path


def _snapshot_doc(doc: Document) -> Dict[str, Any]:
    """Snapshot every field on ``doc`` into a plain dict.

    Handed to :func:`google.cloud.firestore_v1._helpers.encode_dict` to
    build the V1 protobuf ``Value``-per-field payload used inside a
    ``Document`` write.
    """
    return {name: doc.get_field(name) for name in doc.field_names()}
