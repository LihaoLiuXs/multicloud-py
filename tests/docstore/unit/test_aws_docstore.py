"""Unit tests for the AWS DynamoDB implementation of the docstore driver.

Every test injects a ``MagicMock`` as the DynamoDB client via the
``dynamodb_client`` kwarg on :class:`AwsDocStore`, so no boto3 session,
credentials, or real network round-trip is involved.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
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
)
from multicloud.docstore.docstore_aws.aws_docstore import AwsDocStore, WriteOperation
from multicloud.docstore.docstore_client.driver import (
    Action,
    ActionKind,
    CollectionOptions,
    Document,
)


def _client_error(code: str, status: int = 400) -> ClientError:
    """Build a boto3 ClientError with the requested error code + HTTP status."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": f"{code} message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutItem",
    )


def _make_store(
    *,
    partition_key: str = "pk",
    sort_key=None,
    revision_field=None,
    max_outstanding_action_rpcs: int = 0,
) -> tuple[AwsDocStore, MagicMock]:
    ddb = MagicMock()
    store = AwsDocStore(
        CollectionOptions(
            table_name="tbl",
            partition_key=partition_key,
            sort_key=sort_key,
            revision_field=revision_field,
            max_outstanding_action_rpcs=max_outstanding_action_rpcs,
        ),
        dynamodb_client=ddb,
    )
    return store, ddb


class TestConstruction:

    def test_injected_client_used_directly(self):
        store, ddb = _make_store()
        assert store._ddb is ddb
        assert store._session is None
        store.close()

    @patch("multicloud.docstore.docstore_aws.aws_docstore.get_session")
    def test_no_injected_client_creates_session(self, mock_get_session):
        mock_session = MagicMock()
        mock_get_session.return_value = mock_session
        store = AwsDocStore(
            CollectionOptions(table_name="t", partition_key="pk"),
            region="us-west-2",
        )
        mock_get_session.assert_called_once_with(None, "us-west-2")
        mock_session.client.assert_called_once_with("dynamodb", region_name="us-west-2")
        assert store._session is mock_session
        store.close()

    @patch("multicloud.docstore.docstore_aws.aws_docstore.get_session")
    def test_endpoint_url_forwarded_to_boto3(self, mock_get_session):
        mock_session = MagicMock()
        mock_get_session.return_value = mock_session
        store = AwsDocStore(
            CollectionOptions(table_name="t", partition_key="pk"),
            region="us-east-1",
            endpoint_url="https://ddb.example",
        )
        mock_session.client.assert_called_once_with(
            "dynamodb", region_name="us-east-1", endpoint_url="https://ddb.example"
        )
        store.close()


class TestClose:

    def test_close_does_not_touch_injected_client(self):
        store, ddb = _make_store()
        store.close()
        ddb.close.assert_not_called()

    @patch("multicloud.docstore.docstore_aws.aws_docstore.get_session")
    def test_close_closes_owned_client(self, mock_get_session):
        mock_session = MagicMock()
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        mock_get_session.return_value = mock_session
        store = AwsDocStore(CollectionOptions(table_name="t", partition_key="pk"))
        store.close()
        mock_client.close.assert_called_once()

    @patch("multicloud.docstore.docstore_aws.aws_docstore.get_session")
    def test_close_swallows_client_close_errors(self, mock_get_session):
        mock_session = MagicMock()
        mock_client = MagicMock()
        mock_client.close.side_effect = RuntimeError("closed twice")
        mock_session.client.return_value = mock_client
        mock_get_session.return_value = mock_session
        store = AwsDocStore(CollectionOptions(table_name="t", partition_key="pk"))
        # Must not raise even though the underlying client blew up
        store.close()


class TestTranslateError:

    def test_pass_through_substrate_error(self):
        store, _ = _make_store()
        err = ResourceNotFoundException("nope")
        assert store.translate_error(err) is err

    def test_no_credentials_maps_to_unauthorized(self):
        store, _ = _make_store()
        exc = NoCredentialsError()
        result = store.translate_error(exc)
        assert isinstance(result, UnAuthorizedException)

    def test_generic_botocore_error_maps_to_invalid_argument(self):
        store, _ = _make_store()

        class MyBoto(BotoCoreError):
            fmt = "boom"

        result = store.translate_error(MyBoto())
        assert isinstance(result, InvalidArgumentException)

    def test_client_error_conditional_check_failed(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("ConditionalCheckFailedException"))
        assert isinstance(result, FailedPreconditionException)

    def test_client_error_resource_not_found(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("ResourceNotFoundException"))
        assert isinstance(result, ResourceNotFoundException)

    def test_client_error_resource_in_use(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("ResourceInUseException"))
        assert isinstance(result, ResourceAlreadyExistsException)

    def test_client_error_throttling_by_code(self):
        store, _ = _make_store()
        result = store.translate_error(
            _client_error("ProvisionedThroughputExceededException")
        )
        assert isinstance(result, ResourceExhaustedException)

    def test_client_error_throttling_by_status_429(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("Unrecognized", status=429))
        assert isinstance(result, ResourceExhaustedException)

    def test_client_error_access_denied(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("AccessDeniedException"))
        assert isinstance(result, UnAuthorizedException)

    def test_client_error_403_maps_to_unauthorized(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("Whatever", status=403))
        assert isinstance(result, UnAuthorizedException)

    def test_client_error_expired_token(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("ExpiredTokenException"))
        assert isinstance(result, UnAuthorizedException)

    def test_client_error_request_timeout(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("RequestTimeout"))
        assert isinstance(result, DeadlineExceededException)

    def test_client_error_validation(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("ValidationException"))
        assert isinstance(result, InvalidArgumentException)

    def test_client_error_serialization(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("SerializationException"))
        assert isinstance(result, InvalidArgumentException)

    def test_client_error_transaction_canceled(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("TransactionCanceledException"))
        assert isinstance(result, TransactionFailedException)

    def test_client_error_internal_server(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("InternalServerError"))
        assert isinstance(result, UnknownException)

    def test_client_error_unknown_code_falls_back_to_unknown(self):
        store, _ = _make_store()
        result = store.translate_error(_client_error("UnrecognizedCode"))
        assert isinstance(result, UnknownException)

    def test_completely_unknown_error_maps_to_unknown(self):
        store, _ = _make_store()
        result = store.translate_error(RuntimeError("no idea"))
        assert isinstance(result, UnknownException)

    def test_wrapped_client_error_still_classified(self):
        store, _ = _make_store()
        inner = _client_error("ResourceNotFoundException")
        wrapper = RuntimeError("outer")
        wrapper.__cause__ = inner
        result = store.translate_error(wrapper)
        assert isinstance(result, ResourceNotFoundException)


class TestReadRevision:

    def test_none_when_missing(self):
        store, _ = _make_store()
        assert store._read_revision(Document({})) is None

    def test_none_when_empty_string(self):
        store, _ = _make_store()
        assert store._read_revision(Document({store.revision_field: ""})) is None

    def test_returns_string_value(self):
        store, _ = _make_store()
        assert store._read_revision(Document({store.revision_field: "r1"})) == "r1"

    def test_wrong_type_raises(self):
        store, _ = _make_store()
        with pytest.raises(InvalidArgumentException, match="Invalid revision"):
            store._read_revision(Document({store.revision_field: 123}))


class TestBuildPrecondition:

    def _prep(self, kind, doc):
        action = Action(kind=kind, document=Document(doc))
        return action

    def test_create_uses_attribute_not_exists(self):
        store, _ = _make_store()
        action = self._prep(ActionKind.ACTION_KIND_CREATE, {"pk": "k"})
        expr = store._build_precondition(action)
        assert expr == "attribute_not_exists(#pk)"
        assert store._precondition_attribute_names(action) == {"#pk": "pk"}
        assert store._precondition_attribute_values(action) is None

    def test_replace_without_revision_uses_attribute_exists(self):
        store, _ = _make_store()
        action = self._prep(ActionKind.ACTION_KIND_REPLACE, {"pk": "k"})
        expr = store._build_precondition(action)
        assert expr == "attribute_exists(#pk)"
        assert store._precondition_attribute_names(action) == {"#pk": "pk"}
        assert store._precondition_attribute_values(action) is None

    def test_replace_with_revision_matches_revfield(self):
        store, _ = _make_store()
        action = self._prep(
            ActionKind.ACTION_KIND_REPLACE,
            {"pk": "k", store.revision_field: "r0"},
        )
        assert store._build_precondition(action) == "#revField = :revValue"
        assert store._precondition_attribute_names(action) == {
            "#revField": store.revision_field
        }
        assert store._precondition_attribute_values(action) == {
            ":revValue": {"S": "r0"}
        }

    def test_put_without_revision_is_unconditional(self):
        store, _ = _make_store()
        action = self._prep(ActionKind.ACTION_KIND_PUT, {"pk": "k"})
        assert store._build_precondition(action) is None
        assert store._precondition_attribute_names(action) is None
        assert store._precondition_attribute_values(action) is None

    def test_put_with_revision_matches_revfield(self):
        store, _ = _make_store()
        action = self._prep(
            ActionKind.ACTION_KIND_PUT,
            {"pk": "k", store.revision_field: "r0"},
        )
        assert store._build_precondition(action) == "#revField = :revValue"
        assert store._precondition_attribute_names(action) == {
            "#revField": store.revision_field
        }

    def test_delete_without_revision_is_unconditional(self):
        store, _ = _make_store()
        action = self._prep(ActionKind.ACTION_KIND_DELETE, {"pk": "k"})
        assert store._build_precondition(action) is None
        assert store._precondition_attribute_names(action) is None
        assert store._precondition_attribute_values(action) is None

    def test_delete_with_revision_matches_revfield(self):
        store, _ = _make_store()
        action = self._prep(
            ActionKind.ACTION_KIND_DELETE,
            {"pk": "k", store.revision_field: "r0"},
        )
        assert store._build_precondition(action) == "#revField = :revValue"
        assert store._precondition_attribute_names(action) == {
            "#revField": store.revision_field
        }
        assert store._precondition_attribute_values(action) == {
            ":revValue": {"S": "r0"}
        }


class TestBuildProjection:

    def test_auto_adds_partition_key(self):
        store, _ = _make_store()
        projection, names = store._build_projection([["name"]])
        # projection lists aliases separated by commas
        assert projection.count(",") == 1
        # every alias resolves to a real attribute name
        assert set(names.values()) == {"name", "pk"}

    def test_does_not_readd_pk_when_already_projected(self):
        store, _ = _make_store()
        projection, names = store._build_projection([["pk"], ["name"]])
        # Names map still has both pk and name — no duplicate entry needed
        assert list(names.values()).count("pk") == 1

    def test_composite_key_auto_added(self):
        store, _ = _make_store(sort_key="sk")
        projection, names = store._build_projection([["name"]])
        assert set(names.values()) == {"name", "pk", "sk"}

    def test_nested_path_produces_dotted_alias_chain(self):
        store, _ = _make_store()
        projection, names = store._build_projection([["a", "b"]])
        # The first path becomes "#attr0.#attr1"
        first_part = projection.split(",")[0]
        assert first_part.count("#attr") == 2
        assert set(names.values()) == {"a", "b", "pk"}


class TestIsNull:

    def test_null_true(self):
        assert AwsDocStore._is_null({"NULL": True}) is True

    def test_not_null(self):
        assert AwsDocStore._is_null({"S": "x"}) is False


class TestRunPut:

    def _prep(self, kind):
        return Action(kind=kind, document=Document({"pk": "k"}))

    def test_put_item_forwarded_to_ddb(self):
        store, ddb = _make_store()
        action = self._prep(ActionKind.ACTION_KIND_PUT)
        store._run_put(
            action=action,
            item={"pk": {"S": "k"}},
            condition_expression=None,
            expression_attribute_names=None,
            expression_attribute_values=None,
        )
        ddb.put_item.assert_called_once()
        kwargs = ddb.put_item.call_args.kwargs
        assert kwargs["TableName"] == "tbl"
        assert kwargs["Item"] == {"pk": {"S": "k"}}
        assert "ConditionExpression" not in kwargs

    def test_create_precondition_fail_becomes_already_exists(self):
        store, ddb = _make_store()
        ddb.put_item.side_effect = _client_error("ConditionalCheckFailedException")
        with pytest.raises(ResourceAlreadyExistsException):
            store._run_put(
                action=self._prep(ActionKind.ACTION_KIND_CREATE),
                item={"pk": {"S": "k"}},
                condition_expression="attribute_not_exists(#pk)",
                expression_attribute_names={"#pk": "pk"},
                expression_attribute_values=None,
            )

    def test_replace_precondition_fail_becomes_not_found(self):
        store, ddb = _make_store()
        ddb.put_item.side_effect = _client_error("ConditionalCheckFailedException")
        with pytest.raises(ResourceNotFoundException):
            store._run_put(
                action=self._prep(ActionKind.ACTION_KIND_REPLACE),
                item={"pk": {"S": "k"}},
                condition_expression="attribute_exists(#pk)",
                expression_attribute_names={"#pk": "pk"},
                expression_attribute_values=None,
            )

    def test_put_precondition_fail_becomes_failed_precondition(self):
        store, ddb = _make_store()
        ddb.put_item.side_effect = _client_error("ConditionalCheckFailedException")
        with pytest.raises(FailedPreconditionException):
            store._run_put(
                action=self._prep(ActionKind.ACTION_KIND_PUT),
                item={"pk": {"S": "k"}},
                condition_expression="#revField = :revValue",
                expression_attribute_names={"#revField": "rev"},
                expression_attribute_values={":revValue": {"S": "r"}},
            )

    def test_other_client_error_goes_through_translate(self):
        store, ddb = _make_store()
        ddb.put_item.side_effect = _client_error("ResourceNotFoundException")
        with pytest.raises(ResourceNotFoundException):
            store._run_put(
                action=self._prep(ActionKind.ACTION_KIND_PUT),
                item={"pk": {"S": "k"}},
                condition_expression=None,
                expression_attribute_names=None,
                expression_attribute_values=None,
            )

    def test_non_client_error_goes_through_translate(self):
        store, ddb = _make_store()
        ddb.put_item.side_effect = RuntimeError("boom")
        with pytest.raises(UnknownException):
            store._run_put(
                action=self._prep(ActionKind.ACTION_KIND_PUT),
                item={"pk": {"S": "k"}},
                condition_expression=None,
                expression_attribute_names=None,
                expression_attribute_values=None,
            )

    def test_substrate_error_passes_through(self):
        store, ddb = _make_store()
        ddb.put_item.side_effect = ResourceExhaustedException("throttled")
        with pytest.raises(ResourceExhaustedException):
            store._run_put(
                action=self._prep(ActionKind.ACTION_KIND_PUT),
                item={"pk": {"S": "k"}},
                condition_expression=None,
                expression_attribute_names=None,
                expression_attribute_values=None,
            )


class TestNewPut:

    def test_create_auto_generates_partition_key(self):
        store, _ = _make_store()
        source = {}
        action = Action(kind=ActionKind.ACTION_KIND_CREATE, document=Document(source))
        op = store._new_put(action)
        assert op.new_partition_key is not None
        assert source["pk"] == op.new_partition_key
        assert op.new_revision is None
        assert op.write_item is not None

    def test_non_create_missing_pk_raises(self):
        store, _ = _make_store()
        action = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({}))
        with pytest.raises(InvalidArgumentException, match="missing partition key"):
            store._new_put(action)

    def test_composite_missing_sort_key_raises(self):
        store, _ = _make_store(sort_key="sk")
        action = Action(
            kind=ActionKind.ACTION_KIND_CREATE, document=Document({"pk": "k"})
        )
        with pytest.raises(InvalidArgumentException, match="missing sort key"):
            store._new_put(action)

    def test_revision_stamps_fresh_uuid_on_document(self):
        store, _ = _make_store()
        source = {"pk": "k", store.revision_field: "r0"}
        action = Action(kind=ActionKind.ACTION_KIND_REPLACE, document=Document(source))
        op = store._new_put(action)
        # A new revision is prepared for post-write stamping
        assert op.new_revision is not None
        assert op.new_revision != "r0"

    def test_write_operation_runs_put(self):
        store, ddb = _make_store()
        action = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k"}))
        op = store._new_put(action)
        assert op.run is not None
        op.run()
        ddb.put_item.assert_called_once()


class TestRunDelete:

    def _prep(self, doc=None):
        return Action(
            kind=ActionKind.ACTION_KIND_DELETE,
            document=Document(doc or {"pk": "k"}),
        )

    def test_delete_item_forwarded_to_ddb(self):
        store, ddb = _make_store()
        store._run_delete(
            key={"pk": {"S": "k"}},
            condition_expression=None,
            expression_attribute_names=None,
            expression_attribute_values=None,
        )
        ddb.delete_item.assert_called_once()
        kwargs = ddb.delete_item.call_args.kwargs
        assert kwargs["TableName"] == "tbl"
        assert kwargs["Key"] == {"pk": {"S": "k"}}
        assert "ConditionExpression" not in kwargs

    def test_precondition_fields_forwarded(self):
        store, ddb = _make_store(revision_field="rev")
        store._run_delete(
            key={"pk": {"S": "k"}},
            condition_expression="#revField = :revValue",
            expression_attribute_names={"#revField": "rev"},
            expression_attribute_values={":revValue": {"S": "r0"}},
        )
        kwargs = ddb.delete_item.call_args.kwargs
        assert kwargs["ConditionExpression"] == "#revField = :revValue"
        assert kwargs["ExpressionAttributeNames"] == {"#revField": "rev"}
        assert kwargs["ExpressionAttributeValues"] == {":revValue": {"S": "r0"}}

    def test_precondition_fail_becomes_failed_precondition(self):
        store, ddb = _make_store()
        ddb.delete_item.side_effect = _client_error("ConditionalCheckFailedException")
        with pytest.raises(FailedPreconditionException):
            store._run_delete(
                key={"pk": {"S": "k"}},
                condition_expression="#revField = :revValue",
                expression_attribute_names={"#revField": "rev"},
                expression_attribute_values={":revValue": {"S": "r"}},
            )

    def test_other_client_error_goes_through_translate(self):
        store, ddb = _make_store()
        ddb.delete_item.side_effect = _client_error("ResourceNotFoundException")
        with pytest.raises(ResourceNotFoundException):
            store._run_delete(
                key={"pk": {"S": "k"}},
                condition_expression=None,
                expression_attribute_names=None,
                expression_attribute_values=None,
            )

    def test_non_client_error_goes_through_translate(self):
        store, ddb = _make_store()
        ddb.delete_item.side_effect = RuntimeError("boom")
        with pytest.raises(UnknownException):
            store._run_delete(
                key={"pk": {"S": "k"}},
                condition_expression=None,
                expression_attribute_names=None,
                expression_attribute_values=None,
            )

    def test_substrate_error_passes_through(self):
        store, ddb = _make_store()
        ddb.delete_item.side_effect = ResourceExhaustedException("throttled")
        with pytest.raises(ResourceExhaustedException):
            store._run_delete(
                key={"pk": {"S": "k"}},
                condition_expression=None,
                expression_attribute_names=None,
                expression_attribute_values=None,
            )


class TestNewDelete:

    def test_missing_pk_raises(self):
        store, _ = _make_store()
        action = Action(kind=ActionKind.ACTION_KIND_DELETE, document=Document({}))
        with pytest.raises(InvalidArgumentException, match="missing partition key"):
            store._new_delete(action)

    def test_composite_missing_sort_key_raises(self):
        store, _ = _make_store(sort_key="sk")
        action = Action(
            kind=ActionKind.ACTION_KIND_DELETE, document=Document({"pk": "k"})
        )
        with pytest.raises(InvalidArgumentException, match="missing sort key"):
            store._new_delete(action)

    def test_write_item_carries_key_only_and_no_new_revision(self):
        store, _ = _make_store(sort_key="sk")
        action = Action(
            kind=ActionKind.ACTION_KIND_DELETE,
            document=Document({"pk": "k", "sk": "s", "other": "ignored"}),
        )
        op = store._new_delete(action)
        assert op.write_item is not None
        payload = op.write_item["Delete"]
        assert payload["TableName"] == "tbl"
        # Key-only encode: no non-key fields in Delete payload
        assert set(payload["Key"].keys()) == {"pk", "sk"}
        assert payload["Key"]["pk"] == {"S": "k"}
        assert payload["Key"]["sk"] == {"S": "s"}
        # DELETE never mints a new partition key or revision
        assert op.new_partition_key is None
        assert op.new_revision is None
        # No revision precondition attached when the document has no revision
        assert "ConditionExpression" not in payload
        assert "ExpressionAttributeNames" not in payload
        assert "ExpressionAttributeValues" not in payload

    def test_write_item_attaches_revision_precondition(self):
        store, _ = _make_store(revision_field="rev")
        action = Action(
            kind=ActionKind.ACTION_KIND_DELETE,
            document=Document({"pk": "k", "rev": "r0"}),
        )
        op = store._new_delete(action)
        assert op.write_item is not None
        payload = op.write_item["Delete"]
        assert payload["ConditionExpression"] == "#revField = :revValue"
        assert payload["ExpressionAttributeNames"] == {"#revField": "rev"}
        assert payload["ExpressionAttributeValues"] == {":revValue": {"S": "r0"}}

    def test_write_operation_runs_delete(self):
        store, ddb = _make_store()
        action = Action(
            kind=ActionKind.ACTION_KIND_DELETE, document=Document({"pk": "k"})
        )
        op = store._new_delete(action)
        assert op.run is not None
        op.run()
        ddb.delete_item.assert_called_once()

    def test_before_do_fires_before_delete(self):
        store, ddb = _make_store()
        order: list = []

        def hook(_predicate):
            order.append("before")

        ddb.delete_item.side_effect = lambda **_kw: order.append("delete")
        action = Action(
            kind=ActionKind.ACTION_KIND_DELETE, document=Document({"pk": "k"})
        )
        op = store._new_delete(action, before_do=hook)
        assert op.run is not None
        op.run()
        assert order == ["before", "delete"]


class TestNewWriteOperation:

    def test_dispatches_put_family_to_new_put(self):
        store, _ = _make_store()
        for kind in (
            ActionKind.ACTION_KIND_CREATE,
            ActionKind.ACTION_KIND_PUT,
            ActionKind.ACTION_KIND_REPLACE,
        ):
            action = Action(kind=kind, document=Document({"pk": "k"}))
            op = store._new_write_operation(action)
            assert isinstance(op, WriteOperation)

    def test_dispatches_delete_to_new_delete(self):
        store, _ = _make_store()
        action = Action(
            kind=ActionKind.ACTION_KIND_DELETE, document=Document({"pk": "k"})
        )
        op = store._new_write_operation(action)
        assert isinstance(op, WriteOperation)
        assert op.write_item is not None
        # Dispatch built a Delete payload, not a Put
        assert "Delete" in op.write_item
        assert "Put" not in op.write_item

    def test_unsupported_kind_raises(self):
        store, _ = _make_store()
        action = Action(
            kind=ActionKind.ACTION_KIND_UPDATE, document=Document({"pk": "k"})
        )
        with pytest.raises(SubstrateSdkException, match="not implemented"):
            store._new_write_operation(action)


class TestUpdateRevision:

    def test_stamps_revision_on_source_document(self):
        store, _ = _make_store()
        doc = Document({"pk": "k"})
        action = Action(kind=ActionKind.ACTION_KIND_PUT, document=doc)
        op = WriteOperation(action=action, new_revision="new-rev-uuid")
        store._update_revision([op])
        assert doc.get_field(store.revision_field) == "new-rev-uuid"

    def test_no_revision_leaves_document_alone(self):
        store, _ = _make_store()
        doc = Document({"pk": "k"})
        action = Action(kind=ActionKind.ACTION_KIND_PUT, document=doc)
        op = WriteOperation(action=action, new_revision=None)
        store._update_revision([op])
        assert doc.has_field(store.revision_field) is False


class TestRunWrites:

    def test_empty_actions_is_noop(self):
        store, ddb = _make_store()
        store._run_writes([], None)
        ddb.put_item.assert_not_called()

    def test_dispatches_and_stamps_revisions(self):
        store, ddb = _make_store()
        doc1 = Document({"pk": "k1", store.revision_field: "r0"})
        doc2 = Document({"pk": "k2", store.revision_field: "r0"})
        actions = [
            Action(kind=ActionKind.ACTION_KIND_REPLACE, document=doc1),
            Action(kind=ActionKind.ACTION_KIND_REPLACE, document=doc2),
        ]
        store._run_writes(actions, None)
        assert ddb.put_item.call_count == 2
        # Fresh revisions were stamped back onto both source docs
        assert doc1.get_field(store.revision_field) != "r0"
        assert doc2.get_field(store.revision_field) != "r0"

    def test_first_error_reraises_and_revisions_not_stamped(self):
        store, ddb = _make_store()
        ddb.put_item.side_effect = _client_error("ResourceNotFoundException")
        doc = Document({"pk": "k1", store.revision_field: "r0"})
        actions = [Action(kind=ActionKind.ACTION_KIND_REPLACE, document=doc)]
        with pytest.raises(ResourceNotFoundException):
            store._run_writes(actions, None)
        # Fresh revision was not stamped since the put failed
        assert doc.get_field(store.revision_field) == "r0"

    def test_before_do_hook_invoked(self):
        store, ddb = _make_store()
        calls = []
        hook = lambda classifier: calls.append("hook")
        store._run_writes(
            [Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k"}))],
            hook,
        )
        assert calls == ["hook"]


class TestRunTxWrites:

    def test_empty_actions_is_noop(self):
        store, ddb = _make_store()
        store._run_tx_writes([], None)
        ddb.transact_write_items.assert_not_called()

    def test_forwards_tx_items(self):
        store, ddb = _make_store()
        actions = [
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"})),
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k2"})),
        ]
        store._run_tx_writes(actions, None)
        ddb.transact_write_items.assert_called_once()
        kwargs = ddb.transact_write_items.call_args.kwargs
        assert len(kwargs["TransactItems"]) == 2
        assert "ClientRequestToken" in kwargs

    def test_transaction_canceled_becomes_transaction_failed(self):
        store, ddb = _make_store()
        ddb.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException"
        )
        actions = [
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        ]
        with pytest.raises(TransactionFailedException):
            store._run_tx_writes(actions, None)

    def test_other_client_error_translated(self):
        store, ddb = _make_store()
        ddb.transact_write_items.side_effect = _client_error(
            "ResourceNotFoundException"
        )
        actions = [
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        ]
        with pytest.raises(ResourceNotFoundException):
            store._run_tx_writes(actions, None)

    def test_substrate_error_passes_through(self):
        store, ddb = _make_store()
        ddb.transact_write_items.side_effect = InvalidArgumentException("nope")
        actions = [
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        ]
        with pytest.raises(InvalidArgumentException):
            store._run_tx_writes(actions, None)

    def test_non_client_non_substrate_error_translated(self):
        store, ddb = _make_store()
        ddb.transact_write_items.side_effect = RuntimeError("boom")
        actions = [
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        ]
        with pytest.raises(UnknownException):
            store._run_tx_writes(actions, None)

    def test_stamps_revisions_after_success(self):
        store, ddb = _make_store()
        doc = Document({"pk": "k1", store.revision_field: "r0"})
        actions = [Action(kind=ActionKind.ACTION_KIND_REPLACE, document=doc)]
        store._run_tx_writes(actions, None)
        assert doc.get_field(store.revision_field) != "r0"

    def test_before_do_hook_invoked_per_action(self):
        store, ddb = _make_store()
        calls = []
        hook = lambda classifier: calls.append("hook")
        actions = [
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"})),
            Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k2"})),
        ]
        store._run_tx_writes(actions, hook)
        assert calls == ["hook", "hook"]

    def test_exactly_100_actions_dispatches(self):
        store, ddb = _make_store()
        actions = [
            Action(
                kind=ActionKind.ACTION_KIND_PUT,
                document=Document({"pk": f"k{i}"}),
            )
            for i in range(100)
        ]
        store._run_tx_writes(actions, None)
        ddb.transact_write_items.assert_called_once()
        assert len(ddb.transact_write_items.call_args.kwargs["TransactItems"]) == 100

    def test_over_100_actions_rejected_before_dispatch(self):
        store, ddb = _make_store()
        actions = [
            Action(
                kind=ActionKind.ACTION_KIND_PUT,
                document=Document({"pk": f"k{i}"}),
            )
            for i in range(101)
        ]
        with pytest.raises(InvalidArgumentException, match="cannot exceed 100"):
            store._run_tx_writes(actions, None)
        ddb.transact_write_items.assert_not_called()


def _get_action(pk_val: str, sk_val: str | None = None, field_paths=None) -> Action:
    """Build a driver-shaped GET Action with ``.key`` populated."""
    source = {"pk": pk_val}
    if sk_val is not None:
        source["sk"] = sk_val
    action = Action(
        kind=ActionKind.ACTION_KIND_GET,
        document=Document(source),
        field_paths=field_paths,
    )
    action.key = (pk_val, sk_val) if sk_val is not None else pk_val
    return action


class TestBatchGet:

    def test_empty_actions_is_noop(self):
        store, ddb = _make_store()
        store._run_gets([], None, 100)
        ddb.batch_get_item.assert_not_called()

    def test_forwards_keys_and_table(self):
        store, ddb = _make_store()
        ddb.batch_get_item.return_value = {
            "Responses": {"tbl": [{"pk": {"S": "k"}, "name": {"S": "n"}}]}
        }
        action = _get_action("k")
        store._run_gets([action], None, 100)
        ddb.batch_get_item.assert_called_once()
        request_items = ddb.batch_get_item.call_args.kwargs["RequestItems"]
        assert list(request_items.keys()) == ["tbl"]
        entry = request_items["tbl"]
        assert entry["Keys"] == [{"pk": {"S": "k"}}]
        # Java parity: BatchGetItem is always eventually consistent.
        assert entry["ConsistentRead"] is False
        assert "ProjectionExpression" not in entry
        # Response merged onto source document.
        assert action.document.get_field("name") == "n"

    def test_projection_forwarded_with_key_forced_in(self):
        store, ddb = _make_store()
        ddb.batch_get_item.return_value = {"Responses": {"tbl": []}}
        action = _get_action("k", field_paths=[["name"]])
        store._run_gets([action], None, 100)
        entry = ddb.batch_get_item.call_args.kwargs["RequestItems"]["tbl"]
        # Projection references two aliases: caller's "name" + forced "pk".
        assert "ProjectionExpression" in entry
        assert entry["ExpressionAttributeNames"] == {
            "#attr0": "name",
            "#attr1": "pk",
        }
        assert entry["ProjectionExpression"] == "#attr0,#attr1"

    def test_composite_key_forwarded(self):
        store, ddb = _make_store(sort_key="sk")
        ddb.batch_get_item.return_value = {"Responses": {"tbl": []}}
        action = _get_action("k", "s")
        store._run_gets([action], None, 100)
        entry = ddb.batch_get_item.call_args.kwargs["RequestItems"]["tbl"]
        assert entry["Keys"] == [{"pk": {"S": "k"}, "sk": {"S": "s"}}]

    def test_dispatches_response_rows_by_key(self):
        store, ddb = _make_store(sort_key="sk")
        ddb.batch_get_item.return_value = {
            "Responses": {
                "tbl": [
                    {"pk": {"S": "U2"}, "sk": {"S": "eu"}, "name": {"S": "Bob"}},
                    {"pk": {"S": "U1"}, "sk": {"S": "us"}, "name": {"S": "Alice"}},
                ]
            }
        }
        a1 = _get_action("U1", "us")
        a2 = _get_action("U2", "eu")
        store._run_gets([a1, a2], None, 100)
        # Even though DynamoDB returned rows in reverse order, we must
        # route each response back to the caller's Document by key.
        assert a1.document.get_field("name") == "Alice"
        assert a2.document.get_field("name") == "Bob"

    def test_missing_items_silently_ignored(self):
        store, ddb = _make_store()
        # DynamoDB returned only one of the two requested keys.
        ddb.batch_get_item.return_value = {
            "Responses": {"tbl": [{"pk": {"S": "k1"}, "name": {"S": "one"}}]}
        }
        a1 = _get_action("k1")
        a2 = _get_action("k2")
        store._run_gets([a1, a2], None, 100)
        assert a1.document.get_field("name") == "one"
        # Missing keys leave the caller's document untouched — no raise.
        assert not a2.document.has_field("name")

    def test_client_error_translated(self):
        store, ddb = _make_store()
        ddb.batch_get_item.side_effect = _client_error("ResourceNotFoundException")
        with pytest.raises(ResourceNotFoundException):
            store._run_gets([_get_action("k")], None, 100)

    def test_substrate_error_passes_through(self):
        store, ddb = _make_store()
        ddb.batch_get_item.side_effect = InvalidArgumentException("bad")
        with pytest.raises(InvalidArgumentException):
            store._run_gets([_get_action("k")], None, 100)

    def test_before_do_invoked_once_per_batch(self):
        store, ddb = _make_store()
        ddb.batch_get_item.return_value = {"Responses": {"tbl": []}}
        calls: list[str] = []
        hook = lambda classifier: calls.append("hook")
        # Three actions in one batch → hook fires once, not three times.
        store._run_gets(
            [_get_action("k1"), _get_action("k2"), _get_action("k3")], hook, 100
        )
        assert calls == ["hook"]
        assert ddb.batch_get_item.call_count == 1

    def test_batch_size_splits_calls(self):
        # 5 actions with batch_size=2 → ceil(5/2) = 3 BatchGetItem calls.
        store, ddb = _make_store()
        ddb.batch_get_item.return_value = {"Responses": {"tbl": []}}
        actions = [_get_action(f"k{i}") for i in range(5)]
        store._run_gets(actions, None, 2)
        assert ddb.batch_get_item.call_count == 3
        # Reconstruct the union of keys sent across batches; every action
        # is represented exactly once.
        all_keys: list[str] = []
        for call in ddb.batch_get_item.call_args_list:
            entry = call.kwargs["RequestItems"]["tbl"]
            all_keys.extend(k["pk"]["S"] for k in entry["Keys"])
        assert sorted(all_keys) == [f"k{i}" for i in range(5)]

    def test_different_field_paths_ride_different_batches(self):
        # Two actions with distinct projections cannot share a batch.
        store, ddb = _make_store()
        ddb.batch_get_item.return_value = {"Responses": {"tbl": []}}
        a1 = _get_action("k1", field_paths=[["name"]])
        a2 = _get_action("k2", field_paths=[["age"]])
        store._run_gets([a1, a2], None, 100)
        assert ddb.batch_get_item.call_count == 2

    def test_unprocessed_keys_retried_and_completes(self):
        # First call returns k2 in UnprocessedKeys; the retry returns it.
        # Both callers' Documents must end up populated.
        store, ddb = _make_store()
        a1 = _get_action("k1")
        a2 = _get_action("k2")
        ddb.batch_get_item.side_effect = [
            {
                "Responses": {"tbl": [{"pk": {"S": "k1"}, "name": {"S": "one"}}]},
                "UnprocessedKeys": {"tbl": {"Keys": [{"pk": {"S": "k2"}}]}},
            },
            {
                "Responses": {"tbl": [{"pk": {"S": "k2"}, "name": {"S": "two"}}]},
            },
        ]
        with patch("multicloud.docstore.docstore_aws.aws_docstore.time.sleep") as sleep:
            store._run_gets([a1, a2], None, 100)
        assert ddb.batch_get_item.call_count == 2
        # Retry request contained only the unprocessed subset.
        retry_entry = ddb.batch_get_item.call_args_list[1].kwargs["RequestItems"]["tbl"]
        assert retry_entry["Keys"] == [{"pk": {"S": "k2"}}]
        # Exponential backoff kicked in exactly once (50ms).
        sleep.assert_called_once_with(0.05)
        assert a1.document.get_field("name") == "one"
        assert a2.document.get_field("name") == "two"

    def test_unprocessed_keys_exhausted_raises(self):
        # Every attempt returns the same UnprocessedKeys → after 3 tries
        # the driver raises ResourceExhaustedException so a throttled
        # read cannot masquerade as a missing item.
        store, ddb = _make_store()
        ddb.batch_get_item.return_value = {
            "Responses": {"tbl": []},
            "UnprocessedKeys": {"tbl": {"Keys": [{"pk": {"S": "k"}}]}},
        }
        with patch("multicloud.docstore.docstore_aws.aws_docstore.time.sleep") as sleep:
            with pytest.raises(ResourceExhaustedException) as exc_info:
                store._run_gets([_get_action("k")], None, 100)
        # 3 attempts total, backoff between attempts (50ms, 100ms).
        assert ddb.batch_get_item.call_count == 3
        assert sleep.call_args_list == [((0.05,),), ((0.1,),)]
        assert "1 keys" in str(exc_info.value)
        assert "3 attempts" in str(exc_info.value)

    def test_unknown_response_key_is_ignored(self):
        # DynamoDB returns a row whose key isn't in the request slice
        # (defensive path — should not raise, should not touch any
        # caller's Document).
        store, ddb = _make_store()
        ddb.batch_get_item.return_value = {
            "Responses": {
                "tbl": [
                    {"pk": {"S": "k1"}, "name": {"S": "one"}},
                    {"pk": {"S": "unknown"}, "name": {"S": "ghost"}},
                ]
            }
        }
        a1 = _get_action("k1")
        store._run_gets([a1], None, 100)
        assert a1.document.get_field("name") == "one"

    def test_degenerate_range_is_noop(self):
        # start > end short-circuits without any RPC.
        store, ddb = _make_store()
        store._batch_get([_get_action("k")], None, start=5, end=1)
        ddb.batch_get_item.assert_not_called()

    def test_out_of_bounds_range_is_noop(self):
        # end past the slice short-circuits without any RPC.
        store, ddb = _make_store()
        store._batch_get([_get_action("k")], None, start=0, end=99)
        ddb.batch_get_item.assert_not_called()


class TestRunActionsDispatch:

    def test_write_and_get_paths_both_called(self):
        store, ddb = _make_store()
        ddb.batch_get_item.return_value = {"Responses": {"tbl": [{"pk": {"S": "k2"}}]}}
        # Client-layer flow: shape actions first
        write = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        write.key = "k1"
        write.index = 0
        get = Action(kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k2"}))
        get.key = "k2"
        get.index = 1
        store.run_actions([write, get])
        ddb.put_item.assert_called_once()
        ddb.batch_get_item.assert_called_once()

    def test_atomic_group_dispatches_to_transact(self):
        store, ddb = _make_store()
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
        ddb.transact_write_items.assert_called_once()

    def test_before_gets_run_inline_before_writes(self):
        store, ddb = _make_store()
        # Simulate: get k1 then write k1 → should promote get to before-gets.
        get = Action(kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k1"}))
        get.key = "k1"
        get.index = 0
        write = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        write.key = "k1"
        write.index = 1

        call_order: list[str] = []

        def _record_batch_get(**kwargs):
            call_order.append("get")
            return {"Responses": {"tbl": [{"pk": {"S": "k1"}}]}}

        def _record_put(**kwargs):
            call_order.append("put")

        ddb.batch_get_item.side_effect = _record_batch_get
        ddb.put_item.side_effect = _record_put

        store.run_actions([get, write])
        assert call_order == ["get", "put"]

    def test_after_gets_run_last(self):
        store, ddb = _make_store()
        # write k1 then get k1 → get moves to after_gets
        write = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        write.key = "k1"
        write.index = 0
        get = Action(kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k1"}))
        get.key = "k1"
        get.index = 1
        call_order: list[str] = []
        ddb.batch_get_item.side_effect = lambda **kwargs: (
            call_order.append("get") or {"Responses": {"tbl": [{"pk": {"S": "k1"}}]}}
        )
        ddb.put_item.side_effect = lambda **kwargs: call_order.append("put")
        store.run_actions([write, get])
        assert call_order == ["put", "get"]

    def test_after_gets_use_consistent_read(self):
        # Batch: get(k1) → write(k1) → get(k2) → get(k1) partitions into
        # before_gets=[k1], get_list=[k2], after_gets=[k1]. before_gets
        # and get_list stay eventually consistent; after_gets must use
        # ConsistentRead=True for read-your-writes on k1.
        store, ddb = _make_store()
        before_get = Action(
            kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k1"})
        )
        before_get.key = "k1"
        before_get.index = 0
        write = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        write.key = "k1"
        write.index = 1
        unrelated_get = Action(
            kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k2"})
        )
        unrelated_get.key = "k2"
        unrelated_get.index = 2
        after_get = Action(
            kind=ActionKind.ACTION_KIND_GET, document=Document({"pk": "k1"})
        )
        after_get.key = "k1"
        after_get.index = 3

        ddb.batch_get_item.return_value = {"Responses": {"tbl": []}}
        store.run_actions([before_get, write, unrelated_get, after_get])

        # before_gets runs inline first, get_list runs on the executor
        # while writes are in flight, after_gets runs inline last.
        calls = ddb.batch_get_item.call_args_list
        assert len(calls) == 3
        before_entry = calls[0].kwargs["RequestItems"]["tbl"]
        get_list_entry = calls[1].kwargs["RequestItems"]["tbl"]
        after_entry = calls[2].kwargs["RequestItems"]["tbl"]
        assert before_entry["ConsistentRead"] is False
        assert get_list_entry["ConsistentRead"] is False
        assert after_entry["ConsistentRead"] is True

    def test_runtime_error_in_fanout_translated(self):
        store, ddb = _make_store()
        ddb.put_item.side_effect = RuntimeError("boom")
        write = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k1"}))
        write.key = "k1"
        write.index = 0
        with pytest.raises(UnknownException):
            store.run_actions([write])

    def test_non_atomic_delete_fanout_dispatches_delete_item(self):
        store, ddb = _make_store()
        d1 = Action(kind=ActionKind.ACTION_KIND_DELETE, document=Document({"pk": "k1"}))
        d1.key = "k1"
        d1.index = 0
        d2 = Action(kind=ActionKind.ACTION_KIND_DELETE, document=Document({"pk": "k2"}))
        d2.key = "k2"
        d2.index = 1
        store.run_actions([d1, d2])
        # Both non-atomic deletes go through delete_item, not transact
        assert ddb.delete_item.call_count == 2
        ddb.transact_write_items.assert_not_called()

    def test_atomic_group_mixes_put_and_delete(self):
        store, ddb = _make_store()
        p = Action(
            kind=ActionKind.ACTION_KIND_PUT,
            document=Document({"pk": "k1"}),
            in_atomic_write=True,
        )
        p.key = "k1"
        p.index = 0
        d = Action(
            kind=ActionKind.ACTION_KIND_DELETE,
            document=Document({"pk": "k2"}),
            in_atomic_write=True,
        )
        d.key = "k2"
        d.index = 1
        store.run_actions([p, d])
        ddb.transact_write_items.assert_called_once()
        tx_items = ddb.transact_write_items.call_args.kwargs["TransactItems"]
        shapes = [next(iter(item.keys())) for item in tx_items]
        assert shapes == ["Put", "Delete"]
        # No per-op delete_item / put_item on the atomic path
        ddb.delete_item.assert_not_called()
        ddb.put_item.assert_not_called()

    def test_delete_revision_mismatch_maps_to_failed_precondition(self):
        store, ddb = _make_store(revision_field="rev")
        ddb.delete_item.side_effect = _client_error("ConditionalCheckFailedException")
        d = Action(
            kind=ActionKind.ACTION_KIND_DELETE,
            document=Document({"pk": "k1", "rev": "r0"}),
        )
        d.key = "k1"
        d.index = 0
        with pytest.raises(FailedPreconditionException):
            store.run_actions([d])
