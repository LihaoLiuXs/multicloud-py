"""Unit tests for the IAM conformance VCR filter module.

These verify the project-policy member scrubber that strips non-asserted
teammate members from recorded ``getIamPolicy``/``setIamPolicy`` bodies,
keeping only the conformance test identity's members. No network or
credentials — pure body-transformation checks.
"""

from __future__ import annotations

import json

from tests.iam.conformance.vcr_filters import _scrub_project_policy_members

_BASE = "mcp-conf-sa"


def _policy(*bindings: dict) -> str:
    return json.dumps({"version": 1, "etag": "abc", "bindings": list(bindings)})


def test_drops_non_test_members_from_binding() -> None:
    """Members not tied to the test identity are stripped from a binding."""
    body = _policy(
        {
            "role": "roles/storage.objectViewer",
            "members": [
                "user:teammate@example.com",
                "group:some-group@example.com",
                "serviceAccount:mcp-conf-sa-pol-list@proj.iam.gserviceaccount.com",
            ],
        }
    )

    result = json.loads(_scrub_project_policy_members(body, _BASE))

    members = result["bindings"][0]["members"]
    assert members == [
        "serviceAccount:mcp-conf-sa-pol-list@proj.iam.gserviceaccount.com"
    ]
    assert all("@example.com" not in m for m in members)


def test_drops_binding_with_no_test_members() -> None:
    """A binding with no test-identity members is removed entirely."""
    body = _policy(
        {
            "role": "roles/editor",
            "members": ["user:teammate@example.com"],
        },
        {
            "role": "roles/storage.objectViewer",
            "members": [
                "serviceAccount:mcp-conf-sa-pol-attach@proj.iam.gserviceaccount.com"
            ],
        },
    )

    result = json.loads(_scrub_project_policy_members(body, _BASE))

    roles = [b["role"] for b in result["bindings"]]
    assert roles == ["roles/storage.objectViewer"]


def test_retains_deleted_test_member_variant() -> None:
    """The ``deleted:serviceAccount:`` form of the test identity is kept."""
    deleted = (
        "deleted:serviceAccount:mcp-conf-sa-pol-attach@proj.iam."
        "gserviceaccount.com?uid=123"
    )
    body = _policy({"role": "roles/storage.objectViewer", "members": [deleted]})

    result = json.loads(_scrub_project_policy_members(body, _BASE))

    assert result["bindings"][0]["members"] == [deleted]


def test_scrubs_nested_setiampolicy_request_body() -> None:
    """A setIamPolicy request nests the policy under ``policy`` — scrub that too."""
    body = json.dumps(
        {
            "policy": {
                "bindings": [
                    {
                        "role": "roles/storage.objectViewer",
                        "members": [
                            "user:teammate@example.com",
                            "serviceAccount:mcp-conf-sa-pol-remove@proj.iam."
                            "gserviceaccount.com",
                        ],
                    }
                ]
            }
        }
    )

    result = json.loads(_scrub_project_policy_members(body, _BASE))

    members = result["policy"]["bindings"][0]["members"]
    assert members == [
        "serviceAccount:mcp-conf-sa-pol-remove@proj.iam.gserviceaccount.com"
    ]


def test_non_policy_body_passes_through_unchanged() -> None:
    """A body that is not an IAM policy is returned verbatim."""
    body = json.dumps({"email": "x@proj.iam.gserviceaccount.com", "uniqueId": "1"})

    assert _scrub_project_policy_members(body, _BASE) == body


def test_non_json_body_passes_through_unchanged() -> None:
    """A non-JSON body (e.g. AWS XML) is returned verbatim."""
    body = "<GetRoleResponse><Role>x</Role></GetRoleResponse>"

    assert _scrub_project_policy_members(body, _BASE) == body
