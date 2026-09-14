"""Unit tests for :class:`ProviderRegistry` and :class:`Provider`."""

from __future__ import annotations

import pytest
from multicloud.common.exceptions import SubstrateSdkException
from multicloud.common.provider import Provider, ProviderRegistry


class _DummyProvider(Provider):
    """Minimal concrete Provider for tests."""

    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        return SubstrateSdkException(str(exc))


class TestProviderRegistry:
    """Tests for the ProviderRegistry discovery mechanism."""

    def test_register_and_get(self):
        registry = ProviderRegistry(service_name="test-svc")

        @registry.register("mock")
        class MockProvider(_DummyProvider):
            pass

        assert registry.get("mock") is MockProvider

    def test_get_unknown_provider_raises_value_error(self):
        registry = ProviderRegistry(service_name="test-svc")
        with pytest.raises(ValueError, match="No provider registered"):
            registry.get("nonexistent")

    def test_get_unknown_provider_lists_registered(self):
        registry = ProviderRegistry(service_name="test-svc")

        @registry.register("alpha")
        class Alpha(_DummyProvider):
            pass

        with pytest.raises(ValueError, match="alpha"):
            registry.get("beta")

    def test_record_import_error_then_get_raises_helpful_message(self):
        registry = ProviderRegistry(service_name="blob")
        registry.record_import_error("gcp", ImportError("No module named 'google'"))

        with pytest.raises(ValueError, match="could not be loaded") as exc_info:
            registry.get("gcp")
        assert "google" in str(exc_info.value)
        assert "pip install" in str(exc_info.value)

    def test_register_clears_previous_import_error(self):
        registry = ProviderRegistry(service_name="test-svc")
        registry.record_import_error("aws", ImportError("boto3 missing"))

        @registry.register("aws")
        class AwsProvider(_DummyProvider):
            pass

        # Should now succeed (import error cleared)
        assert registry.get("aws") is AwsProvider

    def test_ids_returns_sorted_list(self):
        registry = ProviderRegistry(service_name="test-svc")

        @registry.register("zeta")
        class ZetaProvider(_DummyProvider):
            pass

        @registry.register("alpha")
        class AlphaProvider(_DummyProvider):
            pass

        assert registry.ids() == ["alpha", "zeta"]

    def test_ids_empty_registry(self):
        registry = ProviderRegistry(service_name="empty")
        assert registry.ids() == []

    def test_register_sets_provider_id_on_class(self):
        registry = ProviderRegistry(service_name="test-svc")

        @registry.register("my-cloud")
        class MyCloudProvider(_DummyProvider):
            pass

        assert MyCloudProvider.provider_id == "my-cloud"

    def test_multiple_registries_are_independent(self):
        reg_a = ProviderRegistry(service_name="svc-a")
        reg_b = ProviderRegistry(service_name="svc-b")

        @reg_a.register("foo")
        class FooA(_DummyProvider):
            pass

        @reg_b.register("foo")
        class FooB(_DummyProvider):
            pass

        assert reg_a.get("foo") is FooA
        assert reg_b.get("foo") is FooB


class TestProviderBase:
    """Tests for the :class:`Provider` ABC."""

    def test_get_provider_id_returns_class_attribute(self):
        provider = _DummyProvider()
        provider.provider_id = "test-id"
        assert provider.get_provider_id() == "test-id"

    def test_default_provider_id_is_empty_string(self):
        provider = _DummyProvider()
        assert provider.get_provider_id() == ""

    def test_translate_error_abstract_enforced(self):
        """Cannot instantiate Provider without translate_error."""

        with pytest.raises(TypeError):

            class IncompleteProvider(Provider):
                pass

            IncompleteProvider()  # type: ignore[abstract]
