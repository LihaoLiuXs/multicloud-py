"""Build record/replay GCP IAM clients for the conformance harness.

The GCP IAM provider talks to two Google clients with two transports:

  - ``IAMClient`` (``iam_admin_v1``) — gRPC-only; its service-account calls
    are recorded/replayed through :class:`GrpcFixtureInterceptor` (see
    ``grpc_recorder.py``).
  - ``ProjectsClient`` (``resourcemanager_v3``) — forced to ``transport="rest"``
    so its ``get/set_iam_policy`` calls become HTTP and ride the existing VCR
    cassette path.

This module assembles both clients for a given mode and exposes the
interceptor so a per-test fixture can scope its fixtures. In replay mode no
credentials are needed: the gRPC channel is a dummy that is never dialed, and
the REST client uses anonymous credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import grpc
from google.auth.credentials import AnonymousCredentials
from google.cloud.iam_admin_v1 import IAMClient
from google.cloud.iam_admin_v1.services.iam.transports.grpc import IAMGrpcTransport
from google.cloud.resourcemanager_v3 import ProjectsClient

from tests.iam.conformance.grpc_recorder import GrpcFixtureInterceptor

# Fixtures for the gRPC (IAMClient) service-account calls live here; the
# REST (ProjectsClient) policy calls record into the VCR ``cassettes/`` tree.
GRPC_FIXTURES_DIR = Path(__file__).parent / "grpc_fixtures" / "gcp"

_IAM_HOST = "iam.googleapis.com"


@dataclass
class GcpGrpcClients:
    """The pre-built clients the GCP IAM provider receives, plus the recorder."""

    iam_client: IAMClient
    projects_client: ProjectsClient
    interceptor: GrpcFixtureInterceptor


def _mode_for(record_mode: str) -> str:
    """Map pytest's ``--record-mode`` to the interceptor's mode.

    ``none`` → replay; everything else (``rewrite``/``all``/``once``/``new_episodes``)
    → record. Mirrors how VCR treats the same flag for the REST cassettes.
    """
    return "replay" if record_mode == "none" else "record"


def build_clients(record_mode: str) -> GcpGrpcClients:
    """Build the gRPC + REST clients for the given pytest record mode."""
    mode = _mode_for(record_mode)
    interceptor = GrpcFixtureInterceptor(mode, GRPC_FIXTURES_DIR)

    if mode == "record":
        # Real authenticated gRPC channel (ADC), wrapped by the recorder.
        import google.auth

        credentials, _ = google.auth.default(scopes=IAMGrpcTransport.AUTH_SCOPES)
        base_channel = IAMGrpcTransport.create_channel(
            host=_IAM_HOST,
            credentials=credentials,
            scopes=IAMGrpcTransport.AUTH_SCOPES,
        )
        intercepted = grpc.intercept_channel(base_channel, interceptor)
        iam_client = IAMClient(transport=IAMGrpcTransport(channel=intercepted))
        # REST so policy ops are HTTP and VCR-recordable; real ADC creds.
        projects_client = ProjectsClient(transport="rest")
    else:
        # Replay: dummy channel (never dialed — the interceptor returns a
        # canned call first) + anonymous creds so no ADC is required in CI.
        dummy_channel = grpc.insecure_channel("localhost:1")
        intercepted = grpc.intercept_channel(dummy_channel, interceptor)
        iam_client = IAMClient(transport=IAMGrpcTransport(channel=intercepted))
        projects_client = ProjectsClient(
            transport="rest", credentials=AnonymousCredentials()
        )

    return GcpGrpcClients(
        iam_client=iam_client,
        projects_client=projects_client,
        interceptor=interceptor,
    )
