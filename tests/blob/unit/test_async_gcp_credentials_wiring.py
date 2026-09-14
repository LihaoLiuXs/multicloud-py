"""Unit tests for credential-override wiring in GcpAsyncBlobStore.

Tests validate that the async GCS store resolves an auth token from a
credentials override on first use, passes it to the Storage client, and
closes the owned token on shutdown. An injected Storage instance takes
precedence and skips token resolution entirely.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import GcpAsyncBlobStore
from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
    StsCredentials,
)
from multicloud.gcp_common.gcp_credentials import DEFAULT_SCOPES

try:
    import aiohttp  # noqa: F401
except ImportError:
    pytest.skip("aiohttp not installed", allow_module_level=True)

_MODULE = "multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store"


@pytest.mark.asyncio
class TestGcpAsyncCredentialsWiring:
    """Token resolution, Storage wiring, and token cleanup."""

    async def test_overrider_token_passed_to_storage(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token="ya29.tok"),
        )
        token = MagicMock()
        storage_instance = AsyncMock()

        with (
            patch(f"{_MODULE}.get_token", return_value=token) as mock_get_token,
            patch(
                f"{_MODULE}.Storage", return_value=storage_instance
            ) as mock_storage_cls,
        ):
            store = GcpAsyncBlobStore(
                bucket="test-bucket",
                region="us-central1",
                credentials_overrider=overrider,
            )
            assert store._credentials_overrider is overrider

            await store._ensure_client()

            # Token resolved from the overrider and threaded into Storage.
            # Assert the full call — including scopes= — so a refactor that
            # drops or mangles the scopes kwarg is caught.
            mock_get_token.assert_called_once_with(overrider, scopes=DEFAULT_SCOPES)
            assert mock_get_token.call_args.args[0] is overrider
            assert store._token is token
            assert mock_storage_cls.call_args.kwargs["token"] is token
            assert store._storage is storage_instance

    async def test_none_token_falls_through_to_adc(self):
        with (
            patch(f"{_MODULE}.get_token", return_value=None) as mock_get_token,
            patch(f"{_MODULE}.Storage", return_value=AsyncMock()) as mock_storage_cls,
        ):
            store = GcpAsyncBlobStore(bucket="test-bucket", region="us-central1")

            await store._ensure_client()

            # Unsupported/None override → no token kwarg; ADC used.
            mock_get_token.assert_called_once()
            assert store._token is None
            assert "token" not in mock_storage_cls.call_args.kwargs

    async def test_injected_storage_skips_token_resolution(self):
        injected = AsyncMock()
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token="ya29.tok"),
        )

        with (
            patch(f"{_MODULE}.get_token") as mock_get_token,
            patch(f"{_MODULE}.Storage") as mock_storage_cls,
        ):
            store = GcpAsyncBlobStore(
                bucket="test-bucket",
                credentials_overrider=overrider,
                _storage=injected,
            )

            await store._ensure_client()

            # Injected Storage used directly; no token resolved, none built.
            mock_get_token.assert_not_called()
            mock_storage_cls.assert_not_called()
            assert store._storage is injected

    async def test_owned_token_closed_on_close(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token="ya29.tok"),
        )
        token = AsyncMock()
        storage_instance = AsyncMock()

        with (
            patch(f"{_MODULE}.get_token", return_value=token),
            patch(f"{_MODULE}.Storage", return_value=storage_instance),
        ):
            store = GcpAsyncBlobStore(
                bucket="test-bucket",
                credentials_overrider=overrider,
            )
            await store._ensure_client()
            await store.close()

            # Storage closed and the owned token closed alongside it.
            storage_instance.close.assert_awaited_once()
            token.close.assert_awaited_once()
            assert store._token is None
