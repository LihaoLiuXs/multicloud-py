"""Unit tests for the GCP Firestore implementation of the docstore driver.

Every test injects a ``MagicMock`` as the Firestore client via the
``firestore_client`` kwarg on :class:`FirestoreDocStore`, so no real
Firestore session, credentials, or network round-trip is involved.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest
from google.api_core import exceptions as gax
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.cloud.firestore_v1 import _helpers as fs_helpers
from google.cloud.firestore_v1.types import common as fs_common
from google.cloud.firestore_v1.types import document as fs_document
from google.cloud.firestore_v1.types import firestore as fs_types
from google.cloud.firestore_v1.types import write as fs_write
from google.protobuf import timestamp_pb2
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
    Action,
    ActionKind,
    CollectionOptions,
    Document,
)
from multicloud.docstore.docstore_gcp_firestore.firestore_docstore import (
    FirestoreDocStore,
    WriteOperation,
    _encode_component,
    _parse_table_name,
    _snapshot_doc,
)


def _make_store(
    *,
    table_name: str = "projects/test-project/databases/(default)/documents/coll",
    partition_key: str = "pk",
    sort_key=None,
    revision_field=None,
    max_outstanding_action_rpcs: int = 0,
    project: str = "test-project",
    database: str = "(default)",
    firestore_client=None,
) -> tuple[FirestoreDocStore, MagicMock]:
    """Build a FirestoreDocStore backed by a MagicMock firestore client."""
    client = firestore_client if firestore_client is not None else MagicMock()
    client.project = project
    client._database = database
    store = FirestoreDocStore(
        CollectionOptions(
            table_name=table_name,
            partition_key=partition_key,
            sort_key=sort_key,
            revision_field=revision_field,
            max_outstanding_action_rpcs=max_outstanding_action_rpcs,
        ),
        firestore_client=client,
    )
    return store, client


def _server_timestamp():
    """Return a DatetimeWithNanoseconds instance for update_time results."""
    return DatetimeWithNanoseconds(2024, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _patch_low_level_firestore_factory():
    """Prevent owned-path construction from reaching a real V1 client.

    Owned-path tests mock the high-level ``firestore.Client`` but the
    store now also constructs a low-level ``FirestoreClient`` — which
    would attempt real credential discovery without this patch. Injected
    tests skip the owned path entirely and are unaffected.
    """
    with patch(
        "multicloud.docstore.docstore_gcp_firestore.firestore_docstore._build_firestore_api"
    ) as mock_builder:
        mock_builder.return_value = MagicMock()
        yield mock_builder


class TestConstruction:

    def test_injected_client_used_directly(self):
        store, client = _make_store()
        assert store._firestore is client
        assert store._owns_client is False
        store.close()

    def test_plain_collection_name_rejected(self):
        # Bare collection ids must be rejected at construction. Silently
        # inferring project + database from ambient client defaults would
        # mask a caller misconfiguration (typo, missing env var) and let
        # writes silently retarget whichever project the environment
        # happened to pick up.
        client = MagicMock()
        client.project = "hint-project"
        client._database = "(default)"
        with pytest.raises(InvalidArgumentException, match="full Firestore path"):
            FirestoreDocStore(
                CollectionOptions(table_name="orders", partition_key="pk"),
                project="hint-project",
                firestore_client=client,
            )

    def test_full_path_table_name_parsed(self):
        client = MagicMock()
        client.project = "unused"
        client._database = "unused-db"
        store = FirestoreDocStore(
            CollectionOptions(
                table_name="projects/pp/databases/dd/documents/orders",
                partition_key="pk",
            ),
            firestore_client=client,
        )
        assert store._project == "pp"
        assert store._database_id == "dd"
        assert store._collection_path == "orders"
        store.close()

    def test_subcollection_full_path_preserves_intermediate_segments(self):
        # A nested table_name (subcollection) must keep the whole tail
        # after ``documents/``; collapsing to the leaf would target a
        # same-named top-level collection instead.
        client = MagicMock()
        client.project = "unused"
        client._database = "unused-db"
        store = FirestoreDocStore(
            CollectionOptions(
                table_name=(
                    "projects/pp/databases/dd/documents/orders/order-1/lineItems"
                ),
                partition_key="pk",
            ),
            firestore_client=client,
        )
        assert store._project == "pp"
        assert store._database_id == "dd"
        assert store._collection_path == "orders/order-1/lineItems"
        store.close()

    @patch("multicloud.docstore.docstore_gcp_firestore.firestore_docstore.firestore")
    def test_no_injected_client_creates_firestore_client(self, mock_firestore):
        mock_firestore.Client.return_value = MagicMock()
        store = FirestoreDocStore(
            CollectionOptions(
                table_name="projects/p/databases/(default)/documents/c",
                partition_key="pk",
            ),
            project="p",
        )
        mock_firestore.Client.assert_called_once()
        kwargs = mock_firestore.Client.call_args.kwargs
        assert kwargs["project"] == "p"
        assert store._owns_client is True
        store.close()

    @patch("multicloud.docstore.docstore_gcp_firestore.firestore_docstore.firestore")
    @patch(
        "multicloud.docstore.docstore_gcp_firestore.firestore_docstore.ClientOptions"
    )
    def test_endpoint_url_wraps_in_client_options(
        self, mock_client_options, mock_firestore
    ):
        mock_firestore.Client.return_value = MagicMock()
        FirestoreDocStore(
            CollectionOptions(
                table_name="projects/p/databases/(default)/documents/c",
                partition_key="pk",
            ),
            project="p",
            endpoint_url="https://example.invalid",
        )
        mock_client_options.assert_called_once_with(
            api_endpoint="https://example.invalid"
        )
        assert "client_options" in mock_firestore.Client.call_args.kwargs

    @patch("multicloud.docstore.docstore_gcp_firestore.firestore_docstore.firestore")
    @patch(
        "multicloud.docstore.docstore_gcp_firestore.firestore_docstore.get_credentials"
    )
    def test_credentials_overrider_forwards_credentials(
        self, mock_get_credentials, mock_firestore
    ):
        creds_stub = object()
        mock_get_credentials.return_value = creds_stub
        overrider = lambda: None
        FirestoreDocStore(
            CollectionOptions(
                table_name="projects/p/databases/(default)/documents/c",
                partition_key="pk",
            ),
            credentials_overrider=overrider,
        )
        mock_get_credentials.assert_called_once_with(overrider)
        assert mock_firestore.Client.call_args.kwargs["credentials"] is creds_stub

    @patch("multicloud.docstore.docstore_gcp_firestore.firestore_docstore.firestore")
    @patch(
        "multicloud.docstore.docstore_gcp_firestore.firestore_docstore.get_credentials"
    )
    def test_credentials_overrider_returning_none_skips_kwarg(
        self, mock_get_credentials, mock_firestore
    ):
        mock_get_credentials.return_value = None
        FirestoreDocStore(
            CollectionOptions(
                table_name="projects/p/databases/(default)/documents/c",
                partition_key="pk",
            ),
            credentials_overrider=lambda: None,
        )
        assert "credentials" not in mock_firestore.Client.call_args.kwargs

    @patch("multicloud.docstore.docstore_gcp_firestore.firestore_docstore.firestore")
    def test_full_path_forwards_database_kwarg(self, mock_firestore):
        FirestoreDocStore(
            CollectionOptions(
                table_name="projects/pp/databases/dd/documents/coll",
                partition_key="pk",
            ),
        )
        kwargs = mock_firestore.Client.call_args.kwargs
        assert kwargs["project"] == "pp"
        assert kwargs["database"] == "dd"


class TestClose:

    def test_close_does_not_touch_injected_client(self):
        store, client = _make_store()
        store.close()
        client.close.assert_not_called()

    @patch("multicloud.docstore.docstore_gcp_firestore.firestore_docstore.firestore")
    def test_close_closes_owned_client(self, mock_firestore):
        owned = MagicMock()
        mock_firestore.Client.return_value = owned
        store = FirestoreDocStore(
            CollectionOptions(
                table_name="projects/p/databases/(default)/documents/c",
                partition_key="pk",
            ),
        )
        store.close()
        owned.close.assert_called_once()

    @patch("multicloud.docstore.docstore_gcp_firestore.firestore_docstore.firestore")
    def test_close_swallows_close_errors_but_logs(self, mock_firestore, caplog):
        # close() must not raise even when the underlying client's close
        # blows up — but the failure must be logged so silent connection /
        # credential leaks are diagnosable.
        owned = MagicMock()
        owned.close.side_effect = RuntimeError("closed twice")
        mock_firestore.Client.return_value = owned
        store = FirestoreDocStore(
            CollectionOptions(
                table_name="projects/p/databases/(default)/documents/c",
                partition_key="pk",
            ),
        )
        with caplog.at_level(
            "WARNING",
            logger="multicloud.docstore.docstore_gcp_firestore.firestore_docstore",
        ):
            store.close()
        assert any(
            "close() failed" in rec.getMessage() and rec.levelname == "WARNING"
            for rec in caplog.records
        )

    @patch("multicloud.docstore.docstore_gcp_firestore.firestore_docstore.firestore")
    def test_close_when_owned_client_has_no_close(self, mock_firestore):
        owned = MagicMock(spec=[])  # no ``close`` attribute
        mock_firestore.Client.return_value = owned
        store = FirestoreDocStore(
            CollectionOptions(
                table_name="projects/p/databases/(default)/documents/c",
                partition_key="pk",
            ),
        )
        store.close()  # must not raise

    def test_close_marks_store_closed(self):
        store, _ = _make_store()
        store.close()
        with pytest.raises(InvalidArgumentException):
            store.check_closed()


class TestTranslateError:

    def test_pass_through_substrate_error(self):
        store, _ = _make_store()
        err = ResourceNotFoundException("nope")
        assert store.translate_error(err) is err

    def test_not_found_maps_to_resource_not_found(self):
        store, _ = _make_store()
        result = store.translate_error(gax.NotFound("missing"))
        assert isinstance(result, ResourceNotFoundException)

    def test_already_exists_maps_to_resource_already_exists(self):
        store, _ = _make_store()
        result = store.translate_error(gax.AlreadyExists("dup"))
        assert isinstance(result, ResourceAlreadyExistsException)

    def test_failed_precondition_maps_to_failed_precondition(self):
        store, _ = _make_store()
        result = store.translate_error(gax.FailedPrecondition("nope"))
        assert isinstance(result, FailedPreconditionException)

    def test_aborted_maps_to_transaction_failed(self):
        store, _ = _make_store()
        result = store.translate_error(gax.Aborted("conflict"))
        assert isinstance(result, TransactionFailedException)

    def test_permission_denied_maps_to_unauthorized(self):
        store, _ = _make_store()
        result = store.translate_error(gax.PermissionDenied("no"))
        assert isinstance(result, UnAuthorizedException)

    def test_unauthenticated_maps_to_unauthorized(self):
        store, _ = _make_store()
        result = store.translate_error(gax.Unauthenticated("no"))
        assert isinstance(result, UnAuthorizedException)

    def test_forbidden_maps_to_unauthorized(self):
        store, _ = _make_store()
        result = store.translate_error(gax.Forbidden("no"))
        assert isinstance(result, UnAuthorizedException)

    def test_resource_exhausted_maps_to_resource_exhausted(self):
        store, _ = _make_store()
        result = store.translate_error(gax.ResourceExhausted("slow"))
        assert isinstance(result, ResourceExhaustedException)

    def test_too_many_requests_maps_to_resource_exhausted(self):
        store, _ = _make_store()
        result = store.translate_error(gax.TooManyRequests("slow"))
        assert isinstance(result, ResourceExhaustedException)

    def test_service_unavailable_maps_to_resource_exhausted(self):
        store, _ = _make_store()
        result = store.translate_error(gax.ServiceUnavailable("backend down"))
        assert isinstance(result, ResourceExhaustedException)
        assert "backend down" in str(result)

    def test_deadline_exceeded_maps_to_deadline_exceeded(self):
        store, _ = _make_store()
        result = store.translate_error(gax.DeadlineExceeded("slow"))
        assert isinstance(result, DeadlineExceededException)

    def test_invalid_argument_maps_to_invalid_argument(self):
        store, _ = _make_store()
        result = store.translate_error(gax.InvalidArgument("bad"))
        assert isinstance(result, InvalidArgumentException)

    def test_out_of_range_maps_to_invalid_argument(self):
        store, _ = _make_store()
        result = store.translate_error(gax.OutOfRange("bad range"))
        assert isinstance(result, InvalidArgumentException)

    def test_cancelled_maps_to_transaction_failed(self):
        store, _ = _make_store()
        result = store.translate_error(gax.Cancelled("cancelled by caller"))
        assert isinstance(result, TransactionFailedException)

    def test_method_not_implemented_maps_to_unsupported(self):
        store, _ = _make_store()
        result = store.translate_error(gax.MethodNotImplemented("nope"))
        assert isinstance(result, UnsupportedOperationException)

    def test_unknown_error_falls_back_to_unknown(self):
        store, _ = _make_store()
        result = store.translate_error(RuntimeError("no idea"))
        assert isinstance(result, UnknownException)

    def test_wrapped_error_via_cause_still_classified(self):
        store, _ = _make_store()
        inner = gax.NotFound("missing")
        wrapper = RuntimeError("outer")
        wrapper.__cause__ = inner
        result = store.translate_error(wrapper)
        assert isinstance(result, ResourceNotFoundException)

    def test_wrapped_error_via_context_still_classified(self):
        store, _ = _make_store()
        inner = gax.AlreadyExists("dup")
        wrapper = RuntimeError("outer")
        wrapper.__context__ = inner
        result = store.translate_error(wrapper)
        assert isinstance(result, ResourceAlreadyExistsException)

    def test_deep_cause_chain_bounded_at_five(self):
        # Chain deeper than 5 levels — buried GAX exception should NOT be
        # recognized; fallback to UnknownException.
        store, _ = _make_store()
        deep = gax.NotFound("deep")
        chain = deep
        for _ in range(6):
            wrap = RuntimeError("outer")
            wrap.__cause__ = chain
            chain = wrap
        result = store.translate_error(chain)
        assert isinstance(result, UnknownException)


class TestReadRevision:

    def test_none_when_missing(self):
        store, _ = _make_store()
        assert store._read_revision(Document({})) is None

    def test_returns_present_value(self):
        store, _ = _make_store()
        rev = _server_timestamp()
        doc = Document({store.revision_field: rev})
        assert store._read_revision(doc) is rev


class TestBuildRevisionPrecondition:

    def test_none_when_no_revision(self):
        store, _ = _make_store()
        assert store._build_revision_precondition(Document({})) is None

    def test_valid_timestamp_returns_precondition(self):
        store, _ = _make_store()
        rev = _server_timestamp()
        doc = Document({store.revision_field: rev})
        result = store._build_revision_precondition(doc)
        assert result is not None
        # proto-plus surfaces update_time as a Python datetime.
        assert int(result.update_time.timestamp()) == int(rev.timestamp())

    def test_invalid_revision_raises_failed_precondition(self):
        store, _ = _make_store()
        # A raw string is not marshallable to protobuf Timestamp.
        doc = Document({store.revision_field: "not-a-timestamp"})
        with pytest.raises(FailedPreconditionException, match="invalid"):
            store._build_revision_precondition(doc)

    def test_overflow_error_maps_to_failed_precondition(self):
        # Proto-plus raises OverflowError for datetimes past the year-9999
        # protobuf ceiling. That must land on FailedPrecondition, not
        # escape as an untranslated OverflowError.
        store, _ = _make_store()
        doc = Document({store.revision_field: _server_timestamp()})
        with patch(
            "multicloud.docstore.docstore_gcp_firestore."
            "firestore_docstore.fs_common.Precondition",
            side_effect=OverflowError("timestamp out of range"),
        ):
            with pytest.raises(FailedPreconditionException, match="invalid"):
                store._build_revision_precondition(doc)

    def test_protobuf_encode_error_maps_to_failed_precondition(self):
        # The underlying protobuf layer can raise google.protobuf.message.
        # EncodeError (subclass of message.Error). That must also land on
        # FailedPrecondition, not escape and be reclassified as Unknown by
        # the outer client-layer catch.
        from google.protobuf import message as proto_msg

        store, _ = _make_store()
        doc = Document({store.revision_field: _server_timestamp()})
        with patch(
            "multicloud.docstore.docstore_gcp_firestore."
            "firestore_docstore.fs_common.Precondition",
            side_effect=proto_msg.EncodeError("cannot encode"),
        ):
            with pytest.raises(FailedPreconditionException, match="invalid"):
                store._build_revision_precondition(doc)


class TestBuildPrecondition:

    def _action(self, kind, doc):
        return Action(kind=kind, document=Document(doc))

    def test_create_exists_false(self):
        store, _ = _make_store()
        action = self._action(ActionKind.ACTION_KIND_CREATE, {"pk": "k"})
        result = store._build_precondition(action)
        # Precondition serializes as a proto; `exists=False` is the field.
        assert result.exists is False

    def test_replace_without_revision_exists_true(self):
        store, _ = _make_store()
        action = self._action(ActionKind.ACTION_KIND_REPLACE, {"pk": "k"})
        result = store._build_precondition(action)
        assert result.exists is True

    def test_replace_with_revision_uses_update_time(self):
        store, _ = _make_store()
        rev = _server_timestamp()
        action = self._action(
            ActionKind.ACTION_KIND_REPLACE,
            {"pk": "k", store.revision_field: rev},
        )
        result = store._build_precondition(action)
        # Not exists=True; update_time is set instead
        assert int(result.update_time.timestamp()) == int(rev.timestamp())

    def test_update_without_revision_exists_true(self):
        store, _ = _make_store()
        action = self._action(ActionKind.ACTION_KIND_UPDATE, {"pk": "k"})
        result = store._build_precondition(action)
        assert result.exists is True

    def test_update_with_revision_uses_update_time(self):
        store, _ = _make_store()
        rev = _server_timestamp()
        action = self._action(
            ActionKind.ACTION_KIND_UPDATE,
            {"pk": "k", store.revision_field: rev},
        )
        result = store._build_precondition(action)
        assert int(result.update_time.timestamp()) == int(rev.timestamp())

    def test_put_without_revision_is_none(self):
        store, _ = _make_store()
        action = self._action(ActionKind.ACTION_KIND_PUT, {"pk": "k"})
        assert store._build_precondition(action) is None

    def test_put_with_revision_uses_update_time(self):
        store, _ = _make_store()
        rev = _server_timestamp()
        action = self._action(
            ActionKind.ACTION_KIND_PUT,
            {"pk": "k", store.revision_field: rev},
        )
        result = store._build_precondition(action)
        assert int(result.update_time.timestamp()) == int(rev.timestamp())

    def test_delete_without_revision_is_none(self):
        store, _ = _make_store()
        action = self._action(ActionKind.ACTION_KIND_DELETE, {"pk": "k"})
        assert store._build_precondition(action) is None

    def test_delete_with_revision_uses_update_time(self):
        store, _ = _make_store()
        rev = _server_timestamp()
        action = self._action(
            ActionKind.ACTION_KIND_DELETE,
            {"pk": "k", store.revision_field: rev},
        )
        result = store._build_precondition(action)
        assert int(result.update_time.timestamp()) == int(rev.timestamp())

    def test_get_returns_none(self):
        store, _ = _make_store()
        action = self._action(ActionKind.ACTION_KIND_GET, {"pk": "k"})
        assert store._build_precondition(action) is None


class TestPathHelpers:

    def test_database_path_from_stored_project_and_database(self):
        store, _ = _make_store(project="p1", database="db1")
        # When ``_project`` and ``_database_id`` are set explicitly (via
        # full-path table_name) they win.
        store._project = "p1"
        store._database_id = "db1"
        assert store._database_path() == "projects/p1/databases/db1"

    def test_database_path_falls_back_to_defaults(self):
        store, client = _make_store(project="from-client")
        # Missing ``_project`` falls back to the client's public
        # ``.project`` and missing ``_database_id`` falls back to
        # Firestore's ``(default)`` database id.
        store._project = None
        store._database_id = None
        client.project = "from-client"
        assert store._database_path() == "projects/from-client/databases/(default)"

    def test_document_path_includes_collection_and_id(self):
        store, _ = _make_store(
            table_name="projects/p/databases/db/documents/coll",
        )
        doc = Document({"pk": "id123"})
        assert (
            store._document_path(doc) == "projects/p/databases/db/documents/coll/sid123"
        )

    def test_document_path_preserves_subcollection_segments(self):
        # For a subcollection table_name, the V1 doc path must include
        # the intermediate ``<coll>/<doc>/`` segments verbatim so the
        # write lands in the nested collection, not a same-named
        # top-level one.
        store, _ = _make_store(
            table_name="projects/pp/databases/dd/documents/orders/order-1/lineItems",
        )
        doc = Document({"pk": "line-42"})
        assert store._document_path(doc) == (
            "projects/pp/databases/dd/documents/orders/order-1/lineItems/sline-42"
        )

    def test_document_id_missing_pk_raises(self):
        store, _ = _make_store()
        with pytest.raises(InvalidArgumentException, match="missing partition key"):
            store._document_id_for(Document({}))

    def test_document_id_partition_only(self):
        store, _ = _make_store()
        assert store._document_id_for(Document({"pk": "abc"})) == "sabc"

    def test_document_id_composite_key(self):
        store, _ = _make_store(sort_key="sk")
        assert store._document_id_for(Document({"pk": "a", "sk": "b"})) == "sa:sb"

    def test_document_id_composite_missing_sk_raises(self):
        store, _ = _make_store(sort_key="sk")
        with pytest.raises(InvalidArgumentException, match="missing sort key"):
            store._document_id_for(Document({"pk": "a"}))

    def test_document_id_escapes_slashes_in_partition_key(self):
        store, _ = _make_store()
        assert store._document_id_for(Document({"pk": "a/b"})) == "sa%2Fb"

    def test_document_id_escapes_slashes_in_sort_key(self):
        store, _ = _make_store(sort_key="sk")
        result = store._document_id_for(Document({"pk": "a", "sk": "b/c"}))
        assert result == "sa:sb%2Fc"

    def test_doc_ref_uses_collection_and_document_id(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        doc = Document({"pk": "abc"})
        store._doc_ref_for(doc)
        client.document.assert_called_once_with(f"{store._collection_path}/sabc")

    def test_doc_ref_for_subcollection_uses_full_path(self):
        # A subcollection table_name must resolve to the nested doc,
        # not to a same-named top-level collection. ``Client.document``
        # takes the slash-separated path verbatim.
        store, client = _make_store(
            table_name="projects/pp/databases/dd/documents/orders/order-1/lineItems",
        )
        store._doc_ref_for(Document({"pk": "line-42"}))
        client.document.assert_called_once_with("orders/order-1/lineItems/sline-42")


class TestDocumentIdInjectivity:
    """Regressions for the three collision families flagged in PR review.

    The prior encoder was non-injective: distinct SDK-level keys
    produced the same Firestore document id and could therefore
    overwrite or delete each other's data.
    """

    def test_real_slash_and_literal_percent_2f_produce_distinct_ids(self):
        # ``a/b`` vs the literal string ``a%2Fb``: without escaping ``%``
        # first, both collapsed to ``a%2Fb``.
        store, _ = _make_store()
        real_slash = store._document_id_for(Document({"pk": "a/b"}))
        literal_pct = store._document_id_for(Document({"pk": "a%2Fb"}))
        assert real_slash != literal_pct

    def test_composite_key_boundary_is_unambiguous(self):
        # ``(pk="a:b", sk="c")`` vs ``(pk="a", sk="b:c")``: without
        # escaping ``:`` inside each component, both collapsed to
        # ``a:b:c``.
        store, _ = _make_store(sort_key="sk")
        left = store._document_id_for(Document({"pk": "a:b", "sk": "c"}))
        right = store._document_id_for(Document({"pk": "a", "sk": "b:c"}))
        assert left != right

    def test_int_pk_and_string_pk_of_same_digits_are_distinct(self):
        # int ``1`` vs str ``"1"``: without a type tag, ``str()``
        # coalesced both to ``"1"``.
        store, _ = _make_store()
        as_int = store._document_id_for(Document({"pk": 1}))
        as_str = store._document_id_for(Document({"pk": "1"}))
        assert as_int != as_str


class TestActionToWrite:

    def test_create_dispatches_to_put_with_must_not_exist_precondition(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_CREATE,
            document=Document({"pk": "k", "payload": "v"}),
        )
        op = store._action_to_write(action)
        # CREATE → full-document update carrying an `exists=False` precondition
        assert op.write_item.update.name.endswith("/coll/sk")
        assert op.write_item.delete == ""
        # Every input field must land on the payload document, not just the key.
        fields = op.write_item.update.fields
        assert set(fields.keys()) == {"pk", "payload"}
        assert fields["pk"].string_value == "k"
        assert fields["payload"].string_value == "v"
        assert (
            op.write_item.current_document._pb.WhichOneof("condition_type") == "exists"
        )
        assert op.write_item.current_document.exists is False

    def test_put_dispatches_to_put_without_precondition(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_PUT,
            document=Document({"pk": "k", "payload": "v"}),
        )
        op = store._action_to_write(action)
        # PUT (no revision) → full-document update, unconditional
        assert op.write_item.update.name.endswith("/coll/sk")
        assert op.write_item.delete == ""
        fields = op.write_item.update.fields
        assert set(fields.keys()) == {"pk", "payload"}
        assert fields["pk"].string_value == "k"
        assert fields["payload"].string_value == "v"
        assert op.write_item.current_document._pb.WhichOneof("condition_type") is None

    def test_replace_dispatches_to_put_with_must_exist_precondition(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_REPLACE,
            document=Document({"pk": "k", "payload": "v"}),
        )
        op = store._action_to_write(action)
        # REPLACE (no revision) → update carrying an `exists=True` precondition
        assert op.write_item.update.name.endswith("/coll/sk")
        assert op.write_item.delete == ""
        fields = op.write_item.update.fields
        assert set(fields.keys()) == {"pk", "payload"}
        assert fields["pk"].string_value == "k"
        assert fields["payload"].string_value == "v"
        assert (
            op.write_item.current_document._pb.WhichOneof("condition_type") == "exists"
        )
        assert op.write_item.current_document.exists is True

    def test_delete_dispatches_to_delete(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_DELETE, document=Document({"pk": "k"})
        )
        op = store._action_to_write(action)
        # DELETE → `delete` set to full document path, no update payload
        assert op.write_item.delete.endswith("/coll/sk")
        assert op.write_item.update.name == ""

    def test_update_dispatches_to_update_with_mask(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_UPDATE,
            document=Document({"pk": "k"}),
            mods={"field": "value"},
        )
        op = store._action_to_write(action)
        # UPDATE with mods → update payload + update_mask lists modified paths
        assert op.write_item.update.name.endswith("/coll/sk")
        assert list(op.write_item.update_mask.field_paths) == ["field"]

    def test_unsupported_kind_raises(self):
        store, _ = _make_store()
        action = Action(kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k"}))
        with pytest.raises(SubstrateSdkException, match="not implemented"):
            store._action_to_write(action)


class TestCreatePutWrite:

    def test_create_missing_partition_key_rejected(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(kind=ActionKind.ACTION_KIND_CREATE, document=Document({}))
        with pytest.raises(InvalidArgumentException):
            store._create_put_write(action)

    def test_non_create_does_not_synthesize_key(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        source = {"pk": "existing"}
        action = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document(source))
        op = store._create_put_write(action)
        assert op.new_partition_key is None
        assert source["pk"] == "existing"

    def test_precondition_attached(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_CREATE, document=Document({"pk": "k"})
        )
        op = store._create_put_write(action)
        # CREATE attaches `exists=False`
        assert op.write_item.current_document.exists is False


class TestCreateDeleteWrite:

    def test_delete_targets_full_document_path(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_DELETE, document=Document({"pk": "id1"})
        )
        op = store._create_delete_write(action)
        assert op.write_item.delete.endswith("/coll/sid1")

    def test_delete_with_revision_attaches_precondition(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        rev = _server_timestamp()
        action = Action(
            kind=ActionKind.ACTION_KIND_DELETE,
            document=Document({"pk": "k", store.revision_field: rev}),
        )
        op = store._create_delete_write(action)
        assert int(op.write_item.current_document.update_time.timestamp()) == int(
            rev.timestamp()
        )

    def test_delete_without_revision_no_precondition(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_DELETE, document=Document({"pk": "k"})
        )
        op = store._create_delete_write(action)
        # No precondition set means the fields are default-empty
        assert not op.write_item.current_document.exists


class TestCreateUpdateWrite:

    def test_empty_mods_falls_back_to_put(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_UPDATE,
            document=Document({"pk": "k"}),
            mods={},
        )
        op = store._create_update_write(action)
        # PUT fallback: no update_mask set
        assert not op.write_item.update_mask.field_paths

    def test_non_empty_mods_produces_update_mask(self):
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_UPDATE,
            document=Document({"pk": "k"}),
            mods={"a": 1, "b": 2},
        )
        op = store._create_update_write(action)
        assert set(op.write_item.update_mask.field_paths) == {"a", "b"}

    def test_none_value_becomes_mask_only(self):
        # None mods value → mask lists the path but payload document omits it.
        store, _ = _make_store()
        store._project = "p"
        store._database_id = "db"
        action = Action(
            kind=ActionKind.ACTION_KIND_UPDATE,
            document=Document({"pk": "k"}),
            mods={"field_to_delete": None, "field_to_set": "x"},
        )
        op = store._create_update_write(action)
        assert set(op.write_item.update_mask.field_paths) == {
            "field_to_delete",
            "field_to_set",
        }
        # Only the non-None field lands on the payload doc
        assert "field_to_set" in op.write_item.update.fields
        assert "field_to_delete" not in op.write_item.update.fields


class TestUpdateRevision:

    def test_stamps_update_time_when_field_present(self):
        store, _ = _make_store()
        rev = _server_timestamp()
        doc = Document({"pk": "k", store.revision_field: "old"})
        action = Action(kind=ActionKind.ACTION_KIND_PUT, document=doc)
        op = WriteOperation(action=action, write_item=None)
        result = MagicMock()
        result.update_time = rev
        store._update_revision([op], [result])
        assert doc.get_field(store.revision_field) is rev

    def test_does_not_add_revision_field_when_absent(self):
        store, _ = _make_store()
        doc = Document({"pk": "k"})  # no revision field
        action = Action(kind=ActionKind.ACTION_KIND_PUT, document=doc)
        op = WriteOperation(action=action, write_item=None)
        result = MagicMock()
        result.update_time = _server_timestamp()
        store._update_revision([op], [result])
        assert not doc.has_field(store.revision_field)

    def test_shorter_results_list_tolerated(self):
        store, _ = _make_store()
        doc1 = Document({"pk": "k1", store.revision_field: "old"})
        doc2 = Document({"pk": "k2", store.revision_field: "old"})
        action1 = Action(kind=ActionKind.ACTION_KIND_PUT, document=doc1)
        action2 = Action(kind=ActionKind.ACTION_KIND_PUT, document=doc2)
        op1 = WriteOperation(action=action1, write_item=None)
        op2 = WriteOperation(action=action2, write_item=None)
        result = MagicMock()
        result.update_time = _server_timestamp()
        store._update_revision([op1, op2], [result])
        assert doc1.get_field(store.revision_field) is result.update_time
        assert doc2.get_field(store.revision_field) == "old"

    def test_none_update_time_left_alone(self):
        store, _ = _make_store()
        doc = Document({"pk": "k", store.revision_field: "old"})
        action = Action(kind=ActionKind.ACTION_KIND_PUT, document=doc)
        op = WriteOperation(action=action, write_item=None)
        result = MagicMock()
        result.update_time = None
        store._update_revision([op], [result])
        assert doc.get_field(store.revision_field) == "old"


class TestClassifyWriteError:

    def _op(self, kind):
        return WriteOperation(
            action=Action(kind=kind, document=Document({"pk": "k"})),
            write_item=None,
        )

    def test_single_create_precondition_maps_to_already_exists(self):
        store, _ = _make_store()
        exc = gax.FailedPrecondition("nope")
        result = store._classify_write_error(
            [self._op(ActionKind.ACTION_KIND_CREATE)], exc, atomic=False
        )
        assert isinstance(result, ResourceAlreadyExistsException)

    def test_single_replace_precondition_maps_to_not_found(self):
        store, _ = _make_store()
        exc = gax.FailedPrecondition("nope")
        result = store._classify_write_error(
            [self._op(ActionKind.ACTION_KIND_REPLACE)], exc, atomic=False
        )
        assert isinstance(result, ResourceNotFoundException)

    def test_single_put_precondition_maps_to_not_found(self):
        store, _ = _make_store()
        exc = gax.FailedPrecondition("nope")
        result = store._classify_write_error(
            [self._op(ActionKind.ACTION_KIND_PUT)], exc, atomic=False
        )
        assert isinstance(result, ResourceNotFoundException)

    def test_already_exists_on_single_create(self):
        store, _ = _make_store()
        exc = gax.AlreadyExists("dup")
        result = store._classify_write_error(
            [self._op(ActionKind.ACTION_KIND_CREATE)], exc, atomic=False
        )
        assert isinstance(result, ResourceAlreadyExistsException)

    def test_atomic_multi_precondition_first_create_maps_to_already_exists(self):
        # Atomic batch precondition failure dispatches by first action's kind,
        # regardless of batch size — first action is CREATE, so caller sees
        # ResourceAlreadyExistsException, not the generic TransactionFailed.
        store, _ = _make_store()
        exc = gax.FailedPrecondition("nope")
        ops = [
            self._op(ActionKind.ACTION_KIND_CREATE),
            self._op(ActionKind.ACTION_KIND_REPLACE),
        ]
        result = store._classify_write_error(ops, exc, atomic=True)
        assert isinstance(result, ResourceAlreadyExistsException)

    def test_atomic_multi_precondition_first_replace_maps_to_not_found(self):
        # Same rule, non-CREATE first action → ResourceNotFoundException.
        store, _ = _make_store()
        exc = gax.FailedPrecondition("nope")
        ops = [
            self._op(ActionKind.ACTION_KIND_REPLACE),
            self._op(ActionKind.ACTION_KIND_CREATE),
        ]
        result = store._classify_write_error(ops, exc, atomic=True)
        assert isinstance(result, ResourceNotFoundException)

    def test_atomic_multi_already_exists_first_create_maps_to_already_exists(self):
        # Raw AlreadyExists on an atomic multi-op batch — still dispatch by
        # first action's kind, not a generic TransactionFailed.
        store, _ = _make_store()
        exc = gax.AlreadyExists("dup")
        ops = [
            self._op(ActionKind.ACTION_KIND_CREATE),
            self._op(ActionKind.ACTION_KIND_PUT),
        ]
        result = store._classify_write_error(ops, exc, atomic=True)
        assert isinstance(result, ResourceAlreadyExistsException)

    def test_aborted_with_conflict_first_create_maps_to_already_exists(self):
        # Aborted-with-conflict is treated as a precondition failure and
        # dispatches by first action's kind on an atomic multi-op batch.
        store, _ = _make_store()
        exc = gax.Aborted("conflict on document")
        ops = [
            self._op(ActionKind.ACTION_KIND_CREATE),
            self._op(ActionKind.ACTION_KIND_REPLACE),
        ]
        result = store._classify_write_error(ops, exc, atomic=True)
        assert isinstance(result, ResourceAlreadyExistsException)

    def test_single_not_found_maps_to_not_found(self):
        store, _ = _make_store()
        exc = gax.NotFound("missing")
        result = store._classify_write_error(
            [self._op(ActionKind.ACTION_KIND_REPLACE)], exc, atomic=False
        )
        assert isinstance(result, ResourceNotFoundException)

    def test_atomic_non_precondition_maps_to_transaction_failed(self):
        store, _ = _make_store()
        # Something non-precondition on an atomic multi-op batch
        exc = gax.DeadlineExceeded("timeout")
        ops = [
            self._op(ActionKind.ACTION_KIND_PUT),
            self._op(ActionKind.ACTION_KIND_PUT),
        ]
        result = store._classify_write_error(ops, exc, atomic=True)
        assert isinstance(result, TransactionFailedException)

    def test_non_precondition_non_atomic_translates(self):
        store, _ = _make_store()
        exc = gax.DeadlineExceeded("timeout")
        ops = [self._op(ActionKind.ACTION_KIND_PUT)]
        result = store._classify_write_error(ops, exc, atomic=False)
        assert isinstance(result, DeadlineExceededException)


class TestCommitPrepared:

    def _op(self, kind, doc=None):
        return WriteOperation(
            action=Action(kind=kind, document=Document(doc if doc else {"pk": "k"})),
            write_item=fs_write.Write(),
        )

    def test_calls_commit_with_request(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        response = MagicMock()
        response.write_results = []
        client._firestore_api.commit.return_value = response
        store._rpc_metadata = [("k", "v")]

        # Distinguishable marker Write so payload flow is provable — an
        # empty Write() compares equal to any other empty Write().
        marker_write = fs_write.Write(
            update=fs_document.Document(name="marker/doc"),
        )
        op = WriteOperation(
            action=Action(
                kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k"})
            ),
            write_item=marker_write,
        )
        store._commit_prepared([op], atomic=False)

        client._firestore_api.commit.assert_called_once()
        kwargs = client._firestore_api.commit.call_args.kwargs
        assert kwargs["metadata"] == [("k", "v")]

        request = kwargs["request"]
        assert len(request.writes) == 1
        assert request.writes[0] == marker_write

    def test_commit_error_classified(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        client._firestore_api.commit.side_effect = gax.FailedPrecondition("nope")
        with pytest.raises(ResourceAlreadyExistsException):
            store._commit_prepared(
                [self._op(ActionKind.ACTION_KIND_CREATE)], atomic=False
            )

    def test_success_stamps_revisions(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        rev = _server_timestamp()
        write_result = MagicMock()
        write_result.update_time = rev
        response = MagicMock()
        response.write_results = [write_result]
        client._firestore_api.commit.return_value = response
        store._rpc_metadata = []
        doc = Document({"pk": "k", store.revision_field: "old"})
        op = WriteOperation(
            action=Action(kind=ActionKind.ACTION_KIND_PUT, document=doc),
            write_item=fs_write.Write(),
        )
        store._commit_prepared([op], atomic=False)
        assert doc.get_field(store.revision_field) is rev


class TestRunWritesBatched:

    def test_empty_is_noop(self):
        store, client = _make_store()
        store._run_writes_batched([], None)
        client._firestore_api.commit.assert_not_called()

    def test_dispatches_and_commits(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        response = MagicMock()
        response.write_results = []
        client._firestore_api.commit.return_value = response
        store._rpc_metadata = []
        actions = [
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"})),
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k2"})),
        ]
        store._run_writes_batched(actions, None)
        client._firestore_api.commit.assert_called_once()

    def test_before_do_hook_invoked_per_action(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        response = MagicMock()
        response.write_results = []
        client._firestore_api.commit.return_value = response
        store._rpc_metadata = []
        calls = []
        hook = lambda classifier: calls.append("hook")
        actions = [
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"})),
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k2"})),
        ]
        store._run_writes_batched(actions, hook)
        assert calls == ["hook", "hook"]


class TestRunAtomicWrites:

    def test_empty_is_noop(self):
        store, client = _make_store()
        store._run_atomic_writes([], None)
        client._firestore_api.commit.assert_not_called()

    def test_dispatches_and_commits(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        response = MagicMock()
        response.write_results = []
        client._firestore_api.commit.return_value = response
        store._rpc_metadata = []
        actions = [
            Action(
                kind=ActionKind.ACTION_KIND_PUT,
                document=Document({"pk": "k1"}),
                in_atomic_write=True,
            ),
            Action(
                kind=ActionKind.ACTION_KIND_PUT,
                document=Document({"pk": "k2"}),
                in_atomic_write=True,
            ),
        ]
        store._run_atomic_writes(actions, None)
        client._firestore_api.commit.assert_called_once()

    def test_every_action_forwarded_to_commit_writes(self):
        # Exercises the full prepare-and-forward pipeline: dispatch →
        # `_action_to_write` → `_commit_prepared`. A regression that drops
        # an action during prepare, or shuffles the order, would leave
        # `test_dispatches_and_commits` green because it only counts calls.
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        response = MagicMock()
        response.write_results = []
        client._firestore_api.commit.return_value = response
        store._rpc_metadata = []
        actions = [
            Action(
                kind=ActionKind.ACTION_KIND_CREATE,
                document=Document({"pk": f"k{i}", "payload": f"v{i}"}),
                in_atomic_write=True,
            )
            for i in range(3)
        ]
        store._run_atomic_writes(actions, None)

        request = client._firestore_api.commit.call_args.kwargs["request"]
        assert len(request.writes) == len(actions)
        for write, action in zip(request.writes, actions):
            # Each prepared Write must carry the corresponding action's
            # document path and encoded fields — not just any write.
            assert write.update.name.endswith(
                f"/coll/s{action.document.get_field('pk')}"
            )
            fields = write.update.fields
            assert fields["pk"].string_value == action.document.get_field("pk")
            assert fields["payload"].string_value == action.document.get_field(
                "payload"
            )
            # CREATE precondition survives prepare.
            assert write.current_document.exists is False

    def test_atomic_failure_maps_to_transaction_failed(self):
        # Non-precondition failure on an atomic batch → TransactionFailed
        # (the catch-all). Precondition failures dispatch by first action
        # kind and are covered in TestClassifyWriteError.
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        client._firestore_api.commit.side_effect = gax.DeadlineExceeded("timeout")
        store._rpc_metadata = []
        actions = [
            Action(
                kind=ActionKind.ACTION_KIND_PUT,
                document=Document({"pk": "k1"}),
                in_atomic_write=True,
            ),
            Action(
                kind=ActionKind.ACTION_KIND_PUT,
                document=Document({"pk": "k2"}),
                in_atomic_write=True,
            ),
        ]
        with pytest.raises(TransactionFailedException):
            store._run_atomic_writes(actions, None)

    def test_before_do_hook_invoked_per_action(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        response = MagicMock()
        response.write_results = []
        client._firestore_api.commit.return_value = response
        store._rpc_metadata = []
        calls = []
        hook = lambda classifier: calls.append("hook")
        actions = [
            Action(
                kind=ActionKind.ACTION_KIND_PUT,
                document=Document({"pk": "k1"}),
                in_atomic_write=True,
            ),
        ]
        store._run_atomic_writes(actions, hook)
        assert calls == ["hook"]


def _found_response(doc_path: str, fields: dict, update_time=None):
    """Build a BatchGetDocumentsResponse carrying a found Document."""
    fs_doc = fs_document.Document(
        name=doc_path,
        fields=fs_helpers.encode_dict(fields),
    )
    if update_time is not None:
        fs_doc.update_time = update_time
    return fs_types.BatchGetDocumentsResponse(found=fs_doc)


def _missing_response(doc_path: str):
    """Build a BatchGetDocumentsResponse carrying a missing document name."""
    return fs_types.BatchGetDocumentsResponse(missing=doc_path)


class TestRunGets:
    """Exercises the Java-parity BatchGetDocuments path.

    Mirrors ``FSDocStore.batchGet`` in the Java SDK — one low-level V1
    ``BatchGetDocuments`` RPC per batch, missing documents silently
    ignored, and ``before_do`` invoked once per RPC.
    """

    def test_empty_is_noop(self):
        store, client = _make_store()
        store._run_gets([], None, 100)
        client._firestore_api.batch_get_documents.assert_not_called()

    def test_get_forwards_key_and_merges_fields(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        doc_path = "projects/p/databases/db/documents/coll/sk"
        client._firestore_api.batch_get_documents.return_value = iter(
            [_found_response(doc_path, {"name": "alice", "age": 30})]
        )
        doc = Document({"pk": "k"})
        action = Action(kind=ActionKind.ACTION_KIND_GET, document=doc)
        store._run_gets([action], None, 100)

        request = client._firestore_api.batch_get_documents.call_args.kwargs["request"]
        assert request.database == "projects/p/databases/db"
        assert list(request.documents) == [doc_path]
        # No mask when no field_paths were requested.
        assert not request.mask.field_paths
        # Fields merged onto the caller's document.
        assert doc.get_field("name") == "alice"
        assert doc.get_field("age") == 30

    def test_projection_forwarded_via_mask(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        client._firestore_api.batch_get_documents.return_value = iter([])
        action = Action(
            kind=ActionKind.ACTION_KIND_GET,
            document=Document({"pk": "k"}),
            field_paths=[["a", "b"], ["c"]],
        )
        store._run_gets([action], None, 100)

        request = client._firestore_api.batch_get_documents.call_args.kwargs["request"]
        # Mask carries the requested paths plus the partition key
        # so the returned documents are always dispatchable.
        assert set(request.mask.field_paths) == {"a.b", "c", "pk"}

    def test_projection_mask_includes_sort_key(self):
        store, client = _make_store(sort_key="sk")
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        client._firestore_api.batch_get_documents.return_value = iter([])
        action = Action(
            kind=ActionKind.ACTION_KIND_GET,
            document=Document({"pk": "k", "sk": "s"}),
            field_paths=[["a"]],
        )
        store._run_gets([action], None, 100)

        request = client._firestore_api.batch_get_documents.call_args.kwargs["request"]
        assert set(request.mask.field_paths) == {"a", "pk", "sk"}

    def test_missing_documents_silently_ignored(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        doc_path = "projects/p/databases/db/documents/coll/sk"
        client._firestore_api.batch_get_documents.return_value = iter(
            [_missing_response(doc_path)]
        )
        doc = Document({"pk": "k"})
        action = Action(kind=ActionKind.ACTION_KIND_GET, document=doc)
        # Missing documents do not raise — matches Java behavior.
        store._run_gets([action], None, 100)
        assert not doc.has_field("name")

    def test_client_error_translated(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        client._firestore_api.batch_get_documents.side_effect = gax.NotFound("missing")
        action = Action(kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k"}))
        with pytest.raises(ResourceNotFoundException):
            store._run_gets([action], None, 100)

    def test_stamps_revision_when_field_present(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        ts = timestamp_pb2.Timestamp(seconds=1704178745)
        doc_path = "projects/p/databases/db/documents/coll/sk"
        client._firestore_api.batch_get_documents.return_value = iter(
            [_found_response(doc_path, {}, update_time=ts)]
        )
        doc = Document({"pk": "k", store.revision_field: "old"})
        action = Action(kind=ActionKind.ACTION_KIND_GET, document=doc)
        store._run_gets([action], None, 100)
        stamped = doc.get_field(store.revision_field)
        # proto-plus exposes ``update_time`` as a DatetimeWithNanoseconds
        # whose UNIX timestamp matches the seconds we passed in.
        assert int(stamped.timestamp()) == ts.seconds

    def test_does_not_add_revision_field_when_absent(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        ts = timestamp_pb2.Timestamp(seconds=1704178745)
        doc_path = "projects/p/databases/db/documents/coll/sk"
        client._firestore_api.batch_get_documents.return_value = iter(
            [_found_response(doc_path, {}, update_time=ts)]
        )
        doc = Document({"pk": "k"})
        action = Action(kind=ActionKind.ACTION_KIND_GET, document=doc)
        store._run_gets([action], None, 100)
        assert not doc.has_field(store.revision_field)

    def test_before_do_hook_invoked_once_per_batch(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        client._firestore_api.batch_get_documents.return_value = iter([])
        calls = []
        hook = lambda classifier: calls.append("hook")
        actions = [
            Action(
                kind=ActionKind.ACTION_KIND_GET,
                document=Document({"pk": f"k{i}"}),
            )
            for i in range(3)
        ]
        # Three actions, batch_size=100 → one batch → one hook call.
        store._run_gets(actions, hook, 100)
        assert calls == ["hook"]

    def test_batching_splits_by_batch_size(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        client._firestore_api.batch_get_documents.side_effect = lambda **_kwargs: iter(
            []
        )
        actions = [
            Action(
                kind=ActionKind.ACTION_KIND_GET,
                document=Document({"pk": f"k{i}"}),
            )
            for i in range(5)
        ]
        # 5 actions with batch_size=2 → ceil(5/2)=3 batches → 3 RPC calls.
        store._run_gets(actions, None, 2)
        assert client._firestore_api.batch_get_documents.call_count == 3

    def test_different_field_paths_ride_different_batches(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        client._firestore_api.batch_get_documents.side_effect = lambda **_kwargs: iter(
            []
        )
        # A single ``DocumentMask`` applies to the whole request, so gets
        # that project different paths cannot share a batch.
        a1 = Action(
            kind=ActionKind.ACTION_KIND_GET,
            document=Document({"pk": "k1"}),
            field_paths=[["a"]],
        )
        a2 = Action(
            kind=ActionKind.ACTION_KIND_GET,
            document=Document({"pk": "k2"}),
            field_paths=[["b"]],
        )
        store._run_gets([a1, a2], None, 100)
        assert client._firestore_api.batch_get_documents.call_count == 2

    def test_unknown_document_path_in_response_ignored(self):
        """A ``found`` response for an unrequested path is dropped, not raised."""
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        store._rpc_metadata = []
        stray_path = "projects/p/databases/db/documents/coll/stray"
        client._firestore_api.batch_get_documents.return_value = iter(
            [_found_response(stray_path, {"name": "ghost"})]
        )
        doc = Document({"pk": "k"})
        action = Action(kind=ActionKind.ACTION_KIND_GET, document=doc)
        store._run_gets([action], None, 100)
        # The stray document did not correspond to any requested Action;
        # the caller's document stays untouched.
        assert not doc.has_field("name")


class TestRunActionsDispatch:

    def _setup_commit(self, store, client, write_results=None):
        response = MagicMock()
        response.write_results = write_results or []
        client._firestore_api.commit.return_value = response
        store._rpc_metadata = []

    def _setup_get_snapshot(self, store, client, data=None):
        """Wire ``batch_get_documents`` to return a single found response."""
        store._rpc_metadata = []
        client._firestore_api.batch_get_documents.side_effect = lambda **_kwargs: iter(
            [
                _found_response(
                    "projects/p/databases/db/documents/coll/sk",
                    data or {},
                    update_time=timestamp_pb2.Timestamp(seconds=1704178745),
                )
            ]
        )

    def test_write_and_get_paths_both_called(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        self._setup_commit(store, client)
        self._setup_get_snapshot(store, client)
        write = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        write.key = "k1"
        write.index = 0
        get = Action(kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k2"}))
        get.key = "k2"
        get.index = 1
        store.run_actions([write, get])
        client._firestore_api.commit.assert_called_once()
        client._firestore_api.batch_get_documents.assert_called_once()

    def test_atomic_group_uses_commit(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        self._setup_commit(store, client)
        a1 = Action(
            kind=ActionKind.ACTION_KIND_PUT,
            document=Document({"pk": "k1"}),
            in_atomic_write=True,
        )
        a1.key = "k1"
        a1.index = 0
        a2 = Action(
            kind=ActionKind.ACTION_KIND_PUT,
            document=Document({"pk": "k2"}),
            in_atomic_write=True,
        )
        a2.key = "k2"
        a2.index = 1
        store.run_actions([a1, a2])
        client._firestore_api.commit.assert_called_once()

    def test_before_gets_run_inline(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        self._setup_commit(store, client)
        self._setup_get_snapshot(store, client)
        get = Action(kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k1"}))
        get.key = "k1"
        get.index = 0
        write = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        write.key = "k1"
        write.index = 1
        store.run_actions([get, write])
        # Both were dispatched
        client._firestore_api.commit.assert_called_once()
        client._firestore_api.batch_get_documents.assert_called()

    def test_after_gets_run_last(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        self._setup_commit(store, client)
        self._setup_get_snapshot(store, client)
        write = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        write.key = "k1"
        write.index = 0
        get = Action(kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k1"}))
        get.key = "k1"
        get.index = 1
        store.run_actions([write, get])
        client._firestore_api.commit.assert_called_once()
        client._firestore_api.batch_get_documents.assert_called()

    def test_substrate_error_passes_through(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        # SubstrateSdkException raised inside the fanout should pass through
        client._firestore_api.commit.side_effect = ResourceNotFoundException("nope")
        write = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        write.key = "k1"
        write.index = 0
        with pytest.raises(ResourceNotFoundException):
            store.run_actions([write])

    def test_unknown_error_in_fanout_translated(self):
        store, client = _make_store()
        store._project = "p"
        store._database_id = "db"
        # A totally-generic error from the executor should get translated
        # to UnknownException by the run_actions boundary.
        client._firestore_api.commit.side_effect = RuntimeError("boom")
        store._rpc_metadata = []
        write = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        write.key = "k1"
        write.index = 0
        with pytest.raises(UnknownException):
            store.run_actions([write])


class TestModuleHelpers:

    def test_encode_component_string(self):
        assert _encode_component("abc") == "sabc"

    def test_encode_component_escapes_percent_first_then_slash_and_colon(self):
        # `%` must be escaped first so `%2F`/`%3A` are unambiguous.
        assert _encode_component("a%b/c:d") == "sa%25b%2Fc%3Ad"

    def test_encode_component_int_and_str_of_same_digits_are_distinct(self):
        assert _encode_component(1) == "i1"
        assert _encode_component("1") == "s1"

    def test_encode_component_bool_takes_precedence_over_int(self):
        # isinstance(True, int) is True in Python; bool must route to "b".
        assert _encode_component(True) == "b1"
        assert _encode_component(False) == "b0"

    def test_encode_component_rejects_unsupported_type(self):
        with pytest.raises(InvalidArgumentException, match="unsupported"):
            _encode_component(1.5)

    def test_parse_table_name_bare_collection_rejected(self):
        # Bare collection ids must be rejected — silently inferring
        # project + database from ambient defaults would mask caller
        # misconfiguration.
        with pytest.raises(InvalidArgumentException, match="full Firestore path"):
            _parse_table_name("orders", "hint")

    def test_parse_table_name_bare_no_hint_rejected(self):
        # Same rejection regardless of whether a project hint is passed.
        with pytest.raises(InvalidArgumentException, match="full Firestore path"):
            _parse_table_name("orders", None)

    def test_parse_table_name_partial_path_rejected(self):
        # Missing the ``documents/`` prefix — still not a full path.
        with pytest.raises(InvalidArgumentException, match="full Firestore path"):
            _parse_table_name("projects/pp/databases/dd/orders", None)

    def test_parse_table_name_full_path(self):
        proj, db, coll = _parse_table_name(
            "projects/pp/databases/dd/documents/orders", None
        )
        assert proj == "pp"
        assert db == "dd"
        assert coll == "orders"

    def test_parse_table_name_full_path_with_subcollection(self):
        # Subcollection paths keep the whole tail after ``documents/`` —
        # collapsing to the leaf id would silently retarget writes to a
        # same-named top-level collection.
        proj, db, coll = _parse_table_name(
            "projects/pp/databases/dd/documents/orders/id123/items", None
        )
        assert proj == "pp"
        assert db == "dd"
        assert coll == "orders/id123/items"

    def test_snapshot_doc_returns_plain_dict(self):
        doc = Document({"a": 1, "b": "hello"})
        assert _snapshot_doc(doc) == {"a": 1, "b": "hello"}

    def test_snapshot_doc_on_object(self):
        class Stub:
            def __init__(self):
                self.a = 1
                self.b = "x"

        doc = Document(Stub())
        result = _snapshot_doc(doc)
        assert result == {"a": 1, "b": "x"}


class TestRegistration:

    def test_registered_as_gcp_firestore(self):
        from multicloud.docstore.docstore_client.driver import DOCSTORE_REGISTRY

        # Registered by the @DOCSTORE_REGISTRY.register decorator at import time.
        assert DOCSTORE_REGISTRY.get("gcp-firestore") is FirestoreDocStore
