"""Unit tests for the gRPC record/replay interceptor.

These verify the record→file→replay round trip for the IAM Admin unary
methods using synthetic proto messages — no GCP credentials or network. They
guard the interceptor seam the GCP conformance suite depends on for its
service-account operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import grpc
import pytest
from google.cloud.iam_admin_v1.types import iam as iam_types
from google.iam.v1 import policy_pb2
from google.protobuf import empty_pb2

from tests.iam.conformance.grpc_recorder import (
    GrpcFixtureInterceptor,
    _CannedRpcError,
    _CannedUnaryUnaryCall,
)


class _FakeCallDetails:
    """Minimal client_call_details carrying just the method path."""

    def __init__(self, method: str) -> None:
        self.method = method


def _record_then_replay(
    tmp_path: Path,
    test_id: str,
    method: str,
    request: Any,
    response: Any,
) -> Any:
    """Record (request, response) under a fake continuation, then replay it.

    Returns the proto the replay interceptor reconstructs from the fixture.
    """
    details = _FakeCallDetails(method)

    recorder = GrpcFixtureInterceptor("record", tmp_path)
    recorder.set_test(test_id)

    def continuation(_details: Any, _request: Any) -> Any:
        return _CannedUnaryUnaryCall(response)

    recorded_call = recorder.intercept_unary_unary(continuation, details, request)
    # The recorder forwards the real call's future through unchanged.
    assert recorded_call.result() is response

    replayer = GrpcFixtureInterceptor("replay", tmp_path)
    replayer.set_test(test_id)

    def fail_continuation(_details: Any, _request: Any) -> Any:
        raise AssertionError("replay mode must not invoke the real channel")

    replay_call = replayer.intercept_unary_unary(fail_continuation, details, request)
    return replay_call.result()


def test_service_account_round_trip(tmp_path: Path) -> None:
    """A proto-plus ServiceAccount response survives record→replay intact."""
    request = iam_types.CreateServiceAccountRequest(name="projects/p", account_id="sa")
    response = iam_types.ServiceAccount(
        name="projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com",
        email="sa@p.iam.gserviceaccount.com",
        unique_id="123456789",
    )

    replayed = _record_then_replay(
        tmp_path,
        "TestX.test_create",
        "/google.iam.admin.v1.IAM/CreateServiceAccount",
        request,
        response,
    )

    assert replayed == response
    assert replayed.email == "sa@p.iam.gserviceaccount.com"


def test_empty_response_round_trip(tmp_path: Path) -> None:
    """A raw-protobuf Empty (delete) response survives record→replay."""
    request = iam_types.DeleteServiceAccountRequest(
        name="projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com"
    )
    response = empty_pb2.Empty()

    replayed = _record_then_replay(
        tmp_path,
        "TestX.test_delete",
        "/google.iam.admin.v1.IAM/DeleteServiceAccount",
        request,
        response,
    )

    assert isinstance(replayed, empty_pb2.Empty)
    assert replayed == response


def test_policy_round_trip(tmp_path: Path) -> None:
    """A raw-protobuf Policy (set_iam_policy) response survives round-trip."""
    request = policy_pb2.Policy(version=1, etag=b"abc")
    response = policy_pb2.Policy(version=3, etag=b"xyz")

    replayed = _record_then_replay(
        tmp_path,
        "TestX.test_trust",
        "/google.iam.admin.v1.IAM/SetIamPolicy",
        request,
        response,
    )

    assert isinstance(replayed, policy_pb2.Policy)
    assert replayed == response
    assert replayed.version == 3


def test_fixture_file_shape(tmp_path: Path) -> None:
    """Recording writes a {method, request, response} JSON fixture on disk."""
    import json

    request = iam_types.GetServiceAccountRequest(
        name="projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com"
    )
    response = iam_types.ServiceAccount(
        name=request.name, email="sa@p.iam.gserviceaccount.com", unique_id="9"
    )

    recorder = GrpcFixtureInterceptor("record", tmp_path)
    recorder.set_test("TestX.test_get")
    details = _FakeCallDetails("/google.iam.admin.v1.IAM/GetServiceAccount")
    recorder.intercept_unary_unary(
        lambda d, r: _CannedUnaryUnaryCall(response), details, request
    )

    fixture_path = tmp_path / "TestX.test_get.GetServiceAccount.0.json"
    assert fixture_path.exists()
    fixture = json.loads(fixture_path.read_text())
    assert fixture["method"] == "/google.iam.admin.v1.IAM/GetServiceAccount"
    assert fixture["request"]["name"] == request.name
    assert fixture["response"]["email"] == "sa@p.iam.gserviceaccount.com"


def test_replay_orders_multiple_calls_by_ordinal(tmp_path: Path) -> None:
    """Two same-method calls with distinct requests replay in record order."""
    details = _FakeCallDetails("/google.iam.admin.v1.IAM/GetServiceAccount")

    recorder = GrpcFixtureInterceptor("record", tmp_path)
    recorder.set_test("TestX.test_two")
    for i in (1, 2):
        req = iam_types.GetServiceAccountRequest(
            name=f"projects/p/serviceAccounts/sa{i}"
        )
        resp = iam_types.ServiceAccount(name=req.name, unique_id=str(i))
        recorder.intercept_unary_unary(
            lambda d, r, _resp=resp: _CannedUnaryUnaryCall(_resp), details, req
        )

    replayer = GrpcFixtureInterceptor("replay", tmp_path)
    replayer.set_test("TestX.test_two")
    # Exact request match returns the right fixture regardless of order.
    req2 = iam_types.GetServiceAccountRequest(name="projects/p/serviceAccounts/sa2")
    call = replayer.intercept_unary_unary(
        lambda d, r: pytest.fail("must not dial"), details, req2
    )
    assert call.result().unique_id == "2"


def test_replay_missing_fixture_raises(tmp_path: Path) -> None:
    """Replay with no recorded fixture surfaces a clear error."""
    replayer = GrpcFixtureInterceptor("replay", tmp_path)
    replayer.set_test("TestX.test_absent")
    details = _FakeCallDetails("/google.iam.admin.v1.IAM/GetServiceAccount")
    req = iam_types.GetServiceAccountRequest(name="projects/p/serviceAccounts/x")
    with pytest.raises(FileNotFoundError):
        replayer.intercept_unary_unary(
            lambda d, r: pytest.fail("must not dial"), details, req
        )


def test_invalid_mode_rejected(tmp_path: Path) -> None:
    """Constructing with an unknown mode fails fast."""
    with pytest.raises(ValueError):
        GrpcFixtureInterceptor("bogus", tmp_path)


def test_canned_call_reports_ok_and_done() -> None:
    """The canned call presents a completed, OK gRPC call surface."""
    resp = empty_pb2.Empty()
    call = _CannedUnaryUnaryCall(resp)
    assert call.code() == grpc.StatusCode.OK
    assert call.done() is True
    assert call.result() is resp
    assert call.exception() is None


def test_error_round_trip(tmp_path: Path) -> None:
    """A failed call (NOT_FOUND) records an error fixture and replays as an error."""
    details = _FakeCallDetails("/google.iam.admin.v1.IAM/GetServiceAccount")
    request = iam_types.GetServiceAccountRequest(
        name="projects/p/serviceAccounts/gone@p.iam.gserviceaccount.com"
    )

    recorder = GrpcFixtureInterceptor("record", tmp_path)
    recorder.set_test("TestX.test_err")

    def failing_continuation(_d: Any, _r: Any) -> Any:
        return _CannedUnaryUnaryCall(
            error=(grpc.StatusCode.NOT_FOUND, "service account does not exist")
        )

    # Record mode re-raises the RpcError after saving the error fixture.
    with pytest.raises(grpc.RpcError):
        recorder.intercept_unary_unary(failing_continuation, details, request)

    import json

    fixture_path = tmp_path / "TestX.test_err.GetServiceAccount.0.json"
    fixture = json.loads(fixture_path.read_text())
    assert fixture["error"]["code"] == "NOT_FOUND"
    assert "response" not in fixture

    # Replay reconstructs an equivalent RpcError carrying the same code.
    replayer = GrpcFixtureInterceptor("replay", tmp_path)
    replayer.set_test("TestX.test_err")
    replay_call = replayer.intercept_unary_unary(
        lambda d, r: pytest.fail("must not dial"), details, request
    )
    with pytest.raises(grpc.RpcError) as excinfo:
        replay_call.result()
    assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND


def test_identical_requests_replay_distinct_outcomes_in_order(tmp_path: Path) -> None:
    """Two identical requests with different outcomes replay by call order.

    Mirrors the lifecycle case: the same get_service_account first returns the
    identity, then (after a delete) returns NOT_FOUND. Exact-request matching
    alone would return the first fixture for both; ordinal disambiguation must
    return the 200 body first and the error second.
    """
    details = _FakeCallDetails("/google.iam.admin.v1.IAM/GetServiceAccount")
    request = iam_types.GetServiceAccountRequest(
        name="projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com"
    )
    response = iam_types.ServiceAccount(name=request.name, email="sa@p")

    recorder = GrpcFixtureInterceptor("record", tmp_path)
    recorder.set_test("TestX.test_seq")
    # Call 1: returns the identity.
    recorder.intercept_unary_unary(
        lambda d, r: _CannedUnaryUnaryCall(response), details, request
    )
    # Call 2: same request, now NOT_FOUND.
    with pytest.raises(grpc.RpcError):
        recorder.intercept_unary_unary(
            lambda d, r: _CannedUnaryUnaryCall(
                error=(grpc.StatusCode.NOT_FOUND, "gone")
            ),
            details,
            request,
        )

    replayer = GrpcFixtureInterceptor("replay", tmp_path)
    replayer.set_test("TestX.test_seq")
    first = replayer.intercept_unary_unary(
        lambda d, r: pytest.fail("must not dial"), details, request
    )
    assert first.result().email == "sa@p"
    second = replayer.intercept_unary_unary(
        lambda d, r: pytest.fail("must not dial"), details, request
    )
    with pytest.raises(grpc.RpcError):
        second.result()


def test_canned_rpc_error_is_a_future_that_reraises() -> None:
    """A canned error must satisfy grpc's Future contract by re-raising itself.

    grpc's interceptor plumbing (``grpc._interceptor._with_call``) treats a
    failed intercepted call as its own future and calls ``result()`` on it. A
    real ``_InactiveRpcError`` is a ``grpc.Future`` whose ``result()`` re-raises
    the error; our canned error must do the same, or the interceptor path fails
    with ``AttributeError: object has no attribute 'result'`` (regression guard:
    this only surfaces through the real grpc stack, not a direct call).
    """
    err = _CannedRpcError(grpc.StatusCode.NOT_FOUND, "gone")
    assert isinstance(err, grpc.RpcError)
    assert isinstance(err, grpc.Call)
    assert isinstance(err, grpc.Future)
    assert err.code() == grpc.StatusCode.NOT_FOUND
    assert err.done() is True
    with pytest.raises(_CannedRpcError):
        err.result()
    assert err.exception() is err


def test_rewrite_purges_stale_fixtures(tmp_path: Path) -> None:
    """Entering record mode for a test deletes that test's leftover fixtures."""
    stale = tmp_path / "TestX.test_purge.GetServiceAccount.5.json"
    tmp_path.mkdir(exist_ok=True)
    stale.write_text("{}\n")
    other = tmp_path / "TestY.test_keep.GetServiceAccount.0.json"
    other.write_text("{}\n")

    recorder = GrpcFixtureInterceptor("record", tmp_path)
    recorder.set_test("TestX.test_purge")

    assert not stale.exists(), "stale fixture for this test should be purged"
    assert other.exists(), "another test's fixtures must be left intact"
