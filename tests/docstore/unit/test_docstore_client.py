"""Unit tests for :class:`DocStoreClient`.

Layer-1 tests: exercise the user-facing client without touching any
cloud SDK. We install a fake provider in :data:`DOCSTORE_REGISTRY` for
the duration of each test and assert that construction, options
forwarding, and shortcut methods route correctly to the driver.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence

import pytest
from multicloud.common.exceptions import SubstrateSdkException
from multicloud.docstore.docstore_client.client import DocStoreClient, wrap_document
from multicloud.docstore.docstore_client.driver import (
    DOCSTORE_REGISTRY,
    Action,
    BaseDocStore,
    CollectionOptions,
    Document,
)


class _FakeStore(BaseDocStore):
    """Records every ``run_actions`` call and captures init kwargs.

    The class-level ``last_init`` holds the constructor arguments most
    recently seen so tests can assert on them without threading state
    through the registry.
    """

    last_init: dict = {}
    instances: List["_FakeStore"] = []

    def __init__(
        self,
        collection_options: CollectionOptions,
        *,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        credentials_overrider: Optional[Callable[..., Any]] = None,
    ) -> None:
        super().__init__(
            collection_options=collection_options,
            credentials_overrider=credentials_overrider,
        )
        self.region = region
        self.endpoint_url = endpoint_url
        self.run_calls: List[Sequence[Action]] = []
        _FakeStore.last_init = {
            "collection_options": collection_options,
            "region": region,
            "endpoint_url": endpoint_url,
            "credentials_overrider": credentials_overrider,
        }
        _FakeStore.instances.append(self)

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

    def _batch_get(
        self,
        gets: Sequence[Action],
        before_do: Optional[Callable[[Callable[[Any], bool]], None]],
        start: int,
        end: int,
        consistent_read: bool = False,
    ) -> None:  # pragma: no cover - stub for abstract requirement
        return None


@pytest.fixture(autouse=True)
def _register_fake_provider():
    """Install ``_FakeStore`` under provider id ``"fake"`` for each test.

    We patch a synthetic id (``"fake"``) rather than swapping the real
    ``"aws"`` entry so tests are hermetic even when the AWS provider
    imported at package load time.
    """
    _FakeStore.instances = []
    _FakeStore.last_init = {}

    original = dict(DOCSTORE_REGISTRY._providers)  # noqa: SLF001
    DOCSTORE_REGISTRY._providers["fake"] = _FakeStore  # noqa: SLF001
    try:
        yield
    finally:
        DOCSTORE_REGISTRY._providers = original  # noqa: SLF001


def _client(**overrides: Any) -> DocStoreClient:
    kwargs = {
        "provider": "fake",
        "table_name": "t",
        "partition_key": "pk",
    }
    kwargs.update(overrides)
    return DocStoreClient(**kwargs)


class TestConstruction:

    def test_minimal_kwargs(self):
        client = _client()
        opts = _FakeStore.last_init["collection_options"]
        assert opts.table_name == "t"
        assert opts.partition_key == "pk"
        assert opts.sort_key is None
        assert opts.revision_field is None
        assert opts.allow_scans is True
        assert opts.max_outstanding_action_rpcs == 0
        # Optional kwargs not passed → not forwarded to the driver
        assert _FakeStore.last_init["region"] is None
        assert _FakeStore.last_init["endpoint_url"] is None
        assert _FakeStore.last_init["credentials_overrider"] is None
        client.close()

    def test_all_kwargs_forwarded(self):
        cb = lambda: None
        client = _client(
            sort_key="sk",
            revision_field="rev",
            allow_scans=False,
            max_outstanding_action_rpcs=4,
            region="us-west-2",
            endpoint="https://example.invalid",
            credentials_overrider=cb,
        )
        opts = _FakeStore.last_init["collection_options"]
        assert opts.sort_key == "sk"
        assert opts.revision_field == "rev"
        assert opts.allow_scans is False
        assert opts.max_outstanding_action_rpcs == 4
        assert _FakeStore.last_init["region"] == "us-west-2"
        # `endpoint` at the client is translated to `endpoint_url` for the driver
        assert _FakeStore.last_init["endpoint_url"] == "https://example.invalid"
        assert _FakeStore.last_init["credentials_overrider"] is cb
        client.close()

    def test_positional_args_rejected(self):
        with pytest.raises(TypeError):
            DocStoreClient("fake", "t", "pk")  # type: ignore[misc]

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="No provider registered"):
            DocStoreClient(
                provider="does-not-exist", table_name="t", partition_key="pk"
            )

    def test_store_property_exposes_driver(self):
        client = _client()
        assert isinstance(client.store, _FakeStore)
        client.close()


class TestContextManager:

    def test_enter_exit_closes_driver(self):
        with _client() as client:
            assert client.store._closed is False
        assert client.store._closed is True

    def test_close_direct(self):
        client = _client()
        client.close()
        assert client.store._closed is True


class TestActionsForwarding:

    def test_actions_returns_action_list_bound_to_driver(self):
        client = _client()
        al = client.actions()
        # bound to the underlying driver, not the client
        assert al._docstore is client.store
        client.close()

    def test_create_shortcut_runs_one_action(self):
        client = _client()
        client.create({"pk": "k1"})
        driver = client.store
        assert len(driver.run_calls) == 1
        assert len(driver.run_calls[0]) == 1
        assert driver.run_calls[0][0].kind.name == "ACTION_KIND_CREATE"
        client.close()

    def test_put_shortcut_runs_one_action(self):
        client = _client()
        client.put({"pk": "k1"})
        assert client.store.run_calls[0][0].kind.name == "ACTION_KIND_PUT"
        client.close()

    def test_replace_shortcut_runs_one_action(self):
        client = _client()
        # Row needs to exist for a real backend, but the fake accepts anything.
        client.replace({"pk": "k1"})
        assert client.store.run_calls[0][0].kind.name == "ACTION_KIND_REPLACE"
        client.close()

    def test_get_shortcut_with_projection(self):
        client = _client()
        client.get({"pk": "k1"}, "a.b", "c")
        action = client.store.run_calls[0][0]
        assert action.kind.name == "ACTION_KIND_GET"
        assert action.field_paths == [["a", "b"], ["c"]]
        client.close()

    def test_get_shortcut_without_projection(self):
        client = _client()
        client.get({"pk": "k1"})
        action = client.store.run_calls[0][0]
        assert action.field_paths is None
        client.close()

    def test_put_promoted_to_replace_when_revision_present(self):
        client = _client(revision_field="rev")
        client.put({"pk": "k1", "rev": "r0"})
        action = client.store.run_calls[0][0]
        # PUT + revision → promoted to REPLACE
        assert action.kind.name == "ACTION_KIND_REPLACE"
        client.close()

    def test_batch_put_runs_one_run_with_all_puts(self):
        client = _client()
        client.batch_put([{"pk": "k1"}, {"pk": "k2"}, {"pk": "k3"}])
        driver = client.store
        # Single run_actions call carrying all three PUTs
        assert len(driver.run_calls) == 1
        kinds = [a.kind.name for a in driver.run_calls[0]]
        assert kinds == [
            "ACTION_KIND_PUT",
            "ACTION_KIND_PUT",
            "ACTION_KIND_PUT",
        ]
        keys = [a.document.get_field("pk") for a in driver.run_calls[0]]
        assert keys == ["k1", "k2", "k3"]
        client.close()

    def test_batch_put_promotes_to_replace_when_revision_present(self):
        client = _client(revision_field="rev")
        client.batch_put([{"pk": "k1", "rev": "r0"}, {"pk": "k2"}])
        actions = client.store.run_calls[0]
        # Doc with revision → REPLACE; doc without → stays PUT
        assert actions[0].kind.name == "ACTION_KIND_REPLACE"
        assert actions[1].kind.name == "ACTION_KIND_PUT"
        client.close()

    def test_batch_put_empty_still_calls_run(self):
        client = _client()
        client.batch_put([])
        # An empty batch still triggers exactly one no-op run
        assert len(client.store.run_calls) == 1
        assert client.store.run_calls[0] == []
        client.close()

    def test_batch_get_runs_one_run_with_all_gets(self):
        client = _client()
        client.batch_get([{"pk": "k1"}, {"pk": "k2"}])
        driver = client.store
        assert len(driver.run_calls) == 1
        kinds = [a.kind.name for a in driver.run_calls[0]]
        assert kinds == ["ACTION_KIND_GET", "ACTION_KIND_GET"]
        # No projection was requested; every action should reflect that
        assert all(a.field_paths is None for a in driver.run_calls[0])
        client.close()

    def test_batch_get_from_generator(self):
        client = _client()

        def _gen():
            yield {"pk": "k1"}
            yield {"pk": "k2"}

        client.batch_get(_gen())
        # Generator input must still produce a single batched run
        assert len(client.store.run_calls) == 1
        assert len(client.store.run_calls[0]) == 2
        client.close()

    def test_batch_delete_runs_one_run_with_all_deletes(self):
        client = _client()
        client.batch_delete([{"pk": "k1"}, {"pk": "k2"}, {"pk": "k3"}])
        driver = client.store
        # Single run_actions call carrying all three DELETEs
        assert len(driver.run_calls) == 1
        kinds = [a.kind.name for a in driver.run_calls[0]]
        assert kinds == [
            "ACTION_KIND_DELETE",
            "ACTION_KIND_DELETE",
            "ACTION_KIND_DELETE",
        ]
        keys = [a.document.get_field("pk") for a in driver.run_calls[0]]
        assert keys == ["k1", "k2", "k3"]
        client.close()

    def test_batch_delete_carries_revision_when_present(self):
        client = _client(revision_field="rev")
        client.batch_delete([{"pk": "k1", "rev": "r0"}, {"pk": "k2"}])
        actions = client.store.run_calls[0]
        # Both stay DELETE; revision is a precondition, not an action kind
        assert actions[0].kind.name == "ACTION_KIND_DELETE"
        assert actions[1].kind.name == "ACTION_KIND_DELETE"
        assert actions[0].document.get_field("rev") == "r0"
        assert actions[1].document.get_field("rev") is None
        client.close()

    def test_batch_delete_empty_still_calls_run(self):
        client = _client()
        client.batch_delete([])
        # An empty batch still triggers exactly one no-op run
        assert len(client.store.run_calls) == 1
        assert client.store.run_calls[0] == []
        client.close()

    def test_batch_delete_from_generator(self):
        client = _client()

        def _gen():
            yield {"pk": "k1"}
            yield {"pk": "k2"}

        client.batch_delete(_gen())
        # Generator input must still produce a single batched run
        assert len(client.store.run_calls) == 1
        assert len(client.store.run_calls[0]) == 2
        client.close()


class TestWrapDocument:

    def test_dict_is_wrapped(self):
        doc = wrap_document({"a": 1})
        assert isinstance(doc, Document)
        assert doc.get_field("a") == 1

    def test_existing_document_is_passed_through(self):
        original = Document({"a": 1})
        assert wrap_document(original) is original
