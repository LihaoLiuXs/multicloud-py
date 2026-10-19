"""Behavioral tests for release artifact smoke testing."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import smoke_test_release_artifacts


def _write_wheel(
    directory: Path,
    index: int,
    *,
    imported_module: str | None = None,
) -> None:
    distribution = f"fixture_pkg_{index:02d}"
    module = f"fixture_{index:02d}"
    wheel = directory / f"{distribution}-1.0.0-py3-none-any.whl"
    dist_info = f"{distribution}-1.0.0.dist-info"
    package_source = (
        f"import {imported_module}\n" if imported_module is not None else ""
    )

    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"multicloud/{module}/__init__.py", package_source)
        archive.writestr(
            f"{dist_info}/METADATA",
            "\n".join(
                [
                    "Metadata-Version: 2.1",
                    f"Name: fixture-pkg-{index:02d}",
                    "Version: 1.0.0",
                    "",
                ]
            ),
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "\n".join(
                [
                    "Wheel-Version: 1.0",
                    "Generator: multicloud-py-test",
                    "Root-Is-Purelib: true",
                    "Tag: py3-none-any",
                    "",
                ]
            ),
        )
        archive.writestr(f"{dist_info}/RECORD", "")


def test_rejects_wheel_with_undeclared_sibling_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing a direct dependency must make release validation fail."""
    for index in range(21):
        imported_module = "multicloud.fixture_01" if index == 0 else None
        _write_wheel(tmp_path, index, imported_module=imported_module)

    monkeypatch.setattr(
        sys,
        "argv",
        ["smoke_test_release_artifacts", str(tmp_path)],
    )

    with pytest.raises(subprocess.CalledProcessError):
        smoke_test_release_artifacts.main()
