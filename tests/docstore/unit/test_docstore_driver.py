"""Unit tests for the DocStore driver layer.

Covers the cloud-agnostic pieces: :class:`Document`, :class:`Action`,
:class:`ActionList`, :class:`CollectionOptions`, helpers
(``serialize_object``, ``validate_field_path``, ``group_actions``,
``group_by_field_path``, ``wait_for_all``) and the abstract
:class:`BaseDocStore` template-method wiring.
"""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any, Callable, List, Optional, Sequence

import pytest
from multicloud.common.exceptions import (
    InvalidArgumentException,
    SubstrateSdkException,
)
from multicloud.docstore.docstore_client.driver import (
    DEFAULT_REVISION_FIELD,
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
    wait_for_all,
)


class _StubDoc:
    """Simple attribute holder used to test the object-source branch of Document."""

    def __init__(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


class _FakeDocStore(BaseDocStore):
    """Concrete BaseDocStore that records every ``run_actions`` invocation.

    Deliberately minimal — just enough to exercise the base-class
    template-method plumbing (validation, key extraction, executor
    lifecycle) without going near a real cloud backend.
    """

    def __init__(self, collection_options: CollectionOptions) -> None:
        super().__init__(collection_options=collection_options)
        self.run_calls: List[Sequence[Action]] = []
        self.before_do_hooks: List[Optional[Callable[..., Any]]] = []

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        if isinstance(exc, SubstrateSdkException):
            return exc
        return SubstrateSdkException(str(exc))

    def run_actions(
        self,
        actions: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]] = None,
    ) -> None:
        self.run_calls.append(list(actions))
        self.before_do_hooks.append(before_do)

    def _batch_get(
        self,
        gets: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]],
        start: int,
        end: int,
        consistent_read: bool = False,
    ) -> None:  # pragma: no cover - stub for abstract requirement
        return None


def _make_store(
    *,
    partition_key: str = "pk",
    sort_key: Optional[str] = None,
    revision_field: Optional[str] = None,
    max_outstanding_action_rpcs: int = 0,
    allow_scans: bool = True,
) -> _FakeDocStore:
    return _FakeDocStore(
        CollectionOptions(
            table_name="t",
            partition_key=partition_key,
            sort_key=sort_key,
            revision_field=revision_field,
            max_outstanding_action_rpcs=max_outstanding_action_rpcs,
            allow_scans=allow_scans,
        )
    )


class TestDocument:

    def test_none_source_raises(self):
        with pytest.raises(InvalidArgumentException, match="must not be None"):
            Document(None)

    def test_dict_source_is_map(self):
        doc = Document({"a": 1})
        assert doc.is_map is True
        assert doc.source == {"a": 1}

    def test_object_source_is_not_map(self):
        doc = Document(_StubDoc(a=1))
        assert doc.is_map is False

    def test_has_and_get_field_on_dict(self):
        doc = Document({"pk": "k1", "name": "alice"})
        assert doc.has_field("pk") is True
        assert doc.has_field("missing") is False
        assert doc.get_field("pk") == "k1"
        assert doc.get_field("missing") is None

    def test_has_and_get_field_on_object(self):
        doc = Document(_StubDoc(pk="k1", name="alice"))
        assert doc.has_field("pk") is True
        assert doc.has_field("missing") is False
        assert doc.get_field("pk") == "k1"
        assert doc.get_field("missing") is None

    def test_set_field_on_dict(self):
        doc = Document({})
        doc.set_field("pk", "k")
        assert doc.source == {"pk": "k"}

    def test_set_field_on_object(self):
        stub = _StubDoc()
        doc = Document(stub)
        doc.set_field("pk", "k")
        assert stub.pk == "k"

    def test_field_names_on_dict(self):
        doc = Document({"a": 1, "b": 2})
        assert sorted(doc.field_names()) == ["a", "b"]

    def test_field_names_on_object_skips_underscored(self):
        stub = _StubDoc(a=1, b=2)
        stub._hidden = 3  # underscored attrs must not surface
        doc = Document(stub)
        assert sorted(doc.field_names()) == ["a", "b"]

    def test_get_nested_dict(self):
        doc = Document({"a": {"b": {"c": 42}}})
        assert doc.get(["a", "b", "c"]) == 42

    def test_get_nested_object(self):
        inner = _StubDoc(c=42)
        outer = _StubDoc(a=_StubDoc(b=inner))
        doc = Document(outer)
        assert doc.get(["a", "b", "c"]) == 42

    def test_get_missing_returns_none(self):
        doc = Document({"a": {"b": 1}})
        assert doc.get(["a", "missing", "c"]) is None
        assert doc.get(["missing"]) is None

    def test_get_traverses_past_none(self):
        doc = Document({"a": None})
        assert doc.get(["a", "b"]) is None

    def test_set_empty_path_raises(self):
        with pytest.raises(InvalidArgumentException, match="should not be empty"):
            Document({}).set([], "v")

    def test_set_creates_intermediates_on_dict(self):
        source: dict = {}
        doc = Document(source)
        doc.set(["a", "b", "c"], 42)
        assert source == {"a": {"b": {"c": 42}}}

    def test_set_replaces_non_dict_intermediate_on_dict(self):
        source: dict = {"a": 5}  # int at "a" must be replaced with a dict
        doc = Document(source)
        doc.set(["a", "b"], "v")
        assert source == {"a": {"b": "v"}}

    def test_set_on_object_creates_dict_intermediates(self):
        stub = _StubDoc()
        doc = Document(stub)
        doc.set(["a", "b"], 7)
        # a is set via setattr as a fresh dict, then b lives inside
        assert isinstance(stub.a, dict)
        assert stub.a == {"b": 7}

    def test_set_on_object_leaf(self):
        stub = _StubDoc()
        doc = Document(stub)
        doc.set(["a"], 9)
        assert stub.a == 9

    def test_set_on_object_replaces_non_dict_intermediate(self):
        stub = _StubDoc(a=5)
        doc = Document(stub)
        doc.set(["a", "b"], 7)
        # a's int value is replaced with a fresh dict
        assert stub.a == {"b": 7}


class TestCollectionOptions:

    def test_defaults(self):
        opts = CollectionOptions(table_name="t", partition_key="pk")
        assert opts.sort_key is None
        assert opts.allow_scans is True
        assert opts.revision_field is None
        assert opts.max_outstanding_action_rpcs == 0

    def test_all_fields(self):
        opts = CollectionOptions(
            table_name="t",
            partition_key="pk",
            sort_key="sk",
            allow_scans=False,
            revision_field="rev",
            max_outstanding_action_rpcs=4,
        )
        assert opts.sort_key == "sk"
        assert opts.allow_scans is False
        assert opts.revision_field == "rev"
        assert opts.max_outstanding_action_rpcs == 4

    def test_frozen(self):
        opts = CollectionOptions(table_name="t", partition_key="pk")
        with pytest.raises(Exception):
            opts.table_name = "other"  # type: ignore[misc]


class TestSerializeObject:

    def test_dict_is_stable_across_key_order(self):
        assert serialize_object({"a": 1, "b": 2}) == serialize_object({"b": 2, "a": 1})

    def test_tuple_becomes_list(self):
        # Two equal-content keys expressed as tuple/list serialize identically.
        assert serialize_object(("a", "b")) == serialize_object(["a", "b"])

    def test_object_uses_dict(self):
        stub = _StubDoc(a=1)
        payload = serialize_object(stub)
        assert '"a": 1' in payload

    def test_falls_back_to_str_for_non_serializable(self):
        class NoDict:
            __slots__ = ()

            def __repr__(self) -> str:
                return "<no-dict>"

        # No __dict__ + not primitive → falls through to str()
        payload = serialize_object(NoDict())
        assert "no-dict" in payload

    def test_unserializable_raises_invalid_argument(self):
        class Boom:
            def __repr__(self) -> str:
                raise ValueError("nope")

            def __str__(self) -> str:
                raise ValueError("nope")

        # __dict__ lookup returns something json can't serialize either
        # simplest: use a self-cycle via __dict__
        class Cycle:
            pass

        c = Cycle()
        c.self = c  # type: ignore[attr-defined]
        with pytest.raises(InvalidArgumentException, match="Unable to serialize"):
            serialize_object(c)


class TestValidateFieldPath:

    def test_empty_raises(self):
        with pytest.raises(
            InvalidArgumentException, match="should not be null or empty"
        ):
            validate_field_path("")

    def test_double_dot_raises(self):
        with pytest.raises(InvalidArgumentException, match="part should not be empty"):
            validate_field_path("a..b")

    def test_leading_dot_raises(self):
        with pytest.raises(InvalidArgumentException, match="part should not be empty"):
            validate_field_path(".a")

    def test_valid_path_passes(self):
        validate_field_path("a")
        validate_field_path("a.b.c")


class TestGroupActions:

    def _mk(self, kind, key, *, atomic=False, index=0):
        # A minimal Action with a hand-picked key; document is unused here.
        action = Action(
            kind=kind,
            document=Document({"pk": key}),
            in_atomic_write=atomic,
        )
        action.key = key
        action.index = index
        return action

    def test_writes_and_reads_are_split(self):
        write = self._mk(ActionKind.ACTION_KIND_PUT, "k1", index=0)
        get = self._mk(ActionKind.ACTION_KIND_GET, "k2", index=1)
        b, cg, w, aw, ag = group_actions([write, get])
        assert b == []
        assert [a.key for a in cg] == ["k2"]
        assert [a.key for a in w] == ["k1"]
        assert aw == []
        assert ag == []

    def test_get_then_write_same_key_promotes_to_before_get(self):
        get = self._mk(ActionKind.ACTION_KIND_GET, "k1", index=0)
        write = self._mk(ActionKind.ACTION_KIND_PUT, "k1", index=1)
        b, cg, w, aw, ag = group_actions([get, write])
        assert [a.key for a in b] == ["k1"]
        assert cg == []
        assert [a.key for a in w] == ["k1"]
        assert aw == []
        assert ag == []

    def test_write_then_get_same_key_yields_after_get(self):
        write = self._mk(ActionKind.ACTION_KIND_PUT, "k1", index=0)
        get = self._mk(ActionKind.ACTION_KIND_GET, "k1", index=1)
        b, cg, w, aw, ag = group_actions([write, get])
        assert b == []
        assert cg == []
        assert [a.key for a in w] == ["k1"]
        assert aw == []
        assert [a.key for a in ag] == ["k1"]

    def test_atomic_write_and_get_yield_after_get(self):
        atomic = self._mk(ActionKind.ACTION_KIND_PUT, "k1", atomic=True, index=0)
        get = self._mk(ActionKind.ACTION_KIND_GET, "k1", index=1)
        b, cg, w, aw, ag = group_actions([atomic, get])
        assert [a.key for a in aw] == ["k1"]
        assert [a.key for a in ag] == ["k1"]

    def test_null_key_writes_go_to_write_bucket(self):
        a = Action(kind=ActionKind.ACTION_KIND_CREATE, document=Document({}))
        a.key = None
        a.index = 0
        b, cg, w, aw, ag = group_actions([a])
        assert [a.key for a in w] == [None]

    def test_null_key_atomic_writes_go_to_atomic_bucket(self):
        a = Action(
            kind=ActionKind.ACTION_KIND_CREATE,
            document=Document({}),
            in_atomic_write=True,
        )
        a.key = None
        a.index = 0
        b, cg, w, aw, ag = group_actions([a])
        assert w == []
        assert [a.key for a in aw] == [None]

    def test_ordering_is_stable_by_index(self):
        first = self._mk(ActionKind.ACTION_KIND_PUT, "k1", index=5)
        second = self._mk(ActionKind.ACTION_KIND_PUT, "k2", index=2)
        _, _, w, _, _ = group_actions([first, second])
        assert [a.index for a in w] == [2, 5]


class TestGroupByFieldPath:

    def _get(self, field_paths):
        a = Action(kind=ActionKind.ACTION_KIND_GET, document=Document({}))
        a.field_paths = field_paths
        return a

    def test_all_same_projection_becomes_single_batch(self):
        gets = [self._get([["a"]]), self._get([["a"]])]
        groups = group_by_field_path(gets)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_none_projections_batch_together(self):
        gets = [self._get(None), self._get(None)]
        groups = group_by_field_path(gets)
        assert len(groups) == 1

    def test_differing_projections_end_up_in_separate_batches(self):
        gets = [self._get([["a"]]), self._get([["b"]])]
        groups = group_by_field_path(gets)
        # each unique projection ends up as its own batch
        assert len(groups) == 2

    def test_empty_gets_returns_empty(self):
        assert group_by_field_path([]) == []


class TestWaitForAll:

    def test_all_succeed(self):
        f1: Future[int] = Future()
        f2: Future[int] = Future()
        f1.set_result(1)
        f2.set_result(2)
        wait_for_all([f1, f2])

    def test_first_exception_is_reraised(self):
        f1: Future[Any] = Future()
        f2: Future[Any] = Future()
        f1.set_exception(RuntimeError("boom"))
        f2.set_result("ok")
        with pytest.raises(RuntimeError, match="boom"):
            wait_for_all([f1, f2])

    def test_multiple_exceptions_only_first_reraised(self):
        f1: Future[Any] = Future()
        f2: Future[Any] = Future()
        f1.set_exception(RuntimeError("first"))
        f2.set_exception(ValueError("second"))
        with pytest.raises(RuntimeError, match="first"):
            wait_for_all([f1, f2])


class TestBaseDocStoreLifecycle:

    def test_default_executor_used_when_zero(self):
        store = _make_store(max_outstanding_action_rpcs=0)
        # default fallback = 32 workers
        assert store.executor._max_workers == 32

    def test_custom_executor_size(self):
        store = _make_store(max_outstanding_action_rpcs=4)
        assert store.executor._max_workers == 4

    def test_revision_field_default(self):
        store = _make_store()
        assert store.revision_field == DEFAULT_REVISION_FIELD

    def test_revision_field_override(self):
        store = _make_store(revision_field="myrev")
        assert store.revision_field == "myrev"

    def test_check_closed_raises_after_close(self):
        store = _make_store()
        store.close()
        with pytest.raises(InvalidArgumentException, match="closed"):
            store.check_closed()

    def test_close_is_idempotent(self):
        store = _make_store()
        store.close()
        store.close()  # must not raise

    def test_context_manager_closes(self):
        with _make_store() as store:
            pass
        with pytest.raises(InvalidArgumentException):
            store.check_closed()

    def test_actions_returns_bound_action_list(self):
        store = _make_store()
        actions = store.actions()
        assert isinstance(actions, ActionList)
        assert actions._docstore is store


class TestBaseDocStoreKeyExtraction:

    def test_missing_partition_key_returns_none(self):
        store = _make_store()
        assert store.get_key(Document({})) is None

    def test_partition_key_only(self):
        store = _make_store()
        assert store.get_key(Document({"pk": "k1"})) == "k1"

    def test_composite_key(self):
        store = _make_store(sort_key="sk")
        assert store.get_key(Document({"pk": "k1", "sk": "s1"})) == ("k1", "s1")

    def test_composite_missing_sort_key_returns_none(self):
        store = _make_store(sort_key="sk")
        assert store.get_key(Document({"pk": "k1"})) is None


class TestBaseDocStoreToDriverAction:

    def test_create_may_omit_partition_key(self):
        store = _make_store()
        action = Action(kind=ActionKind.ACTION_KIND_CREATE, document=Document({}))
        store._to_driver_action(action, 3)
        assert action.index == 3
        assert action.key is None

    def test_non_create_without_key_raises(self):
        store = _make_store()
        action = Action(kind=ActionKind.ACTION_KIND_GET, document=Document({}))
        with pytest.raises(InvalidArgumentException, match="missing key"):
            store._to_driver_action(action, 0)

    def test_create_with_revision_raises(self):
        store = _make_store()
        action = Action(
            kind=ActionKind.ACTION_KIND_CREATE,
            document=Document({DEFAULT_REVISION_FIELD: "r1"}),
        )
        with pytest.raises(
            InvalidArgumentException, match="must not be set for CREATE"
        ):
            store._to_driver_action(action, 0)

    def test_put_with_revision_is_promoted_to_replace(self):
        store = _make_store()
        action = Action(
            kind=ActionKind.ACTION_KIND_PUT,
            document=Document({"pk": "k", DEFAULT_REVISION_FIELD: "r1"}),
        )
        store._to_driver_action(action, 0)
        assert action.kind == ActionKind.ACTION_KIND_REPLACE

    def test_put_without_revision_stays_put(self):
        store = _make_store()
        action = Action(kind=ActionKind.ACTION_KIND_PUT, document=Document({"pk": "k"}))
        store._to_driver_action(action, 0)
        assert action.kind == ActionKind.ACTION_KIND_PUT

    def test_field_path_validation_bubbles_up(self):
        # Note: field_paths is a list of segment-lists; validate_field_path
        # is called on each SEGMENT, so an empty segment fails.
        store = _make_store()
        action = Action(
            kind=ActionKind.ACTION_KIND_GET,
            document=Document({"pk": "k"}),
            field_paths=[[""]],
        )
        with pytest.raises(InvalidArgumentException):
            store._to_driver_action(action, 0)


class TestActionListBuilder:

    def test_create_appends_action(self):
        store = _make_store()
        al = store.actions().create({"pk": "k1"})
        assert isinstance(al, ActionList)
        assert len(al._actions) == 1
        assert al._actions[0].kind == ActionKind.ACTION_KIND_CREATE

    def test_replace_appends_action(self):
        store = _make_store()
        al = store.actions().replace({"pk": "k1"})
        assert al._actions[0].kind == ActionKind.ACTION_KIND_REPLACE

    def test_put_appends_action(self):
        store = _make_store()
        al = store.actions().put({"pk": "k1"})
        assert al._actions[0].kind == ActionKind.ACTION_KIND_PUT

    def test_get_appends_action_and_splits_paths(self):
        store = _make_store()
        al = store.actions().get({"pk": "k1"}, "a.b", "c")
        action = al._actions[0]
        assert action.kind == ActionKind.ACTION_KIND_GET
        assert action.field_paths == [["a", "b"], ["c"]]

    def test_get_without_paths_leaves_none(self):
        store = _make_store()
        al = store.actions().get({"pk": "k1"})
        assert al._actions[0].field_paths is None

    def test_accepts_pre_wrapped_document(self):
        store = _make_store()
        doc = Document({"pk": "k1"})
        al = store.actions().put(doc)
        assert al._actions[0].document is doc

    def test_enable_atomic_writes_flag_propagates(self):
        store = _make_store()
        al = store.actions().enable_atomic_writes().create({"pk": "k1"})
        assert al._actions[0].in_atomic_write is True

    def test_get_is_never_atomic_even_when_flag_set(self):
        store = _make_store()
        al = store.actions().enable_atomic_writes().get({"pk": "k1"})
        assert al._actions[0].in_atomic_write is False

    def test_before_do_is_stored(self):
        store = _make_store()
        cb = lambda _classifier: None
        al = store.actions().before_do(cb)
        assert al._before_do is cb


class TestActionListRun:

    def test_run_delegates_to_store(self):
        store = _make_store()
        store.actions().create({"pk": "k1"}).put({"pk": "k2"}).run()
        assert len(store.run_calls) == 1
        assert len(store.run_calls[0]) == 2

    def test_run_forwards_before_do(self):
        store = _make_store()
        cb = lambda _classifier: None
        store.actions().before_do(cb).create({"pk": "k1"}).run()
        assert store.before_do_hooks[0] is cb

    def test_run_shapes_actions_with_index_and_key(self):
        store = _make_store()
        store.actions().put({"pk": "a"}).put({"pk": "b"}).run()
        actions = store.run_calls[0]
        assert [a.index for a in actions] == [0, 1]
        assert [a.key for a in actions] == ["a", "b"]

    def test_run_after_close_raises(self):
        store = _make_store()
        store.close()
        with pytest.raises(InvalidArgumentException, match="closed"):
            store.actions().put({"pk": "k1"}).run()

    def test_duplicate_key_same_kind_raises(self):
        store = _make_store()
        with pytest.raises(InvalidArgumentException, match="duplicate action"):
            store.actions().put({"pk": "a"}).put({"pk": "a"}).run()

    def test_duplicate_null_key_creates_are_allowed(self):
        # Two CREATEs with no partition key both survive dedup because
        # their key is None (each will get a UUID at provider time).
        store = _make_store()
        store.actions().create({}).create({}).run()
        assert len(store.run_calls[0]) == 2

    def test_run_pass_through_substrate_error(self):

        class _ExplodingStore(_FakeDocStore):
            def run_actions(self, actions, before_do=None):
                raise InvalidArgumentException("bad")

        store = _ExplodingStore(CollectionOptions(table_name="t", partition_key="pk"))
        with pytest.raises(InvalidArgumentException, match="bad"):
            store.actions().put({"pk": "k"}).run()

    def test_run_wraps_unknown_error_as_invalid_argument(self):

        class _ExplodingStore(_FakeDocStore):
            def run_actions(self, actions, before_do=None):
                raise RuntimeError("boom")

        store = _ExplodingStore(CollectionOptions(table_name="t", partition_key="pk"))
        with pytest.raises(InvalidArgumentException, match="boom"):
            store.actions().put({"pk": "k"}).run()


class TestAction:

    def test_default_fields(self):
        action = Action(
            kind=ActionKind.ACTION_KIND_PUT,
            document=Document({"pk": "k"}),
        )
        assert action.field_paths is None
        assert action.mods is None
        assert action.in_atomic_write is False
        assert action.key is None
        assert action.index == 0


class TestActionKind:

    def test_names_are_stable(self):
        assert {k.name for k in ActionKind} == {
            "ACTION_KIND_CREATE",
            "ACTION_KIND_REPLACE",
            "ACTION_KIND_PUT",
            "ACTION_KIND_GET",
            "ACTION_KIND_DELETE",
            "ACTION_KIND_UPDATE",
        }
