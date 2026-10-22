"""Regression coverage for the repository-wide Ali naming contract."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RETIRED_PROVIDER_TOKEN = "ali" + "baba"

ALI_PACKAGES = {
    Path("packages/blob/multicloud-blob-ali/pyproject.toml"): (
        "multicloud-py-blob-ali",
        ("multicloud.blob.providers",),
    ),
    Path("packages/pubsub/multicloud-pubsub-ali/pyproject.toml"): (
        "multicloud-py-pubsub-ali",
        (
            "multicloud.pubsub.topic.providers",
            "multicloud.pubsub.subscription.providers",
        ),
    ),
    Path("packages/sts/multicloud-sts-ali/pyproject.toml"): (
        "multicloud-py-sts-ali",
        ("multicloud.sts.providers",),
    ),
}


def _tracked_paths() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO_ROOT)
    return [Path(item.decode()) for item in output.split(b"\0") if item]


def test_retired_provider_token_is_absent() -> None:
    token = RETIRED_PROVIDER_TOKEN.encode()
    violations: list[str] = []

    for relative_path in _tracked_paths():
        if RETIRED_PROVIDER_TOKEN in relative_path.as_posix().lower():
            violations.append(f"path:{relative_path}")

        tracked_path = REPO_ROOT / relative_path
        content = (
            tracked_path.readlink().as_posix().encode()
            if tracked_path.is_symlink()
            else tracked_path.read_bytes()
        )
        if b"\0" not in content and token in content.lower():
            violations.append(f"content:{relative_path}")

    assert violations == []


def test_ali_distribution_names_and_entry_points() -> None:
    for relative_path, (expected_name, groups) in ALI_PACKAGES.items():
        metadata = tomllib.loads((REPO_ROOT / relative_path).read_text())
        assert metadata["project"]["name"] == expected_name

        entry_points = metadata["project"]["entry-points"]
        for group in groups:
            assert set(entry_points[group]) == {"ali"}
