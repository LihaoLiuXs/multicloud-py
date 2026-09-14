"""Unit tests for multicloud.common.credentials."""

from __future__ import annotations

from multicloud.common.credentials import StsCredentials


class TestStsCredentialsRepr:

    def test_secret_fields_are_masked(self):
        creds = StsCredentials(
            access_key_id="AKIAEXAMPLE",
            access_key_secret="super-secret",
            security_token="bearer-token-value",
        )
        rendered = repr(creds)

        assert "super-secret" not in rendered
        assert "bearer-token-value" not in rendered
        assert "***" in rendered

    def test_non_secret_fields_remain_visible(self):
        creds = StsCredentials(access_key_id="AKIAEXAMPLE")
        rendered = repr(creds)

        assert "AKIAEXAMPLE" in rendered
