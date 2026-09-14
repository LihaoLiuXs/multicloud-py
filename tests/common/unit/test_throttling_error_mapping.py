"""Throttling and service-unavailable error mapping across providers.

One place to assert that every provider classifies rate limiting the same way,
so a change to one provider cannot quietly drift from the others.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from multicloud.common.exceptions import (
    DeadlineExceededException,
    InvalidArgumentException,
    ResourceExhaustedException,
    ResourceNotFoundException,
    UnAuthorizedException,
    UnknownException,
)


def _client_error(code: str, status: int):
    """Build a botocore ClientError.

    Imported here rather than at module scope so a install without the AWS
    extra can still run the GCP cases in this file.
    """
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "SomeOperation",
    )


@pytest.fixture(
    params=["blob_sync", "blob_async", "sts", "iam"],
)
def aws_store(request):
    """Every AWS-backed store that translates errors."""
    pytest.importorskip("botocore", reason="AWS extra not installed")
    if request.param == "blob_sync":
        from multicloud.blob.blob_aws.aws_blob_store import AwsBlobStore

        return AwsBlobStore(bucket="test-bucket", region="us-west-2")
    if request.param == "blob_async":
        from multicloud.blob.blob_aws.async_blob.aws_async_blob_store import (
            AwsAsyncBlobStore,
        )

        return AwsAsyncBlobStore(bucket="test-bucket", region="us-west-2")
    if request.param == "sts":
        from multicloud.sts.sts_aws.aws_sts import AwsSts

        return AwsSts(region="us-west-2")
    from multicloud.iam.iam_aws.aws_iam import AwsIam

    return AwsIam(region="us-west-2")


class TestThrottlingCodeSet:
    """The shared set must keep up with what AWS itself calls throttling."""

    def test_covers_every_code_botocore_retries_as_throttling(self):
        """Catches a code being added upstream that we would otherwise miss."""
        from botocore.retries.standard import ThrottledRetryableChecker
        from multicloud.aws_common.error_mapping import AWS_THROTTLING_CODES

        missing = set(ThrottledRetryableChecker._THROTTLED_ERROR_CODES) - set(
            AWS_THROTTLING_CODES
        )
        assert not missing, f"unclassified throttling codes: {sorted(missing)}"

    def test_the_query_protocol_code_is_present(self):
        """STS and IAM send bare 'Throttling', not the Exception-suffixed form."""
        from multicloud.aws_common.error_mapping import AWS_THROTTLING_CODES

        assert "Throttling" in AWS_THROTTLING_CODES


class TestAwsThrottlingCodes:
    """The codes AWS returns when it is throttling map to one exception."""

    @pytest.mark.parametrize(
        "code",
        [
            "ThrottlingException",
            "Throttling",
            "ServiceUnavailable",
            "RequestExpired",
            "RequestLimitExceeded",
            "TooManyRequestsException",
            "SlowDown",
        ],
    )
    def test_throttling_code_is_resource_exhausted(self, aws_store, code):
        result = aws_store.translate_error(_client_error(code, 503))
        assert isinstance(result, ResourceExhaustedException)

    def test_throttling_wins_over_a_400_status(self, aws_store):
        """AWS reports throttling with HTTP 400 on some services.

        The status alone would read as an invalid argument, which is why the
        error code is checked first.
        """
        for code in ("ThrottlingException", "Throttling"):
            result = aws_store.translate_error(_client_error(code, 400))
            assert isinstance(result, ResourceExhaustedException), code

    def test_status_429_is_resource_exhausted_without_a_known_code(self, aws_store):
        result = aws_store.translate_error(_client_error("SomethingNew", 429))
        assert isinstance(result, ResourceExhaustedException)

    def test_unrelated_codes_are_untouched(self, aws_store):
        """Guards the reordering: earlier mappings must still win their cases."""
        assert isinstance(
            aws_store.translate_error(_client_error("AccessDenied", 403)),
            UnAuthorizedException,
        )
        assert isinstance(
            aws_store.translate_error(_client_error("InvalidArgument", 400)),
            InvalidArgumentException,
        )


class TestAwsIamQuotaCodes:
    """IAM reports quota exhaustion with its own codes."""

    @pytest.fixture
    def iam(self):
        from multicloud.iam.iam_aws.aws_iam import AwsIam

        return AwsIam(region="us-west-2")

    @pytest.mark.parametrize("code", ["LimitExceeded", "LimitExceededException"])
    def test_limit_exceeded_is_resource_exhausted(self, iam, code):
        result = iam.translate_error(_client_error(code, 409))
        assert isinstance(result, ResourceExhaustedException)

    def test_other_services_do_not_claim_iam_codes(self):
        from multicloud.blob.blob_aws.aws_blob_store import AwsBlobStore

        store = AwsBlobStore(bucket="test-bucket", region="us-west-2")
        result = store.translate_error(_client_error("LimitExceeded", 409))
        assert isinstance(result, UnknownException)


class TestGcpSyncStatusMapping:
    """GCS status codes map onto the common exception hierarchy."""

    @pytest.fixture
    def store(self):
        pytest.importorskip("google.cloud.storage")
        from multicloud.blob.blob_gcp.gcp_blob_store import GcpBlobStore

        # Patched so the suite needs no credentials: constructing the real
        # client would fall through to google.auth.default().
        with (
            patch("google.auth.default") as mock_auth,
            patch("multicloud.blob.blob_gcp.gcp_blob_store.storage.Client"),
        ):
            mock_auth.return_value = (MagicMock(), "test-project")
            yield GcpBlobStore(bucket="test-bucket", region="us-west1")

    def test_too_many_requests_is_resource_exhausted(self, store):
        from google.api_core import exceptions

        result = store.translate_error(exceptions.TooManyRequests("rate limited"))
        assert isinstance(result, ResourceExhaustedException)

    def test_resource_exhausted_is_resource_exhausted(self, store):
        from google.api_core import exceptions

        result = store.translate_error(exceptions.ResourceExhausted("quota"))
        assert isinstance(result, ResourceExhaustedException)

    def test_service_unavailable_is_unknown(self, store):
        """503 is not the caller exceeding a quota, so it is not exhaustion."""
        from google.api_core import exceptions

        result = store.translate_error(exceptions.ServiceUnavailable("backend down"))
        assert isinstance(result, UnknownException)
        assert not isinstance(result, ResourceExhaustedException)

    def test_gateway_timeout_is_deadline_exceeded(self, store):
        from google.api_core import exceptions

        result = store.translate_error(exceptions.GatewayTimeout("too slow"))
        assert isinstance(result, DeadlineExceededException)

    def test_internal_error_is_unknown(self, store):
        from google.api_core import exceptions

        result = store.translate_error(exceptions.InternalServerError("boom"))
        assert isinstance(result, UnknownException)


class TestGcpIamStatusMapping:
    """IAM shares the GCS status mapping, so it must classify identically."""

    @pytest.fixture
    def iam(self):
        pytest.importorskip("google.cloud.iam_admin_v1")
        from multicloud.iam.iam_gcp.gcp_iam import GcpIam

        # Injected so the suite needs no credentials.
        return GcpIam(
            region="us-west1", iam_client=MagicMock(), projects_client=MagicMock()
        )

    @pytest.mark.parametrize(
        "exc_name,expected",
        [
            ("TooManyRequests", ResourceExhaustedException),
            ("ResourceExhausted", ResourceExhaustedException),
            ("ServiceUnavailable", UnknownException),
            ("GatewayTimeout", DeadlineExceededException),
        ],
    )
    def test_status_maps_to_expected_exception(self, iam, exc_name, expected):
        from google.api_core import exceptions

        result = iam.translate_error(getattr(exceptions, exc_name)("boom"))
        assert isinstance(result, expected)


class TestGcpAsyncStatusMapping:
    """The async provider sees raw HTTP statuses and must agree with sync."""

    @pytest.fixture
    def store(self):
        pytest.importorskip("gcloud.aio.storage")
        from multicloud.blob.blob_gcp.async_blob.gcp_async_blob_store import (
            GcpAsyncBlobStore,
        )

        # translate_error is pure; no client is created until first use.
        return GcpAsyncBlobStore(bucket="test-bucket", region="us-west1")

    @staticmethod
    def _response_error(status: int):
        import aiohttp

        request_info = aiohttp.RequestInfo(
            url="https://storage.googleapis.com/x",
            method="GET",
            headers=None,
            real_url="https://storage.googleapis.com/x",
        )
        return aiohttp.ClientResponseError(
            request_info, (), status=status, message=f"HTTP {status}"
        )

    @pytest.mark.parametrize(
        "status,expected",
        [
            (404, ResourceNotFoundException),
            (401, UnAuthorizedException),
            (403, UnAuthorizedException),
            (400, InvalidArgumentException),
            (429, ResourceExhaustedException),
            (500, UnknownException),
            (503, UnknownException),
            (504, DeadlineExceededException),
        ],
    )
    def test_status_maps_to_expected_exception(self, store, status, expected):
        result = store.translate_error(self._response_error(status))
        assert isinstance(result, expected)

    def test_503_is_not_reported_as_exhaustion(self, store):
        result = store.translate_error(self._response_error(503))
        assert not isinstance(result, ResourceExhaustedException)
