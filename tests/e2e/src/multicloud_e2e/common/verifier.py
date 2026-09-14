"""Base verifier ABC for provider-specific verification."""

from abc import ABC, abstractmethod


class ServiceVerifier(ABC):
    """Base class for provider-specific resource verification.

    Each service+provider combination implements this to independently
    confirm resources exist/are deleted using the raw provider SDK.
    """

    @property
    @abstractmethod
    def api_names(self) -> dict:
        """Return a mapping of operation → provider SDK method name.

        Example:
            {"create": "iam.create_role()", "get": "iam.get_role()", "delete": "iam.delete_role()"}
        """

    @abstractmethod
    def verify_exists(
        self, identity_name: str, tenant_id: str, expected_id: str
    ) -> bool:
        """Verify the resource exists and matches expected_id."""

    @abstractmethod
    def verify_deleted(self, identity_name: str, tenant_id: str) -> bool:
        """Verify the resource no longer exists."""

    @abstractmethod
    def cleanup(self, identity_name: str, tenant_id: str) -> None:
        """Clean up the resource (best-effort, suppress not-found)."""

    def verify_trust_config(
        self, identity_name: str, tenant_id: str, expected_principal: str
    ) -> bool:
        """Verify trust configuration contains the expected principal.

        Override in provider-specific verifiers. Returns False by default.
        """
        return False
