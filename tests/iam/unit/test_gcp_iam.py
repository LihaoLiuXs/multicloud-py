"""Unit tests for iam_gcp/gcp_iam.py — GCP IAM provider."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from multicloud.common.exceptions import (
    ResourceConflictException,
    ResourceExistsException,
    ResourceNotFoundException,
    UnAuthorizedException,
    UnknownException,
)
from multicloud.iam.iam_client.driver import (
    CreateOptions,
    TrustConfiguration,
)


def _install_gcp_mocks():
    """Install mock GCP modules into sys.modules BEFORE importing gcp_iam."""
    mock_modules = {}
    modules_to_mock = [
        "google",
        "google.api_core",
        "google.api_core.exceptions",
        "google.auth",
        "google.auth.exceptions",
        "google.auth.impersonated_credentials",
        "google.cloud",
        "google.cloud.iam_admin_v1",
        "google.cloud.resourcemanager_v3",
        "google.iam",
        "google.iam.v1",
        "google.iam.v1.iam_policy_pb2",
        "google.iam.v1.policy_pb2",
        "google.oauth2",
        "google.oauth2.credentials",
    ]

    for mod_name in modules_to_mock:
        mock_mod = MagicMock(spec=ModuleType)
        mock_mod.__name__ = mod_name
        mock_modules[mod_name] = mock_mod
        sys.modules[mod_name] = mock_mod

    # Wire up parent→child relationships
    mock_modules["google"].api_core = mock_modules["google.api_core"]
    mock_modules["google"].auth = mock_modules["google.auth"]
    mock_modules["google"].cloud = mock_modules["google.cloud"]
    mock_modules["google"].iam = mock_modules["google.iam"]
    mock_modules["google"].oauth2 = mock_modules["google.oauth2"]
    mock_modules["google.auth"].exceptions = mock_modules["google.auth.exceptions"]
    mock_modules["google.auth"].impersonated_credentials = mock_modules[
        "google.auth.impersonated_credentials"
    ]
    mock_modules["google.oauth2"].credentials = mock_modules[
        "google.oauth2.credentials"
    ]
    mock_modules["google.api_core"].exceptions = mock_modules[
        "google.api_core.exceptions"
    ]
    mock_modules["google.cloud"].iam_admin_v1 = mock_modules[
        "google.cloud.iam_admin_v1"
    ]
    mock_modules["google.cloud"].resourcemanager_v3 = mock_modules[
        "google.cloud.resourcemanager_v3"
    ]
    mock_modules["google.iam"].v1 = mock_modules["google.iam.v1"]
    mock_modules["google.iam.v1"].iam_policy_pb2 = mock_modules[
        "google.iam.v1.iam_policy_pb2"
    ]
    mock_modules["google.iam.v1"].policy_pb2 = mock_modules["google.iam.v1.policy_pb2"]

    # Setup exception classes
    exceptions_mod = mock_modules["google.api_core.exceptions"]
    exceptions_mod.GoogleAPIError = type("GoogleAPIError", (Exception,), {})
    exceptions_mod.NotFound = type("NotFound", (exceptions_mod.GoogleAPIError,), {})
    exceptions_mod.AlreadyExists = type(
        "AlreadyExists", (exceptions_mod.GoogleAPIError,), {}
    )
    exceptions_mod.Forbidden = type("Forbidden", (exceptions_mod.GoogleAPIError,), {})
    exceptions_mod.Unauthorized = type(
        "Unauthorized", (exceptions_mod.GoogleAPIError,), {}
    )
    exceptions_mod.BadRequest = type("BadRequest", (exceptions_mod.GoogleAPIError,), {})
    exceptions_mod.FailedPrecondition = type(
        "FailedPrecondition", (exceptions_mod.GoogleAPIError,), {}
    )
    exceptions_mod.Conflict = type("Conflict", (exceptions_mod.GoogleAPIError,), {})
    exceptions_mod.TooManyRequests = type(
        "TooManyRequests", (exceptions_mod.GoogleAPIError,), {}
    )
    exceptions_mod.ResourceExhausted = type(
        "ResourceExhausted", (exceptions_mod.GoogleAPIError,), {}
    )
    exceptions_mod.GatewayTimeout = type(
        "GatewayTimeout", (exceptions_mod.GoogleAPIError,), {}
    )
    exceptions_mod.DeadlineExceeded = type(
        "DeadlineExceeded", (exceptions_mod.GoogleAPIError,), {}
    )

    # Setup IAMClient and request/response types
    iam_mod = mock_modules["google.cloud.iam_admin_v1"]
    iam_mod.IAMClient = MagicMock
    iam_mod.ServiceAccount = MagicMock
    iam_mod.CreateServiceAccountRequest = MagicMock
    iam_mod.GetServiceAccountRequest = MagicMock
    iam_mod.DeleteServiceAccountRequest = MagicMock

    # Setup ProjectsClient
    rm_mod = mock_modules["google.cloud.resourcemanager_v3"]
    rm_mod.ProjectsClient = MagicMock

    # Setup protobuf mocks
    iam_policy_pb2 = mock_modules["google.iam.v1.iam_policy_pb2"]
    iam_policy_pb2.GetIamPolicyRequest = MagicMock
    iam_policy_pb2.SetIamPolicyRequest = MagicMock

    policy_pb2 = mock_modules["google.iam.v1.policy_pb2"]
    policy_pb2.Binding = MagicMock

    return mock_modules


# Module-level reference populated by the autouse fixture below.
_GCP_MOCKS: dict = {}
GcpIam = None  # type: ignore[assignment]


@pytest.fixture(scope="module", autouse=True)
def _gcp_mocks():
    """Install GCP mocks for IAM tests; restore originals on teardown."""
    global _GCP_MOCKS, GcpIam

    originals = {k: sys.modules[k] for k in list(sys.modules) if k.startswith("google")}

    mocks = _install_gcp_mocks()
    _GCP_MOCKS = mocks

    # Reload gcp_iam against mocked modules
    sys.modules.pop("multicloud.iam.iam_gcp.gcp_iam", None)
    sys.modules.pop("multicloud.iam.iam_gcp", None)
    import multicloud.iam.iam_gcp.gcp_iam  # noqa: E402

    importlib.reload(multicloud.iam.iam_gcp.gcp_iam)
    GcpIam = multicloud.iam.iam_gcp.gcp_iam.GcpIam

    yield mocks

    # Cleanup: remove injected mocks, restore originals
    for mod_name in list(sys.modules):
        if mod_name.startswith("google"):
            del sys.modules[mod_name]
    for mod_name, mod in originals.items():
        sys.modules[mod_name] = mod

    # Also remove the cached gcp_iam so later imports get the real module
    sys.modules.pop("multicloud.iam.iam_gcp.gcp_iam", None)
    sys.modules.pop("multicloud.iam.iam_gcp", None)


@pytest.fixture
def mock_iam_client():
    return MagicMock()


@pytest.fixture
def mock_projects_client():
    return MagicMock()


@pytest.fixture
def gcp_store(mock_iam_client, mock_projects_client):
    return GcpIam(
        region="us-central1",
        iam_client=mock_iam_client,
        projects_client=mock_projects_client,
    )


class TestGcpIamCreateIdentity:
    def test_create_identity_basic(self, gcp_store, mock_iam_client):
        mock_sa = MagicMock()
        mock_sa.email = "test-sa@my-project.iam.gserviceaccount.com"
        mock_sa.name = "projects/my-project/serviceAccounts/test-sa@my-project.iam.gserviceaccount.com"
        mock_iam_client.create_service_account.return_value = mock_sa

        result = gcp_store.create_identity(
            "test-sa",
            description="Test SA",
            tenant_id="my-project",
            region="us-central1",
            trust_config=TrustConfiguration(),
            options=CreateOptions(),
        )

        assert result == "test-sa@my-project.iam.gserviceaccount.com"
        mock_iam_client.create_service_account.assert_called_once()

    def test_create_identity_with_trust_config_sets_bindings(
        self, gcp_store, mock_iam_client
    ):
        mock_sa = MagicMock()
        mock_sa.email = "test-sa@proj.iam.gserviceaccount.com"
        mock_sa.name = (
            "projects/proj/serviceAccounts/test-sa@proj.iam.gserviceaccount.com"
        )
        mock_iam_client.create_service_account.return_value = mock_sa

        mock_policy = MagicMock()
        mock_policy.bindings = []
        mock_iam_client.get_iam_policy.return_value = mock_policy

        tc = TrustConfiguration(
            trusted_principals=["serviceAccount:other@proj.iam.gserviceaccount.com"],
        )

        result = gcp_store.create_identity(
            "test-sa",
            description="desc",
            tenant_id="proj",
            region=None,
            trust_config=tc,
            options=CreateOptions(),
        )

        assert result == "test-sa@proj.iam.gserviceaccount.com"
        mock_iam_client.get_iam_policy.assert_called_once()
        mock_iam_client.set_iam_policy.assert_called_once()

    def test_create_identity_already_exists_returns_existing(
        self, gcp_store, mock_iam_client
    ):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]
        mock_iam_client.create_service_account.side_effect = (
            exceptions_mod.AlreadyExists("exists")
        )

        mock_sa = MagicMock()
        mock_sa.email = "existing@proj.iam.gserviceaccount.com"
        mock_iam_client.get_service_account.return_value = mock_sa

        result = gcp_store.create_identity(
            "existing",
            description="desc",
            tenant_id="proj",
            region=None,
            trust_config=TrustConfiguration(),
            options=CreateOptions(),
        )

        assert result == "existing@proj.iam.gserviceaccount.com"


class TestGcpIamGetIdentity:
    def test_get_identity_returns_email(self, gcp_store, mock_iam_client):
        mock_sa = MagicMock()
        mock_sa.email = "my-sa@proj.iam.gserviceaccount.com"
        mock_iam_client.get_service_account.return_value = mock_sa

        result = gcp_store.get_identity("my-sa", tenant_id="proj")

        assert result == "my-sa@proj.iam.gserviceaccount.com"

    def test_get_identity_not_found(self, gcp_store, mock_iam_client):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]
        mock_iam_client.get_service_account.side_effect = exceptions_mod.NotFound(
            "not found"
        )

        with pytest.raises(ResourceNotFoundException):
            gcp_store.get_identity("nonexistent", tenant_id="proj")


class TestGcpIamDeleteIdentity:
    def test_delete_identity_success(self, gcp_store, mock_iam_client):
        mock_iam_client.delete_service_account.return_value = None

        gcp_store.delete_identity("my-sa", tenant_id="proj")

        mock_iam_client.delete_service_account.assert_called_once()

    def test_delete_identity_not_found(self, gcp_store, mock_iam_client):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]
        mock_iam_client.delete_service_account.side_effect = exceptions_mod.NotFound(
            "not found"
        )

        with pytest.raises(ResourceNotFoundException):
            gcp_store.delete_identity("nonexistent", tenant_id="proj")


class TestGcpIamClose:
    def test_close_calls_underlying_clients_close(
        self, gcp_store, mock_iam_client, mock_projects_client
    ):
        mock_iam_client.close = MagicMock()
        mock_projects_client.close = MagicMock()
        gcp_store.close()
        mock_iam_client.close.assert_called_once()
        mock_projects_client.close.assert_called_once()

    def test_close_no_op_when_clients_have_no_close(self):
        client = MagicMock(spec=[])
        projects = MagicMock(spec=[])
        store = GcpIam(iam_client=client, projects_client=projects)
        store.close()


class TestGcpIamTrustBindings:
    def test_set_trust_bindings_error_translated(self, gcp_store, mock_iam_client):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]

        mock_sa = MagicMock()
        mock_sa.email = "sa@proj.iam.gserviceaccount.com"
        mock_sa.name = "projects/proj/serviceAccounts/sa@proj.iam.gserviceaccount.com"
        mock_iam_client.create_service_account.return_value = mock_sa
        mock_iam_client.get_iam_policy.side_effect = exceptions_mod.Forbidden(
            "forbidden"
        )

        tc = TrustConfiguration(
            trusted_principals=["serviceAccount:other@proj.iam.gserviceaccount.com"],
        )

        with pytest.raises(UnAuthorizedException):
            gcp_store.create_identity(
                "sa",
                description="desc",
                tenant_id="proj",
                region=None,
                trust_config=tc,
                options=CreateOptions(),
            )

    def test_set_trust_bindings_deduplicates_existing_member(
        self, gcp_store, mock_iam_client
    ):
        mock_sa = MagicMock()
        mock_sa.email = "sa@proj.iam.gserviceaccount.com"
        mock_sa.name = "projects/proj/serviceAccounts/sa@proj.iam.gserviceaccount.com"
        mock_iam_client.create_service_account.return_value = mock_sa

        existing_binding = MagicMock()
        existing_binding.role = "roles/iam.serviceAccountTokenCreator"
        existing_binding.members = ["serviceAccount:other@proj.iam.gserviceaccount.com"]

        mock_policy = MagicMock()
        mock_policy.bindings = [existing_binding]
        mock_iam_client.get_iam_policy.return_value = mock_policy

        tc = TrustConfiguration(
            trusted_principals=["serviceAccount:other@proj.iam.gserviceaccount.com"],
        )

        result = gcp_store.create_identity(
            "sa",
            description="desc",
            tenant_id="proj",
            region=None,
            trust_config=tc,
            options=CreateOptions(),
        )

        assert result == "sa@proj.iam.gserviceaccount.com"
        assert len(existing_binding.members) == 1

    def test_format_principal_already_prefixed(self, gcp_store, mock_iam_client):
        mock_sa = MagicMock()
        mock_sa.email = "sa@proj.iam.gserviceaccount.com"
        mock_sa.name = "projects/proj/serviceAccounts/sa@proj.iam.gserviceaccount.com"
        mock_iam_client.create_service_account.return_value = mock_sa

        mock_policy = MagicMock()
        mock_policy.bindings = []
        mock_iam_client.get_iam_policy.return_value = mock_policy

        tc = TrustConfiguration(
            trusted_principals=["user:admin@example.com"],
        )

        gcp_store.create_identity(
            "sa",
            description="desc",
            tenant_id="proj",
            region=None,
            trust_config=tc,
            options=CreateOptions(),
        )

        mock_iam_client.set_iam_policy.assert_called_once()


class TestGcpIamCreateErrors:
    def test_create_service_account_error_translated(self, gcp_store, mock_iam_client):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]
        mock_iam_client.create_service_account.side_effect = exceptions_mod.BadRequest(
            "invalid"
        )

        from multicloud.common.exceptions import InvalidArgumentException

        with pytest.raises(InvalidArgumentException):
            gcp_store.create_identity(
                "bad-sa",
                description="desc",
                tenant_id="proj",
                region=None,
            )

    def test_create_with_none_trust_config_and_options(
        self, gcp_store, mock_iam_client
    ):
        mock_sa = MagicMock()
        mock_sa.email = "sa@proj.iam.gserviceaccount.com"
        mock_sa.name = "projects/proj/serviceAccounts/sa@proj.iam.gserviceaccount.com"
        mock_iam_client.create_service_account.return_value = mock_sa

        result = gcp_store.create_identity(
            "sa",
            description="desc",
            tenant_id="proj",
            region=None,
            trust_config=None,
            options=None,
        )

        assert result == "sa@proj.iam.gserviceaccount.com"
        mock_iam_client.get_iam_policy.assert_not_called()


class TestTranslateError:
    def test_already_translated_passthrough(self):
        exc = ResourceNotFoundException("already translated")
        result = GcpIam(iam_client=MagicMock()).translate_error(exc)
        assert result is exc

    def test_bad_request(self):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]
        exc = exceptions_mod.BadRequest("bad input")
        from multicloud.common.exceptions import InvalidArgumentException

        assert isinstance(
            GcpIam(iam_client=MagicMock()).translate_error(exc),
            InvalidArgumentException,
        )

    def test_not_found(self):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]
        exc = exceptions_mod.NotFound("not found")
        assert isinstance(
            GcpIam(iam_client=MagicMock()).translate_error(exc),
            ResourceNotFoundException,
        )

    def test_already_exists(self):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]
        exc = exceptions_mod.AlreadyExists("exists")
        assert isinstance(
            GcpIam(iam_client=MagicMock()).translate_error(exc), ResourceExistsException
        )

    def test_forbidden(self):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]
        exc = exceptions_mod.Forbidden("forbidden")
        assert isinstance(
            GcpIam(iam_client=MagicMock()).translate_error(exc), UnAuthorizedException
        )

    def test_failed_precondition(self):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]
        exc = exceptions_mod.FailedPrecondition("has attached policies")
        assert isinstance(
            GcpIam(iam_client=MagicMock()).translate_error(exc),
            ResourceConflictException,
        )

    def test_conflict(self):
        exceptions_mod = _GCP_MOCKS["google.api_core.exceptions"]
        exc = exceptions_mod.Conflict("conflict")
        assert isinstance(
            GcpIam(iam_client=MagicMock()).translate_error(exc),
            ResourceConflictException,
        )

    def test_unknown(self):
        exc = RuntimeError("something")
        assert isinstance(
            GcpIam(iam_client=MagicMock()).translate_error(exc), UnknownException
        )


class TestGcpIamAttachInlinePolicy:
    def test_attach_inline_policy_adds_bindings(self, gcp_store, mock_projects_client):
        from multicloud.iam.iam_client.model import (
            Effect,
            PolicyDocument,
            Statement,
            StorageActions,
        )

        mock_binding = MagicMock()
        mock_binding.role = "roles/storage.objectViewer"
        mock_binding.members = ["serviceAccount:other@proj.iam.gserviceaccount.com"]

        mock_policy = MagicMock()
        mock_policy.bindings = [mock_binding]
        mock_projects_client.get_iam_policy.return_value = mock_policy

        doc = PolicyDocument(
            name="P",
            statements=(
                Statement(
                    effect=Effect.ALLOW,
                    actions=(StorageActions.GET_OBJECT,),
                ),
            ),
        )

        gcp_store.attach_inline_policy(
            "test-sa", policy_document=doc, tenant_id="my-project"
        )

        mock_projects_client.get_iam_policy.assert_called_once()
        mock_projects_client.set_iam_policy.assert_called_once()

    def test_attach_inline_policy_skips_deny(self, gcp_store, mock_projects_client):
        from multicloud.iam.iam_client.model import (
            Effect,
            PolicyDocument,
            Statement,
            StorageActions,
        )

        mock_policy = MagicMock()
        mock_policy.bindings = []
        mock_projects_client.get_iam_policy.return_value = mock_policy

        doc = PolicyDocument(
            name="P",
            statements=(
                Statement(effect=Effect.DENY, actions=(StorageActions.DELETE_OBJECT,)),
            ),
        )

        gcp_store.attach_inline_policy(
            "test-sa", policy_document=doc, tenant_id="my-project"
        )

        mock_projects_client.set_iam_policy.assert_not_called()


class TestGcpIamGetInlinePolicyDetails:
    def test_returns_json_when_binding_found(self, gcp_store, mock_projects_client):
        mock_binding = MagicMock()
        mock_binding.role = "roles/storage.objectViewer"
        mock_binding.members = [
            "serviceAccount:test-sa@my-project.iam.gserviceaccount.com"
        ]

        mock_policy = MagicMock()
        mock_policy.bindings = [mock_binding]
        mock_projects_client.get_iam_policy.return_value = mock_policy

        result = gcp_store.get_inline_policy_details(
            "test-sa", policy_name="roles/storage.objectViewer", tenant_id="my-project"
        )

        assert result is not None
        assert "roles/storage.objectViewer" in result

    def test_returns_none_when_not_found(self, gcp_store, mock_projects_client):
        mock_binding = MagicMock()
        mock_binding.role = "roles/other"
        mock_binding.members = ["serviceAccount:other@proj.iam.gserviceaccount.com"]

        mock_policy = MagicMock()
        mock_policy.bindings = [mock_binding]
        mock_projects_client.get_iam_policy.return_value = mock_policy

        result = gcp_store.get_inline_policy_details(
            "test-sa", policy_name="roles/storage.objectViewer", tenant_id="my-project"
        )

        assert result is None


class TestGcpIamGetAttachedPolicies:
    def test_returns_roles_for_member(self, gcp_store, mock_projects_client):
        member = "serviceAccount:test-sa@my-project.iam.gserviceaccount.com"
        binding1 = MagicMock()
        binding1.role = "roles/storage.objectViewer"
        binding1.members = [member]
        binding2 = MagicMock()
        binding2.role = "roles/compute.viewer"
        binding2.members = ["serviceAccount:other@proj.iam.gserviceaccount.com"]

        mock_policy = MagicMock()
        mock_policy.bindings = [binding1, binding2]
        mock_projects_client.get_iam_policy.return_value = mock_policy

        result = gcp_store.get_attached_policies("test-sa", tenant_id="my-project")

        assert "roles/storage.objectViewer" in result
        assert "roles/compute.viewer" not in result


class TestGcpIamRemovePolicy:
    def test_removes_member_from_binding(self, gcp_store, mock_projects_client):
        member = "serviceAccount:test-sa@my-project.iam.gserviceaccount.com"
        mock_binding = MagicMock()
        mock_binding.role = "roles/storage.objectViewer"
        mock_binding.members = [
            member,
            "serviceAccount:other@proj.iam.gserviceaccount.com",
        ]

        mock_policy = MagicMock()
        mock_policy.bindings = [mock_binding]
        mock_projects_client.get_iam_policy.return_value = mock_policy

        gcp_store.remove_policy(
            "test-sa", policy_name="roles/storage.objectViewer", tenant_id="my-project"
        )

        mock_projects_client.set_iam_policy.assert_called_once()

    def test_no_op_when_not_member(self, gcp_store, mock_projects_client):
        mock_binding = MagicMock()
        mock_binding.role = "roles/storage.objectViewer"
        mock_binding.members = ["serviceAccount:other@proj.iam.gserviceaccount.com"]

        mock_policy = MagicMock()
        mock_policy.bindings = [mock_binding]
        mock_projects_client.get_iam_policy.return_value = mock_policy

        gcp_store.remove_policy(
            "test-sa", policy_name="roles/storage.objectViewer", tenant_id="my-project"
        )

        mock_projects_client.set_iam_policy.assert_not_called()


class TestGcpIamCredentialsOverrider:
    def test_session_override_builds_clients_with_resolved_credentials(self):
        from multicloud.common.credentials import StsCredentials
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token="ya29.explicit-token"),
        )
        with (
            patch(
                "multicloud.iam.iam_gcp.gcp_iam.get_credentials"
            ) as mock_get_credentials,
            patch("multicloud.iam.iam_gcp.gcp_iam.IAMClient") as mock_iam_cls,
            patch("multicloud.iam.iam_gcp.gcp_iam.ProjectsClient") as mock_projects_cls,
        ):
            resolved = MagicMock()
            mock_get_credentials.return_value = resolved

            store = GcpIam(region="us-central1", credentials_overrider=overrider)

            mock_get_credentials.assert_called_once_with(overrider)
            mock_iam_cls.assert_called_once_with(credentials=resolved)
            mock_projects_cls.assert_called_once_with(credentials=resolved)
            assert store._credentials_overrider is overrider

    def test_assume_role_override_builds_clients_with_resolved_credentials(self):
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE,
            role="target-sa@other-project.iam.gserviceaccount.com",
            duration_seconds=1800,
        )
        with (
            patch(
                "multicloud.iam.iam_gcp.gcp_iam.get_credentials"
            ) as mock_get_credentials,
            patch("multicloud.iam.iam_gcp.gcp_iam.IAMClient") as mock_iam_cls,
            patch("multicloud.iam.iam_gcp.gcp_iam.ProjectsClient") as mock_projects_cls,
        ):
            resolved = MagicMock()
            mock_get_credentials.return_value = resolved

            GcpIam(region="us-central1", credentials_overrider=overrider)

            mock_get_credentials.assert_called_once_with(overrider)
            mock_iam_cls.assert_called_once_with(credentials=resolved)
            mock_projects_cls.assert_called_once_with(credentials=resolved)

    def test_override_resolving_to_none_falls_through_to_default_clients(self):
        # ASSUME_ROLE_WEB_IDENTITY is unsupported by GCP get_credentials, which
        # returns None; the provider must fall through to default client init.
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.ASSUME_ROLE_WEB_IDENTITY,
            role="sa@proj.iam.gserviceaccount.com",
            web_identity_token_supplier=lambda: "jwt",
        )
        with (
            patch(
                "multicloud.iam.iam_gcp.gcp_iam.get_credentials"
            ) as mock_get_credentials,
            patch("multicloud.iam.iam_gcp.gcp_iam.IAMClient") as mock_iam_cls,
            patch("multicloud.iam.iam_gcp.gcp_iam.ProjectsClient") as mock_projects_cls,
        ):
            mock_get_credentials.return_value = None

            GcpIam(region="us-central1", credentials_overrider=overrider)

            mock_get_credentials.assert_called_once_with(overrider)
            mock_iam_cls.assert_called_once_with()
            mock_projects_cls.assert_called_once_with()

    def test_no_overrider_builds_default_clients(self):
        with (
            patch(
                "multicloud.iam.iam_gcp.gcp_iam.get_credentials"
            ) as mock_get_credentials,
            patch("multicloud.iam.iam_gcp.gcp_iam.IAMClient") as mock_iam_cls,
            patch("multicloud.iam.iam_gcp.gcp_iam.ProjectsClient") as mock_projects_cls,
        ):
            GcpIam(region="us-central1")

            mock_get_credentials.assert_not_called()
            mock_iam_cls.assert_called_once_with()
            mock_projects_cls.assert_called_once_with()

    def test_injected_clients_take_precedence_over_overrider(self):
        from multicloud.common.credentials import StsCredentials
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token="tok"),
        )
        injected_iam = MagicMock()
        injected_projects = MagicMock()
        with patch(
            "multicloud.iam.iam_gcp.gcp_iam.get_credentials"
        ) as mock_get_credentials:
            store = GcpIam(
                region="us-central1",
                iam_client=injected_iam,
                projects_client=injected_projects,
                credentials_overrider=overrider,
            )

            assert store._iam_client is injected_iam
            assert store._projects_client is injected_projects
            mock_get_credentials.assert_not_called()

    def test_one_injected_client_still_honors_overrider_for_the_other(self):
        # Injecting only one client must not silently drop the overrider for
        # the other: the injected client is kept, while the un-injected one is
        # built from the resolved credentials.
        from multicloud.common.credentials import StsCredentials
        from multicloud.iam.iam_client.driver import (
            CredentialsOverrider,
            CredentialsType,
        )

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token="tok"),
        )
        injected_iam = MagicMock()
        with (
            patch(
                "multicloud.iam.iam_gcp.gcp_iam.get_credentials"
            ) as mock_get_credentials,
            patch("multicloud.iam.iam_gcp.gcp_iam.ProjectsClient") as mock_projects_cls,
        ):
            resolved = MagicMock()
            mock_get_credentials.return_value = resolved

            store = GcpIam(
                region="us-central1",
                iam_client=injected_iam,
                credentials_overrider=overrider,
            )

            # Injected IAM client kept as-is.
            assert store._iam_client is injected_iam
            # Projects client built from the resolved overrider credentials,
            # not from defaults.
            mock_get_credentials.assert_called_once_with(overrider)
            mock_projects_cls.assert_called_once_with(credentials=resolved)
