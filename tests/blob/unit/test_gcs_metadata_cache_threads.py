"""Guard against google-cloud-storage starting background threads in tests.

Its bucket-metadata cache starts daemon threads that issue real HTTP on no
schedule. Under VCR those land in whichever cassette is open, which corrupts
recordings and exhausts interactions other tests need.

The session fixture in ``tests/conftest.py`` neutralizes them. These tests cover
both halves of that: the vendor really does start threads, and the fixture
really does stop it. The first half uses ``real_bucket_metadata_cache`` to put
the genuine methods back -- asserting against the stub would only prove the stub
is a stub.

``Thread.start`` is patched to record instead of run, so nothing here can issue
a request.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def thread_starts(monkeypatch):
    """Record every thread that would start, without starting it."""
    started = []
    monkeypatch.setattr(
        threading.Thread, "start", lambda self: started.append(self), raising=True
    )
    return started


def _target_names(threads):
    return sorted(getattr(t, "_target", None).__name__ for t in threads)


class TestVendorStartsThreads:
    """The hazard is real. These fail if the vendor stops doing it, or renames."""

    def test_cache_miss_starts_a_fetch_thread(
        self, real_bucket_metadata_cache, thread_starts
    ):
        cache = real_bucket_metadata_cache(MagicMock())

        cache.get_or_queue_fetch("some-bucket")

        assert _target_names(thread_starts) == ["_fetch_background"]

    def test_eviction_check_starts_a_verify_thread(
        self, real_bucket_metadata_cache, thread_starts
    ):
        """The path the vendor env var never gated."""
        cache = real_bucket_metadata_cache(MagicMock())
        cache.update_cache("some-bucket", "projects/_/buckets/some-bucket", "US")

        cache.check_and_evict("some-bucket")

        assert _target_names(thread_starts) == ["_verify_existence_background"]


class TestFixtureNeutralizesThem:
    """The session fixture stops both. These fail if it stops being applied."""

    def test_cache_miss_starts_nothing(self, thread_starts):
        from google.cloud.storage._bucket_metadata_cache import BucketMetadataCache

        cache = BucketMetadataCache(MagicMock())

        assert cache.get_or_queue_fetch("some-bucket") is None
        assert thread_starts == []

    def test_eviction_check_starts_nothing(self, thread_starts):
        from google.cloud.storage._bucket_metadata_cache import BucketMetadataCache

        cache = BucketMetadataCache(MagicMock())
        cache.update_cache("some-bucket", "projects/_/buckets/some-bucket", "US")

        cache.check_and_evict("some-bucket")

        assert thread_starts == []
