"""AWS DynamoDB implementation of :class:`BaseDocStore`.

Owns the write / read paths against DynamoDB:

* :meth:`run_actions` — five-bucket dispatch (before-gets, writes,
  atomic writes, gets, after-gets). Writes are fanned out through the
  store's internal thread pool; atomic writes go through DynamoDB
  ``TransactWriteItems``.
* :meth:`_run_writes` — prepare non-atomic writes serially, dispatch
  each ``op.run`` through the store's thread pool, wait for every
  future, then stamp fresh revisions back onto the caller's documents.
* :meth:`_new_write_operation` / :meth:`_new_put` — encode the
  document, auto-generate the partition key when missing on CREATE,
  optionally stamp a fresh revision UUID, and build the appropriate
  precondition.
* :meth:`_run_put` — execute the ``PutItem`` and translate
  ``ConditionalCheckFailedException`` per action kind.* :meth:`_batch_get` — serves one chunk of GET actions (dispatched
  by the base class ``_run_gets``) with ``BatchGetItem``, retrying
  ``UnprocessedKeys`` up to 3 times. Missing items are silently
  ignored; still-unprocessed keys raise
  :class:`ResourceExhaustedException`.
* :meth:`translate_error` — surface AWS SDK exceptions as the common
  :class:`SubstrateSdkException` hierarchy.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from boto3.dynamodb.types import (  # type: ignore[import-not-found]
    TypeDeserializer,
    TypeSerializer,
)
from botocore.exceptions import (  # type: ignore[import-not-found]
    BotoCoreError,
    ClientError,
    NoCredentialsError,
)
from multicloud.aws_common.aws_credentials import get_session
from multicloud.aws_common.error_mapping import AWS_THROTTLING_CODES
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
    serialize_object,
    wait_for_all,
)

_logger = logging.getLogger(__name__)

# DynamoDB TransactWriteItems accepts at most 100 actions per request.
_MAX_TRANSACT_WRITE_ITEMS = 100


@dataclass
class WriteOperation:
    """A prepared write ready to be executed.

    * ``action`` — the source Action (kind + document).
    * ``write_item`` — the transactional shape used by
      ``TransactWriteItems`` when this write is in an atomic group.
    * ``new_partition_key`` / ``new_revision`` — values freshly
      generated at prepare time so a retry uses the same UUIDs.
    * ``run`` — callable that executes the non-atomic form of the
      write; ``_run_writes`` invokes this on the worker thread.
    """

    action: Action
    write_item: Optional[Dict[str, Any]] = None
    new_partition_key: Optional[str] = None
    new_revision: Optional[str] = None
    run: Optional[Callable[[], None]] = None


@DOCSTORE_REGISTRY.register("aws")
class AwsDocStore(BaseDocStore):
    """DynamoDB-backed :class:`BaseDocStore`."""

    def __init__(
        self,
        collection_options: CollectionOptions,
        *,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        credentials_overrider: Optional[CredentialsOverrider] = None,
        dynamodb_client: Any = None,
    ) -> None:
        super().__init__(
            collection_options=collection_options,
            credentials_overrider=credentials_overrider,
        )
        self._serializer = TypeSerializer()

        if dynamodb_client is not None:
            self._ddb = dynamodb_client
            self._session = None
            return

        self._session = get_session(credentials_overrider, region)
        client_kwargs: Dict[str, Any] = {"region_name": region}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        self._ddb = self._session.client("dynamodb", **client_kwargs)

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate an AWS SDK / DynamoDB exception into the common hierarchy.

        Walks the cause chain up to five levels deep so a wrapped
        :class:`ClientError` is still recognized. :class:`ClientError`
        is dispatched by DynamoDB error code (via
        :meth:`_map_client_error`); non-client :class:`BotoCoreError`
        types map to :class:`InvalidArgumentException`; unrecognized
        errors surface as :class:`UnknownException`. Already-translated
        :class:`SubstrateSdkException` instances pass through unchanged.

        :param exc: The exception raised by the AWS SDK layer.
        :return: The equivalent :class:`SubstrateSdkException`.
        """
        if isinstance(exc, SubstrateSdkException):
            return exc

        # Walk the cause chain up to five levels deep so a wrapped
        # ClientError still gets classified.
        current: Optional[BaseException] = exc
        depth = 0
        while current is not None and depth < 5:
            if isinstance(current, NoCredentialsError):
                return UnAuthorizedException(str(exc))
            if isinstance(current, BotoCoreError) and not isinstance(
                current, ClientError
            ):
                return InvalidArgumentException(str(exc))
            if isinstance(current, ClientError):
                return self._map_client_error(current, exc)
            current = current.__cause__ or current.__context__
            depth += 1

        return UnknownException(str(exc))

    @staticmethod
    def _map_client_error(
        client_error: ClientError, original: Exception
    ) -> SubstrateSdkException:
        error_info = client_error.response.get("Error", {})
        code = error_info.get("Code", "")
        status = client_error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")

        if code == "ConditionalCheckFailedException":
            # Only reached when translate_error is called with a raw
            # ClientError that wasn't already caught by _run_put. Treat
            # as a precondition failure by default; _run_put maps it to
            # ResourceAlreadyExistsException on CREATE explicitly.
            return FailedPreconditionException(str(original))
        if code == "ResourceNotFoundException":
            return ResourceNotFoundException(str(original))
        if code in ("ResourceInUseException",):
            return ResourceAlreadyExistsException(str(original))
        if code in AWS_THROTTLING_CODES or status == 429:
            return ResourceExhaustedException(str(original))
        if (
            code
            in (
                "AccessDeniedException",
                "AccessDenied",
                "UnrecognizedClientException",
                "ExpiredTokenException",
            )
            or status == 403
        ):
            return UnAuthorizedException(str(original))
        if code in ("RequestTimeout", "RequestTimeoutException"):
            return DeadlineExceededException(str(original))
        if code in (
            "ValidationException",
            "InvalidParameterValue",
            "SerializationException",
        ):
            return InvalidArgumentException(str(original))
        if code == "TransactionCanceledException":
            return TransactionFailedException(str(original))
        if code == "InternalServerError":
            return UnknownException(str(original))
        return UnknownException(str(original))

    def close(self) -> None:
        """Close the boto3 DynamoDB client we own, then delegate upward.

        Only closes ``self._ddb`` when this store constructed it (i.e.
        ``self._session`` is set). A caller-injected ``dynamodb_client``
        is not ours to close.
        """
        if self._session is not None:
            try:
                self._ddb.close()
            except Exception:  # noqa: BLE001 — close must not raise
                pass
        super().close()

    def run_actions(
        self,
        actions: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]] = None,
    ) -> None:
        """Execute a batch of driver-shaped actions against DynamoDB.

        Splits the batch into five ordered buckets (before-gets,
        writes, atomic writes, gets, after-gets). Before-gets run
        inline so writes can see their result; writes, atomic writes,
        and gets are fanned out through the store's internal thread
        pool and awaited together; after-gets run last so they can see
        the post-write state. Atomic writes go through
        ``TransactWriteItems``; every other write goes through
        ``PutItem``.

        :param actions: Actions previously shaped by
            :meth:`ActionList.run`.
        :param before_do: Optional per-action hook invoked immediately
            before each backend call.
        :raises SubstrateSdkException: Any AWS error surfaces as a
            translated exception from :meth:`translate_error`.
        """
        before_gets, get_list, write_list, atomic_write_list, after_gets = (
            group_actions(actions)
        )

        # Before-gets must complete before any write (they depend on
        # pre-write state).
        if before_gets:
            self._run_gets(before_gets, before_do, batch_size=100)

        futures: List[Future[Any]] = []
        if write_list:
            futures.append(
                self._executor.submit(self._run_writes, write_list, before_do)
            )
        if atomic_write_list:
            futures.append(
                self._executor.submit(self._run_tx_writes, atomic_write_list, before_do)
            )
        if get_list:
            futures.append(
                self._executor.submit(
                    self._run_gets, get_list, before_do, batch_size=100
                )
            )

        try:
            wait_for_all(futures)
        except SubstrateSdkException:
            raise
        except Exception as exc:
            raise self.translate_error(exc) from exc

        if after_gets:
            self._run_gets(
                after_gets,
                before_do,
                batch_size=100,
                consistent_read=True,
            )

    def _run_writes(
        self,
        actions: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]],
    ) -> None:
        """Execute non-atomic writes concurrently, then stamp new revisions.

        Prepares a :class:`WriteOperation` per action, submits every
        write's runnable to the store's thread-pool executor, waits for
        all futures to settle, and — only then — writes the fresh
        revision back onto each caller's Document via
        :meth:`_update_revision`. If any write raises, every other
        future is still awaited; the first exception is re-raised after
        the fan-out completes and revisions are not stamped.

        :param actions: Non-atomic write actions to execute.
        :param before_do: Optional per-action hook invoked on the
            executor worker thread immediately before that action's
            backend call.
        :raises SubstrateSdkException: The first translated error from
            any failed write.
        """
        if not actions:
            return
        ops: List[WriteOperation] = [
            self._new_write_operation(action, before_do) for action in actions
        ]
        futures: List[Future[Any]] = [
            self._executor.submit(op.run) for op in ops if op.run is not None
        ]
        wait_for_all(futures)
        self._update_revision(ops)

    def _update_revision(self, write_operations: Sequence[WriteOperation]) -> None:
        """Write fresh revisions back onto the source Documents.

        Runs post-write so a failed ``PutItem`` leaves the caller's
        Document unchanged.
        """
        for op in write_operations:
            if op.new_revision:
                op.action.document.set_field(self.revision_field, op.new_revision)

    def _run_tx_writes(
        self,
        actions: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]],
    ) -> None:
        """Execute an atomic write group via DynamoDB TransactWriteItems.

        Prepares each Action into a :class:`WriteOperation` (via
        :meth:`_new_write_operation`), collects the transactional
        payloads, calls ``TransactWriteItems`` under a fresh UUID
        ``ClientRequestToken``, then stamps new revisions.
        """
        if not actions:
            return
        if len(actions) > _MAX_TRANSACT_WRITE_ITEMS:
            raise InvalidArgumentException(
                f"atomic write groups cannot exceed "
                f"{_MAX_TRANSACT_WRITE_ITEMS} actions "
                f"(got {len(actions)})"
            )
        write_operations: List[WriteOperation] = []
        tx_writes: List[Dict[str, Any]] = []
        for action in actions:
            if before_do is not None:
                before_do(lambda _cls: True)
            op = self._new_write_operation(action)
            write_operations.append(op)
            if op.write_item is not None:
                tx_writes.append(op.write_item)
        try:
            self._ddb.transact_write_items(
                TransactItems=tx_writes,
                ClientRequestToken=str(uuid.uuid4()),
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                raise TransactionFailedException(str(exc)) from exc
            raise self.translate_error(exc) from exc
        except SubstrateSdkException:
            raise
        except Exception as exc:
            raise self.translate_error(exc) from exc
        self._update_revision(write_operations)

    def _batch_get(
        self,
        gets: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]],
        start: int,
        end: int,
        consistent_read: bool = False,
    ) -> None:
        """Execute one BatchGetItem RPC for ``gets[start..end]`` (inclusive).

        Encodes the primary key of every action in the slice into
        DynamoDB's ``AttributeValue`` shape, builds a single
        ``KeysAndAttributes`` (with an aliased ``ProjectionExpression``
        when the caller asked for specific fields — force-adding the
        partition key and sort key so responses can be dispatched by
        key), calls ``BatchGetItem``, and merges each returned row back
        into the caller's :class:`Document`. Missing items are silently
        ignored — the caller's Document is left untouched.

        ``BatchGetItem`` may return a subset of the requested keys in
        ``UnprocessedKeys`` (throttling, 16 MB response cap, ...).
        boto3 does not auto-retry those, so up to 3 attempts are made
        with exponential backoff, retrying only the unprocessed
        subset. If keys are still unprocessed after the final attempt,
        :class:`ResourceExhaustedException` is raised so a throttled
        read cannot masquerade as a missing item.

        :param gets: A homogeneous field-path group of GET actions.
        :param before_do: Optional hook invoked once per batch,
            immediately before the RPC.
        :param start: Inclusive start index into ``gets``.
        :param end: Inclusive end index into ``gets``.
        :param consistent_read: Forwarded as ``ConsistentRead`` in the
            ``KeysAndAttributes`` payload. True gives read-your-writes
            semantics at the cost of one additional RCU per item.
        :raises ResourceExhaustedException: If ``UnprocessedKeys`` is
            still non-empty after 3 attempts.
        """
        if not gets or start > end or start < 0 or end >= len(gets):
            return

        keys_list: List[Dict[str, Any]] = []
        action_by_key: Dict[str, Action] = {}
        for i in range(start, end + 1):
            encoded_key = self._encode_key(gets[i].document)
            keys_list.append(encoded_key)
            action_by_key[serialize_object(encoded_key)] = gets[i]

        keys_and_attrs: Dict[str, Any] = {
            "Keys": keys_list,
            "ConsistentRead": consistent_read,
        }

        first = gets[start]
        if first.field_paths:
            projection, names = self._build_projection(first.field_paths)
            keys_and_attrs["ProjectionExpression"] = projection
            keys_and_attrs["ExpressionAttributeNames"] = names

        if before_do is not None:
            before_do(lambda _cls: True)

        table_name = self._collection_options.table_name
        deserializer = TypeDeserializer()
        pk_name = self._collection_options.partition_key
        sk_name = self._collection_options.sort_key

        # boto3 does not auto-retry UnprocessedKeys; retry the
        # unprocessed subset up to 3 times with exponential backoff.
        request_items: Dict[str, Any] = {table_name: keys_and_attrs}
        unprocessed: Dict[str, Any] = {}
        for attempt in range(3):
            try:
                resp = self._ddb.batch_get_item(RequestItems=request_items)
            except SubstrateSdkException:
                raise
            except Exception as exc:
                raise self.translate_error(exc) from exc

            for item in resp.get("Responses", {}).get(table_name, []):
                response_key: Dict[str, Any] = {pk_name: item[pk_name]}
                if sk_name:
                    response_key[sk_name] = item[sk_name]
                action = action_by_key.get(serialize_object(response_key))
                if action is None:
                    continue
                for attr_name, attr_value in item.items():
                    action.document.set_field(
                        attr_name, deserializer.deserialize(attr_value)
                    )

            unprocessed = resp.get("UnprocessedKeys", {}).get(table_name) or {}
            if not unprocessed.get("Keys"):
                return
            if attempt < 2:
                time.sleep(0.05 * (2**attempt))  # 50ms, 100ms
                request_items = {table_name: unprocessed}

        raise ResourceExhaustedException(
            f"BatchGetItem: {len(unprocessed.get('Keys', []))} keys "
            "unprocessed after 3 attempts"
        )

    def _encode_key(self, document: Any) -> Dict[str, Any]:
        """Encode a Document's primary key into an AttributeValue map."""
        pk_name = self._collection_options.partition_key
        key: Dict[str, Any] = {
            pk_name: self._serializer.serialize(document.get_field(pk_name))
        }
        sk_name = self._collection_options.sort_key
        if sk_name:
            key[sk_name] = self._serializer.serialize(document.get_field(sk_name))
        return key

    def _build_projection(
        self, field_paths: List[List[str]]
    ) -> Tuple[str, Dict[str, str]]:
        """Build a ProjectionExpression + ExpressionAttributeNames pair.

        Every segment of every path becomes ``#attrN`` so DynamoDB
        reserved words don't collide. Key attributes are auto-added when
        absent so the provider can dispatch the returned item back to
        its Action by key.
        """
        names: Dict[str, str] = {}
        parts: List[str] = []
        has_pk = has_sk = False
        pk_name = self._collection_options.partition_key
        sk_name = self._collection_options.sort_key
        for fp in field_paths:
            segs: List[str] = []
            for seg in fp:
                alias = f"#attr{len(names)}"
                names[alias] = seg
                segs.append(alias)
            parts.append(".".join(segs))
            joined = ".".join(fp)
            if joined == pk_name:
                has_pk = True
            if sk_name and joined == sk_name:
                has_sk = True
        if not has_pk:
            alias = f"#attr{len(names)}"
            names[alias] = pk_name
            parts.append(alias)
        if sk_name and not has_sk:
            alias = f"#attr{len(names)}"
            names[alias] = sk_name
            parts.append(alias)
        return ",".join(parts), names

    def _new_write_operation(
        self,
        action: Action,
        before_do: Optional[Callable[[Callable[[Any], bool]], None]] = None,
    ) -> WriteOperation:
        """Dispatch on ``action.kind`` and build a :class:`WriteOperation`.

        CREATE / PUT / REPLACE funnel through :meth:`_new_put` —
        DynamoDB's ``PutItem`` is the natural write-side primitive for
        each, differing only in the precondition attached to the
        request. DELETE funnels through :meth:`_new_delete`. UPDATE is
        not yet wired and raises so accidental use surfaces clearly.

        :param action: The write action to prepare.
        :param before_do: Optional hook invoked from inside the returned
            ``WriteOperation.run`` closure — i.e. on the executor worker
            thread — immediately before the backend call. Threaded here
            (rather than fired by the caller) so non-atomic writes see
            the same ``beforeDo`` ordering as the Java driver: on the
            executor thread, per prepared write. Atomic writes call
            :meth:`_new_write_operation` without ``before_do`` and
            invoke the hook inline; there is no per-op executor run
            for the transactional shape.
        """
        if action.kind in (
            ActionKind.ACTION_KIND_CREATE,
            ActionKind.ACTION_KIND_PUT,
            ActionKind.ACTION_KIND_REPLACE,
        ):
            return self._new_put(action, before_do)
        if action.kind == ActionKind.ACTION_KIND_DELETE:
            return self._new_delete(action, before_do)
        raise UnsupportedOperationException(
            f"Action kind {action.kind.name} not implemented on this port."
        )

    def _new_put(
        self,
        action: Action,
        before_do: Optional[Callable[[Callable[[Any], bool]], None]] = None,
    ) -> WriteOperation:
        """Build the prepared ``PutItem`` for a CREATE / PUT / REPLACE action.

        Encodes every field on the document into DynamoDB's
        ``AttributeValue`` shape via boto3's ``TypeSerializer``. On
        CREATE, a missing partition key is synthesized as a UUID and
        stamped back onto the document so the caller can see the
        assigned key; on any other kind, a missing key is a hard error.
        When the document carries the revision field, a fresh UUID
        revision is generated and stamped onto both the encoded item
        and (post-write) the caller's document via
        :meth:`_update_revision`. Finally the precondition is composed
        from :meth:`_build_precondition` plus its expression-attribute
        pair.

        :param action: The CREATE / PUT / REPLACE action to prepare.
        :param before_do: Optional per-write hook captured inside the
            returned ``run`` closure and invoked on the worker thread
            immediately before ``_run_put``.
        :return: A :class:`WriteOperation` carrying both the executable
            callable (used by non-atomic writes) and the transactional
            payload (used by ``TransactWriteItems``).
        :raises InvalidArgumentException: If the partition key is
            missing on a non-CREATE action, or the sort key is missing
            on a composite-key table.
        """
        options = self._collection_options
        doc = action.document

        # Encode every field currently present on the document.
        encoded_item: Dict[str, Any] = {
            name: self._serializer.serialize(doc.get_field(name))
            for name in doc.field_names()
        }

        new_partition_key: Optional[str] = None
        pk_name = options.partition_key
        if pk_name not in encoded_item or self._is_null(encoded_item[pk_name]):
            if action.kind != ActionKind.ACTION_KIND_CREATE:
                raise InvalidArgumentException(
                    f"missing partition key {pk_name!r} for {action.kind.name}"
                )
            new_partition_key = str(uuid.uuid4())
            doc.set_field(pk_name, new_partition_key)
            encoded_item[pk_name] = self._serializer.serialize(new_partition_key)

        # Sort key must be present for any composite-key table.
        sk_name = options.sort_key
        if sk_name and (
            sk_name not in encoded_item or self._is_null(encoded_item[sk_name])
        ):
            raise InvalidArgumentException(
                f"missing sort key {sk_name!r} for {action.kind.name}"
            )

        # Only stamp a fresh revision when the caller opted in by
        # carrying the revision field on the document.
        new_revision: Optional[str] = None
        if doc.has_field(self.revision_field):
            new_revision = str(uuid.uuid4())
            encoded_item[self.revision_field] = self._serializer.serialize(new_revision)

        condition_expression = self._build_precondition(action)
        expression_attribute_names = self._precondition_attribute_names(action)
        expression_attribute_values = self._precondition_attribute_values(action)

        put_payload: Dict[str, Any] = {
            "TableName": options.table_name,
            "Item": encoded_item,
        }
        if condition_expression is not None:
            put_payload["ConditionExpression"] = condition_expression
        if expression_attribute_names:
            put_payload["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values:
            put_payload["ExpressionAttributeValues"] = expression_attribute_values

        def _run() -> None:
            if before_do is not None:
                before_do(lambda _cls: True)
            self._run_put(
                action=action,
                item=encoded_item,
                condition_expression=condition_expression,
                expression_attribute_names=expression_attribute_names,
                expression_attribute_values=expression_attribute_values,
            )

        return WriteOperation(
            action=action,
            write_item={"Put": put_payload},
            new_partition_key=new_partition_key,
            new_revision=new_revision,
            run=_run,
        )

    def _new_delete(
        self,
        action: Action,
        before_do: Optional[Callable[[Callable[[Any], bool]], None]] = None,
    ) -> WriteOperation:
        """Build the prepared ``DeleteItem`` for a DELETE action.

        Encodes the document's primary key into an ``AttributeValue``
        map, composes an optional revision precondition, and returns
        both the transactional payload (used by ``TransactWriteItems``)
        and a callable that runs the non-atomic ``DeleteItem`` on the
        worker thread.

        :param action: The DELETE action to prepare.
        :param before_do: Optional per-write hook captured inside the
            returned ``run`` closure and invoked on the worker thread
            immediately before :meth:`_run_delete`.
        :return: A :class:`WriteOperation` carrying both the executable
            callable and the transactional payload.
        :raises InvalidArgumentException: If the partition key or (when
            configured) the sort key is missing on the document.
        """
        options = self._collection_options
        doc = action.document

        pk_name = options.partition_key
        if not doc.has_field(pk_name) or doc.get_field(pk_name) in (None, ""):
            raise InvalidArgumentException(
                f"missing partition key {pk_name!r} for {action.kind.name}"
            )
        sk_name = options.sort_key
        if sk_name and (
            not doc.has_field(sk_name) or doc.get_field(sk_name) in (None, "")
        ):
            raise InvalidArgumentException(
                f"missing sort key {sk_name!r} for {action.kind.name}"
            )

        key = self._encode_key(doc)

        condition_expression = self._build_precondition(action)
        expression_attribute_names = self._precondition_attribute_names(action)
        expression_attribute_values = self._precondition_attribute_values(action)

        delete_payload: Dict[str, Any] = {
            "TableName": options.table_name,
            "Key": key,
        }
        if condition_expression is not None:
            delete_payload["ConditionExpression"] = condition_expression
        if expression_attribute_names:
            delete_payload["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values:
            delete_payload["ExpressionAttributeValues"] = expression_attribute_values

        def _run() -> None:
            if before_do is not None:
                before_do(lambda _cls: True)
            self._run_delete(
                key=key,
                condition_expression=condition_expression,
                expression_attribute_names=expression_attribute_names,
                expression_attribute_values=expression_attribute_values,
            )

        return WriteOperation(
            action=action,
            write_item={"Delete": delete_payload},
            new_partition_key=None,
            new_revision=None,
            run=_run,
        )

    def _run_delete(
        self,
        key: Dict[str, Any],
        condition_expression: Optional[str],
        expression_attribute_names: Optional[Dict[str, str]],
        expression_attribute_values: Optional[Dict[str, Any]],
    ) -> None:
        """Execute a single ``DeleteItem`` and classify precondition failures.

        A ``ConditionalCheckFailedException`` is surfaced as
        :class:`FailedPreconditionException` — the revision guard did
        not match. Every other AWS error surfaces through
        :meth:`translate_error`.

        :param key: Serialized primary-key ``AttributeValue`` map.
        :param condition_expression: Optional DynamoDB
            ``ConditionExpression``; ``None`` means an unconditional
            delete.
        :param expression_attribute_names: Placeholder-to-attribute
            mapping referenced by ``condition_expression``.
        :param expression_attribute_values: Placeholder-to-value
            mapping referenced by ``condition_expression``.
        :raises SubstrateSdkException: Any AWS error, mapped to the
            appropriate translated exception.
        """
        kwargs: Dict[str, Any] = {
            "TableName": self._collection_options.table_name,
            "Key": key,
        }
        if condition_expression is not None:
            kwargs["ConditionExpression"] = condition_expression
        if expression_attribute_names:
            kwargs["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values:
            kwargs["ExpressionAttributeValues"] = expression_attribute_values

        try:
            self._ddb.delete_item(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise FailedPreconditionException(str(exc)) from exc
            raise self.translate_error(exc) from exc
        except SubstrateSdkException:
            raise
        except Exception as exc:
            raise self.translate_error(exc) from exc

    def _build_precondition(self, action: Action) -> Optional[str]:
        """Return the DynamoDB ``ConditionExpression`` for ``action``.

        The expression is selected from the action kind × has-revision
        matrix:

        * **CREATE** → ``attribute_not_exists(#pk)`` — the row must not
          already exist.
        * **REPLACE** with revision → ``#revField = :revValue`` — the
          row must exist *and* the stored revision must match.
        * **REPLACE** without revision → ``attribute_exists(#pk)`` —
          the row must exist.
        * **PUT** with revision → ``#revField = :revValue`` — as
          REPLACE with revision.
        * **PUT** without revision → ``None`` — unconditional upsert.
        * **DELETE** with revision → ``#revField = :revValue`` — the
          stored revision must match.
        * **DELETE** without revision → ``None`` — unconditional delete.

        Placeholder names are resolved by
        :meth:`_precondition_attribute_names`; values by
        :meth:`_precondition_attribute_values`.

        :param action: The action being prepared.
        :return: The expression string, or ``None`` for unconditional
            writes.
        """
        if action.kind == ActionKind.ACTION_KIND_CREATE:
            # Uses placeholder to sidestep DynamoDB reserved-word conflicts.
            return "attribute_not_exists(#pk)"
        if action.kind == ActionKind.ACTION_KIND_REPLACE:
            return (
                "#revField = :revValue"
                if self._read_revision(action.document) is not None
                else "attribute_exists(#pk)"
            )
        if action.kind == ActionKind.ACTION_KIND_PUT:
            return (
                "#revField = :revValue"
                if self._read_revision(action.document) is not None
                else None
            )
        if action.kind == ActionKind.ACTION_KIND_DELETE:
            return (
                "#revField = :revValue"
                if self._read_revision(action.document) is not None
                else None
            )
        return None

    def _precondition_attribute_names(self, action: Action) -> Optional[Dict[str, str]]:
        """ExpressionAttributeNames map for the current precondition."""
        pk_name = self._collection_options.partition_key
        if action.kind == ActionKind.ACTION_KIND_CREATE:
            return {"#pk": pk_name}
        if action.kind == ActionKind.ACTION_KIND_REPLACE:
            return (
                {"#revField": self.revision_field}
                if self._read_revision(action.document) is not None
                else {"#pk": pk_name}
            )
        if action.kind in (
            ActionKind.ACTION_KIND_PUT,
            ActionKind.ACTION_KIND_DELETE,
        ):
            if self._read_revision(action.document) is not None:
                return {"#revField": self.revision_field}
        return None

    def _precondition_attribute_values(
        self, action: Action
    ) -> Optional[Dict[str, Any]]:
        """ExpressionAttributeValues map for the current precondition.

        Binds ``:revValue`` to the caller-supplied revision so REPLACE
        / PUT with revision guard becomes an equality check on the
        stored value. CREATE has no value bindings.
        """
        if action.kind in (
            ActionKind.ACTION_KIND_REPLACE,
            ActionKind.ACTION_KIND_PUT,
            ActionKind.ACTION_KIND_DELETE,
        ):
            rev = self._read_revision(action.document)
            if rev is not None:
                return {":revValue": self._serializer.serialize(rev)}
        return None

    def _read_revision(self, doc: Document) -> Optional[str]:
        """Return the caller-supplied revision string, type-checking it.

        The revision field, when present, must be a non-empty string.
        A non-string raises :class:`InvalidArgumentException`; a missing
        or empty field is treated as "no revision".
        """
        if not doc.has_field(self.revision_field):
            return None
        v = doc.get_field(self.revision_field)
        if v is None or v == "":
            return None
        if not isinstance(v, str):
            raise InvalidArgumentException(
                f"Invalid revision field {self.revision_field!r} type as "
                f"{type(v).__name__}, expect str."
            )
        return v

    def _run_put(
        self,
        action: Action,
        item: Dict[str, Any],
        condition_expression: Optional[str],
        expression_attribute_names: Optional[Dict[str, str]],
        expression_attribute_values: Optional[Dict[str, Any]],
    ) -> None:
        """Execute a single ``PutItem`` and classify precondition failures.

        A ``ConditionalCheckFailedException`` is translated per action
        kind: CREATE → :class:`ResourceAlreadyExistsException`, REPLACE
        → :class:`ResourceNotFoundException`, any other kind →
        :class:`FailedPreconditionException`. Every other AWS error
        surfaces through :meth:`translate_error`.

        :param action: The originating action (used to pick the right
            precondition-failure translation).
        :param item: The serialized ``AttributeValue`` map to write.
        :param condition_expression: Optional DynamoDB
            ``ConditionExpression``; ``None`` means an unconditional
            write.
        :param expression_attribute_names: Placeholder-to-attribute
            mapping referenced by ``condition_expression``.
        :param expression_attribute_values: Placeholder-to-value
            mapping referenced by ``condition_expression``.
        :raises SubstrateSdkException: Any AWS error, mapped to the
            appropriate translated exception.
        """
        kwargs: Dict[str, Any] = {
            "TableName": self._collection_options.table_name,
            "Item": item,
        }
        if condition_expression is not None:
            kwargs["ConditionExpression"] = condition_expression
        if expression_attribute_names:
            kwargs["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values:
            kwargs["ExpressionAttributeValues"] = expression_attribute_values

        try:
            self._ddb.put_item(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                if action.kind == ActionKind.ACTION_KIND_CREATE:
                    raise ResourceAlreadyExistsException(str(exc)) from exc
                if action.kind == ActionKind.ACTION_KIND_REPLACE:
                    raise ResourceNotFoundException(str(exc)) from exc
                raise FailedPreconditionException(str(exc)) from exc
            raise self.translate_error(exc) from exc
        except SubstrateSdkException:
            raise
        except Exception as exc:
            raise self.translate_error(exc) from exc

    @staticmethod
    def _is_null(attr_value: Dict[str, Any]) -> bool:
        """True when a serialized AttributeValue represents NULL."""
        return bool(attr_value.get("NULL"))
