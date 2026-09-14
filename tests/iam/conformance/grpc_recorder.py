"""gRPC record/replay for the GCP IAM Admin client (test-only).

The GCP IAM Admin service (``google.cloud.iam_admin_v1.IAMClient``) is
gRPC-only — it ships no REST transport — so VCR.py (which records HTTP)
captures nothing for its service-account operations. This module provides
the missing piece: a ``grpc.UnaryUnaryClientInterceptor`` that records every
unary call's request/response proto to a JSON fixture in record mode and
replays it from that fixture in replay mode, short-circuiting the network.

It uses a client interceptor that, in record mode, forwards-and-saves and,
in replay mode, matches-and-returns, expressed with the seams the installed
``grpcio`` and GAPIC stack already expose:

  - ``grpc.intercept_channel(real_channel, interceptor)`` in record mode.
  - ``IAMClient(transport=IAMGrpcTransport(channel=intercepted_dummy))`` in
    replay mode, where the interceptor returns a canned call before the dummy
    channel is ever dialed.

Only the unary-unary IAM Admin methods the provider actually invokes are
handled (create / get / delete service account, plus the SA-level
get/set IAM policy used by trust configuration) — this is a focused
record/replay shim, not a general gRPC-over-VCR framework.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import grpc
import proto
from google.cloud.iam_admin_v1.types import iam as _iam_types
from google.iam.v1 import policy_pb2
from google.protobuf import empty_pb2, json_format
from google.protobuf.message import Message as _PbMessage

# Provider-invoked IAM Admin unary methods → their response message type.
# The interceptor sees the request proto pre-serialization but not the stub's
# response deserializer, so the response type is recovered from this map keyed
# by the gRPC method path. proto-plus types round-trip via ``to_json`` /
# ``from_json``; raw protobuf types via ``google.protobuf.json_format``.
_RESPONSE_TYPES: Dict[str, type] = {
    "/google.iam.admin.v1.IAM/CreateServiceAccount": _iam_types.ServiceAccount,
    "/google.iam.admin.v1.IAM/GetServiceAccount": _iam_types.ServiceAccount,
    "/google.iam.admin.v1.IAM/DeleteServiceAccount": empty_pb2.Empty,
    "/google.iam.admin.v1.IAM/GetIamPolicy": policy_pb2.Policy,
    "/google.iam.admin.v1.IAM/SetIamPolicy": policy_pb2.Policy,
}


def _message_to_json(message: Any) -> str:
    """Serialize a proto-plus or raw-protobuf message to canonical JSON."""
    if isinstance(message, proto.Message):
        return type(message).to_json(message, sort_keys=True)
    if isinstance(message, _PbMessage):
        return json_format.MessageToJson(message, sort_keys=True)
    raise TypeError(f"Cannot serialize non-proto message of type {type(message)!r}")


def _json_to_message(payload: str, message_type: type) -> Any:
    """Deserialize JSON into an instance of ``message_type``."""
    if issubclass(message_type, proto.Message):
        return message_type.from_json(payload)
    return json_format.Parse(payload, message_type())


def _method_short_name(method: str) -> str:
    """``/google.iam.admin.v1.IAM/CreateServiceAccount`` -> ``CreateServiceAccount``."""
    return method.rsplit("/", 1)[-1]


class _CannedRpcError(grpc.RpcError, grpc.Call, grpc.Future):
    """A network-free gRPC error carrying a recorded status code and details.

    Mirrors grpc's own ``_InactiveRpcError``, which is simultaneously a
    ``grpc.RpcError``, ``grpc.Call``, and ``grpc.Future``. The triple identity
    matters: grpc's interceptor plumbing (``grpc._interceptor``) treats a
    failed intercepted call as its own future and calls ``result()`` on it —
    which must re-raise the error. Being a ``grpc.Call`` lets GAPIC's error
    wrapper (``google.api_core.grpc_helpers``) read ``code()``/``details()`` and
    translate it to the matching ``google.api_core.exceptions`` subtype
    (e.g. ``NOT_FOUND`` → ``NotFound``), exactly as a real failed call would.
    """

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        self._code = code
        self._details = details

    # grpc.Call -----------------------------------------------------------
    def initial_metadata(self) -> Tuple[Any, ...]:
        return ()

    def trailing_metadata(self) -> Tuple[Any, ...]:
        return ()

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details

    def is_active(self) -> bool:
        return False

    def time_remaining(self) -> Optional[float]:
        return None

    def cancel(self) -> bool:
        return False

    def add_callback(self, callback: Callable[[], None]) -> bool:
        return False

    # grpc.Future ---------------------------------------------------------
    def cancelled(self) -> bool:
        return False

    def running(self) -> bool:
        return False

    def done(self) -> bool:
        return True

    def result(self, timeout: Optional[float] = None) -> Any:
        # grpc's interceptor path calls result() on the error-as-future;
        # a real failed call re-raises itself here, so we do the same.
        raise self

    def exception(self, timeout: Optional[float] = None) -> "BaseException":
        return self

    def traceback(self, timeout: Optional[float] = None) -> Any:
        return None

    def add_done_callback(self, fn: Callable[["_CannedRpcError"], None]) -> None:
        fn(self)


class _CannedUnaryUnaryCall(grpc.Call, grpc.Future):
    """A completed, network-free gRPC call returning a pre-recorded outcome.

    Implements the slice of the ``grpc.Call`` / ``grpc.Future`` surface that
    the blocking interceptor path and GAPIC's logging interceptor consume:
    ``result()`` returns the response proto (or, for a recorded error outcome,
    raises a :class:`_CannedRpcError`); the call otherwise reports done with
    empty metadata. The injected channel is never dialed because the
    interceptor returns this object before invoking ``continuation``.
    """

    def __init__(
        self,
        response: Any = None,
        error: Optional[Tuple[grpc.StatusCode, str]] = None,
    ) -> None:
        self._response = response
        self._error = error

    # grpc.Call -----------------------------------------------------------
    def initial_metadata(self) -> Tuple[Any, ...]:
        return ()

    def trailing_metadata(self) -> Tuple[Any, ...]:
        return ()

    def code(self) -> grpc.StatusCode:
        return self._error[0] if self._error else grpc.StatusCode.OK

    def details(self) -> str:
        return self._error[1] if self._error else ""

    def is_active(self) -> bool:
        return False

    def time_remaining(self) -> Optional[float]:
        return None

    def cancel(self) -> bool:
        return False

    def add_callback(self, callback: Callable[[], None]) -> bool:
        return False

    # grpc.Future ---------------------------------------------------------
    def cancelled(self) -> bool:
        return False

    def running(self) -> bool:
        return False

    def done(self) -> bool:
        return True

    def result(self, timeout: Optional[float] = None) -> Any:
        if self._error is not None:
            raise _CannedRpcError(self._error[0], self._error[1])
        return self._response

    def exception(self, timeout: Optional[float] = None) -> Optional[BaseException]:
        if self._error is not None:
            return _CannedRpcError(self._error[0], self._error[1])
        return None

    def traceback(self, timeout: Optional[float] = None) -> Any:
        return None

    def add_done_callback(self, fn: Callable[["_CannedUnaryUnaryCall"], None]) -> None:
        fn(self)


class GrpcFixtureInterceptor(grpc.UnaryUnaryClientInterceptor):
    """Record or replay unary IAM Admin gRPC calls via JSON fixtures.

    In record mode the interceptor forwards the call to the real channel,
    captures the request/response protos as JSON, and appends a fixture file.
    In replay mode it matches the outgoing request against the recorded
    fixtures and returns a :class:`_CannedUnaryUnaryCall`, so no network call
    occurs.

    Fixtures are scoped to the running test (set via :meth:`set_test`) so each
    test's calls record and replay independently. Within a test, fixtures are
    ordered per method; replay matches on the canonical request JSON first and
    falls back to call ordinal for requests with volatile fields.
    """

    def __init__(self, mode: str, fixtures_dir: Path) -> None:
        if mode not in ("record", "replay"):
            raise ValueError(f"mode must be 'record' or 'replay', got {mode!r}")
        self._mode = mode
        self._fixtures_dir = Path(fixtures_dir)
        self._test_id: Optional[str] = None
        # Per-(test, method) ordinal counters, reset for each test.
        self._write_ordinals: Dict[str, int] = {}
        self._read_ordinals: Dict[str, int] = {}

    @property
    def mode(self) -> str:
        return self._mode

    def set_test(self, test_id: str) -> None:
        """Scope subsequent fixtures to ``test_id`` and reset call ordinals.

        In record mode, purge this test's existing fixtures first so a
        re-recording that produces a different call sequence (e.g. a call that
        now errors where it previously returned a body) cannot leave stale
        fixtures behind for replay to match against.
        """
        self._test_id = test_id
        self._write_ordinals = {}
        self._read_ordinals = {}
        if self._mode == "record":
            self._purge_test_fixtures(test_id)

    def _purge_test_fixtures(self, test_id: str) -> None:
        """Delete all recorded fixtures scoped to ``test_id``."""
        if not self._fixtures_dir.exists():
            return
        for path in self._fixtures_dir.glob(f"{test_id}.*.json"):
            path.unlink()

    # intercept
    def intercept_unary_unary(
        self,
        continuation: Callable[[Any, Any], Any],
        client_call_details: Any,
        request: Any,
    ) -> Any:
        method = client_call_details.method
        if isinstance(method, bytes):
            method = method.decode("utf-8")

        if self._mode == "record":
            response_future = continuation(client_call_details, request)
            try:
                result = response_future.result()
            except grpc.RpcError as exc:
                # A failed call (e.g. a get-after-delete returning NOT_FOUND)
                # is a legitimate recorded outcome for negative-path tests.
                # Persist the status code + details so replay can reproduce it,
                # then re-raise so record-mode behavior is unchanged.
                self._save_error(method, request, exc)
                raise
            self._save(method, request, result)
            return response_future

        return self._load_and_match(method, request)

    # fixture I/O
    def _fixture_path(self, method: str, ordinal: int) -> Path:
        if self._test_id is None:
            raise RuntimeError(
                "GrpcFixtureInterceptor.set_test() must be called before any "
                "intercepted call (no test scope is active)."
            )
        short = _method_short_name(method)
        name = f"{self._test_id}.{short}.{ordinal}.json"
        return self._fixtures_dir / name

    def _save(self, method: str, request: Any, response: Any) -> None:
        # Fixtures store the raw request/response proto as JSON, unscrubbed by
        # design: the only identifiers they carry (service-account emails /
        # uniqueIds, project ids) are non-secret test-environment values that
        # replay matches requests against, so scrubbing them would break replay.
        # No credentials or tokens flow through these unary IAM Admin calls.
        self._write_fixture(
            method,
            request,
            {"response": json.loads(_message_to_json(response))},
        )

    def _save_error(self, method: str, request: Any, exc: grpc.RpcError) -> None:
        # A recorded failed call: persist the status code name + details so
        # replay can reconstruct an equivalent RpcError. ``code()``/``details()``
        # live on grpc.Call — a real failed call from the plumbing is an
        # ``_InactiveRpcError``, which subclasses both grpc.RpcError and
        # grpc.Call. Call them unconditionally: a genuinely malformed error
        # that lacks them should blow up loudly at record time (a human is
        # watching) rather than silently persist an UNKNOWN status that would
        # then replay forever.
        code = exc.code()
        details = exc.details()
        self._write_fixture(
            method,
            request,
            {"error": {"code": code.name, "message": details}},
        )

    def _write_fixture(self, method: str, request: Any, outcome: dict) -> None:
        ordinal = self._write_ordinals.get(method, 0)
        self._write_ordinals[method] = ordinal + 1
        path = self._fixture_path(method, ordinal)
        path.parent.mkdir(parents=True, exist_ok=True)
        fixture = {
            "method": method,
            "request": json.loads(_message_to_json(request)),
            **outcome,
        }
        path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")

    def _load_and_match(self, method: str, request: Any) -> _CannedUnaryUnaryCall:
        response_type = _RESPONSE_TYPES.get(method)
        if response_type is None:
            raise KeyError(
                f"No recorded response type registered for gRPC method {method!r}. "
                "Add it to grpc_recorder._RESPONSE_TYPES."
            )

        request_json = json.loads(_message_to_json(request))
        candidates = self._candidate_fixtures(method)
        if not candidates:
            raise FileNotFoundError(
                f"No gRPC fixtures for method {method!r} in test "
                f"{self._test_id!r} under {self._fixtures_dir}. Record with "
                "--record-mode=rewrite first."
            )

        # Advance the per-method call ordinal unconditionally, before matching,
        # so the ordinal fallback stays in lockstep with the actual call
        # sequence even when an earlier call in the same method matched exactly.
        # (If the counter only advanced on the fallback path, a mix of
        # exact-match and fallback calls to one method would replay the wrong
        # fixture for the fallback calls.)
        ordinal = self._read_ordinals.get(method, 0)
        self._read_ordinals[method] = ordinal + 1

        # Prefer the fixture at this call's ordinal when its request matches.
        # This is what disambiguates repeated identical requests that have
        # different outcomes in sequence (e.g. a get returning the identity,
        # then the same get returning NOT_FOUND after a delete): exact-match
        # alone would return the first fixture for both calls.
        if ordinal < len(candidates):
            fixture = json.loads(candidates[ordinal].read_text())
            if fixture.get("request") == request_json:
                return self._fixture_to_call(fixture, response_type)

        # Otherwise fall back to the first fixture whose request matches
        # exactly (handles calls arriving in a different order than recorded).
        for path in candidates:
            fixture = json.loads(path.read_text())
            if fixture.get("request") == request_json:
                return self._fixture_to_call(fixture, response_type)

        # Last resort: the fixture at this ordinal, for requests carrying
        # volatile fields that never match exactly.
        if ordinal < len(candidates):
            fixture = json.loads(candidates[ordinal].read_text())
            return self._fixture_to_call(fixture, response_type)

        # Deliberate hard fail-fast: candidates are re-globbed fresh on every
        # call and never consumed/mutated, so the read ordinal stays in lockstep
        # with the call sequence. Reaching here means neither the ordinal nor an
        # exact request matched, and blowing up loudly is preferred over silently
        # replaying the wrong fixture.
        raise AssertionError(
            f"No matching gRPC fixture for method {method!r} call #{ordinal} "
            f"in test {self._test_id!r}. Recorded {len(candidates)} call(s)."
        )

    @staticmethod
    def _fixture_to_call(fixture: dict, response_type: type) -> _CannedUnaryUnaryCall:
        """Build a canned call from a fixture's recorded response or error."""
        error = fixture.get("error")
        if error is not None:
            code = grpc.StatusCode[error["code"]]
            return _CannedUnaryUnaryCall(error=(code, error.get("message", "")))
        response = _json_to_message(json.dumps(fixture["response"]), response_type)
        return _CannedUnaryUnaryCall(response=response)

    def _candidate_fixtures(self, method: str) -> list:
        """Return this test's fixtures for ``method``, ordered by ordinal."""
        if self._test_id is None:
            raise RuntimeError("No test scope is active.")
        short = _method_short_name(method)
        prefix = f"{self._test_id}.{short}."
        matches = [
            p
            for p in self._fixtures_dir.glob(f"{prefix}*.json")
            if p.name.removeprefix(prefix).removesuffix(".json").isdigit()
        ]
        return sorted(
            matches,
            key=lambda p: int(p.name.removeprefix(prefix).removesuffix(".json")),
        )
