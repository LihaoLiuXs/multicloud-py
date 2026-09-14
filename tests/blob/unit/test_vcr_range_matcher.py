from types import SimpleNamespace

import pytest

from tests.blob.conformance.conftest import vcr_config
from tests.blob.conformance.vcr_filters import (
    RANGE_HEADER_MATCHER,
    range_header,
    register_range_header_matcher,
)


def _request(headers=None):
    return SimpleNamespace(headers=headers or {})


def test_range_header_accepts_recorded_bytes_and_live_text():
    range_header(
        _request({"range": [b"bytes=2-7"]}),
        _request({"Range": "bytes=2-7"}),
    )


@pytest.mark.parametrize("header", [None, "bytes=3-8"])
def test_range_header_rejects_missing_or_changed_value(header):
    live_headers = {} if header is None else {"Range": header}

    with pytest.raises(AssertionError, match="range headers differ"):
        range_header(_request({"Range": "bytes=2-7"}), _request(live_headers))


def test_vcr_config_scopes_range_matcher_to_stream_range(tmp_path):
    test_class = type("TestGcpAsyncBlobConformance", (), {})
    ranged = SimpleNamespace(
        cls=test_class,
        module=SimpleNamespace(__name__="unused"),
        node=SimpleNamespace(originalname="test_download_stream_range"),
    )
    unrelated = SimpleNamespace(
        cls=test_class,
        module=ranged.module,
        node=SimpleNamespace(originalname="test_download_range_middle"),
    )

    assert (
        RANGE_HEADER_MATCHER
        in vcr_config.__wrapped__(ranged, str(tmp_path))["match_on"]
    )
    assert (
        RANGE_HEADER_MATCHER
        not in vcr_config.__wrapped__(unrelated, str(tmp_path))["match_on"]
    )


def test_register_range_header_matcher():
    registered = {}
    fake_vcr = SimpleNamespace(
        register_matcher=lambda name, matcher: registered.__setitem__(name, matcher)
    )

    register_range_header_matcher(fake_vcr)

    assert registered == {RANGE_HEADER_MATCHER: range_header}
