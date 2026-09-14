"""Unit tests for GcpAsyncBlobStore presigned URL generation.

These tests drive the real google-cloud-storage V4 signer using a
throwaway RSA service-account key (generated in-process, never network
I/O), so they exercise the actual signed URL rather than a mock of it.
They assert the two properties that matter for a usable signed URL:

- PUT: every header folded into X-Goog-SignedHeaders is surfaced in
  required_headers, so the caller can reproduce the request. A signed
  header the caller is not told about causes a 403 SignatureDoesNotMatch
  at upload time. GCS stores metadata and tags as x-goog-meta-* headers.
- GET: response overrides (content-disposition) and the object
  generation ride as query parameters, not signed request headers, so
  the caller needs no extra headers.

The sync google-cloud-storage SDK does the signing (gcloud-aio-storage
cannot sign), so we inject a credentialed client by patching the
``sync_storage.Client`` factory the provider calls.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip(
    "google.cloud.storage",
    reason="google-cloud-storage not installed (optional gcp dependency)",
)
pytest.importorskip(
    "cryptography", reason="cryptography not installed (needed to build a signing key)"
)

from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.cloud import storage as real_storage
from google.oauth2 import service_account
from multicloud.blob.blob_client.async_blob.async_driver import PresignedUrlResult
from multicloud.blob.blob_gcp.async_blob import gcp_async_blob_store as gcp_mod
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import GcpAsyncBlobStore


def _make_signing_client() -> "real_storage.Client":
    """Build a real storage.Client backed by a throwaway RSA key.

    The key is generated in-process and never leaves it. It is a valid
    signer, so blob.generate_signed_url produces a genuine V4 signature
    entirely offline.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    info = {
        "type": "service_account",
        "project_id": "test-proj",
        "private_key_id": "kid",
        "private_key": pem,
        "client_email": "signer@test-proj.iam.gserviceaccount.com",
        "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    creds = service_account.Credentials.from_service_account_info(info)
    return real_storage.Client(project="test-proj", credentials=creds)


@pytest.fixture(scope="module")
def signing_client() -> "real_storage.Client":
    return _make_signing_client()


@pytest.fixture
def store() -> GcpAsyncBlobStore:
    # do_generate_presigned_url never touches self._storage; signing goes
    # through sync_storage.Client(), which the tests patch.
    return GcpAsyncBlobStore(bucket="test-bucket")


def _signed_headers(url: str) -> set[str]:
    query = parse_qs(urlparse(url).query)
    raw = query.get("X-Goog-SignedHeaders", [""])[0]
    return {h for h in raw.split(";") if h}


def _mock_signing_client(
    *,
    key_id,
    requires_scopes=False,
    scopes=("https://www.googleapis.com/auth/cloud-platform",),
    token="tok-abc",
    service_account_email="sa@proj.iam.gserviceaccount.com",
    valid=True,
    url="https://storage.googleapis.com/b/o?X-Goog-Signature=deadbeef",
):
    """Build a mock sync ``storage.Client`` with a configurable credential.

    Lets the IAM-signBlob branch be exercised entirely offline. ``key_id=None``
    simulates an impersonated/ADC credential with no local private key (signs via
    the IAM API); a string ``key_id`` simulates a service-account key file that
    signs locally. ``client._credentials`` is what the provider reads back.
    """
    creds = MagicMock()
    creds.signer.key_id = key_id
    creds.requires_scopes = requires_scopes
    creds.scopes = list(scopes) if scopes else scopes
    creds.token = token
    creds.service_account_email = service_account_email
    creds.valid = valid
    # By default, re-scoping returns the same credential so token/email persist.
    creds.with_scopes.return_value = creds
    client = MagicMock()
    client._credentials = creds
    client.bucket.return_value.blob.return_value.generate_signed_url.return_value = url
    return client, creds


def _signed_url_kwargs(client):
    """Return the kwargs the provider passed to blob.generate_signed_url."""
    blob = client.bucket.return_value.blob.return_value
    return blob.generate_signed_url.call_args[1]


class TestGcpAsyncPresignedImpersonatedSigning:
    """Signing credential selection for the sync-SDK V4 signer.

    Mirrors the synchronous provider's contract (see
    test_gcp_blob_store.py::TestGcpPresignedUrl): a credential with a local
    private key signs directly; an impersonated/ADC credential without one
    signs via the IAM signBlob API and must be handed a fresh access token,
    its service-account email, and — unique to the async provider, which lets
    the client resolve ADC rather than requesting scopes up front — a
    cloud-platform-scoped token.
    """

    @pytest.mark.asyncio
    async def test_iam_backed_signer_passes_access_token(self, store):
        # signer exists but key_id is None => impersonated SA / ADC.
        client, creds = _mock_signing_client(key_id=None)
        with patch.object(gcp_mod.sync_storage, "Client", return_value=client):
            await store.generate_presigned_url("k.txt", "PUT")

        kwargs = _signed_url_kwargs(client)
        assert kwargs["access_token"] == "tok-abc"
        assert kwargs["service_account_email"] == "sa@proj.iam.gserviceaccount.com"

    @pytest.mark.asyncio
    async def test_local_signer_skips_iam_token(self, store):
        # A real key_id => local private key; sign directly, no IAM token.
        client, creds = _mock_signing_client(key_id="real-private-key-id")
        with patch.object(gcp_mod.sync_storage, "Client", return_value=client):
            await store.generate_presigned_url("k.txt", "PUT")

        kwargs = _signed_url_kwargs(client)
        assert "access_token" not in kwargs
        assert "service_account_email" not in kwargs
        creds.with_scopes.assert_not_called()
        creds.refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_adc_without_scopes_is_rescoped_to_cloud_platform(self, store):
        # Regression: ADC from `gcloud auth application-default login` carry no
        # scopes; the IAM signBlob call 403s with "insufficient authentication
        # scopes" unless we re-scope. The rescoped credential's token must be
        # the one handed to the signer.
        client, creds = _mock_signing_client(
            key_id=None, requires_scopes=True, scopes=None
        )
        rescoped = MagicMock()
        rescoped.valid = True
        rescoped.token = "rescoped-token"
        rescoped.service_account_email = "sa@proj.iam.gserviceaccount.com"
        creds.with_scopes.return_value = rescoped
        with patch.object(gcp_mod.sync_storage, "Client", return_value=client):
            await store.generate_presigned_url("k.txt", "PUT")

        creds.with_scopes.assert_called_once_with(gcp_mod.DEFAULT_SCOPES)
        kwargs = _signed_url_kwargs(client)
        assert kwargs["access_token"] == "rescoped-token"

    @pytest.mark.asyncio
    async def test_scoped_credential_is_not_rescoped(self, store):
        # Already cloud-platform scoped => no re-scope round trip.
        client, creds = _mock_signing_client(
            key_id=None,
            requires_scopes=False,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        with patch.object(gcp_mod.sync_storage, "Client", return_value=client):
            await store.generate_presigned_url("k.txt", "PUT")

        creds.with_scopes.assert_not_called()
        assert _signed_url_kwargs(client)["access_token"] == "tok-abc"

    @pytest.mark.asyncio
    async def test_expired_credential_is_refreshed(self, store):
        client, creds = _mock_signing_client(
            key_id=None,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            valid=False,
        )
        with (
            patch.object(gcp_mod.sync_storage, "Client", return_value=client),
            patch("google.auth.transport.requests.Request") as req_cls,
        ):
            await store.generate_presigned_url("k.txt", "PUT")

        creds.refresh.assert_called_once_with(req_cls())


class TestGcpAsyncMakeSyncClient:
    """`_make_sync_client` builds the sync SDK client for URL signing."""

    def test_projectless_adc_client(self):
        # No service file => let the client resolve ADC, project=None so a
        # missing default project doesn't raise OSError.
        s = GcpAsyncBlobStore.__new__(GcpAsyncBlobStore)
        s._service_file = None
        with patch.object(gcp_mod.sync_storage, "Client") as client_cls:
            client = s._make_sync_client()

        client_cls.assert_called_once_with(project=None)
        assert client is client_cls.return_value

    def test_service_account_file_client(self):
        # A service file is itself a local signer; build the client from it.
        s = GcpAsyncBlobStore.__new__(GcpAsyncBlobStore)
        s._service_file = "/path/to/key.json"
        fake_creds = MagicMock()
        fake_creds.project_id = "file-proj"
        with (
            patch.object(
                gcp_mod.service_account.Credentials,
                "from_service_account_file",
                return_value=fake_creds,
            ) as from_file,
            patch.object(gcp_mod.sync_storage, "Client") as client_cls,
        ):
            client = s._make_sync_client()

        from_file.assert_called_once_with(
            "/path/to/key.json", scopes=gcp_mod.DEFAULT_SCOPES
        )
        client_cls.assert_called_once_with(credentials=fake_creds, project="file-proj")
        assert client is client_cls.return_value


class TestGcpAsyncPresignedPut:
    """PUT must surface every signed header so the caller can replay it."""

    @pytest.mark.asyncio
    async def test_put_url_is_signed_string(self, store, signing_client):
        with patch.object(gcp_mod.sync_storage, "Client", return_value=signing_client):
            result = await store.generate_presigned_url("k.txt", "PUT")

        assert isinstance(result, PresignedUrlResult)
        assert isinstance(result.url, str)
        assert result.url.startswith("https://")
        assert "X-Goog-Signature=" in result.url
        assert result.http_method == "PUT"

    @pytest.mark.asyncio
    async def test_put_surfaces_all_signed_headers(self, store, signing_client):
        with patch.object(gcp_mod.sync_storage, "Client", return_value=signing_client):
            result = await store.generate_presigned_url(
                "k.txt",
                "PUT",
                content_type="application/json",
                metadata={"team": "blob", "env": "prod"},
                tags={"project": "x"},
                content_disposition="attachment; filename=x.json",
            )

        # Contract: caller must send every signed header except host.
        signed = _signed_headers(result.url)
        assert signed, "expected V4 signed headers on the URL"
        surfaced = {h.lower() for h in result.required_headers}
        missing = {h for h in signed if h != "host"} - surfaced
        assert not missing, f"signed headers not surfaced to caller: {missing}"

        # Metadata rides as x-goog-meta-*; tags as x-goog-meta-gcp-tag-*.
        assert result.required_headers["Content-Type"] == "application/json"
        assert result.required_headers["x-goog-meta-team"] == "blob"
        assert result.required_headers["x-goog-meta-env"] == "prod"
        assert result.required_headers["x-goog-meta-gcp-tag-project"] == "x"
        assert (
            result.required_headers["Content-Disposition"]
            == "attachment; filename=x.json"
        )

    @pytest.mark.asyncio
    async def test_put_without_extras_needs_no_headers(self, store, signing_client):
        with patch.object(gcp_mod.sync_storage, "Client", return_value=signing_client):
            result = await store.generate_presigned_url("k.txt", "PUT")

        assert dict(result.required_headers) == {}


class TestGcpAsyncPresignedGet:
    """GET response overrides ride as query params, not signed headers."""

    @pytest.mark.asyncio
    async def test_get_url_is_signed_string(self, store, signing_client):
        with patch.object(gcp_mod.sync_storage, "Client", return_value=signing_client):
            result = await store.generate_presigned_url("k.txt", "GET")

        assert result.url.startswith("https://")
        assert "X-Goog-Signature=" in result.url
        assert result.http_method == "GET"

    @pytest.mark.asyncio
    async def test_get_disposition_is_query_param_not_header(
        self, store, signing_client
    ):
        with patch.object(gcp_mod.sync_storage, "Client", return_value=signing_client):
            result = await store.generate_presigned_url(
                "k.txt", "GET", content_disposition="attachment; filename=x.txt"
            )

        # No signed request headers beyond host -> caller needs none.
        assert dict(result.required_headers) == {}
        query = parse_qs(urlparse(result.url).query)
        assert "response-content-disposition" in query
        # content-disposition must NOT be a signed request header on GET.
        assert "content-disposition" not in _signed_headers(result.url)

    @pytest.mark.asyncio
    async def test_get_version_id_is_generation_query_param(
        self, store, signing_client
    ):
        with patch.object(gcp_mod.sync_storage, "Client", return_value=signing_client):
            result = await store.generate_presigned_url(
                "k.txt", "GET", version_id="1234567890"
            )

        query = parse_qs(urlparse(result.url).query)
        assert query.get("generation") == ["1234567890"]
        assert dict(result.required_headers) == {}
