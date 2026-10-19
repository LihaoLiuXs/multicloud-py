"""Regression coverage for the public release workflows."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ALI_PROJECTS = {
    "multicloud-py-blob-ali",
    "multicloud-py-pubsub-ali",
    "multicloud-py-sts-ali",
}


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _workflow_dispatch_inputs(workflow: dict) -> dict:
    # PyYAML 1.1 may parse the unquoted YAML key ``on`` as ``True``.
    return workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]


def test_release_workflow_publishes_validated_artifacts_directly() -> None:
    workflow = _load("release.yml")

    assert workflow["name"] == "Release"
    assert workflow["permissions"] == {}
    assert workflow["jobs"]["release-please"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
    assert workflow["concurrency"] == {
        "group": "release-main",
        "cancel-in-progress": False,
    }

    validate = workflow["jobs"]["validate-release"]
    artifact_name = validate["steps"][-1]["with"]["name"]
    assert artifact_name == (
        "pypi-release-${{ needs.release-please.outputs.tag_name }}"
    )

    publish = workflow["jobs"]["publish"]
    assert publish["needs"] == ["release-please", "validate-release"]
    assert "needs.release-please.outputs.release_created == 'true'" in publish["if"]
    assert "needs.validate-release.result == 'success'" in publish["if"]
    assert publish["strategy"]["fail-fast"] is False
    assert publish["strategy"]["max-parallel"] == 3
    assert publish["environment"] == "pypi-${{ matrix.project }}"
    assert publish["permissions"] == {"id-token": "write"}

    download = publish["steps"][0]
    assert download["with"]["name"] == artifact_name
    assert download["with"]["path"] == "dist"
    assert all("uv build" not in step.get("run", "") for step in publish["steps"])

    upload = publish["steps"][-1]
    assert upload["with"]["packages-dir"] == "package-dist/"
    assert upload["with"]["skip-existing"] is True


def test_release_publish_matrix_covers_every_distribution_once() -> None:
    workflow = _load("release.yml")
    selected_projects = workflow["jobs"]["publish"]["strategy"]["matrix"]["project"]
    package_projects = {
        tomllib.loads(path.read_text())["project"]["name"]
        for path in REPO_ROOT.glob("packages/*/*/pyproject.toml")
    }

    assert len(selected_projects) == 21
    assert len(set(selected_projects)) == 21
    assert set(selected_projects) == package_projects
    assert ALI_PROJECTS <= set(selected_projects)


def test_testpypi_rehearsal_bootstraps_three_projects_per_selected_batch() -> None:
    workflow = _load("test-pypi.yml")
    inputs = _workflow_dispatch_inputs(workflow)

    assert workflow["permissions"] == {}
    assert inputs["batch"] == {
        "description": "Bootstrap batch to publish (three packages per run)",
        "required": True,
        "default": "1",
        "type": "choice",
        "options": ["1", "2", "3", "4", "5", "6", "7"],
    }
    assert workflow["jobs"]["select-bootstrap-batch"]["permissions"] == {}
    assert workflow["jobs"]["build-and-validate"]["permissions"] == {"contents": "read"}
    publish = workflow["jobs"]["publish-testpypi"]
    assert publish["needs"] == ["select-bootstrap-batch", "build-and-validate"]
    assert publish["strategy"]["max-parallel"] == 3
    assert publish["environment"] == "testpypi-${{ matrix.project }}"
    assert publish["permissions"] == {"id-token": "write"}
    upload = publish["steps"][-1]
    assert upload["with"]["skip-existing"] is True


def test_testpypi_bootstrap_batches_cover_every_distribution_once() -> None:
    workflow = _load("test-pypi.yml")
    selector = workflow["jobs"]["select-bootstrap-batch"]["steps"][0]["run"]
    selected_projects = re.findall(r'"project":"([^"]+)"', selector)
    package_projects = {
        tomllib.loads(path.read_text())["project"]["name"]
        for path in REPO_ROOT.glob("packages/*/*/pyproject.toml")
    }

    assert len(selected_projects) == 21
    assert len(set(selected_projects)) == 21
    assert set(selected_projects) == package_projects
    assert ALI_PROJECTS <= set(selected_projects)


def test_distribution_build_backends_are_exactly_pinned() -> None:
    manifests = sorted(REPO_ROOT.glob("packages/*/*/pyproject.toml"))

    assert len(manifests) == 21
    for manifest in manifests:
        build_requirements = tomllib.loads(manifest.read_text())["build-system"][
            "requires"
        ]
        assert build_requirements
        assert all("==" in requirement for requirement in build_requirements), manifest


def test_release_guide_lists_ali_trusted_publishers() -> None:
    guide = (REPO_ROOT / "docs" / "maintainers" / "releasing.md").read_text()

    for project in ALI_PROJECTS:
        expected_row = (
            f"| `{project}` | `release.yml` | `pypi-{project}` | "
            f"`test-pypi.yml` | `testpypi-{project}` |"
        )
        assert expected_row in guide
