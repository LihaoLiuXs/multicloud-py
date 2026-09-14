"""Unit tests for :class:`AwsStsUtilities` — the AWS SigV4 signer."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
from unittest.mock import MagicMock, patch

import pytest
from botocore.credentials import Credentials
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from multicloud.common.exceptions import (
    InvalidArgumentException,
    ResourceExhaustedException,
    UnAuthorizedException,
    UnknownException,
)
from multicloud.sts.sts_aws.aws_sts_utilities import AwsStsUtilities

_AK = "AKIDEXAMPLE"
_SK = "not-a-real-secret-key-for-signing-tests"


def _fixed_credentials(token: str | None = "SESSION_TOKEN") -> Credentials:
    return Credentials(access_key=_AK, secret_key=_SK, token=token)


def _get(headers: dict, name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _signed_headers(authorization: str) -> set[str]:
    segment = next(
        part for part in authorization.split(", ") if part.startswith("SignedHeaders=")
    )
    return set(segment[len("SignedHeaders=") :].split(";"))


def _authorization_signature(authorization: str) -> str:
    segment = next(
        part for part in authorization.split(", ") if part.startswith("Signature=")
    )
    return segment[len("Signature=") :]


def _recompute_sigv4_signature(
    *,
    method: str,
    headers: dict,
    body: bytes,
    region: str,
    service: str,
    secret_key: str,
) -> str:
    """Recompute the SigV4 signature independently of the signer under test.

    Rebuilds the canonical request / string-to-sign / signing-key chain with
    hmac + hashlib. Reads the emitted ``X-Amz-Date`` so the timestamp matches
    by construction — no need to freeze the clock.
    """
    authorization = _get(headers, "Authorization")
    amz_date = _get(headers, "X-Amz-Date")
    date_stamp = amz_date[:8]

    signed_header_names = sorted(_signed_headers(authorization))
    canonical_headers = "".join(
        f"{name}:{' '.join(_get(headers, name).split())}\n"
        for name in signed_header_names
    )
    signed_headers_str = ";".join(signed_header_names)
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = (
        f"{method}\n"
        "/\n"  # canonical URI: the STS endpoint path
        "\n"  # canonical query string: none
        f"{canonical_headers}\n"
        f"{signed_headers_str}\n"
        f"{payload_hash}"
    )

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(f"AWS4{secret_key}".encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    return hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def utils():
    return AwsStsUtilities(region="us-west-2", credentials=_fixed_credentials())


class TestAwsStsUtilitiesConstruction:

    def test_injected_credentials_used(self):
        u = AwsStsUtilities(region="us-east-1", credentials=_fixed_credentials())
        assert u.region == "us-east-1"
        assert u.get_provider_id() == "aws"

    @patch("multicloud.sts.sts_aws.aws_sts_utilities.get_session")
    def test_default_chain_resolved_at_signing(self, mock_get_session):
        session = MagicMock()
        session.get_credentials.return_value = _fixed_credentials()
        mock_get_session.return_value = session

        u = AwsStsUtilities(region="eu-west-1")
        # Construction resolves nothing — resolution is deferred to signing.
        mock_get_session.assert_not_called()

        u.create_signed_auth_request()
        mock_get_session.assert_called_once_with(None, "eu-west-1")
        session.get_credentials.assert_called_once()

    @patch("multicloud.sts.sts_aws.aws_sts_utilities.get_session")
    def test_credentials_overrider_forwarded(self, mock_get_session):
        from multicloud.common.credentials import (
            CredentialsOverrider,
            CredentialsType,
            StsCredentials,
        )

        session = MagicMock()
        session.get_credentials.return_value = _fixed_credentials()
        mock_get_session.return_value = session

        overrider = CredentialsOverrider(
            credentials_type=CredentialsType.SESSION,
            session_credentials=StsCredentials(
                access_key_id="AK", access_key_secret="SK"
            ),
        )
        AwsStsUtilities(
            region="us-west-2", credentials_overrider=overrider
        ).create_signed_auth_request()
        mock_get_session.assert_called_once_with(overrider, "us-west-2")

    @patch("multicloud.sts.sts_aws.aws_sts_utilities.get_session")
    def test_injected_credentials_take_precedence(self, mock_get_session):
        creds = _fixed_credentials()
        u = AwsStsUtilities(region="us-west-2", credentials=creds)
        resp = u.create_signed_auth_request()
        # Injected credentials sign the request; the default chain is never used.
        assert resp.credentials.access_key_id == _AK
        mock_get_session.assert_not_called()

    @patch("multicloud.sts.sts_aws.aws_sts_utilities.get_session")
    def test_construction_defers_credential_resolution(self, mock_get_session):
        # A native credential-resolution error must not escape the public
        # constructor: resolution — and thus the error — is deferred to signing
        # time, where the client layer's translate_error can convert it.
        mock_get_session.side_effect = BotoCoreError()

        u = AwsStsUtilities(region="us-west-2")  # must not raise
        mock_get_session.assert_not_called()

        with pytest.raises(BotoCoreError):
            u.create_signed_auth_request()

    @patch("multicloud.sts.sts_aws.aws_sts_utilities.get_session")
    def test_transient_none_is_not_pinned(self, mock_get_session):
        # A transient default-chain failure (get_credentials -> None) must not
        # brick the instance: a later call retries and can succeed.
        failing = MagicMock()
        failing.get_credentials.return_value = None
        recovering = MagicMock()
        recovering.get_credentials.return_value = _fixed_credentials()
        mock_get_session.side_effect = [failing, recovering]

        u = AwsStsUtilities(region="us-west-2")
        with pytest.raises(NoCredentialsError):
            u.create_signed_auth_request()

        resp = u.create_signed_auth_request()
        assert resp.credentials.access_key_id == _AK
        assert mock_get_session.call_count == 2


class TestAwsStsUtilitiesSigning:

    def test_signs_get_caller_identity(self, utils):
        resp = utils.create_signed_auth_request()

        assert resp.method == "POST"
        assert resp.url == "https://sts.us-west-2.amazonaws.com/"
        assert resp.body == b"Action=GetCallerIdentity&Version=2011-06-15"

        auth = _get(resp.headers, "Authorization")
        assert auth is not None
        assert auth.startswith("AWS4-HMAC-SHA256 ")
        # Credential scope ties the signature to this key, region, and service.
        assert f"Credential={_AK}/" in auth
        assert "/us-west-2/sts/aws4_request" in auth
        assert _get(resp.headers, "X-Amz-Date") is not None
        assert _get(resp.headers, "Content-Type") == (
            "application/x-www-form-urlencoded; charset=utf-8"
        )
        assert _get(resp.headers, "Host") == "sts.us-west-2.amazonaws.com"

    def test_returns_signing_credentials(self, utils):
        resp = utils.create_signed_auth_request()
        assert resp.credentials.access_key_id == _AK
        assert resp.credentials.security_token == "SESSION_TOKEN"
        # The signing secret is deliberately not carried on the result: the
        # SigV4 signature is already in the headers and the broker replaying
        # the request never needs the raw secret.
        assert resp.credentials.access_key_secret == ""

    def test_signs_with_a_single_frozen_credential_snapshot(self):
        # A refreshable credential can return one generation when read during
        # signing and another when read again for the response — signing with
        # one key but reporting a different one. Freezing once before signing
        # must make the key in the signature and the returned key identical.
        # Model the hazard: get_frozen_credentials() stays pinned while every
        # live attribute read rotates.
        frozen = _fixed_credentials().get_frozen_credentials()

        class _RotatingCredentials:
            def __init__(self) -> None:
                self._n = 0

            def get_frozen_credentials(self):
                return frozen

            @property
            def access_key(self) -> str:
                self._n += 1
                return f"AKIA_ROTATED_{self._n}"

            @property
            def secret_key(self) -> str:
                return "rotated-secret"

            @property
            def token(self) -> str:
                self._n += 1
                return f"TOKEN_ROTATED_{self._n}"

        u = AwsStsUtilities(region="us-west-2", credentials=_RotatingCredentials())
        resp = u.create_signed_auth_request()

        auth = _get(resp.headers, "Authorization")
        # The signature's credential scope and the returned metadata must both
        # name the frozen snapshot, never a rotated live read.
        assert f"Credential={_AK}/" in auth
        assert resp.credentials.access_key_id == _AK
        assert _get(resp.headers, "X-Amz-Security-Token") == "SESSION_TOKEN"
        assert resp.credentials.security_token == "SESSION_TOKEN"

    def test_secret_key_never_leaks_into_serialized_request(self, utils):
        # Serializing the returned request (dataclasses.asdict / json.dumps)
        # must never expose the long-term secret key — the secret is only a
        # signing input, so nothing downstream should be able to recover it.
        resp = utils.create_signed_auth_request()
        assert _SK not in str(dataclasses.asdict(resp))

    def test_custom_header_is_signed(self, utils):
        resp = utils.create_signed_auth_request(
            custom_headers={"X-Vault-AWS-IAM-Server-ID": "vault.example.com"}
        )

        assert _get(resp.headers, "X-Vault-AWS-IAM-Server-ID") == "vault.example.com"
        signed = _signed_headers(_get(resp.headers, "Authorization"))
        # The custom header must be part of the signature, not just attached.
        assert "x-vault-aws-iam-server-id" in signed
        assert {"host", "content-type", "x-amz-date"}.issubset(signed)

    def test_signature_is_valid_sigv4_bound_to_the_secret(self, utils):
        # The shape checks above pass even for a signer that used the wrong
        # secret. Recompute the signature independently and assert the bytes
        # match, then assert a wrong secret yields a different signature — so a
        # wrong-secret signer would be caught here.
        resp = utils.create_signed_auth_request(
            custom_headers={"X-Vault-AWS-IAM-Server-ID": "vault.example.com"}
        )
        produced = _authorization_signature(_get(resp.headers, "Authorization"))

        recomputed = _recompute_sigv4_signature(
            method=resp.method,
            headers=resp.headers,
            body=resp.body,
            region="us-west-2",
            service="sts",
            secret_key=_SK,
        )
        assert recomputed == produced

        wrong = _recompute_sigv4_signature(
            method=resp.method,
            headers=resp.headers,
            body=resp.body,
            region="us-west-2",
            service="sts",
            secret_key="a-different-secret-key",
        )
        assert wrong != produced

    def test_session_token_header_signed_when_present(self, utils):
        resp = utils.create_signed_auth_request()
        assert _get(resp.headers, "X-Amz-Security-Token") == "SESSION_TOKEN"
        assert "x-amz-security-token" in _signed_headers(
            _get(resp.headers, "Authorization")
        )

    def test_no_session_token_omits_security_token_header(self):
        u = AwsStsUtilities(region="us-west-2", credentials=_fixed_credentials(None))
        resp = u.create_signed_auth_request()
        assert _get(resp.headers, "X-Amz-Security-Token") is None
        assert resp.credentials.security_token == ""

    def test_distinct_custom_headers_change_signature(self, utils):
        a = utils.create_signed_auth_request(custom_headers={"X-Server-ID": "one"})
        b = utils.create_signed_auth_request(custom_headers={"X-Server-ID": "two"})
        assert _get(a.headers, "Authorization") != _get(b.headers, "Authorization")

    @pytest.mark.parametrize(
        "colliding_key", ["host", "Host", "HOST", "content-type", "Content-Type"]
    )
    def test_custom_header_colliding_with_reserved_key_rejected(
        self, utils, colliding_key
    ):
        # A custom header that case-insensitively collides with a reserved header
        # (Host / Content-Type) is signed with one value but returned with
        # another, so the broker replays a request whose signature does not
        # match its headers. Reject the collision loudly instead.
        with pytest.raises(InvalidArgumentException, match="reserved"):
            utils.create_signed_auth_request(custom_headers={colliding_key: "x"})

    @pytest.mark.parametrize("unbound_key", ["User-Agent", "user-agent", "Expect"])
    def test_custom_header_excluded_from_signature_rejected(self, utils, unbound_key):
        # SigV4 never folds certain headers (e.g. User-Agent, Expect) into
        # SignedHeaders. A caller who supplies one would get it attached but
        # unsigned, so a broker replaying the request could not trust it as
        # identity proof. Reject it loudly instead of returning it unbound.
        with pytest.raises(InvalidArgumentException, match="SignedHeaders"):
            utils.create_signed_auth_request(custom_headers={unbound_key: "x"})

    def test_missing_region_raises_invalid_argument(self):
        u = AwsStsUtilities(credentials=_fixed_credentials())
        with pytest.raises(InvalidArgumentException, match="region"):
            u.create_signed_auth_request()

    @patch("multicloud.sts.sts_aws.aws_sts_utilities.get_session")
    def test_missing_credentials_raises_no_credentials(self, mock_get_session):
        session = MagicMock()
        session.get_credentials.return_value = None
        mock_get_session.return_value = session

        u = AwsStsUtilities(region="us-west-2")
        with pytest.raises(NoCredentialsError):
            u.create_signed_auth_request()


class TestAwsStsUtilitiesTranslateError:

    def test_already_translated(self, utils):
        exc = UnAuthorizedException("already")
        assert utils.translate_error(exc) is exc

    def test_no_credentials(self, utils):
        assert isinstance(
            utils.translate_error(NoCredentialsError()), UnAuthorizedException
        )

    def test_expired_token(self, utils):
        exc = ClientError(
            {
                "Error": {"Code": "ExpiredToken"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "GetCallerIdentity",
        )
        assert isinstance(utils.translate_error(exc), UnAuthorizedException)

    def test_invalid_argument(self, utils):
        exc = ClientError(
            {
                "Error": {"Code": "ValidationError"},
                "ResponseMetadata": {"HTTPStatusCode": 400},
            },
            "GetCallerIdentity",
        )
        assert isinstance(utils.translate_error(exc), InvalidArgumentException)

    def test_throttling(self, utils):
        exc = ClientError(
            {
                "Error": {"Code": "Throttling"},
                "ResponseMetadata": {"HTTPStatusCode": 400},
            },
            "GetCallerIdentity",
        )
        assert isinstance(utils.translate_error(exc), ResourceExhaustedException)

    def test_botocore_error(self, utils):
        assert isinstance(
            utils.translate_error(BotoCoreError()), InvalidArgumentException
        )

    def test_unknown_exception(self, utils):
        assert isinstance(utils.translate_error(RuntimeError("x")), UnknownException)


class TestAwsStsUtilitiesLifecycle:

    def test_close_does_not_disable_signer(self, utils):
        # close() is a documented no-op: it tears down no state, so the signer
        # must still produce a valid signed request afterwards.
        utils.close()
        resp = utils.create_signed_auth_request()
        assert resp.credentials.access_key_id == _AK
