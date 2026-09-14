"""Reusable IAM conformance test suite.

A write-once suite of 13 conformance scenarios exercised by every IAM
provider through the cloud-agnostic ``IamClient``, expressed with VCR.py
record/replay (a ``BaseIamConformance`` base + a ``Harness`` ABC +
one-line provider ``Test*`` classes + per-provider cassettes).

Each provider supplies a ``Harness`` (with its environment-specific
identity names, tenant id, region, and policy details) plus ``client``
and ``harness`` fixtures. The tests stay cloud-agnostic: every assertion
is on an observable property (an id, list non-emptiness, or a
specifically-typed exception), never a provider-specific field.

Class naming notes:
  - ``BaseIamConformance`` ends in ``Conformance`` (not ``Test*``) so
    pytest's default ``python_classes = Test*`` collection rule doesn't
    pick it up. Concrete subclasses must start with ``Test`` to be
    collected.
"""

from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from typing import List, Optional

import pytest
from multicloud.common.exceptions import ResourceNotFoundException
from multicloud.iam.iam_client import (
    Action,
    CreateOptions,
    Effect,
    IamClient,
    PolicyDocument,
    Statement,
    TrustConfiguration,
)


class Harness(ABC):
    """Test harness interface that providers implement.

    The harness owns the lifecycle of an :class:`IamClient` instance for
    a single test session and supplies the environment-specific names and
    policy details the tests operate on, so the tests stay cloud-agnostic.
    """

    @abstractmethod
    def create_client(self) -> IamClient:
        """Return an IamClient configured for the test environment."""
        ...

    @abstractmethod
    def get_provider_id(self) -> str:
        """Return the provider id (e.g. ``"aws"``, ``"gcp"``)."""
        ...

    @abstractmethod
    def get_tenant_id(self) -> str:
        """Return the tenant identifier (account id / project id)."""
        ...

    def get_region(self) -> Optional[str]:
        """Return the region passed per-operation. Optional."""
        return None

    @abstractmethod
    def get_test_identity_name(self) -> str:
        """Return the base name for the identities the tests create.

        Individual tests suffix this (``-trusted``, ``-options``,
        ``-nodesc``, ``-get``, ``-delete``, ``-lifecycle``, ``-pol-*``) so
        they don't collide. Suffixes are lowercase/hyphenated so the
        derived names stay valid across providers (some providers reject
        uppercase or long identity names).
        """
        ...

    @abstractmethod
    def get_trusted_principal(self) -> str:
        """Return a principal id to use in trust configuration."""
        ...

    @abstractmethod
    def get_test_policy_name(self) -> str:
        """Return the policy name used by the policy tests.

        Its meaning is provider-specific (an inline-policy name for some
        providers, a role string for others); the harness owns that
        detail so the tests stay cloud-agnostic.
        """
        ...

    def get_test_policy_actions(self) -> List[str]:
        """Return the cloud-agnostic actions for the test policy."""
        return ["storage:GetObject", "storage:PutObject"]

    def get_policy_version(self) -> str:
        """Return the policy document version string. Default empty."""
        return ""

    def get_test_policy_resource(self) -> Optional[str]:
        """Return the resource for the test policy statement, if required."""
        return None

    def get_expected_policy_detail_tokens(self) -> List[str]:
        """Return substrings the retrieved inline-policy detail must contain.

        ``get_inline_policy_details`` returns a provider-specific JSON string
        whose shape differs by provider, so the tests can't assert on a fixed
        schema. Each harness instead declares the substrings that MUST appear
        in its provider's serialized detail (e.g. the granted operations, the
        effect, or the role string), letting the shared test verify the
        returned document actually reflects what was attached — not merely
        that a non-empty string came back.
        """
        return []

    def settle(self) -> None:  # pragma: no cover - default no-op
        """Wait for an eventually-consistent write to become visible.

        Some providers do not make a freshly created identity immediately
        visible to a follow-up read or policy write. Tests that create an
        identity and then immediately use it call this between the two
        operations. The default is a no-op; a provider whose backend needs
        settling time overrides it (and guards the wait to record mode, so
        replay stays fast and offline).
        """

    def close(self) -> None:  # pragma: no cover - default no-op
        """Clean up resources. Override if needed."""


def _build_policy_document(harness: Harness) -> PolicyDocument:
    """Build a cloud-agnostic policy document from harness settings."""
    resource = harness.get_test_policy_resource()
    resources = (resource,) if resource else ()
    statement = Statement(
        effect=Effect.ALLOW,
        actions=tuple(Action.of(a) for a in harness.get_test_policy_actions()),
        resources=resources,
    )
    return PolicyDocument(
        name=harness.get_test_policy_name(),
        version=harness.get_policy_version(),
        statements=(statement,),
    )


class BaseIamConformance:
    """Abstract conformance test suite for IAM providers.

    Conformance tests are written ONCE here and executed by each provider.
    Provider test classes inherit from this class and supply ``client``
    and ``harness`` fixtures.
    """

    def _safe_delete(
        self, client: IamClient, identity_name: str, tenant_id: str
    ) -> None:
        """Delete an identity ignoring errors.

        Prevents test-identity accumulation in real cloud accounts during
        record mode. Each test that creates an identity calls this in a
        ``finally`` block.

        A negative-path test may have already deleted the identity and
        proven it gone, so an "already absent" result here is expected and
        stays quiet; only an unexpected failure warns.
        """
        try:
            client.delete_identity(identity_name, tenant_id=tenant_id)
        except ResourceNotFoundException:
            # Already deleted (e.g. by the test body itself) — nothing to
            # clean up, so this is success, not a cleanup failure.
            pass
        except Exception as e:  # noqa: BLE001
            warnings.warn(f"Cleanup failed for {identity_name}: {e}")

    def _safe_remove_policy(
        self,
        client: IamClient,
        identity_name: str,
        policy_name: str,
        tenant_id: str,
    ) -> None:
        """Remove a policy from an identity ignoring errors.

        Policy tests create their own identity and must detach the policy
        before the identity can be deleted (some providers reject deleting
        an identity that still has a policy attached). Called in a
        ``finally`` block ahead of :meth:`_safe_delete`.

        A test that already removed the policy in its body leaves nothing to
        detach here, so an "already absent" result stays quiet; only an
        unexpected failure warns.
        """
        try:
            client.remove_policy(
                identity_name, policy_name=policy_name, tenant_id=tenant_id
            )
        except ResourceNotFoundException:
            # Already removed (e.g. by the test body itself) — nothing to
            # clean up, so this is success, not a cleanup failure.
            pass
        except Exception as e:  # noqa: BLE001
            warnings.warn(f"Policy cleanup failed for {identity_name}: {e}")

    # Policy operations (self-contained: create identity, then exercise policy)

    def test_attach_inline_policy(self, client: IamClient, harness: Harness):
        """Attach an inline policy, then read it back to prove it took effect.

        Rather than only checking the attach call does not raise, this reads
        the policy back and asserts the retrieved detail reflects the policy
        that was attached (see ``get_expected_policy_detail_tokens``).
        """
        identity_name = harness.get_test_identity_name() + "-pol-attach"
        policy_name = harness.get_test_policy_name()
        tenant_id = harness.get_tenant_id()
        try:
            client.create_identity(
                identity_name,
                description="Test identity for attach-policy",
                tenant_id=tenant_id,
            )
            harness.settle()
            client.attach_inline_policy(
                identity_name,
                policy_document=_build_policy_document(harness),
                tenant_id=tenant_id,
            )

            details = client.get_inline_policy_details(
                identity_name,
                policy_name=policy_name,
                tenant_id=tenant_id,
            )
            assert details, "Attached policy should be retrievable"
            for token in harness.get_expected_policy_detail_tokens():
                assert token in details, (
                    f"Retrieved policy detail should reflect the attached "
                    f"policy; missing {token!r} in {details!r}"
                )
        finally:
            self._safe_remove_policy(client, identity_name, policy_name, tenant_id)
            self._safe_delete(client, identity_name, tenant_id)

    def test_get_inline_policy_details(self, client: IamClient, harness: Harness):
        """Create an identity, attach a policy, then retrieve its details.

        Verifies the returned details are present and non-empty.
        """
        identity_name = harness.get_test_identity_name() + "-pol-details"
        policy_name = harness.get_test_policy_name()
        tenant_id = harness.get_tenant_id()
        try:
            client.create_identity(
                identity_name,
                description="Test identity for inline-policy details",
                tenant_id=tenant_id,
            )
            harness.settle()
            client.attach_inline_policy(
                identity_name,
                policy_document=_build_policy_document(harness),
                tenant_id=tenant_id,
            )

            details = client.get_inline_policy_details(
                identity_name,
                policy_name=policy_name,
                tenant_id=tenant_id,
            )
            assert details is not None, "Policy details should not be None"
            assert details.strip(), "Policy details should not be empty"
            # Not just non-empty: the returned detail must reflect the policy
            # that was attached (granted operations / effect / role string).
            for token in harness.get_expected_policy_detail_tokens():
                assert token in details, (
                    f"Retrieved policy detail should contain {token!r}; "
                    f"got {details!r}"
                )
        finally:
            self._safe_remove_policy(client, identity_name, policy_name, tenant_id)
            self._safe_delete(client, identity_name, tenant_id)

    def test_get_attached_policies(self, client: IamClient, harness: Harness):
        """Create an identity, attach a policy, then list attached policies.

        Verifies the returned list is present and non-empty.
        """
        identity_name = harness.get_test_identity_name() + "-pol-list"
        policy_name = harness.get_test_policy_name()
        tenant_id = harness.get_tenant_id()
        try:
            client.create_identity(
                identity_name,
                description="Test identity for attached-policies list",
                tenant_id=tenant_id,
            )
            harness.settle()
            client.attach_inline_policy(
                identity_name,
                policy_document=_build_policy_document(harness),
                tenant_id=tenant_id,
            )

            attached = client.get_attached_policies(
                identity_name,
                tenant_id=tenant_id,
            )
            assert attached is not None, "Attached policies list should not be None"
            assert attached, "Attached policies list should not be empty"
            # Not just non-empty: the specific policy we attached must be listed
            # (backends may return additional bindings, so assert membership).
            assert (
                policy_name in attached
            ), f"Attached policies should include {policy_name!r}; got {attached!r}"
        finally:
            self._safe_remove_policy(client, identity_name, policy_name, tenant_id)
            self._safe_delete(client, identity_name, tenant_id)

    def test_remove_policy(self, client: IamClient, harness: Harness):
        """Attach a policy, remove it, then prove it is no longer attached.

        Rather than only checking the remove call does not raise, this lists
        the attached policies afterward and asserts the removed policy is gone.
        """
        identity_name = harness.get_test_identity_name() + "-pol-remove"
        policy_name = harness.get_test_policy_name()
        tenant_id = harness.get_tenant_id()
        try:
            client.create_identity(
                identity_name,
                description="Test identity for remove-policy",
                tenant_id=tenant_id,
            )
            harness.settle()
            client.attach_inline_policy(
                identity_name,
                policy_document=_build_policy_document(harness),
                tenant_id=tenant_id,
            )

            # Removing the just-attached policy must not raise.
            client.remove_policy(
                identity_name,
                policy_name=policy_name,
                tenant_id=tenant_id,
            )

            # The removed policy must no longer be attached.
            attached = client.get_attached_policies(
                identity_name,
                tenant_id=tenant_id,
            )
            assert policy_name not in attached, (
                f"Removed policy {policy_name!r} should not remain attached; "
                f"got {attached!r}"
            )
        finally:
            self._safe_remove_policy(client, identity_name, policy_name, tenant_id)
            self._safe_delete(client, identity_name, tenant_id)

    # Identity creation

    def test_create_identity_without_trust_config(
        self, client: IamClient, harness: Harness
    ):
        """Create an identity with no trust configuration.

        Verifies a non-empty identity id is returned.
        """
        identity_name = harness.get_test_identity_name()
        try:
            identity_id = client.create_identity(
                identity_name,
                description="Test identity for integration tests",
                tenant_id=harness.get_tenant_id(),
            )
            assert identity_id, "Identity id should not be empty"
        finally:
            self._safe_delete(client, identity_name, harness.get_tenant_id())

    def test_create_identity_with_trust_config(
        self, client: IamClient, harness: Harness
    ):
        """Create an identity with a trust configuration, then read it back.

        NOTE: the trust configuration itself is NOT verified here — the
        cloud-agnostic API exposes no read path for the applied trust policy,
        so an honest round-trip can only confirm the identity was created and
        is retrievable by the same id. Verifying that the trust principals
        were actually applied is tracked in a follow-up work item.
        """
        identity_name = harness.get_test_identity_name() + "-trusted"
        trust_config = TrustConfiguration(
            trusted_principals=[harness.get_trusted_principal()]
        )
        try:
            identity_id = client.create_identity(
                identity_name,
                description="Test identity with trust configuration",
                tenant_id=harness.get_tenant_id(),
                trust_config=trust_config,
            )
            assert identity_id, "Identity id should not be empty"

            harness.settle()
            retrieved = client.get_identity(
                identity_name,
                tenant_id=harness.get_tenant_id(),
            )
            assert (
                retrieved == identity_id
            ), "Retrieved identity id should equal the created id"
        finally:
            self._safe_delete(client, identity_name, harness.get_tenant_id())

    def test_create_identity_with_options(self, client: IamClient, harness: Harness):
        """Create an identity with creation options, then read it back.

        NOTE: the creation options themselves are NOT verified here — the
        cloud-agnostic API exposes no read path for applied options, so an
        honest round-trip can only confirm the identity was created and is
        retrievable by the same id. Verifying that the options were actually
        applied is tracked in a follow-up work item.
        """
        identity_name = harness.get_test_identity_name() + "-options"
        try:
            identity_id = client.create_identity(
                identity_name,
                description="Test identity with options",
                tenant_id=harness.get_tenant_id(),
                options=CreateOptions(),
            )
            assert identity_id, "Identity id should not be empty"

            harness.settle()
            retrieved = client.get_identity(
                identity_name,
                tenant_id=harness.get_tenant_id(),
            )
            assert (
                retrieved == identity_id
            ), "Retrieved identity id should equal the created id"
        finally:
            self._safe_delete(client, identity_name, harness.get_tenant_id())

    def test_create_identity_with_null_description(
        self, client: IamClient, harness: Harness
    ):
        """Create an identity with no description.

        Verifies a non-empty identity id is returned.
        """
        identity_name = harness.get_test_identity_name() + "-nodesc"
        try:
            identity_id = client.create_identity(
                identity_name,
                description=None,
                tenant_id=harness.get_tenant_id(),
            )
            assert identity_id, "Identity id should not be empty"
        finally:
            self._safe_delete(client, identity_name, harness.get_tenant_id())

    # Identity retrieval / deletion / lifecycle

    def test_get_identity(self, client: IamClient, harness: Harness):
        """Create an identity, then retrieve it.

        Verifies the retrieved identity id is non-empty.
        """
        identity_name = harness.get_test_identity_name() + "-get"
        try:
            created_id = client.create_identity(
                identity_name,
                description="Test identity for get operation",
                tenant_id=harness.get_tenant_id(),
            )
            harness.settle()

            retrieved = client.get_identity(
                identity_name,
                tenant_id=harness.get_tenant_id(),
            )
            assert retrieved, "Retrieved identity should not be empty"
            # Not just non-empty: the retrieved id must be the same identity
            # that was just created.
            assert (
                retrieved == created_id
            ), "Retrieved identity id should equal the created id"
        finally:
            self._safe_delete(client, identity_name, harness.get_tenant_id())

    def test_delete_identity(self, client: IamClient, harness: Harness):
        """Create an identity, delete it, then prove it is gone.

        Rather than only checking delete does not raise, this asserts a
        subsequent get raises ``ResourceNotFoundException``.
        """
        identity_name = harness.get_test_identity_name() + "-delete"
        try:
            identity_id = client.create_identity(
                identity_name,
                description="Test identity for delete operation",
                tenant_id=harness.get_tenant_id(),
            )
            assert identity_id, "Identity id should not be empty"

            # Wait for the freshly created identity to become visible before
            # deleting it; some backends do not make it immediately deletable.
            harness.settle()
            # Should not raise.
            client.delete_identity(identity_name, tenant_id=harness.get_tenant_id())

            # Deletion may be eventually consistent; wait for it to propagate
            # before asserting the identity is gone.
            harness.settle()
            # The deleted identity must no longer be retrievable.
            with pytest.raises(ResourceNotFoundException):
                client.get_identity(
                    identity_name,
                    tenant_id=harness.get_tenant_id(),
                )
        finally:
            self._safe_delete(client, identity_name, harness.get_tenant_id())

    def test_identity_lifecycle(self, client: IamClient, harness: Harness):
        """Exercise the full create -> get -> delete lifecycle.

        Verifies a non-empty id at creation, a non-empty id on retrieval,
        and that deletion completes without raising.
        """
        identity_name = harness.get_test_identity_name() + "-lifecycle"
        try:
            identity_id = client.create_identity(
                identity_name,
                description="Test identity for lifecycle test",
                tenant_id=harness.get_tenant_id(),
            )
            assert identity_id, "Identity id should not be empty after creation"

            harness.settle()
            retrieved = client.get_identity(
                identity_name,
                tenant_id=harness.get_tenant_id(),
            )
            assert retrieved, "Retrieved identity should not be empty"
            assert (
                retrieved == identity_id
            ), "Retrieved identity id should equal the created id"

            # Should not raise.
            client.delete_identity(identity_name, tenant_id=harness.get_tenant_id())

            # Deletion may be eventually consistent; wait for it to propagate
            # before asserting the identity is gone.
            harness.settle()
            # After deletion the identity must no longer be retrievable.
            with pytest.raises(ResourceNotFoundException):
                client.get_identity(
                    identity_name,
                    tenant_id=harness.get_tenant_id(),
                )
        finally:
            self._safe_delete(client, identity_name, harness.get_tenant_id())

    # White-box tests (no network — carry no cassette)

    def test_provider_id(self, client: IamClient, harness: Harness):
        """Provider id matches the harness-declared value."""
        assert client.provider == harness.get_provider_id()
