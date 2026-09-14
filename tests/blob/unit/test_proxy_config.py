"""Proxy configuration on the blob clients.

The case that matters is a pod whose environment advertises an egress proxy
while some traffic must stay inside the network. These tests set such an
environment and assert what each provider's HTTP layer would actually do with
it, rather than only that the setting was stored.
"""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from multicloud.blob.blob_client import AsyncBucketClient, BucketClient
from multicloud.common.exceptions import InvalidArgumentException
from multicloud.common.proxy import ProxyConfig

POD_PROXY = "http://proxy.example.net:8443"
CORP_PROXY = "http://corp.proxy.example:3128"
DIRECT = ProxyConfig(use_environment_variable_proxy_values=False)


@pytest.fixture
def proxied_env(monkeypatch):
    """An environment advertising an egress proxy, as a pod would."""
    monkeypatch.setenv("HTTP_PROXY", POD_PROXY)
    monkeypatch.setenv("HTTPS_PROXY", POD_PROXY)


class TestProxyConfigModel:
    def test_defaults_to_inheriting_the_environment(self):
        config = ProxyConfig()
        assert config.endpoint is None
        assert config.use_environment_variable_proxy_values is True

    def test_blank_endpoint_is_rejected(self):
        with pytest.raises(InvalidArgumentException, match="must not be blank"):
            ProxyConfig(endpoint="   ")

    def test_endpoint_without_a_scheme_is_rejected(self):
        """A bare host:port silently fails to proxy, so it is refused here."""
        with pytest.raises(InvalidArgumentException, match="must include a scheme"):
            ProxyConfig(endpoint="proxy.example.net:8443")

    def test_is_immutable(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            DIRECT.endpoint = CORP_PROXY  # type: ignore[misc]

    def test_endpoint_whitespace_is_stripped(self):
        """A padded endpoint passes validation but would not proxy."""
        assert ProxyConfig(endpoint=f"  {CORP_PROXY}  ").endpoint == CORP_PROXY


S3_URL = "https://s3.us-west-2.amazonaws.com/test-bucket"


class TestAwsSyncProxy:
    """Asserted through the proxy botocore resolves, not the config just set."""

    @staticmethod
    def _resolved(proxy_config):
        client = BucketClient(
            provider="aws",
            bucket="test-bucket",
            region="us-west-2",
            proxy_config=proxy_config,
        )
        session = client._store._s3._endpoint.http_session
        return session._proxy_config.proxy_url_for(S3_URL)

    @staticmethod
    def _proxies(proxy_config):
        client = BucketClient(
            provider="aws",
            bucket="test-bucket",
            region="us-west-2",
            proxy_config=proxy_config,
        )
        return client._store._s3.meta.config.proxies

    def test_no_config_inherits_the_pod_proxy(self, proxied_env):
        assert self._resolved(None) == POD_PROXY

    def test_going_direct_resolves_to_no_proxy(self, proxied_env):
        assert self._resolved(DIRECT) is None

    def test_explicit_endpoint_is_what_gets_resolved(self, proxied_env):
        assert self._resolved(ProxyConfig(endpoint=CORP_PROXY)) == CORP_PROXY

    def test_going_direct_sets_an_empty_mapping_not_an_absent_one(self, proxied_env):
        """Empty is not the same as unset: unset is what re-enables the environment."""
        assert self._proxies(DIRECT) == {}
        assert self._proxies(None) is None

    def test_signature_version_is_still_pinned(self, proxied_env):
        client = BucketClient(
            provider="aws",
            bucket="test-bucket",
            region="us-west-2",
            proxy_config=DIRECT,
        )
        assert client._store._s3.meta.config.signature_version == "s3v4"


class TestAwsAsyncProxy:
    @staticmethod
    def _proxies(proxy_config):
        async def run():
            client = AsyncBucketClient(
                provider="aws",
                bucket="test-bucket",
                region="us-west-2",
                proxy_config=proxy_config,
            )
            await client._store._ensure_client()
            proxies = client._store._client.meta.config.proxies
            await client.close()
            return proxies

        return asyncio.run(run())

    def test_no_config_leaves_botocore_reading_the_environment(self, proxied_env):
        assert self._proxies(None) is None

    def test_going_direct_sets_an_empty_mapping(self, proxied_env):
        assert self._proxies(DIRECT) == {}

    def test_explicit_endpoint_is_used(self, proxied_env):
        assert self._proxies(ProxyConfig(endpoint=CORP_PROXY))["https"] == CORP_PROXY


class TestGcpSyncProxy:
    """Asserted through merge_environment_settings, which is what send() uses.

    Reading ``session.proxies`` would miss the environment merge that overrides
    it.
    """

    STORAGE_URL = "https://storage.googleapis.com/storage/v1/b/test-bucket"

    def _sessions(self, proxy_config):
        pytest.importorskip("google.cloud.storage")
        # Patched so the suite needs no credentials; the sessions under test
        # are still the real ones the store builds.
        with (
            patch("google.auth.default") as mock_auth,
            patch(
                "multicloud.blob.blob_gcp.gcp_blob_store.storage.Client"
            ) as mock_client_cls,
        ):
            mock_auth.return_value = (MagicMock(), "test-project")
            client = BucketClient(
                provider="gcp",
                bucket="test-bucket",
                region="us-west1",
                proxy_config=proxy_config,
            )
            http = mock_client_cls.call_args.kwargs.get("_http")
        data_session = http
        auth_session = getattr(
            getattr(client._store, "_auth_request", None), "session", None
        )
        return data_session, auth_session

    def _effective(self, session, url=None):
        return session.merge_environment_settings(
            url or self.STORAGE_URL, {}, None, None, None
        )

    def test_no_config_leaves_the_vendor_session_in_place(self, proxied_env):
        """Its own session trusts the environment, so the pod proxy applies."""
        data_session, _ = self._sessions(None)
        assert data_session is None

    def test_a_default_config_also_leaves_the_vendor_session_in_place(
        self, proxied_env
    ):
        """ProxyConfig() asks for the existing behaviour, so nothing is replaced."""
        data_session, _ = self._sessions(ProxyConfig())
        assert data_session is None

    def test_going_direct_uses_no_proxy(self, proxied_env):
        data_session, _ = self._sessions(DIRECT)
        assert not self._effective(data_session)["proxies"]

    def test_going_direct_also_applies_to_the_token_endpoint(self, proxied_env):
        """Credentials refresh over a separate session, which needs the same
        treatment or the data path succeeds while the token request fails."""
        _, auth_session = self._sessions(DIRECT)
        assert auth_session is not None
        effective = self._effective(auth_session, "https://oauth2.googleapis.com/token")
        assert not effective["proxies"]

    def test_going_direct_preserves_a_custom_certificate_bundle(
        self, proxied_env, monkeypatch, tmp_path
    ):
        """Disabling the proxy must not silently drop a custom CA."""
        bundle = tmp_path / "corp-ca.pem"
        bundle.write_text("-----BEGIN CERTIFICATE-----\n")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))

        data_session, auth_session = self._sessions(DIRECT)

        assert self._effective(data_session)["verify"] == str(bundle)
        assert self._effective(auth_session)["verify"] == str(bundle)

    def test_explicit_endpoint_overrides_the_pod_proxy(self, proxied_env):
        data_session, _ = self._sessions(ProxyConfig(endpoint=CORP_PROXY))
        assert self._effective(data_session)["proxies"].get("https") == CORP_PROXY


class TestGcpAsyncProxy:
    """aiohttp ignores the environment unless asked to trust it."""

    @staticmethod
    def _session(proxy_config):
        pytest.importorskip("gcloud.aio.storage")

        async def run():
            # Storage is patched so the suite needs no credentials; the session
            # under test is still the real one the store builds.
            with patch(
                "multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store.Storage",
                return_value=AsyncMock(),
            ):
                client = AsyncBucketClient(
                    provider="gcp",
                    bucket="test-bucket",
                    region="us-west1",
                    proxy_config=proxy_config,
                )
                await client._store._ensure_client()
                session = client._store._owned_session
                detail = (
                    None
                    if session is None
                    else (session.trust_env, session._default_proxy)
                )
                await client.close()
                return detail

        return asyncio.run(run())

    def test_no_config_leaves_the_default_session_alone(self, proxied_env):
        assert self._session(None) is None

    def test_asking_to_inherit_opts_into_trusting_the_environment(self, proxied_env):
        trust_env, proxy = self._session(ProxyConfig())
        assert trust_env is True
        assert proxy is None

    def test_going_direct_does_not_trust_the_environment(self, proxied_env):
        trust_env, proxy = self._session(DIRECT)
        assert trust_env is False
        assert proxy is None

    def test_explicit_endpoint_is_set_on_the_session(self, proxied_env):
        _, proxy = self._session(ProxyConfig(endpoint=CORP_PROXY))
        assert str(proxy) == CORP_PROXY

    def test_the_created_session_is_closed_with_the_client(self, proxied_env):
        """A session this store owns must not outlive it."""

        async def run():
            with patch(
                "multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store.Storage",
                return_value=AsyncMock(),
            ):
                client = AsyncBucketClient(
                    provider="gcp",
                    bucket="test-bucket",
                    region="us-west1",
                    proxy_config=DIRECT,
                )
                await client._store._ensure_client()
                session = client._store._owned_session
                await client.close()
                return session.closed

        assert asyncio.run(run()) is True
