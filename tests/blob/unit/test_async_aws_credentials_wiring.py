"""Unit tests for credential-override wiring in AwsAsyncBlobStore.

Tests validate that the async S3 store resolves its aiobotocore session
from a credentials override on first use, and that an injected session
takes precedence over the override (so the resolver is not called).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import AwsAsyncBlobStore
from multicloud.common.credentials import (
    CredentialsOverrider,
    CredentialsType,
    StsCredentials,
)

try:
    import aiobotocore.session  # noqa: F401
except ImportError:
    pytest.skip("aiobotocore not installed", allow_module_level=True)


def _make_session():
    """Build a fake aiobotocore session whose create_client yields a client."""
    session = MagicMock()
    client = AsyncMock()
    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=client)
    client_cm.__aexit__ = AsyncMock(return_value=None)
    session.create_client.return_value = client_cm
    return session, client


@pytest.mark.asyncio
class TestAwsAsyncCredentialsWiring:
    """Credential-override resolution and injected-session precedence."""

    async def test_overrider_forwarded_to_resolver_on_first_use(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                access_key_id="AK",
                access_key_secret="SK",
                security_token="ST",
            ),
        )
        session, client = _make_session()

        with patch(
            "multicloud.blob.blob_aws.async_blob.aws_async_blob_store.get_aio_session",
            new=AsyncMock(return_value=session),
        ) as mock_get_session:
            store = AwsAsyncBlobStore(
                bucket="test-bucket",
                region="us-west-2",
                credentials_overrider=overrider,
            )
            # Stored on the driver base for the resolver to consume.
            assert store._credentials_overrider is overrider

            await store._ensure_client()

            # Resolver called once with the overrider and region.
            mock_get_session.assert_awaited_once_with(overrider, "us-west-2")
            assert store._session is session
            assert store._client is client
            session.create_client.assert_called_once()

    async def test_injected_session_takes_precedence_over_overrider(self):
        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(security_token="ST"),
        )
        session, client = _make_session()

        with patch(
            "multicloud.blob.blob_aws.async_blob.aws_async_blob_store.get_aio_session",
            new=AsyncMock(),
        ) as mock_get_session:
            store = AwsAsyncBlobStore(
                bucket="test-bucket",
                region="us-west-2",
                credentials_overrider=overrider,
                _session=session,
            )

            await store._ensure_client()

            # Injected session used directly; resolver never called.
            mock_get_session.assert_not_awaited()
            assert store._client is client
            session.create_client.assert_called_once()

    async def test_no_overrider_resolves_default_session(self):
        session, client = _make_session()

        with patch(
            "multicloud.blob.blob_aws.async_blob.aws_async_blob_store.get_aio_session",
            new=AsyncMock(return_value=session),
        ) as mock_get_session:
            store = AwsAsyncBlobStore(bucket="test-bucket", region="us-west-2")

            await store._ensure_client()

            # Resolver still called, with None so it builds a default session.
            mock_get_session.assert_awaited_once_with(None, "us-west-2")
            assert store._client is client
