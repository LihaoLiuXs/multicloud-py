"""Provider discovery registry.

A small in-process registry keyed by provider id (e.g. ``"aws"``,
``"gcp"``). Provider classes register themselves with the
``@registry.register("aws")`` decorator at import time; the user-facing
client (e.g. ``BucketClient``) looks up the right class by id at
construction time.

This is a service-locator pattern — it emerges when a single API must
dispatch to multiple backend implementations at runtime.

Provider modules in this SDK are loaded lazily from setuptools entry
points. Each provider distribution declares an entry point in the
``multicloud.<svc>.providers`` group; :meth:`ProviderRegistry.discover`
imports those modules on the first :meth:`ProviderRegistry.get` call so
the ``@register`` decorators run and the providers become resolvable.
"""

from __future__ import annotations

import importlib
import importlib.metadata
from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Type

from multicloud.common.exceptions import SubstrateSdkException


class Provider(ABC):
    """Base class for all provider implementations across all services.

    Every provider (for blob, STS, or any other service) must implement
    error translation to map provider-specific exceptions to the common
    SubstrateSdkException hierarchy.
    """

    # Set by @registry.register() — identifies which cloud this provider handles.
    provider_id: str = ""

    def get_provider_id(self) -> str:
        """Return the provider identifier (e.g., 'aws', 'gcp', 'ali').
        Read from the class attribute stamped by the registry decorator.
        """
        return self.provider_id

    @abstractmethod
    def translate_error(self, exc: Exception) -> SubstrateSdkException:
        """Translate provider-specific exceptions to the common hierarchy.

        Each provider maps its native SDK errors into SubstrateSdkException
        subtypes. This normalizes diverse error shapes into a single
        hierarchy that client code can catch uniformly.

        Args:
            exc: The provider-specific exception to translate.

        Returns:
            A SubstrateSdkException subclass instance.
        """


class ProviderRegistry:
    """A per-service registry that maps provider_id → provider class.

    Each service (blob, sts, docstore, ...) owns its own registry instance so
    the namespaces don't collide. A lightweight service-locator that avoids
    hard imports of optional dependencies.

    Provider discovery is via setuptools entry points. The registry is
    constructed with an ``entry_point_group`` (e.g. ``"multicloud.blob.providers"``)
    that provider distributions advertise themselves under. The registry
    imports those modules lazily on the first :meth:`get` call; each
    provider module's import side-effect is to call
    ``@registry.register(...)`` on its class.
    """

    def __init__(
        self,
        service_name: str,
        entry_point_group: str | None = None,
    ) -> None:
        # entry_point_group is None for in-test registries that don't
        # need plugin discovery. Real services (blob, sts, ...) pass
        # something like "multicloud.blob.providers".
        self._service_name = service_name
        self._entry_point_group = entry_point_group
        self._providers: Dict[str, Type[Provider]] = {}
        self._import_errors: Dict[str, ImportError] = {}
        self._discovered = False

    def register(self, provider_id: str) -> Callable[[Type[Provider]], Type[Provider]]:
        """Class decorator that registers a provider implementation.
        Stamps provider_id on the class and adds it to the lookup table.
        """

        def _decorator(cls: Type[Provider]) -> Type[Provider]:
            cls.provider_id = provider_id
            self._providers[provider_id] = cls
            # Registration wins over any previously recorded import error.
            self._import_errors.pop(provider_id, None)
            return cls

        return _decorator

    def record_import_error(self, provider_id: str, exc: ImportError) -> None:
        """Stash an ImportError for deferred reporting (lazy-import pattern).

        The error surfaces with a helpful "pip install" hint when the
        user first tries to use that provider.
        """
        self._import_errors[provider_id] = exc

    def discover(self) -> None:
        """Import every provider module advertised in our entry-point group.

        Idempotent. Safe to call multiple times. Each provider module's
        import side-effect (the ``@registry.register("aws")`` decorator)
        populates the registry. Missing optional dependencies surface as
        ImportError and are recorded under the entry-point name so a
        later ``get(...)`` call yields a clear "pip install" hint.
        """
        if self._discovered:
            return
        self._discovered = True
        if self._entry_point_group is None:
            return

        eps = importlib.metadata.entry_points()
        # Python 3.10+: select() yields entry points in the requested group.
        group_eps = eps.select(group=self._entry_point_group)
        for ep in group_eps:
            try:
                # Importing the module triggers @registry.register().
                # We don't need ep.load() (which returns the attribute) —
                # the side-effect is what registers the provider.
                importlib.import_module(ep.value.split(":")[0])
            except ImportError as exc:
                # ep.name is the provider id ("aws", "gcp", "ali").
                self.record_import_error(ep.name, exc)

    def get(self, provider_id: str) -> Type[Provider]:
        """Look up a provider class by id — the core dispatch mechanism.
        Raises ValueError with actionable guidance if not found or import failed.
        """
        self.discover()

        if provider_id in self._providers:
            return self._providers[provider_id]
        if provider_id in self._import_errors:
            import_exc = self._import_errors[provider_id]
            raise ValueError(
                f"Provider {provider_id!r} for service={self._service_name!r} "
                f"could not be loaded: {import_exc}. "
                f"Try installing it (e.g. "
                f"`pip install 'multicloud-{self._service_name}-{provider_id}'`)."
            ) from import_exc
        raise ValueError(
            f"No provider registered for service={self._service_name!r} "
            f"provider_id={provider_id!r}. "
            f"Registered: {sorted(self._providers)}"
        )

    def ids(self) -> List[str]:
        """Return sorted list of registered provider ids (for display/logging)."""
        self.discover()
        return sorted(self._providers)
