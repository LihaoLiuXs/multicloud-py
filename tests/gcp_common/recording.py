"""Shared record-mode helpers for GCP async conformance suites.

GCP async providers mint OAuth tokens over gcloud-aio/aiohttp. aiohttp builds
its default verified SSL context once, at import time, from
``ssl.create_default_context()`` -- which loads CA certs from OpenSSL's default
verify paths. On hosts where those paths are empty, that import-time singleton
trusts no CAs, so recording (``--record-mode=rewrite``) fails at token
acquisition with ``CERTIFICATE_VERIFY_FAILED`` even when credentials are valid.

Setting ``SSL_CERT_FILE`` at runtime cannot help: the singleton is already
frozen by the time any fixture runs (that only works when set before the
process imports aiohttp). This module swaps that singleton for a certifi-backed
context for the duration of a recording run. Replay never performs a real token
fetch, so it is unaffected.
"""

from __future__ import annotations

import contextlib
import ssl
from typing import Iterator

import certifi
from aiohttp import connector as _aiohttp_connector


@contextlib.contextmanager
def certifi_trust_store() -> Iterator[None]:
    """Back aiohttp's default verified SSL context with the certifi CA bundle.

    aiohttp resolves default TLS connections against the module-level
    ``connector._SSL_CONTEXT_VERIFIED`` singleton, built at import time from the
    host OS trust store. Swap it for a certifi-backed context -- the same trust
    store the sync/urllib3 and AWS paths use -- so GCP async recording does not
    depend on the host OS trust store, restoring the original on exit. Wrap only
    the record-mode path; replay does not open real TLS connections.
    """
    original = _aiohttp_connector._SSL_CONTEXT_VERIFIED
    _aiohttp_connector._SSL_CONTEXT_VERIFIED = ssl.create_default_context(
        cafile=certifi.where()
    )
    try:
        yield
    finally:
        _aiohttp_connector._SSL_CONTEXT_VERIFIED = original
