import json
from types import SimpleNamespace

import pytest

from tests.blob.conformance.conftest import (
    _is_direct_tag_test,
    _is_tagged_multipart_sync_test,
    _is_tagged_multipart_test,
    _played_requests_in_recorded_order,
    _requires_complete_playback,
    vcr_config,
)
from tests.blob.conformance.vcr_filters import (
    DIRECT_TAG_MATCHERS,
    MULTIPART_TAG_MATCHERS,
    get_default_vcr_config,
    multipart_destination_metadata,
    multipart_metadata_headers,
    multipart_part_payload,
    multipart_tag_header,
    object_tagging,
    patched_metadata,
    register_blob_tag_matchers,
)


def _request(*, method="POST", headers=None, body=None):
    return SimpleNamespace(
        method=method, headers=headers or {}, body=body, uri="https://example.test/o"
    )


def test_tagged_multipart_scope_includes_sync_and_async_tests():
    test_name = "test_multipart_applies_tags_given_at_initiate"
    sync_request = SimpleNamespace(
        cls=type("TestAwsBlobConformance", (), {}),
        node=SimpleNamespace(originalname=test_name),
    )
    async_request = SimpleNamespace(
        cls=type("TestAwsAsyncBlobConformance", (), {}),
        node=SimpleNamespace(originalname=test_name),
    )

    assert _is_tagged_multipart_test(sync_request) is True
    assert _is_tagged_multipart_test(async_request) is True
    assert _is_tagged_multipart_sync_test(sync_request) is True
    assert _is_tagged_multipart_sync_test(async_request) is False


def test_vcr_config_scopes_matchers_to_tagged_multipart_tests(tmp_path):
    tagged = SimpleNamespace(
        cls=type("TestAwsAsyncBlobConformance", (), {}),
        module=SimpleNamespace(__name__="unused"),
        node=SimpleNamespace(
            originalname="test_multipart_applies_tags_given_at_initiate"
        ),
    )
    unrelated = SimpleNamespace(
        cls=tagged.cls,
        module=tagged.module,
        node=SimpleNamespace(originalname="test_upload_round_trip"),
    )

    tagged_config = vcr_config.__wrapped__(tagged, str(tmp_path))
    unrelated_config = vcr_config.__wrapped__(unrelated, str(tmp_path))

    assert tagged_config["match_on"][-len(MULTIPART_TAG_MATCHERS) :] == list(
        MULTIPART_TAG_MATCHERS
    )
    assert not set(MULTIPART_TAG_MATCHERS).intersection(unrelated_config["match_on"])


def test_vcr_config_scopes_matchers_to_direct_tag_tests(tmp_path):
    tagged = SimpleNamespace(
        cls=type("TestAwsAsyncBlobConformance", (), {}),
        module=SimpleNamespace(__name__="unused"),
        node=SimpleNamespace(originalname="test_set_tags_replaces_existing_tags"),
    )
    unrelated = SimpleNamespace(
        cls=tagged.cls,
        module=tagged.module,
        node=SimpleNamespace(originalname="test_get_metadata_reports_attributes"),
    )

    assert _is_direct_tag_test(tagged) is True
    assert _is_direct_tag_test(unrelated) is False
    assert vcr_config.__wrapped__(tagged, str(tmp_path))["match_on"][
        -len(DIRECT_TAG_MATCHERS) :
    ] == list(DIRECT_TAG_MATCHERS)
    assert not set(DIRECT_TAG_MATCHERS).intersection(
        vcr_config.__wrapped__(unrelated, str(tmp_path))["match_on"]
    )


def test_complete_playback_is_required_for_direct_tags_and_sync_multipart():
    direct = SimpleNamespace(
        cls=type("TestAwsAsyncBlobConformance", (), {}),
        node=SimpleNamespace(originalname="test_set_tags_replaces_existing_tags"),
    )
    sync_multipart = SimpleNamespace(
        cls=type("TestAwsBlobConformance", (), {}),
        node=SimpleNamespace(
            originalname="test_multipart_applies_tags_given_at_initiate"
        ),
    )
    unrelated = SimpleNamespace(
        cls=direct.cls,
        node=SimpleNamespace(originalname="test_get_metadata_reports_attributes"),
    )

    assert _requires_complete_playback(direct) is True
    assert _requires_complete_playback(sync_multipart) is True
    assert _requires_complete_playback(unrelated) is False


def test_played_requests_must_follow_recorded_order():
    first = _request(method="PUT", body=b"first")
    second = _request(method="PUT", body=b"second")
    ordered = SimpleNamespace(
        requests=[first, second],
        _played_interactions=[(first, {}), (second, {})],
    )
    reversed_order = SimpleNamespace(
        requests=[first, second],
        _played_interactions=[(second, {}), (first, {})],
    )

    assert _played_requests_in_recorded_order(ordered) is True
    assert _played_requests_in_recorded_order(reversed_order) is False


def test_multipart_tag_header_accepts_recorded_bytes_and_live_text():
    recorded = _request(headers={"x-amz-tagging": [b"env=test&note=a%20b"]})
    live = _request(headers={"X-Amz-Tagging": "env=test&note=a%20b"})

    multipart_tag_header(recorded, live)


@pytest.mark.parametrize("header", [None, "env=test&note=wrong"])
def test_multipart_tag_header_rejects_missing_or_changed_value(header):
    recorded = _request(headers={"x-amz-tagging": [b"env=test&note=a%20b"]})
    live = _request(headers={} if header is None else {"x-amz-tagging": header})

    with pytest.raises(AssertionError, match="multipart tag headers differ"):
        multipart_tag_header(recorded, live)


def test_multipart_metadata_headers_reject_missing_or_changed_values():
    recorded = _request(
        headers={"x-amz-meta-team": [b"one"], "x-amz-meta-owner": [b"two"]}
    )

    with pytest.raises(AssertionError, match="multipart metadata headers differ"):
        multipart_metadata_headers(
            recorded, _request(headers={"x-amz-meta-team": "wrong"})
        )


def test_multipart_metadata_headers_ignore_unrelated_headers():
    multipart_metadata_headers(
        _request(headers={"Accept": "one"}),
        _request(headers={"Accept": "two"}),
    )


def test_multipart_destination_metadata_accepts_equal_maps():
    recorded = _request(
        body=json.dumps(
            {"destination": {"metadata": {"team": "one", "gcp-tag-env": "test"}}}
        )
    )
    live = _request(
        body=json.dumps(
            {"destination": {"metadata": {"gcp-tag-env": "test", "team": "one"}}}
        ).encode()
    )

    multipart_destination_metadata(recorded, live)


@pytest.mark.parametrize(
    "metadata",
    [
        {"team": "one", "gcp-tag-env": "wrong"},
        {"gcp-tag-env": "test"},
        None,
    ],
)
def test_multipart_destination_metadata_rejects_changed_or_missing_values(metadata):
    recorded = _request(
        body=json.dumps(
            {"destination": {"metadata": {"team": "one", "gcp-tag-env": "test"}}}
        )
    )
    live = _request(body=json.dumps({"destination": {"metadata": metadata}}))

    with pytest.raises(AssertionError, match="multipart destination metadata differs"):
        multipart_destination_metadata(recorded, live)


def test_non_compose_requests_do_not_add_body_matching():
    multipart_destination_metadata(
        _request(body=json.dumps({"metadata": {"gcp-tag-env": "old"}})),
        _request(body=json.dumps({"metadata": {"gcp-tag-env": "new"}})),
    )


def test_patched_metadata_rejects_changed_values():
    recorded = _request(
        method="PATCH",
        body=json.dumps({"metadata": {"team": "one", "gcp-tag-env": "conformance"}}),
    )
    live = _request(method="PATCH", body=json.dumps({"metadata": {"team": "one"}}))

    with pytest.raises(AssertionError, match="patched metadata differs"):
        patched_metadata(recorded, live)


def test_patched_metadata_ignores_non_patch_bodies():
    patched_metadata(
        _request(body=json.dumps({"metadata": {"gcp-tag-env": "old"}})),
        _request(body=json.dumps({"metadata": {"gcp-tag-env": "new"}})),
    )


def test_object_tagging_accepts_equal_tags_in_different_order():
    recorded = _request(
        method="PUT",
        body=(
            b'<Tagging xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            b"<TagSet><Tag><Key>owner</Key><Value>sdk</Value></Tag>"
            b"<Tag><Key>tier</Key><Value>archive</Value></Tag></TagSet></Tagging>"
        ),
    )
    recorded.uri += "?tagging"
    live = _request(
        method="PUT",
        body=(
            b"<Tagging><TagSet><Tag><Key>tier</Key><Value>archive</Value></Tag>"
            b"<Tag><Key>owner</Key><Value>sdk</Value></Tag></TagSet></Tagging>"
        ),
    )
    live.uri = recorded.uri

    object_tagging(recorded, live)


@pytest.mark.parametrize(
    "body",
    [
        b"<Tagging><TagSet></TagSet></Tagging>",
        (
            b"<Tagging><TagSet><Tag><Key>owner</Key>"
            b"<Value>wrong</Value></Tag></TagSet></Tagging>"
        ),
    ],
)
def test_object_tagging_rejects_missing_or_changed_tags(body):
    recorded = _request(
        method="PUT",
        body=(
            b"<Tagging><TagSet><Tag><Key>owner</Key>"
            b"<Value>sdk</Value></Tag></TagSet></Tagging>"
        ),
    )
    recorded.uri += "?tagging"
    live = _request(method="PUT", body=body)
    live.uri = recorded.uri

    with pytest.raises(AssertionError, match="object tags differ"):
        object_tagging(recorded, live)


def test_object_tagging_ignores_ordinary_put_bodies():
    object_tagging(
        _request(method="PUT", body=b"recorded payload"),
        _request(method="PUT", body=b"live payload"),
    )


def test_multipart_part_payload_rejects_changed_aws_bytes():
    recorded = _request(method="PUT", body=b"one")
    recorded.uri += "?uploadId=id&partNumber=1"
    live = _request(method="PUT", body=b"two")
    live.uri += "?uploadId=id&partNumber=1"

    with pytest.raises(AssertionError, match="multipart part payloads differ"):
        multipart_part_payload(recorded, live)


def test_multipart_part_payload_normalizes_gcp_boundaries():
    def body(boundary, payload):
        return (
            (
                f"--{boundary}\r\ncontent-type: application/json\r\n\r\n{{}}\r\n"
                f"--{boundary}\r\ncontent-type: application/octet-stream\r\n\r\n"
            ).encode()
            + payload
            + f"\r\n--{boundary}--".encode()
        )

    recorded = _request(body=body("recorded", b"same"))
    recorded.uri = "https://storage.googleapis.com/upload/storage/v1/b/b/o"
    live = _request(body=body("live", b"same"))
    live.uri = recorded.uri

    multipart_part_payload(recorded, live)
    live.body = body("live", b"changed")
    with pytest.raises(AssertionError, match="multipart part payloads differ"):
        multipart_part_payload(recorded, live)


def test_default_config_does_not_enable_multipart_matchers(tmp_path):
    config = get_default_vcr_config(str(tmp_path))

    assert not set(MULTIPART_TAG_MATCHERS).intersection(config["match_on"])


def test_register_blob_tag_matchers_registers_all():
    registered = {}
    fake_vcr = SimpleNamespace(
        register_matcher=lambda name, matcher: registered.__setitem__(name, matcher)
    )

    register_blob_tag_matchers(fake_vcr)

    assert registered == {
        "object_tagging": object_tagging,
        "multipart_tag_header": multipart_tag_header,
        "multipart_metadata_headers": multipart_metadata_headers,
        "multipart_destination_metadata": multipart_destination_metadata,
        "patched_metadata": patched_metadata,
        "multipart_part_payload": multipart_part_payload,
    }
