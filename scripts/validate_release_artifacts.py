"""Validate the complete, lockstep artifact set before publishing it."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

from packaging.utils import (
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LICENSE_TEXT = (REPO_ROOT / "LICENSE.txt").read_text()


def _expected_projects() -> dict[str, str]:
    projects: dict[str, str] = {}
    for pyproject in sorted(REPO_ROOT.glob("packages/*/*/pyproject.toml")):
        lines = pyproject.read_text().splitlines()
        name = next(
            line.split("=", 1)[1].strip().strip('"')
            for line in lines
            if line.startswith("name = ")
        )
        version = next(
            line.split("=", 1)[1].strip().strip('"')
            for line in lines
            if line.startswith("version = ")
        )
        projects[canonicalize_name(name)] = version
    if len(projects) != 21:
        raise ValueError(f"expected 21 publishable projects, found {len(projects)}")
    return projects


def _has_license(artifact: Path) -> bool:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            return any(
                name.endswith(".dist-info/licenses/LICENSE.txt")
                and archive.read(name).decode() == LICENSE_TEXT
                for name in archive.namelist()
            )
    with tarfile.open(artifact) as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.name.endswith("/LICENSE.txt")
        ]
        return (
            len(members) == 1
            and archive.extractfile(members[0]).read().decode() == LICENSE_TEXT
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_directory", type=Path)
    args = parser.parse_args()

    expected = _expected_projects()
    wheels = list(args.artifact_directory.glob("*.whl"))
    sdists = list(args.artifact_directory.glob("*.tar.gz"))
    if len(wheels) != len(expected) or len(sdists) != len(expected):
        raise SystemExit(
            f"expected {len(expected)} wheels and sdists; found {len(wheels)} and {len(sdists)}"
        )

    for artifact in wheels + sdists:
        name, version, *_ = (
            parse_wheel_filename(artifact.name)
            if artifact.suffix == ".whl"
            else parse_sdist_filename(artifact.name)
        )
        normalized = canonicalize_name(name)
        if expected.get(normalized) != str(version):
            raise SystemExit(f"unexpected artifact {artifact.name}")
        if not _has_license(artifact):
            raise SystemExit(
                f"missing or mismatched Apache-2.0 license in {artifact.name}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
